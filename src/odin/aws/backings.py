"""Real AWS-compatible backing services.

s3/sqs/sns/dynamodb nodes become resources inside real backing containers —
RustFS (s3), goaws (sqs+sns in one process, so SNS→SQS delivery works),
dynalite (dynamodb) — one shared container per (env, backing), run through the
same RuntimeDriver as every other workload. The Reconciler's `_aws` seam:
provision/exists/deprovision plus lifecycle (ensure_backing/gc/aws_env/facts)
and a host-side boto3 `client` for tests.

`ecr`'s registry:2 (V2b) joins the SAME per-env container lifecycle
(ensure_backing/gc, ENSURE_KINDS below) but is deliberately absent from
PROVISIONED and every `client()`-based method's if/elif: it's a real Docker
Registry v2 server, not an AWS API, so it has no boto3 client, no
provision()/exists()/deprovision()/facts() case, and its own readiness
probe (`_await_registry_ready`) speaks its native `/v2/` HTTP endpoint
instead of a boto3 call. Its actual AWS-shaped resource (the ECR
`Repository`) is owned entirely by the gateway's control-plane model
(gateway/models/ecr.py, all-synth like ec2/iam) -- this module only ever
boots/tears down the container the real image bytes live in.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
import httpx
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from odin.gateway import DEFAULT_GATEWAY_PORT
from odin.runtime.colima import CONTAINER_HOST, ContainerSpec


class BackingUnavailable(RuntimeError):
    """A backing container isn't publishing the expected port (gone, or a
    gateway_port mismatch with its creator). Loud by default; best-effort
    paths (deprovision) catch it explicitly."""


PROVISIONED = ("s3", "sqs", "sns", "dynamodb")
# Kinds whose backing CONTAINER needs ensure_backing/gc lifecycle before use
# -- a superset of PROVISIONED. "ecr"'s own resource CRUD (CreateRepository &
# co.) happens entirely through the gateway's control-plane model
# (gateway/models/ecr.py, all-synth like ec2/iam), never through this
# module's client()-based provision/exists/deprovision/facts dispatch -- so
# it's deliberately absent from PROVISIONED (plan.py/reconciler._execute
# stay untouched) but must still get its registry:2 CONTAINER booted ahead
# of Apply, same as every other AWS-shaped kind (Reconciler.ensure_backings).
ENSURE_KINDS = PROVISIONED + ("ecr",)
ACCESS_KEY = "odin"
SECRET_KEY = "odin-secret-key"
REGION = "us-east-1"
ACCOUNT = "000000000000"

READY_TIMEOUT = 120.0  # first-run image pulls (dynalite no longer re-fetches on every boot)

# backing_ports() is called every reconciler tick (~2s per env) and each
# recompute is one `docker inspect`-shaped subprocess call per BackingDef.
# Container state only changes through THIS instance (ensure_backing/gc), so
# one poll interval of staleness is the exact window the reconciler already
# tolerates between ticks.
PORTS_CACHE_TTL = 2.0

# dynalite has no maintained image, so it used to run as bare `node:alpine` +
# `npx -y dynalite` on every container boot -- normally a ~3s npm-registry
# fetch (task-4-report), but a slow/flaky registry turns that into a
# multi-minute stall that blows the readiness probe's whole budget (S5 e2e:
# `never became ready`, empty container logs -- the process was still
# mid-download). Bake the npm install into a local image ONCE per machine
# (`_ensure_dynalite_image`, network access accepted there -- the same
# one-time cost as pulling any other backing's published image) so every
# subsequent boot is instant and fully offline.
_DYNALITE_IMAGE = "odin-dynalite:1"
_DYNALITE_DOCKERFILE = """\
FROM node:20-alpine
RUN npm install -g dynalite
ENTRYPOINT ["dynalite"]
CMD ["--port", "4567"]
"""
DYNALITE_IMAGE = _DYNALITE_IMAGE  # public alias: `odin doctor` inspects/prebakes this tag


@dataclass(frozen=True)
class BackingDef:
    name: str                  # container name suffix: odin-aws-{name}-{env}
    image: str
    port: int                  # container port of the wire API
    env: dict[str, str]
    command: tuple[str, ...]
    kinds: tuple[str, ...]     # node kinds this backing serves

BACKINGS: tuple[BackingDef, ...] = (
    BackingDef(name="rustfs", image="rustfs/rustfs:latest", port=9000,
               env={"RUSTFS_ACCESS_KEY": ACCESS_KEY, "RUSTFS_SECRET_KEY": SECRET_KEY},
               command=(), kinds=("s3",)),
    BackingDef(name="goaws", image="admiralpiett/goaws:v0.5.4", port=4100,
               env={}, command=("-config", "/conf/goaws.yaml", "Local"),
               kinds=("sqs", "sns")),   # ONE container serves both; SNS→SQS delivery is in-process
    BackingDef(name="dynalite", image=_DYNALITE_IMAGE, port=4567, env={},
               command=("--port", "4567"),
               kinds=("dynamodb",)),    # baked image (see _ensure_dynalite_image) — instant,
                                         # offline boot; no TTL/Streams — accepted
    BackingDef(name="registry", image="registry:2", port=5000, env={},
               command=(), kinds=("ecr",)),  # CNCF Distribution, Apache-2.0 (V2b) — the ECR
                                              # DATA plane only; the gateway's ecr.py model
                                              # owns the control plane (Create/Describe/Delete
                                              # Repository & co.) entirely, so this container
                                              # never gets a client()/provision() dispatch case
                                              # -- see ENSURE_KINDS above. Anonymous/auth-less
                                              # by design (`GetAuthorizationToken`'s synthetic
                                              # token is compat-only, gateway/models/ecr.py).
)

# Key casing verified live (AccountId shows up in returned ARNs); the `Local`
# top-level key matches the positional `Local` in goaws's command. Host/Port
# point at the gateway, not goaws's own container port: goaws bakes this pair
# into every QueueUrl/TopicArn it returns, so routing it through the gateway
# up front is what makes a returned QueueUrl re-dial through the gateway.
def _goaws_config(gateway_port: int) -> str:
    return f"""\
