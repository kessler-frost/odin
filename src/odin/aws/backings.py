"""Real AWS-compatible backing services.

s3/sqs/sns/dynamodb nodes become resources inside real backing containers —
RustFS (s3), goaws (sqs+sns in one process, so SNS→SQS delivery works),
dynalite (dynamodb) — one shared container per (env, backing), run through the
same RuntimeDriver as every other workload. The Reconciler's `_aws` seam:
provision/exists/deprovision plus lifecycle (ensure_backing/gc/aws_env/facts)
and a host-side boto3 `client` for tests.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from odin.gateway import DEFAULT_GATEWAY_PORT
from odin.runtime.colima import CONTAINER_HOST, ContainerSpec

PROVISIONED = ("s3", "sqs", "sns", "dynamodb")
ACCESS_KEY = "allfather"
SECRET_KEY = "allfather-secret-key"
REGION = "us-east-1"
ACCOUNT = "000000000000"

READY_TIMEOUT = 120.0  # dynalite's npx fetch + first-run image pulls


@dataclass(frozen=True)
class BackingDef:
    name: str                  # container name suffix: allfather-aws-{name}-{env}
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
    BackingDef(name="dynalite", image="node:alpine", port=4567, env={},
               command=("npx", "-y", "dynalite", "--port", "4567"),
               kinds=("dynamodb",)),    # ~20s cold start (npx fetch); no TTL/Streams — accepted
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

    def _backing_for(self, service: str) -> BackingDef:
        return next(d for d in BACKINGS if service in d.kinds)

    def _cname(self, d: BackingDef) -> str:
        return f"allfather-aws-{d.name}-{self._env}"

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
        if self._rt.status(cname) == "running":
            return
        self._rt.stop(cname)  # clear any exited remnant (same contract as PostgresRds)
        volumes: dict[str, str] = {}
        if d.name == "goaws":
            # Config must live under the repo tree ($HOME is the only tree
            # Colima shares into the VM — a /tmp mount silently comes up empty).
            conf_dir = (self._root / self._env).resolve()
            conf_dir.mkdir(parents=True, exist_ok=True)
            (conf_dir / "goaws.yaml").write_text(_goaws_config(self._gateway_port))
            volumes = {str(conf_dir): "/conf"}
        self._rt.run_container(ContainerSpec(
            name=cname, image=d.image, env=d.env, ports={self._listen_port(d): 0},
            labels={"allfather-env": self._env}, command=d.command, volumes=volumes,
        ))
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                getattr(self.client(service), _PROBES[service])()
                return
            except (ClientError, BotoCoreError):
                time.sleep(1)
        raise RuntimeError(f"{cname} never became ready:\n{self._rt.logs(cname)}")

    def client(self, service: str):
        """Host-side client against the backing's published port (tests/e2e)."""
        d = self._backing_for(service)
        endpoint = f"http://127.0.0.1:{self._rt.host_port(self._cname(d), self._listen_port(d))}"
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

    def deprovision(self, service: str, name: str) -> None:
        client = self.client(service)
        try:
            if service == "s3":
                client.delete_bucket(Bucket=name)
            elif service == "sqs":
                client.delete_queue(QueueUrl=client.get_queue_url(QueueName=name)["QueueUrl"])
            elif service == "dynamodb":
                client.delete_table(TableName=name)
            elif service == "sns":
                client.delete_topic(TopicArn=f"arn:aws:sns:{REGION}:{ACCOUNT}:{name}")
        except (ClientError, BotoCoreError):
            pass

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
        port (PRD: no cache that outlives an Apply)."""
        ports: dict[str, int] = {}
        running = (d for d in BACKINGS if self._rt.status(self._cname(d)) == "running")
        for d in running:
            port = self._rt.host_port(self._cname(d), self._listen_port(d))
            for kind in d.kinds:
                ports[kind] = port
        return ports

    def gc(self, active_kinds: set[str]) -> None:
        for d in BACKINGS:
            if set(d.kinds).isdisjoint(active_kinds):
                self._rt.stop(self._cname(d))  # stop is idempotent on absent names
