"""BackingAws: shared per-env backing containers (RustFS/goaws/dynalite).

Unit-only — a FakeRuntime stands in for Colima and a fake client factory for
boto3. Real containers are exercised in the integration pass (Task 4).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest
from botocore.exceptions import ClientError

from odin.aws import backings
from odin.aws.backings import ACCESS_KEY, ACCOUNT, BackingAws, REGION, SECRET_KEY
from odin.gateway import DEFAULT_GATEWAY_PORT
from odin.runtime.colima import ContainerSpec


@dataclass
class FakeRuntime:
    runs: list[ContainerSpec] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    published: dict[str, set[int]] = field(default_factory=dict)  # name -> inside-ports really published
    images: set[str] = field(default_factory=set)
    builds: list[str] = field(default_factory=list)
    # call logs — each entry is a real docker-CLI-shaped subprocess in prod,
    # so the caching tests assert against their lengths
    status_calls: list[str] = field(default_factory=list)
    port_calls: list[str] = field(default_factory=list)

    def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.statuses[spec.name] = "running"
        self.ports[spec.name] = 51000 + len(self.runs)
        # Real docker only answers `docker port` for a port the container was
        # actually created WITH -- load-bearing for the stranded-port
        # self-heal (a goaws container published on the OLD gateway port
        # publishes nothing on the new one).
        self.published[spec.name] = set(spec.ports)

    def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.statuses.pop(name, None)
        self.ports.pop(name, None)
        self.published.pop(name, None)

    def status(self, name: str) -> str:
        self.status_calls.append(name)
        return self.statuses.get(name, "absent")

    def host_port(self, name: str, container_port: int) -> int:
        self.port_calls.append(name)
        if container_port not in self.published.get(name, set()):
            return 0
        return self.ports.get(name, 0)

    def logs(self, name: str, tail: int = 20) -> str:
        return f"fake logs of {name}"

    def image_exists(self, tag: str) -> bool:
        return tag in self.images

    def build(self, tag: str, dockerfile: str) -> None:
        self.builds.append(tag)
        self.images.add(tag)


class FakeClient:
    def __init__(self, service: str, factory: "FakeClientFactory") -> None:
        self._service = service
        self._factory = factory

    def __getattr__(self, method: str):
        def call(**kwargs):
            self._factory.calls.append((self._service, method, kwargs))
            error = self._factory.errors.get((self._service, method))
            if error:
                raise error
            return self._factory.responses.get((self._service, method), {})
        return call


class FakeClientFactory:
    """Test seam for boto3: records constructions + calls, replays responses."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: dict[tuple[str, str], dict] = {}
        self.errors: dict[tuple[str, str], Exception] = {}

    def __call__(self, service: str, endpoint_url: str) -> FakeClient:
        self.created.append((service, endpoint_url))
        return FakeClient(service, self)


@pytest.fixture
def rt():
    return FakeRuntime()


@pytest.fixture
def factory():
    return FakeClientFactory()


def _aws(rt, factory, tmp_path, env="default"):
    return BackingAws(rt, env=env, root=tmp_path, client_factory=factory)


def test_container_name_is_the_public_form_of_the_real_backing_name(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path, env="prod")
    assert aws.container_name("s3") == "odin-aws-rustfs-prod"
    assert aws.container_name("sqs") == "odin-aws-goaws-prod"  # sqs/sns share one container
    assert aws.container_name("sns") == "odin-aws-goaws-prod"
    assert aws.container_name("dynamodb") == "odin-aws-dynalite-prod"


