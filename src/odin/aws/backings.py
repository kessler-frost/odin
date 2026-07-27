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

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
import httpx
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from odin.fabric.sidecar import MeshSidecar
from odin.gateway import DEFAULT_GATEWAY_PORT
from odin.runtime.colima import CONTAINER_HOST, ContainerSpec, PortUnreadable
from odin.util import private_mkdir


class BackingUnavailable(RuntimeError):
    """A backing container isn't publishing the expected port (gone, stopped,
    or a gateway_port mismatch with its creator). Loud by default; best-effort
    paths (deprovision) catch it explicitly.

    Carries the CONTAINER NAME and the container state odin actually observed
    as structured attributes, not only inside the message: `/apply-full`'s
    failure verdict (server.py::_EXCEPTION_VERDICTS) names them in its JSON
    body, and a field parsed back out of a message string is the regex trap
    this repo's conventions forbid. Both default to "" so the one-argument
    construction every existing caller/test uses still works.
    """

    def __init__(self, message: str, container: str = "", observed: str = "") -> None:
        super().__init__(message)
        self.container = container
        self.observed = observed


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

# What a container's OBSERVED STATE means when the runtime answered the
# port-map read and the answer was "nothing published here". Probed against
# the real docker (28.4.0) on this machine rather than assumed -- honesty
# rule 1, and the assumption it corrects was live:
#
#   running, -p 0:80   {"80/tcp":[{"HostIp":"0.0.0.0","HostPort":"33947"}]} rc=0
#   created            {}                                                  rc=0
#   exited             {}                                                  rc=0
#   force-removed      (empty)  error: no such object: <name>              rc=1
#
# So an EMPTY port map is not evidence of a port mismatch -- it is evidence
# the container is not running. The old message asserted "gateway_port
# mismatch ... ?" for all three, and a real `docker rm -f` mid-apply (the
# reproduced /apply-full race) hit exactly this branch and was told to go
# looking for a port mismatch that did not exist. Anything this map has no
# entry for falls through to `_UNKNOWN_STATE` -- a state odin has no reading
# for says so instead of inheriting the running case's diagnosis.
_STATE_MEANING = {
    "absent": "no container by that name exists (it was deleted, or was never created)",
    "created": "the container was created but never started",
    "exited": "the container exited",
    "paused": "the container is paused",
    "restarting": "the container is restarting",
    "running": (
        "the container IS running, so this is a gateway_port mismatch between this "
        "BackingAws and whatever created the container"
    ),
}
_UNKNOWN_STATE = "odin has no reading for that container state"