Local:
  Host: "{CONTAINER_HOST}"
  Port: "{gateway_port}"
  Region: "us-east-1"
  AccountId: "000000000000"
  LogToFile: false
"""

_PROBES = {"s3": "list_buckets", "sqs": "list_queues",
           "sns": "list_topics", "dynamodb": "list_tables"}


class BackingAws:
    def __init__(self, runtime, env: str = "default", root: Path = Path(".odin"),
                 client_factory=None, gateway_port: int = DEFAULT_GATEWAY_PORT) -> None:
        self._rt = runtime
        self._env = env
        self._root = root
        self._client_factory = client_factory
        self._gateway_port = gateway_port
        # Guards ensure_backing's check-then-create against TOCTOU: S5's
        # /apply-full calls it directly (to make the gateway routable before
        # tofu runs) while the SAME instance's Reconciler background loop can
        # independently call it too (provision() -> ensure_backing()) --
        # without this, both threads can see "not running" and race
        # `docker run` with the same container name (a hard Conflict error).
        # A threading.Lock, not asyncio.Lock: this runs under
        # asyncio.to_thread on separate OS threads, not on the event loop.
        self._ensure_lock = threading.Lock()
        # Per-tick docker-call cache. backing_ports() answers from a short-TTL
        # cache and gc() skips its whole stop-sweep when neither the active
        # kinds nor container state changed since the last sweep. Both are
        # invalidated ONLY on a real state change: _create_backing_container
        # actually booting something, or gc() actually stopping something.
        self._ports_cache: dict[str, int] | None = None
        self._ports_cache_at = 0.0
        self._last_gc_kinds: frozenset[str] | None = None
        self._dirty = True  # start dirty so the very first gc always sweeps

    def _backing_for(self, service: str) -> BackingDef:
        return next(d for d in BACKINGS if service in d.kinds)

    def _cname(self, d: BackingDef) -> str:
        return f"odin-aws-{d.name}-{self._env}"

    def container_name(self, service: str) -> str:
        """The public form of `_cname`/`_backing_for` -- for callers outside
        this class that need to name the backing container directly (the
        /logs route, the reconciler's own crash-verdict log tail)."""
        return self._cname(self._backing_for(service))

    def _listen_port(self, d: BackingDef) -> int:
        """The port the backing's process actually listens on INSIDE its
        container. Normally that's the BackingDef's fixed wire port -- but
        goaws binds its listener to whatever `Local.Port` its own mounted
        config says (verified against the real image), and that config is
        deliberately pointed at the gateway's port, not goaws's own (G4:
        "goaws builds returned QueueUrls from its configured Host/Port" so
        a re-dialed QueueUrl stays inside the gateway). So for goaws the
        real listen port tracks the gateway, and every Docker publish/
        lookup must use THIS, never the BackingDef's nominal 4100 (which
        nothing is actually listening on)."""
        return self._gateway_port if d.name == "goaws" else d.port

    def ensure_backing(self, service: str) -> None:
        d = self._backing_for(service)
        cname = self._cname(d)
        # Only the check+create is serialized -- the readiness wait below
        # runs OUTSIDE the lock so concurrent ensure_backing calls for
        # DIFFERENT services (S5's ensure_backings runs one asyncio.to_thread
        # per kind, in parallel) aren't forced sequential by a single
        # per-instance lock.
        with self._ensure_lock:
            if self._rt.status(cname) != "running" or self._stranded(d, cname):
                self._create_backing_container(d, cname)
        self._await_ready(cname, service)

    def _stranded(self, d: BackingDef, cname: str) -> bool:
        """A container that's RUNNING but no longer publishes the inside-port
        this instance needs (W2.2). goaws is the real case: its listener port
        IS the gateway port (`_listen_port`), and that's baked into the
        container by `docker run -p`, so a gateway-port change (a restart onto
        a different ephemeral port, an edited ODIN_GATEWAY_PORT) leaves a
        perfectly healthy container publishing the OLD one. `ensure_backing`
        used to adopt it, after which every `client()` call could only raise
        BackingUnavailable — forever, with no self-heal. Recreating it onto
        the CURRENT port is the fix (`_create_backing_container` rewrites
        goaws.yaml with that port too).

        Deliberately generic rather than `if d.name == "goaws"`: "the port I
        need isn't published" is wrong for any backing, whatever stranded it,
        and it's the exact condition `client()` already fails loud on. Costs
        one `docker port` per ensure_backing (an Apply/provision path, not the
        every-tick one — `backing_ports` has its own cache)."""
        return self._rt.host_port(cname, self._listen_port(d)) == 0

    def _create_backing_container(self, d: BackingDef, cname: str) -> None:
        self._rt.stop(cname)  # clear any exited remnant (same contract as PostgresRds)
        volumes: dict[str, str] = {}
        if d.name == "goaws":
            # Config must live under the repo tree ($HOME is the only tree
            # Colima shares into the VM — a /tmp mount silently comes up empty).
            conf_dir = (self._root / self._env).resolve()
            conf_dir.mkdir(parents=True, exist_ok=True)
            (conf_dir / "goaws.yaml").write_text(_goaws_config(self._gateway_port))
            volumes = {str(conf_dir): "/conf"}
        if d.name == "dynalite":
            self._ensure_dynalite_image()
        try:
            self._rt.run_container(ContainerSpec(
                name=cname, image=d.image, env=d.env, ports={self._listen_port(d): 0},
                labels={"odin-env": self._env}, command=d.command, volumes=volumes,
            ))
        except RuntimeError as exc:
            if "already in use" not in str(exc):
                raise
            # Belt-and-braces: the per-instance lock above should already
            # prevent a same-instance race, but a stale remnant from a
            # different process/instance can still lose this exact race.
            # The container exists either way -- heal by waiting for Docker
            # to report it running, then fall through to the readiness
            # probe like the happy path.
            deadline = time.monotonic() + READY_TIMEOUT
            while time.monotonic() < deadline and self._rt.status(cname) != "running":
                time.sleep(0.2)
        # A backing genuinely came up (booted here, or by the concurrent
        # creator the heal above waited on): the cached ports table is stale
        # and the next gc() must re-sweep even with unchanged active kinds
        # (e.g. a crash-recovery restart). ensure_backing's fast path (already
        # running) never reaches here, so a no-op ensure stays cache-neutral.
        self._ports_cache = None
        self._dirty = True

    def _ensure_dynalite_image(self) -> None:
        """One-time (per machine) build of the baked dynalite image -- see
        the module-level comment by `_DYNALITE_IMAGE`. Runs inside
        `ensure_backing`'s `_ensure_lock`, so two threads racing to boot
        dynalite for the first time never both `docker build` the same tag."""
        if not self._rt.image_exists(_DYNALITE_IMAGE):
            self._rt.build(_DYNALITE_IMAGE, _DYNALITE_DOCKERFILE)

    def ensure_dynalite_image(self) -> None:
        """Public seam for `odin doctor --prebake`: bake the dynalite image
        ahead of the first DynamoDB Apply. Idempotent; lock-free is fine here
        (a one-shot CLI, not the reconciler's concurrent ensure path)."""
        self._ensure_dynalite_image()

    def _await_ready(self, cname: str, service: str) -> None:
        if service == "ecr":
            self._await_registry_ready(cname)
            return
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                getattr(self.client(service), _PROBES[service])()
                return
            except (ClientError, BotoCoreError):
                time.sleep(1)
        raise RuntimeError(f"{cname} never became ready:\n{self._rt.logs(cname)}")

    def _await_registry_ready(self, cname: str) -> None:
        """registry:2 speaks the Docker Registry v2 HTTP protocol, not any
        AWS wire shape -- `client()`'s boto3-ecr seam (every OTHER backing's
        readiness probe) can never reach it, since registry:2 doesn't
        understand SigV4 or the ECR JSON protocol at all. A raw GET to
        `/v2/` (the registry's own liveness/version endpoint, 200 when
        ready) is the real check. Gated on `_client_factory`, the SAME seam
        every other probe already uses to skip real I/O in unit tests
        (FakeRuntime never actually listens on its fake ports) -- a test
        that injects a client_factory gets the same instant-success
        behavior a FakeClient call already gives the AWS-protocol probes."""
        if self._client_factory is not None:
            return
        d = self._backing_for("ecr")
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            port = self._rt.host_port(cname, self._listen_port(d))
            try:
                if port:
                    httpx.get(f"http://127.0.0.1:{port}/v2/", timeout=2.0).raise_for_status()
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"{cname} never became ready:\n{self._rt.logs(cname)}")

    def client(self, service: str):
        """Host-side client against the backing's published port (tests/e2e)."""
        d = self._backing_for(service)
        port = self._rt.host_port(self._cname(d), self._listen_port(d))
        if not port:
            # Fail loud: a 0 here means the container publishes a DIFFERENT
            # inside-port than this instance expects (for goaws: a
            # gateway_port mismatch with the container's creator — construct
            # with the app's /health gateway.port) or no container at all.
            # Typed so best-effort paths (deprovision) can swallow it.
            raise BackingUnavailable(
                f"{self._cname(d)} publishes no port {self._listen_port(d)} — "
                f"gateway_port mismatch between this BackingAws and the container's creator?"
            )
        endpoint = f"http://127.0.0.1:{port}"
        if self._client_factory:
            return self._client_factory(service, endpoint)
        config = Config(signature_version="s3v4", s3={"addressing_style": "path"}) \
            if service == "s3" else None
        return boto3.client(service, endpoint_url=endpoint, aws_access_key_id=ACCESS_KEY,
                            aws_secret_access_key=SECRET_KEY, region_name=REGION, config=config)

    def provision(self, service: str, name: str, subscriptions: tuple[str, ...] = ()) -> None:
        self.ensure_backing(service)
        client = self.client(service)
        try:
            if service == "s3":
                client.create_bucket(Bucket=name)
            elif service == "sqs":
                client.create_queue(QueueName=name)
            elif service == "dynamodb":
                client.create_table(
                    TableName=name, BillingMode="PAY_PER_REQUEST",
                    AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                    KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                )
            elif service == "sns":
                topic_arn = client.create_topic(Name=name)["TopicArn"]  # RETURNED, not constructed
                sqs = self.client("sqs")
                for queue in subscriptions:
                    # idempotent — the sqs node's own provision may not have run yet
                    queue_url = sqs.create_queue(QueueName=queue)["QueueUrl"]
                    qarn = sqs.get_queue_attributes(
                        QueueUrl=queue_url, AttributeNames=["QueueArn"],
                    )["Attributes"]["QueueArn"]
                    client.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=qarn,
                                     Attributes={"RawMessageDelivery": "true"})
        except ClientError as exc:
            if not any(w in str(exc) for w in ("Exist", "Conflict", "InUse")):
                raise

    def exists(self, service: str, name: str) -> bool:
        d = self._backing_for(service)
        # Cheap liveness first: a dead backing must demote nodes without HTTP timeouts.
        if self._rt.status(self._cname(d)) != "running":
            return False
        client = self.client(service)
        try:
            if service == "s3":
                client.head_bucket(Bucket=name)
            elif service == "sqs":
                client.get_queue_url(QueueName=name)
            elif service == "sns":
                arns = [t["TopicArn"] for t in client.list_topics().get("Topics", [])]
                return any(a.endswith(f":{name}") for a in arns)
            elif service == "dynamodb":
                client.describe_table(TableName=name)
            return True
        except (ClientError, BotoCoreError):  # BotoCoreError: backing just died mid-check
            return False

    def subscriptions(self, topic: str) -> tuple[str, ...]:
        """Queue names currently subscribed to `topic` (the raw-delivery SQS
        subscriptions provision() creates), read back through
        ListSubscriptionsByTopic -- so the reconciler can diff desired vs
        actual on a live canvas edit without recreating the topic. Endpoint
        is the queue ARN provision() subscribed with
        (arn:aws:sqs:region:account:name), so the queue name is the ARN's
        last colon-segment."""
        sns = self.client("sns")
        topic_arn = f"arn:aws:sns:{REGION}:{ACCOUNT}:{topic}"
        subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
        return tuple(s["Endpoint"].rsplit(":", 1)[-1] for s in subs if s["Protocol"] == "sqs")

    def deprovision(self, service: str, name: str) -> None:
        try:
            client = self.client(service)
            if service == "s3":
                client.delete_bucket(Bucket=name)
            elif service == "sqs":
                client.delete_queue(QueueUrl=client.get_queue_url(QueueName=name)["QueueUrl"])
            elif service == "dynamodb":
                client.delete_table(TableName=name)
            elif service == "sns":
                client.delete_topic(TopicArn=f"arn:aws:sns:{REGION}:{ACCOUNT}:{name}")
        except (ClientError, BotoCoreError, BackingUnavailable):
            pass  # best-effort: the resource or its whole backing may already be gone

    def facts(self, service: str, name: str) -> dict:
        d = self._backing_for(service)
        endpoint = f"http://{CONTAINER_HOST}:{self._rt.host_port(self._cname(d), self._listen_port(d))}"
        # QUEUE_URL is constructed canonically, pointed at the gateway (not
        # goaws's own direct port) to match the Host/Port baked into
        # goaws.yaml -- the fact and what goaws itself now returns agree.
        gateway_endpoint = f"http://{CONTAINER_HOST}:{self._gateway_port}"
        return {
            "s3": {"BUCKET": name, "endpoint": endpoint},
            "sqs": {"QUEUE_URL": f"{gateway_endpoint}/{ACCOUNT}/{name}", "endpoint": endpoint},
            "sns": {"TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:{name}", "endpoint": endpoint},
            "dynamodb": {"TABLE": name, "endpoint": endpoint},
        }[service]

    def aws_env(self) -> dict[str, str]:
        env = {"AWS_ACCESS_KEY_ID": ACCESS_KEY, "AWS_SECRET_ACCESS_KEY": SECRET_KEY,
               "AWS_DEFAULT_REGION": REGION}
        running = (d for d in BACKINGS if self._rt.status(self._cname(d)) == "running")
        for d in running:
            endpoint = f"http://{CONTAINER_HOST}:{self._rt.host_port(self._cname(d), self._listen_port(d))}"
            for kind in d.kinds:  # goaws yields both _SQS and _SNS from one container
                env[f"AWS_ENDPOINT_URL_{kind.upper()}"] = endpoint
        return env

    def backing_ports(self) -> dict[str, int]:
        """service -> host port of its running backing; the routing table
        GatewayState.update forwards proxied requests against. A backing
        that isn't running (or never started) is simply absent -- the
        gateway then answers with service-unavailable rather than a stale
        port (PRD: no cache that outlives an Apply). Answers from a
        PORTS_CACHE_TTL cache (invalidated on any real start/stop this
        instance performs); callers treat the dict as read-only."""
        if self._ports_cache is not None and time.monotonic() - self._ports_cache_at < PORTS_CACHE_TTL:
            return self._ports_cache
        ports: dict[str, int] = {}
        running = (d for d in BACKINGS if self._rt.status(self._cname(d)) == "running")
        for d in running:
            port = self._rt.host_port(self._cname(d), self._listen_port(d))
            for kind in d.kinds:
                ports[kind] = port
        self._ports_cache = ports
        self._ports_cache_at = time.monotonic()
        return ports

    def gc(self, active_kinds: set[str]) -> None:
        kinds = frozenset(active_kinds)
        if kinds == self._last_gc_kinds and not self._dirty:
            return  # same kinds, nothing started since the last sweep: zero docker calls
        for d in BACKINGS:
            if set(d.kinds).isdisjoint(active_kinds):
                self._rt.stop(self._cname(d))  # stop is idempotent on absent names
                self._ports_cache = None       # a backing may have really stopped
        self._last_gc_kinds = kinds
        self._dirty = False