def test_ensure_backing_s3_runs_rustfs_with_creds_and_dynamic_port(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    spec = rt.runs[0]
    assert spec.name == "odin-aws-rustfs-default"
    assert spec.image == "rustfs/rustfs:latest"
    assert spec.env == {"RUSTFS_ACCESS_KEY": ACCESS_KEY, "RUSTFS_SECRET_KEY": SECRET_KEY}
    assert spec.ports == {9000: 0}
    assert spec.labels == {"odin-env": "default"}
    # remnant-clear contract: stop() before run, same as PostgresRds.create_db
    assert rt.stopped == ["odin-aws-rustfs-default"]


def test_ensure_backing_is_idempotent_while_running(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    aws.ensure_backing("s3")
    assert len(rt.runs) == 1


# --- W2.2: the stranded-port self-heal. A running container that no longer
# publishes the inside-port this instance needs must be RECREATED, not
# adopted -- goaws's listener port IS the gateway port, so a gateway-port
# change used to strand it as BackingUnavailable forever. --------------------


def test_goaws_container_on_a_stale_gateway_port_is_recreated(rt, factory, tmp_path):
    old = BackingAws(rt, env="default", root=tmp_path, client_factory=factory, gateway_port=4266)
    old.ensure_backing("sqs")
    assert rt.runs[0].ports == {4266: 0}  # goaws listens on the gateway's port

    # The app restarted onto a different gateway port; the container from the
    # previous run is still up, still publishing 4266 and nothing else.
    fresh = BackingAws(rt, env="default", root=tmp_path, client_factory=factory, gateway_port=4300)
    fresh.ensure_backing("sqs")

    assert len(rt.runs) == 2, "adopting the stranded container is the bug"
    assert rt.runs[1].ports == {4300: 0}
    assert rt.stopped.count("odin-aws-goaws-default") == 2  # rm -f before each run
    # And the whole point: a client can actually be built now.
    assert fresh.client("sqs") is not None
    # goaws.yaml was rewritten with the current port too, else the container
    # would publish 4300 while its listener bound 4266.
    assert 'Port: "4300"' in (tmp_path / "default" / "goaws.yaml").read_text()


def test_a_running_backing_that_publishes_its_port_is_still_adopted(rt, factory, tmp_path):
    # The normal path must be untouched: same gateway port, no recreate.
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("sqs")
    aws.ensure_backing("sqs")
    other = BackingAws(
        rt, env="default", root=tmp_path, client_factory=factory, gateway_port=DEFAULT_GATEWAY_PORT,
    )
    other.ensure_backing("sns")  # the SAME container serves both kinds

    assert len(rt.runs) == 1
    assert rt.stopped.count("odin-aws-goaws-default") == 1


def test_ensure_backing_is_thread_safe_against_concurrent_callers(rt, factory, tmp_path):
    """S5 regression: /apply-full calls ensure_backing directly while the
    SAME BackingAws instance's Reconciler background loop can independently
    call it too (provision() -> ensure_backing()) on another OS thread
    (both run under asyncio.to_thread). Without a lock around the
    check-then-create, two threads can both observe "not running" and both
    call docker run for the identical container name -- exactly the
    Conflict error a real docker daemon raises. The fake mimics that: a
    slight sleep before registering the container widens the race window
    (like a real `docker run` taking real wall time), and a second create
    for a name already running raises the same error shape Colima did."""

    class RacyRuntime(FakeRuntime):
        def run_container(self, spec):
            if spec.name in self.statuses:
                raise RuntimeError(
                    f'docker run ... failed: Conflict. The container name '
                    f'"/{spec.name}" is already in use by container "deadbeef"'
                )
            time.sleep(0.05)  # widen the window a real `docker run` leaves open
            super().run_container(spec)

    racy_rt = RacyRuntime()
    aws = _aws(racy_rt, factory, tmp_path)
    errors: list[Exception] = []

    def _call() -> None:
        try:
            aws.ensure_backing("s3")
        except Exception as exc:  # noqa: BLE001 — capturing across threads for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []  # the lock serializes the race away — no Conflict ever surfaces
    assert len(racy_rt.runs) == 1  # exactly one effective run


def test_ensure_backing_heals_a_stale_already_in_use_conflict(rt, factory, tmp_path, monkeypatch):
    """Belt-and-braces: even with the per-instance lock, a stale remnant
    from a different process/instance can still lose the name race (the
    lock only serializes callers on THIS instance). ensure_backing must
    heal -- wait for Docker to report the container running, then proceed
    to the readiness probe -- rather than raising."""
    monkeypatch.setattr(backings, "READY_TIMEOUT", 2.0)
    cname = "odin-aws-rustfs-default"

    class ConflictingRuntime(FakeRuntime):
        def run_container(self, spec):
            raise RuntimeError(
                f'docker run ... failed: Conflict. The container name '
                f'"/{spec.name}" is already in use by container "deadbeef"'
            )

    conflict_rt = ConflictingRuntime()
    aws = _aws(conflict_rt, factory, tmp_path)

    def _external_creator_wins() -> None:
        time.sleep(0.1)  # simulate the other creator's docker run finishing shortly after
        conflict_rt.statuses[cname] = "running"
        conflict_rt.ports[cname] = 51000
        conflict_rt.published[cname] = {9000}  # ...publishing rustfs's real wire port

    threading.Thread(target=_external_creator_wins).start()

    aws.ensure_backing("s3")  # must not raise -- heals via the except branch
    assert conflict_rt.runs == []  # this instance never successfully created anything itself


def test_sqs_and_sns_share_one_goaws_container_with_mounted_config(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path, env="staging")
    aws.ensure_backing("sqs")
    aws.ensure_backing("sns")
    assert len(rt.runs) == 1
    spec = rt.runs[0]
    assert spec.name == "odin-aws-goaws-staging"
    assert spec.image == "admiralpiett/goaws:v0.5.4"
    assert spec.command == ("-config", "/conf/goaws.yaml", "Local")
    # goaws binds its listener to the config's Local.Port (verified against
    # the real image), which is deliberately the GATEWAY's port, not 4100 --
    # Docker must publish that same port or nothing is reachable (G5 bug).
    assert spec.ports == {DEFAULT_GATEWAY_PORT: 0}
    assert spec.volumes == {str((tmp_path / "staging").resolve()): "/conf"}
    config_text = (tmp_path / "staging" / "goaws.yaml").read_text()
    assert 'AccountId: "000000000000"' in config_text
    # Host/Port point at the gateway (not goaws's own container port) so
    # goaws's returned QueueUrls/TopicArns re-dial through the gateway.
    assert 'Host: "host.docker.internal"' in config_text
    assert f'Port: "{DEFAULT_GATEWAY_PORT}"' in config_text


def test_goaws_config_uses_the_configured_gateway_port(rt, factory, tmp_path):
    aws = BackingAws(rt, env="staging", root=tmp_path, client_factory=factory, gateway_port=5555)
    aws.ensure_backing("sqs")
    config_text = (tmp_path / "staging" / "goaws.yaml").read_text()
    assert 'Host: "host.docker.internal"' in config_text
    assert 'Port: "5555"' in config_text
    # the published/queried container port must track the SAME gateway port
    # goaws was actually told to listen on -- not the BackingDef's nominal 4100.
    assert rt.runs[0].ports == {5555: 0}


def test_ensure_backing_timeout_raises_with_logs(rt, factory, tmp_path, monkeypatch):
    monkeypatch.setattr(backings, "READY_TIMEOUT", 0.0)
    with pytest.raises(RuntimeError, match="fake logs of odin-aws-dynalite-default"):
        _aws(rt, factory, tmp_path).ensure_backing("dynamodb")


def test_ensure_backing_dynamodb_builds_the_baked_image_when_absent(rt, factory, tmp_path):
    """S5 e2e root cause #2: bare `node:alpine` + `npx -y dynalite` re-fetched
    from the npm registry on every boot, and a slow/flaky registry blew the
    readiness probe's budget ("never became ready", empty container logs).
    The baked image is built ONCE (network access accepted there) so every
    subsequent boot is instant and offline."""
    _aws(rt, factory, tmp_path).ensure_backing("dynamodb")
    assert rt.builds == [backings._DYNALITE_IMAGE]
    spec = rt.runs[0]
    assert spec.image == backings._DYNALITE_IMAGE
    assert spec.command == ("--port", "4567")  # no more npx -y dynalite


def test_ensure_backing_dynamodb_skips_the_build_when_the_image_already_exists(rt, factory, tmp_path):
    rt.images.add(backings._DYNALITE_IMAGE)
    _aws(rt, factory, tmp_path).ensure_backing("dynamodb")
    assert rt.builds == []


def test_provision_dynamodb_creates_table_with_id_hash_key(rt, factory, tmp_path):
    _aws(rt, factory, tmp_path).provision("dynamodb", "jobs")
    assert ("dynamodb", "create_table", {
        "TableName": "jobs", "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [{"AttributeName": "id", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
    }) in factory.calls


def test_provision_sns_subscribes_queues_using_returned_arns(rt, factory, tmp_path):
    factory.responses[("sns", "create_topic")] = {"TopicArn": "arn:fake:alerts"}
    factory.responses[("sqs", "create_queue")] = {"QueueUrl": "http://q/jobs"}
    factory.responses[("sqs", "get_queue_attributes")] = {"Attributes": {"QueueArn": "arn:fake:jobs"}}
    _aws(rt, factory, tmp_path).provision("sns", "alerts", subscriptions=("jobs",))

    assert ("sqs", "create_queue", {"QueueName": "jobs"}) in factory.calls
    get_attrs = next(c for c in factory.calls if c[1] == "get_queue_attributes")
    assert get_attrs[2] == {"QueueUrl": "http://q/jobs", "AttributeNames": ["QueueArn"]}
    subscribe = next(c for c in factory.calls if c[1] == "subscribe")
    assert subscribe[2] == {
        "TopicArn": "arn:fake:alerts", "Protocol": "sqs",
        "Endpoint": "arn:fake:jobs", "Attributes": {"RawMessageDelivery": "true"},
    }


def test_provision_tolerates_already_exists_client_errors(rt, factory, tmp_path):
    factory.errors[("s3", "create_bucket")] = ClientError(
        {"Error": {"Code": "BucketAlreadyExists", "Message": "Exists"}}, "CreateBucket")
    _aws(rt, factory, tmp_path).provision("s3", "uploads")  # must not raise


def test_provision_raises_on_other_client_errors(rt, factory, tmp_path):
    factory.errors[("s3", "create_bucket")] = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "CreateBucket")
    with pytest.raises(ClientError):
        _aws(rt, factory, tmp_path).provision("s3", "uploads")


def test_exists_false_when_backing_down_without_any_client_call(rt, factory, tmp_path):
    assert _aws(rt, factory, tmp_path).exists("s3", "uploads") is False
    assert factory.created == []


def test_exists_true_when_backing_up_and_check_passes(rt, factory, tmp_path):
    factory.responses[("sns", "list_topics")] = {
        "Topics": [{"TopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:alerts"}]}
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    aws.ensure_backing("sns")
    assert aws.exists("s3", "uploads") is True
    assert aws.exists("sns", "alerts") is True
    assert aws.exists("sns", "other") is False


def test_exists_false_when_check_raises(rt, factory, tmp_path):
    factory.errors[("dynamodb", "describe_table")] = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, "DescribeTable")
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("dynamodb")
    assert aws.exists("dynamodb", "jobs") is False