class BackingAws:
    def __init__(self, runtime, env: str = "default", root: Path = Path(".odin"),
                 client_factory=None, gateway_port: int = DEFAULT_GATEWAY_PORT,
                 mesh: MeshSidecar | None = None) -> None:
        self._rt = runtime
        self._env = env
        self._root = root
        self._client_factory = client_factory
        self._gateway_port = gateway_port
        # W2.6: puts each backing container on the env's Nebula overlay
        # (fabric/sidecar.py). Inert unless the env has a Nebula network.
        self._mesh = mesh or MeshSidecar(runtime, env, root)
        # Guards ensure_backing's check-then-create against TOCTOU: S5's
        # /apply-full calls it directly (to make the gateway routable before
        # tofu runs) while the SAME instance's Reconciler background loop can
        # independently call it too (provision() -> ensure_backing()) --
        # without this, both threads can see "not running" and race
        # `docker run` with the same container name (a hard Conflict error).
        # TODAY a threading.Lock rather than an asyncio.Lock, because today
        # this runs under asyncio.to_thread on separate OS threads, not on the
        # event loop. That is a statement about the current process model, not
        # a preference -- see the verdict below for what it becomes.
        #
        # v0.7.7 DE-THREADING VERDICT (verified, not assumed): this lock does
        # NOT disappear when the threads do -- it becomes an `asyncio.Lock`.
        # The rule "if the critical section contains no `await`, delete the
        # lock" is applied to the code AFTER the conversion, not before, and
        # this critical section is three `docker` calls (`_rt.status`,
        # `_stranded`, `_create_backing_container`) that the conversion turns
        # into `await`s. Suspension points inside the section are exactly what
        # lets two tasks interleave, so deleting it would restore the
        # `docker run` name Conflict described above -- as a task race on one
        # loop instead of a thread race. Its contender (`reconciler.py`'s
        # `gather(to_thread(ensure_backing, k) ...)`) stays genuinely
        # concurrent after conversion: `gather` of awaits, not of threads.
        self._ensure_lock = asyncio.Lock()
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

    async def _published_port(self, d: BackingDef) -> int:
        """This backing's real published host port, or `BackingUnavailable`
        naming why there isn't one. The ONE place this class turns a port read
        into a number, so no caller can re-invent the hazard below.

        THE HAZARD (field test 5's facts audit). `host_port` used to answer any
        failed `docker port` with 0, and 0 is shaped like a port: `facts()`
        interpolated it into the durable `endpoint` fact
        (`http://host.docker.internal:0`), `backing_ports()` handed it to the
        gateway as a routing target, `aws_env()` into a workload's endpoint
        vars. The `endpoint` fact is the worst of the three because it is
        published ONLY on the starting->healthy transition and never refreshed,
        so one transient hiccup corrupted World permanently and silently.

        So there are exactly two answers here now: a port you can dial, or an
        exception saying why not -- "nothing published on that inside-port" (a
        goaws whose creator used a different gateway port, or no container at
        all) and "the runtime could not be asked" (`PortUnreadable`) are both
        the second, carrying their own real reason. Never 0.

        Each branch says only what it KNOWS. The unreadable branch does not go
        on to ask for the container's state: the runtime just failed to answer a
        question, so a second question to the same runtime proves nothing and
        its own failure would replace a real reason with a worse one. The
        empty-map branch DOES ask, because there the runtime answered and the
        state is the whole diagnosis (see `_STATE_MEANING`) -- one extra docker
        call, only ever on a path that is already failing."""
        cname, inside = self._cname(d), self._listen_port(d)
        try:
            port = await self._rt.host_port(cname, inside)
        except PortUnreadable as exc:
            raise BackingUnavailable(
                f"the {'/'.join(d.kinds)} backing container {cname} is unavailable: {exc}",
                container=cname, observed="unreadable",
            ) from exc
        if not port:
            observed = await self._rt.status(cname)
            raise BackingUnavailable(
                f"the {'/'.join(d.kinds)} backing container {cname} is unavailable: it publishes "
                f"no port {inside}, and the container runtime reports its state as {observed!r} "
                f"-- {_STATE_MEANING.get(observed, _UNKNOWN_STATE)}",
                container=cname, observed=observed,
            )
        return port

    async def _published_port_or_none(self, d: BackingDef) -> int | None:
        """`_published_port` for the callers that must OMIT rather than raise:
        the gateway routing table, the workload endpoint vars, and the two
        internal readiness/self-heal probes. `backing_ports`'s own contract
        already says a backing that isn't running is simply ABSENT from the
        table so the gateway 503s instead of forwarding somewhere wrong --
        absent is the honest answer for an unreadable one too. 0 never was."""
        try:
            return await self._published_port(d)
        except BackingUnavailable:
            return None

    async def ensure_backing(self, service: str) -> None:
        d = self._backing_for(service)
        cname = self._cname(d)
        # Only the check+create is serialized -- the readiness wait below
        # runs OUTSIDE the lock so concurrent ensure_backing calls for
        # DIFFERENT services (S5's ensure_backings runs one asyncio.to_thread
        # per kind, in parallel) aren't forced sequential by a single
        # per-instance lock.
        async with self._ensure_lock:
            if await self._rt.status(cname) != "running" or await self._stranded(d):
                await self._create_backing_container(d, cname)
        await self._await_ready(cname, service)
        # W2.6: the backing joins the env's Nebula overlay (a real cert + a
        # sticky overlay IP), so it is a mesh member a firewall can gate --
        # a no-op unless the canvas drew a VPC (fabric/sidecar.py::enabled).
        # ADDITIVE: the published host port the gateway forwards to, the
        # readiness probe above, and every host-side client are untouched.
        # These four are AWS's own non-VPC services (S3/SQS/SNS/DynamoDB/ECR
        # aren't SG-gated in real AWS either -- IAM and endpoint policy are
        # their access control, which is the gateway's job), so they join
        # with nebula's allow-all default rather than a compiled SG; rds,
        # which IS VPC-resident, gets its drawn SG (aws/rds.py).
        await self._mesh.ensure(cname, cname)

    async def _stranded(self, d: BackingDef) -> bool:
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
        one port read per ensure_backing (an Apply/provision path, not the
        every-tick one — `backing_ports` has its own cache).

        An UNREADABLE port counts as stranded too (`_published_port_or_none`),
        which is the safe direction: recreating a container we can't get a port
        for is recoverable, adopting one we can't reach is the stall this
        method exists to break. It only ever runs on a container `ensure_backing`
        has just seen `running`, under the same lock."""
        return await self._published_port_or_none(d) is None

    async def _create_backing_container(self, d: BackingDef, cname: str) -> None:
        await self._rt.stop(cname)  # clear any exited remnant (same contract as PostgresRds)
        volumes: dict[str, str] = {}
        if d.name == "goaws":
            # Config must live under the repo tree ($HOME is the only tree
            # Colima shares into the VM — a /tmp mount silently comes up empty).
            conf_dir = private_mkdir((self._root / self._env).resolve())
            (conf_dir / "goaws.yaml").write_text(_goaws_config(self._gateway_port))
            volumes = {str(conf_dir): "/conf"}
        if d.name == "dynalite":
            await self._ensure_dynalite_image()
        try:
            await self._rt.run_container(ContainerSpec(
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
            while time.monotonic() < deadline and await self._rt.status(cname) != "running":
                await asyncio.sleep(0.2)
        # A backing genuinely came up (booted here, or by the concurrent
        # creator the heal above waited on): the cached ports table is stale
        # and the next gc() must re-sweep even with unchanged active kinds
        # (e.g. a crash-recovery restart). ensure_backing's fast path (already
        # running) never reaches here, so a no-op ensure stays cache-neutral.
        self._ports_cache = None
        self._dirty = True

    async def _ensure_dynalite_image(self) -> None:
        """One-time (per machine) build of the baked dynalite image -- see
        the module-level comment by `_DYNALITE_IMAGE`. Runs inside
        `ensure_backing`'s `_ensure_lock`, so two threads racing to boot
        dynalite for the first time never both `docker build` the same tag."""
        if not await self._rt.image_exists(_DYNALITE_IMAGE):
            await self._rt.build(_DYNALITE_IMAGE, _DYNALITE_DOCKERFILE)

    async def ensure_dynalite_image(self) -> None:
        """Public seam for `odin doctor --prebake`: bake the dynalite image
        ahead of the first DynamoDB Apply. Idempotent; lock-free is fine here
        (a one-shot CLI, not the reconciler's concurrent ensure path)."""
        await self._ensure_dynalite_image()

    async def _await_ready(self, cname: str, service: str) -> None:
        if service == "ecr":
            await self._await_registry_ready(cname)
            return
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                getattr(await self.client(service), _PROBES[service])()
                return
            except (ClientError, BotoCoreError):
                await asyncio.sleep(1)
        reason = await self._not_ready_reason(
            cname, self._backing_for(service), f"the {service} {_PROBES[service]} probe never succeeded",
        )
        raise RuntimeError(f"{cname} never became ready: {reason}")

    async def _not_ready_reason(self, cname: str, d: BackingDef, probe: str) -> str:
        """WHY the wait ended, in a form that is never empty.

        The module docstring above already cites this exact failure as a real
        incident (`"never became ready"`, empty container logs) -- the fix just
        took longer to arrive than the diagnosis. It used to be
        `f"{cname} never became ready:\\n{await self._rt.logs(cname)}"`, and
        `logs` answers `""` both for a container that wrote nothing and for one
        the runtime could not read. Measured against REAL containers, driven to
        a real timeout with the canonical names so `client()` resolved a
        published port:

          backing                          rendered                    status   exit  port
          odin-aws-rustfs-swpprobe    '... never became ready:\\n'  running  0     34071
          odin-aws-registry-swpprobe  '... never became ready:\\n'  running  0     34072

        A dangling colon and a blank line, with `status`, `exit_code` and
        `host_port` all readable at that instant and all three discarded.

        Same treatment as `CacheRuntime._not_ready_reason` and
        `FunctionRuntime`'s before it, and for the same reasons: `host_port`
        discriminates the two real failures (docker never published the port
        at all, versus a port that is published and never answers), the log
        tail is a trailing bonus rather than the headline (a backing's logs can
        end on a line that reads like success while the actual reason sits in
        the port), and the exit code is reported only for a container that is
        NOT running -- a live container's `{{.State.ExitCode}}` is `0`, and
        "exit code 0" printed under a failure sends a reader down the wrong
        path."""
        status = await self._rt.status(cname)
        state = status if status == "running" else f"{status}, exit code {await self._rt.exit_code(cname)}"
        port = await self._published_port_or_none(d)
        published = f"published on host port {port}, but {probe}" if port else (
            f"docker never published its {d.port}, so nothing could reach it"
        )
        logs = await self._rt.logs(cname)
        tail = f"Its logs:\n{logs}" if logs else (
            "It has logged nothing, so the container state above is the whole of it."
        )
        return f"{published}, after {READY_TIMEOUT:g}s. Container: {state}. {tail}"

    async def _await_registry_ready(self, cname: str) -> None:
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
            port = await self._published_port_or_none(d)
            try:
                if port:
                    httpx.get(f"http://127.0.0.1:{port}/v2/", timeout=2.0).raise_for_status()
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"{cname} never became ready: {await self._not_ready_reason(cname, d, 'GET /v2/ never returned 200')}"
        )

    async def client(self, service: str):
        """Host-side client against the backing's published port (tests/e2e).

        Fails loud (`BackingUnavailable`, typed so best-effort paths like
        `deprovision` can swallow it) rather than dialing a made-up port --
        `_published_port` owns that judgement and the reason text."""
        d = self._backing_for(service)
        endpoint = f"http://127.0.0.1:{await self._published_port(d)}"
        if self._client_factory:
            return self._client_factory(service, endpoint)
        config = Config(signature_version="s3v4", s3={"addressing_style": "path"}) \
            if service == "s3" else None
        return await boto3.client(service, endpoint_url=endpoint, aws_access_key_id=ACCESS_KEY,
                            aws_secret_access_key=SECRET_KEY, region_name=REGION, config=config)

    async def provision(self, service: str, name: str, subscriptions: tuple[str, ...] = ()) -> None:
        await self.ensure_backing(service)
        client = await self.client(service)
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
                sqs = await self.client("sqs")
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

    async def exists(self, service: str, name: str) -> bool:
        d = self._backing_for(service)
        # Cheap liveness first: a dead backing must demote nodes without HTTP timeouts.
        if await self._rt.status(self._cname(d)) != "running":
            return False
        client = await self.client(service)
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

    async def subscriptions(self, topic: str) -> tuple[str, ...]:
        """Queue names currently subscribed to `topic` (the raw-delivery SQS
        subscriptions provision() creates), read back through
        ListSubscriptionsByTopic -- so the reconciler can diff desired vs
        actual on a live canvas edit without recreating the topic. Endpoint
        is the queue ARN provision() subscribed with
        (arn:aws:sqs:region:account:name), so the queue name is the ARN's
        last colon-segment."""
        sns = await self.client("sns")
        topic_arn = f"arn:aws:sns:{REGION}:{ACCOUNT}:{topic}"
        subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
        return tuple(s["Endpoint"].rsplit(":", 1)[-1] for s in subs if s["Protocol"] == "sqs")

    async def deprovision(self, service: str, name: str) -> None:
        try:
            client = await self.client(service)
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

    async def facts(self, service: str, name: str) -> dict:
        """This resource's World facts. Every value is a `str` -- see
        `Reconciler._assert_string_facts` for why that is load-bearing rather
        than stylistic (a fact that doesn't survive a JSON round-trip unchanged
        is a permanent delta storm).

        Raises `BackingUnavailable` when the backing's published port can't be
        read, instead of naming an endpoint it does not know. These facts are
        published exactly ONCE, on the starting->healthy transition, and never
        refreshed -- so a wrong one here is wrong in `world.json` forever. The
        reconciler's caller keeps the resource `starting` and carries the
        reason (reconcile/reconciler.py::_observe_provisioned)."""
        d = self._backing_for(service)
        endpoint = f"http://{CONTAINER_HOST}:{await self._published_port(d)}"
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

    async def aws_env(self) -> dict[str, str]:
        """The creds + per-service endpoint vars a consumer needs. A backing
        whose port can't be read contributes NO endpoint var (rather than one
        pointing at `:0`): an absent override is a visible failure at the first
        call, a bogus one is a connection refused blamed on the service."""
        env = {"AWS_ACCESS_KEY_ID": ACCESS_KEY, "AWS_SECRET_ACCESS_KEY": SECRET_KEY,
               "AWS_DEFAULT_REGION": REGION}
        # A plain loop, not the generator this used to be: `await` inside a
        # genexp makes it an ASYNC generator, which a `for` cannot iterate.
        for d in BACKINGS:
            if await self._rt.status(self._cname(d)) != "running":
                continue
            port = await self._published_port_or_none(d)
            for kind in d.kinds if port else ():  # goaws yields both _SQS and _SNS from one container
                env[f"AWS_ENDPOINT_URL_{kind.upper()}"] = f"http://{CONTAINER_HOST}:{port}"
        return env

    async def backing_ports(self) -> dict[str, int]:
        """service -> host port of its running backing; the routing table
        GatewayState.update forwards proxied requests against. A backing
        that isn't running (or never started) is simply absent -- the
        gateway then answers with service-unavailable rather than a stale
        port (PRD: no cache that outlives an Apply). A backing whose port
        cannot be READ is absent by that same rule and for the same reason:
        service-unavailable is true, a route to port 0 is not. Answers from a
        PORTS_CACHE_TTL cache (invalidated on any real start/stop this
        instance performs); callers treat the dict as read-only."""
        if self._ports_cache is not None and time.monotonic() - self._ports_cache_at < PORTS_CACHE_TTL:
            return self._ports_cache
        ports: dict[str, int] = {}
        for d in BACKINGS:
            if await self._rt.status(self._cname(d)) != "running":
                continue
            port = await self._published_port_or_none(d)
            for kind in d.kinds if port else ():
                ports[kind] = port
        self._ports_cache = ports
        self._ports_cache_at = time.monotonic()
        return ports

    async def gc(self, active_kinds: set[str]) -> None:
        kinds = frozenset(active_kinds)
        if kinds == self._last_gc_kinds and not self._dirty:
            return  # same kinds, nothing started since the last sweep: zero docker calls
        for d in BACKINGS:
            if set(d.kinds).isdisjoint(active_kinds):
                await self._mesh.stop(self._cname(d))  # its mesh sidecar dies with it
                await self._rt.stop(self._cname(d))  # stop is idempotent on absent names
                self._ports_cache = None       # a backing may have really stopped
        self._last_gc_kinds = kinds
        self._dirty = False