def test_deprovision_is_best_effort(rt, factory, tmp_path):
    factory.errors[("s3", "delete_bucket")] = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "no"}}, "DeleteBucket")
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("sqs")
    aws.deprovision("s3", "uploads")  # must not raise
    factory.responses[("sqs", "get_queue_url")] = {"QueueUrl": "http://q/jobs"}
    aws.deprovision("sqs", "jobs")
    assert ("sqs", "delete_queue", {"QueueUrl": "http://q/jobs"}) in factory.calls


def test_facts_shapes_for_all_four_kinds(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    for service in ("s3", "sqs", "dynamodb"):
        aws.ensure_backing(service)
    s3_ep = f"http://host.docker.internal:{rt.ports['odin-aws-rustfs-default']}"
    goaws_ep = f"http://host.docker.internal:{rt.ports['odin-aws-goaws-default']}"
    ddb_ep = f"http://host.docker.internal:{rt.ports['odin-aws-dynalite-default']}"
    gateway_ep = f"http://host.docker.internal:{DEFAULT_GATEWAY_PORT}"
    assert aws.facts("s3", "uploads") == {"BUCKET": "uploads", "endpoint": s3_ep}
    # QUEUE_URL is the one fact re-pointed at the gateway (matches goaws.yaml's
    # own Host/Port); "endpoint" stays the backing's own direct port.
    assert aws.facts("sqs", "jobs") == {
        "QUEUE_URL": f"{gateway_ep}/{ACCOUNT}/jobs", "endpoint": goaws_ep}
    assert aws.facts("sns", "alerts") == {
        "TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:alerts", "endpoint": goaws_ep}
    assert aws.facts("dynamodb", "tasks") == {"TABLE": "tasks", "endpoint": ddb_ep}


def test_backing_ports_maps_service_to_running_backings_host_port(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    aws.ensure_backing("sqs")  # goaws also serves sns from the same container
    assert aws.backing_ports() == {
        "s3": rt.ports["odin-aws-rustfs-default"],
        "sqs": rt.ports["odin-aws-goaws-default"],
        "sns": rt.ports["odin-aws-goaws-default"],
    }


def test_backing_ports_empty_when_nothing_running(rt, factory, tmp_path):
    assert _aws(rt, factory, tmp_path).backing_ports() == {}


def test_aws_env_yields_sqs_and_sns_from_one_goaws_plus_creds(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("sqs")  # only goaws runs
    env = aws.aws_env()
    goaws_ep = f"http://host.docker.internal:{rt.ports['odin-aws-goaws-default']}"
    assert env["AWS_ENDPOINT_URL_SQS"] == goaws_ep
    assert env["AWS_ENDPOINT_URL_SNS"] == goaws_ep
    assert "AWS_ENDPOINT_URL_S3" not in env
    assert "AWS_ENDPOINT_URL_DYNAMODB" not in env
    assert env["AWS_ACCESS_KEY_ID"] == ACCESS_KEY
    assert env["AWS_SECRET_ACCESS_KEY"] == SECRET_KEY
    assert env["AWS_DEFAULT_REGION"] == REGION


# W2.6: each stopped backing takes its mesh SIDECAR with it (`<backing>-mesh`,
# fabric/sidecar.py) -- it lives in that container's network namespace, so it
# would die anyway; stopping it explicitly is what leaves nothing behind.
def test_gc_stops_backings_whose_kinds_are_all_inactive(rt, factory, tmp_path):
    _aws(rt, factory, tmp_path).gc({"s3"})
    assert set(rt.stopped) == {
        "odin-aws-goaws-default", "odin-aws-dynalite-default", "odin-aws-registry-default",
        "odin-aws-goaws-default-mesh", "odin-aws-dynalite-default-mesh", "odin-aws-registry-default-mesh"}


def test_gc_with_no_active_kinds_stops_everything(rt, factory, tmp_path):
    _aws(rt, factory, tmp_path).gc(set())
    assert set(rt.stopped) == {
        "odin-aws-rustfs-default",
        "odin-aws-goaws-default",
        "odin-aws-dynalite-default",
        "odin-aws-registry-default",
        "odin-aws-rustfs-default-mesh",
        "odin-aws-goaws-default-mesh",
        "odin-aws-dynalite-default-mesh",
        "odin-aws-registry-default-mesh",
    }


# --- per-tick docker-call cache: backing_ports TTL + gc's nothing-changed skip --------
# The reconciler calls both every ~2s per env; each recompute/sweep is real
# docker subprocess traffic, so steady-state ticks must answer from cache.


def test_backing_ports_second_call_within_ttl_makes_no_runtime_calls(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    first = aws.backing_ports()
    counts = (len(rt.status_calls), len(rt.port_calls))
    assert aws.backing_ports() == first
    assert (len(rt.status_calls), len(rt.port_calls)) == counts  # served from cache


def test_backing_ports_requeries_after_the_ttl_expires(rt, factory, tmp_path, monkeypatch):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    aws.backing_ports()
    status_count = len(rt.status_calls)
    now = time.monotonic()
    monkeypatch.setattr(backings.time, "monotonic", lambda: now + backings.PORTS_CACHE_TTL)
    assert aws.backing_ports() == {"s3": rt.ports["odin-aws-rustfs-default"]}
    assert len(rt.status_calls) > status_count  # expired: swept the runtime for real


def test_backing_ports_cache_invalidated_when_a_backing_starts(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    assert set(aws.backing_ports()) == {"s3"}
    aws.ensure_backing("sqs")  # goaws really boots: the s3-only table is stale now
    assert set(aws.backing_ports()) == {"s3", "sqs", "sns"}


def test_backing_ports_cache_invalidated_when_gc_stops_a_backing(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    aws.ensure_backing("sqs")
    assert set(aws.backing_ports()) == {"s3", "sqs", "sns"}
    aws.gc({"s3"})  # stops goaws — the cache must not keep serving sqs/sns
    assert set(aws.backing_ports()) == {"s3"}


def test_gc_skips_the_docker_sweep_when_kinds_unchanged_and_nothing_started(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.gc({"s3"})
    swept = list(rt.stopped)
    aws.gc({"s3"})  # same kinds, nothing ensured in between: zero docker calls
    assert rt.stopped == swept


def test_gc_resweeps_when_the_active_kinds_change(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.gc({"s3", "sqs"})
    swept = len(rt.stopped)
    aws.gc({"s3"})  # goaws just became inactive — must be swept away
    assert "odin-aws-goaws-default" in rt.stopped[swept:]


def test_gc_resweeps_after_ensure_backing_actually_starts_a_container(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.gc({"s3"})            # sweep once, then the skip arms
    aws.ensure_backing("s3")  # a real boot (e.g. crash recovery): re-arms the dirty flag
    swept = len(rt.stopped)
    aws.gc({"s3"})            # same kinds, but a container just started: must re-sweep
    assert len(rt.stopped) > swept


def test_noop_ensure_backing_on_a_running_container_keeps_gcs_skip_armed(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")  # boots rustfs (dirty)
    aws.gc({"s3"})            # sweeps, then the skip arms
    aws.ensure_backing("s3")  # already running: NOT a state change
    swept = list(rt.stopped)
    aws.gc({"s3"})            # unchanged kinds + nothing started: still skipped
    assert rt.stopped == swept


# --- V2b: the ecr registry:2 backing --------------------------------------------------


def test_ensure_backing_ecr_runs_registry_with_dynamic_port(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("ecr")
    spec = rt.runs[0]
    assert spec.name == "odin-aws-registry-default"
    assert spec.image == "registry:2"
    assert spec.ports == {5000: 0}
    assert spec.env == {}
    assert spec.command == ()


def test_ensure_backing_ecr_is_idempotent_while_running(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("ecr")
    aws.ensure_backing("ecr")
    assert len(rt.runs) == 1


def test_backing_ports_includes_ecr_when_running(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("ecr")
    assert aws.backing_ports() == {"ecr": rt.ports["odin-aws-registry-default"]}


def test_gc_keeps_registry_running_while_ecr_is_active(rt, factory, tmp_path):
    # No ensure_backing() first (unlike the running-container assertions
    # above): ensure_backing's OWN pre-create "clear any exited remnant"
    # stop() would otherwise pollute rt.stopped before gc() ever runs,
    # matching test_gc_stops_backings_whose_kinds_are_all_inactive's pattern.
    _aws(rt, factory, tmp_path).gc({"ecr"})
    assert "odin-aws-registry-default" not in rt.stopped
