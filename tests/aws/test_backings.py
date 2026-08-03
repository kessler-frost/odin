"""BackingAws: shared per-env backing containers (RustFS/goaws/dynalite).

Unit-only — a FakeRuntime stands in for Colima and a fake client factory for
boto3. Real containers are exercised in the integration pass (Task 4).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import pytest
from botocore.exceptions import ClientError

from odin.aws import backings
from odin.aws.backings import ACCESS_KEY, ACCOUNT, BackingAws, REGION, SECRET_KEY
from odin.gateway import DEFAULT_GATEWAY_PORT
from odin.runtime.colima import ColimaRuntime, ContainerSpec, _Proc


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
    # a real `docker inspect -f {{.State.ExitCode}}` answers 0 for a LIVE
    # container, which is why `_not_ready_reason` only prints it when the
    # container has actually stopped
    exit_codes: dict[str, int] = field(default_factory=dict)
    log_text: dict[str, str] = field(default_factory=dict)

    async def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.statuses[spec.name] = "running"
        self.ports[spec.name] = 51000 + len(self.runs)
        # Real docker only answers `docker port` for a port the container was
        # actually created WITH -- load-bearing for the stranded-port
        # self-heal (a goaws container published on the OLD gateway port
        # publishes nothing on the new one).
        self.published[spec.name] = set(spec.ports)

    async def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.statuses.pop(name, None)
        self.ports.pop(name, None)
        self.published.pop(name, None)

    async def status(self, name: str) -> str:
        self.status_calls.append(name)
        return self.statuses.get(name, "absent")

    async def host_port(self, name: str, container_port: int) -> int:
        self.port_calls.append(name)
        if container_port not in self.published.get(name, set()):
            return 0
        return self.ports.get(name, 0)

    async def exit_code(self, name: str) -> int:
        return self.exit_codes.get(name, 0)

    async def logs(self, name: str, tail: int = 20) -> str:
        return self.log_text.get(name, f"fake logs of {name}")

    async def image_exists(self, tag: str) -> bool:
        return tag in self.images

    async def build(self, tag: str, dockerfile: str) -> None:
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


async def test_ensure_backing_s3_runs_rustfs_with_creds_and_dynamic_port(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    spec = rt.runs[0]
    assert spec.name == "odin-aws-rustfs-default"
    assert spec.image == "rustfs/rustfs:latest"
    assert spec.env == {"RUSTFS_ACCESS_KEY": ACCESS_KEY, "RUSTFS_SECRET_KEY": SECRET_KEY}
    assert spec.ports == {9000: 0}
    assert spec.labels == {"odin-env": "default"}
    # remnant-clear contract: stop() before run, same as PostgresRds.create_db
    assert rt.stopped == ["odin-aws-rustfs-default"]


async def test_ensure_backing_is_idempotent_while_running(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    await aws.ensure_backing("s3")
    assert len(rt.runs) == 1


# --- W2.2: the stranded-port self-heal. A running container that no longer
# publishes the inside-port this instance needs must be RECREATED, not
# adopted -- goaws's listener port IS the gateway port, so a gateway-port
# change used to strand it as BackingUnavailable forever. --------------------


async def test_goaws_container_on_a_stale_gateway_port_is_recreated(rt, factory, tmp_path):
    old = BackingAws(rt, env="default", root=tmp_path, client_factory=factory, gateway_port=4266)
    await old.ensure_backing("sqs")
    assert rt.runs[0].ports == {4266: 0}  # goaws listens on the gateway's port

    # The app restarted onto a different gateway port; the container from the
    # previous run is still up, still publishing 4266 and nothing else.
    fresh = BackingAws(rt, env="default", root=tmp_path, client_factory=factory, gateway_port=4300)
    await fresh.ensure_backing("sqs")

    assert len(rt.runs) == 2, "adopting the stranded container is the bug"
    assert rt.runs[1].ports == {4300: 0}
    assert rt.stopped.count("odin-aws-goaws-default") == 2  # rm -f before each run
    # And the whole point: a client can actually be built now.
    assert await fresh.client("sqs") is not None
    # goaws.yaml was rewritten with the current port too, else the container
    # would publish 4300 while its listener bound 4266.
    assert 'Port: "4300"' in (tmp_path / "default" / "goaws.yaml").read_text()


async def test_a_running_backing_that_publishes_its_port_is_still_adopted(rt, factory, tmp_path):
    # The normal path must be untouched: same gateway port, no recreate.
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("sqs")
    await aws.ensure_backing("sqs")
    other = BackingAws(
        rt, env="default", root=tmp_path, client_factory=factory, gateway_port=DEFAULT_GATEWAY_PORT,
    )
    await other.ensure_backing("sns")  # the SAME container serves both kinds

    assert len(rt.runs) == 1
    assert rt.stopped.count("odin-aws-goaws-default") == 1


async def test_ensure_backing_serializes_concurrent_callers(rt, factory, tmp_path):
    """S5 regression, restated for v0.7.7's event loop: /apply-full calls
    ensure_backing directly while the SAME BackingAws instance's Reconciler
    background loop can independently call it too (provision() ->
    ensure_backing()). Before de-threading those were two OS threads; now they
    are two asyncio TASKS, and the hazard is identical because the critical
    section (`status` -> `_stranded` -> `_create_backing_container`) is now
    three `await`s -- every one of them a point where the loop can hand the
    other task control. Without `_ensure_lock` both observe "not running" and
    both call docker run for the identical container name, which is the
    Conflict error a real docker daemon raises.

    The fake mimics the daemon: an `await asyncio.sleep` before registering the
    container widens the window a real `docker run` leaves open, and a second
    create for a name already running raises the same error shape Colima did.
    That sleep is what makes this a real test of the lock -- delete
    `_ensure_lock` from `ensure_backing` and `len(runs)` becomes 2."""

    class RacyRuntime(FakeRuntime):
        async def run_container(self, spec):
            if spec.name in self.statuses:
                raise RuntimeError(
                    f'docker run ... failed: Conflict. The container name '
                    f'"/{spec.name}" is already in use by container "deadbeef"'
                )
            await asyncio.sleep(0.05)  # widen the window a real `docker run` leaves open
            await super().run_container(spec)

    racy_rt = RacyRuntime()
    aws = _aws(racy_rt, factory, tmp_path)

    outcomes = await asyncio.gather(
        aws.ensure_backing("s3"), aws.ensure_backing("s3"), return_exceptions=True,
    )

    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert errors == []  # the lock serializes the race away — no Conflict ever surfaces
    assert len(racy_rt.runs) == 1  # exactly one effective run


async def test_ensure_backing_heals_a_stale_already_in_use_conflict(rt, factory, tmp_path, monkeypatch):
    """Belt-and-braces: even with the per-instance lock, a stale remnant
    from a different process/instance can still lose the name race (the
    lock only serializes callers on THIS instance). ensure_backing must
    heal -- wait for Docker to report the container running, then proceed
    to the readiness probe -- rather than raising."""
    monkeypatch.setattr(backings, "READY_TIMEOUT", 2.0)
    cname = "odin-aws-rustfs-default"

    class ConflictingRuntime(FakeRuntime):
        async def run_container(self, spec):
            raise RuntimeError(
                f'docker run ... failed: Conflict. The container name '
                f'"/{spec.name}" is already in use by container "deadbeef"'
            )

    conflict_rt = ConflictingRuntime()
    aws = _aws(conflict_rt, factory, tmp_path)

    async def _external_creator_wins() -> None:
        # The other creator is a different PROCESS in production; here it is a
        # concurrent task, which is enough because `_create_backing_container`'s
        # heal loop `await`s -- that suspension is exactly what lets this run.
        await asyncio.sleep(0.1)
        conflict_rt.statuses[cname] = "running"
        conflict_rt.ports[cname] = 51000
        conflict_rt.published[cname] = {9000}  # ...publishing rustfs's real wire port

    creator = asyncio.create_task(_external_creator_wins())

    await aws.ensure_backing("s3")  # must not raise -- heals via the except branch
    await creator
    assert conflict_rt.runs == []  # this instance never successfully created anything itself


async def test_sqs_and_sns_share_one_goaws_container_with_mounted_config(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path, env="staging")
    await aws.ensure_backing("sqs")
    await aws.ensure_backing("sns")
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


async def test_goaws_config_uses_the_configured_gateway_port(rt, factory, tmp_path):
    aws = BackingAws(rt, env="staging", root=tmp_path, client_factory=factory, gateway_port=5555)
    await aws.ensure_backing("sqs")
    config_text = (tmp_path / "staging" / "goaws.yaml").read_text()
    assert 'Host: "host.docker.internal"' in config_text
    assert 'Port: "5555"' in config_text
    # the published/queried container port must track the SAME gateway port
    # goaws was actually told to listen on -- not the BackingDef's nominal 4100.
    assert rt.runs[0].ports == {5555: 0}


async def test_ensure_backing_timeout_raises_with_logs(rt, factory, tmp_path, monkeypatch):
    monkeypatch.setattr(backings, "READY_TIMEOUT", 0.0)
    with pytest.raises(RuntimeError, match="fake logs of odin-aws-dynalite-default"):
        await _aws(rt, factory, tmp_path).ensure_backing("dynamodb")


async def test_a_backing_that_never_became_ready_says_WHY_not_only_that_it_did_not(rt, factory, tmp_path, monkeypatch):
    """The S5 incident this module's own docstring cites, finally answered.

    The message was `f"{cname} never became ready:\\n{logs}"`, and `logs`
    answers `""` both for a container that wrote nothing and for one the
    runtime could not read -- so the entire explanation was a dangling colon
    and a blank line. Measured against real RustFS and real registry
    containers driven to a real timeout, with `status`, `exit_code` and
    `host_port` all readable at that instant and all three discarded.

    These tests pin the RENDERING; the real-container measurement is recorded
    in `_not_ready_reason`'s docstring, because a fabricated signal proves the
    formatter, not the integration."""
    monkeypatch.setattr(backings, "READY_TIMEOUT", 0.0)
    with pytest.raises(RuntimeError) as exc:
        await _aws(rt, factory, tmp_path).ensure_backing("s3")
    msg = str(exc.value)
    assert not msg.endswith(":\n") and not msg.endswith(":")   # the defect itself
    assert "the s3 list_buckets probe never succeeded" in msg  # WHICH probe, not just "not ready"
    assert "published on host port 51001" in msg              # the discriminator that was unread
    assert "after 0s" in msg
    assert "Container: running" in msg
    assert "exit code" not in msg                             # a LIVE container's exit code is 0


async def test_a_stopped_backing_names_its_exit_code_and_a_live_one_never_does(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    d = aws._backing_for("s3")
    cname = aws._cname(d)
    rt.statuses[cname] = "running"
    rt.published[cname] = {d.port}
    rt.ports[cname] = 34071

    live = await aws._not_ready_reason(cname, d, "the probe never succeeded")
    assert "Container: running." in live
    # "exit code 0" under a failure sends a reader down the wrong path
    assert "exit code" not in live

    rt.statuses[cname] = "exited"
    rt.exit_codes[cname] = 5
    assert "Container: exited, exit code 5." in await aws._not_ready_reason(cname, d, "the probe never succeeded")


async def test_an_unpublished_port_is_a_different_reason_from_a_mute_one(rt, factory, tmp_path):
    """The discrimination the old message could not make at all: docker never
    published the port, versus a port that is published and never answers."""
    aws = _aws(rt, factory, tmp_path)
    d = aws._backing_for("s3")
    cname = aws._cname(d)
    rt.statuses[cname] = "running"

    assert f"docker never published its {d.port}" in await aws._not_ready_reason(cname, d, "the probe never succeeded")


async def test_a_silent_backing_says_the_container_state_is_the_whole_of_it(rt, factory, tmp_path):
    """`logs == ""` was the entire old message; now it is the one case that
    says so out loud, and a talkative container keeps its tail as a BONUS
    rather than the headline (a backing can log a line that reads like success
    while the real reason sits in the port)."""
    aws = _aws(rt, factory, tmp_path)
    d = aws._backing_for("s3")
    cname = aws._cname(d)

    rt.log_text[cname] = ""
    assert "It has logged nothing, so the container state above is the whole of it." in \
        await aws._not_ready_reason(cname, d, "the probe never succeeded")

    rt.log_text.pop(cname)
    assert f"Its logs:\nfake logs of {cname}" in await aws._not_ready_reason(cname, d, "the probe never succeeded")


async def test_the_registry_backing_names_its_own_wire_shape_not_a_boto3_probe(rt, tmp_path, monkeypatch):
    """ecr is the one backing whose probe is NOT a boto3 call -- registry:2
    speaks the Docker Registry v2 protocol and understands neither SigV4 nor
    the ECR JSON shape -- so its reason must name `GET /v2/`. Built without a
    client_factory on purpose: that seam is exactly what makes the other
    probes skip real I/O."""
    monkeypatch.setattr(backings, "READY_TIMEOUT", 0.0)
    aws = BackingAws(rt, env="default", root=tmp_path)
    d = aws._backing_for("ecr")
    cname = aws._cname(d)
    rt.statuses[cname] = "running"
    rt.published[cname] = {d.port}
    rt.ports[cname] = 34072

    with pytest.raises(RuntimeError) as exc:
        await aws._await_registry_ready(cname)
    msg = str(exc.value)
    assert "GET /v2/ never returned 200" in msg
    assert "list_buckets" not in msg and "probe never succeeded" not in msg


async def test_ensure_backing_dynamodb_builds_the_baked_image_when_absent(rt, factory, tmp_path):
    """S5 e2e root cause #2: bare `node:alpine` + `npx -y dynalite` re-fetched
    from the npm registry on every boot, and a slow/flaky registry blew the
    readiness probe's budget ("never became ready", empty container logs).
    The baked image is built ONCE (network access accepted there) so every
    subsequent boot is instant and offline."""
    await _aws(rt, factory, tmp_path).ensure_backing("dynamodb")
    assert rt.builds == [backings._DYNALITE_IMAGE]
    spec = rt.runs[0]
    assert spec.image == backings._DYNALITE_IMAGE
    assert spec.command == ("--port", "4567")  # no more npx -y dynalite


async def test_ensure_backing_dynamodb_skips_the_build_when_the_image_already_exists(rt, factory, tmp_path):
    rt.images.add(backings._DYNALITE_IMAGE)
    await _aws(rt, factory, tmp_path).ensure_backing("dynamodb")
    assert rt.builds == []


async def test_provision_dynamodb_creates_table_with_id_hash_key(rt, factory, tmp_path):
    await _aws(rt, factory, tmp_path).provision("dynamodb", "jobs")
    assert ("dynamodb", "create_table", {
        "TableName": "jobs", "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [{"AttributeName": "id", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
    }) in factory.calls


async def test_provision_sns_subscribes_queues_using_returned_arns(rt, factory, tmp_path):
    factory.responses[("sns", "create_topic")] = {"TopicArn": "arn:fake:alerts"}
    factory.responses[("sqs", "create_queue")] = {"QueueUrl": "http://q/jobs"}
    factory.responses[("sqs", "get_queue_attributes")] = {"Attributes": {"QueueArn": "arn:fake:jobs"}}
    await _aws(rt, factory, tmp_path).provision("sns", "alerts", subscriptions=(("jobs", True),))

    assert ("sqs", "create_queue", {"QueueName": "jobs"}) in factory.calls
    get_attrs = next(c for c in factory.calls if c[1] == "get_queue_attributes")
    assert get_attrs[2] == {"QueueUrl": "http://q/jobs", "AttributeNames": ["QueueArn"]}
    subscribe = next(c for c in factory.calls if c[1] == "subscribe")
    assert subscribe[2] == {
        "TopicArn": "arn:fake:alerts", "Protocol": "sqs",
        "Endpoint": "arn:fake:jobs", "Attributes": {"RawMessageDelivery": "true"},
    }


async def test_an_edge_that_turned_raw_delivery_OFF_subscribes_with_false(rt, factory, tmp_path):
    """The non-tofu Apply path has to agree with the generated HCL. It
    hardcoded `"true"` until v0.8.21, so a canvas that turned the flag off got
    the JSON envelope through `tofu apply` and the raw body through Apply --
    the same topic delivering two different shapes depending on which button
    the user pressed, visible only to whatever parsed the message."""
    factory.responses[("sns", "create_topic")] = {"TopicArn": "arn:fake:alerts"}
    factory.responses[("sqs", "create_queue")] = {"QueueUrl": "http://q/jobs"}
    factory.responses[("sqs", "get_queue_attributes")] = {"Attributes": {"QueueArn": "arn:fake:jobs"}}
    await _aws(rt, factory, tmp_path).provision("sns", "alerts", subscriptions=(("jobs", False),))

    subscribe = next(c for c in factory.calls if c[1] == "subscribe")
    assert subscribe[2]["Attributes"] == {"RawMessageDelivery": "false"}


async def test_provision_tolerates_already_exists_client_errors(rt, factory, tmp_path):
    factory.errors[("s3", "create_bucket")] = ClientError(
        {"Error": {"Code": "BucketAlreadyExists", "Message": "Exists"}}, "CreateBucket")
    await _aws(rt, factory, tmp_path).provision("s3", "uploads")  # must not raise


async def test_provision_raises_on_other_client_errors(rt, factory, tmp_path):
    factory.errors[("s3", "create_bucket")] = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "CreateBucket")
    with pytest.raises(ClientError):
        await _aws(rt, factory, tmp_path).provision("s3", "uploads")


async def test_exists_false_when_backing_down_without_any_client_call(rt, factory, tmp_path):
    assert await _aws(rt, factory, tmp_path).exists("s3", "uploads") is False
    assert factory.created == []


async def test_exists_true_when_backing_up_and_check_passes(rt, factory, tmp_path):
    factory.responses[("sns", "list_topics")] = {
        "Topics": [{"TopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:alerts"}]}
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    await aws.ensure_backing("sns")
    assert await aws.exists("s3", "uploads") is True
    assert await aws.exists("sns", "alerts") is True
    assert await aws.exists("sns", "other") is False


async def test_exists_false_when_check_raises(rt, factory, tmp_path):
    factory.errors[("dynamodb", "describe_table")] = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, "DescribeTable")
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("dynamodb")
    assert await aws.exists("dynamodb", "jobs") is False


async def test_deprovision_is_best_effort_AND_SAYS_WHICH(rt, factory, tmp_path):
    """Best-effort is the deliberate contract (the resource or its whole backing
    may already be gone, and a raise would fail a teardown for having nothing to
    tear down) -- but "swallowed" and "deleted" must not be the same answer.

    This test asserted only "must not raise" until v0.8.18, which is exactly the
    ambiguity: `deprovision` returned None either way, and
    `reconciler.py::_execute` prunes the World entry regardless, so a bucket
    that survived the delete vanished from the canvas anyway.
    """
    factory.errors[("s3", "delete_bucket")] = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "no"}}, "DeleteBucket")
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("sqs")
    assert await aws.deprovision("s3", "uploads") is False  # swallowed, not deleted
    factory.responses[("sqs", "get_queue_url")] = {"QueueUrl": "http://q/jobs"}
    assert await aws.deprovision("sqs", "jobs") is True
    assert ("sqs", "delete_queue", {"QueueUrl": "http://q/jobs"}) in factory.calls


async def test_facts_shapes_for_all_four_kinds(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    for service in ("s3", "sqs", "dynamodb"):
        await aws.ensure_backing(service)
    s3_ep = f"http://host.docker.internal:{rt.ports['odin-aws-rustfs-default']}"
    goaws_ep = f"http://host.docker.internal:{rt.ports['odin-aws-goaws-default']}"
    ddb_ep = f"http://host.docker.internal:{rt.ports['odin-aws-dynalite-default']}"
    gateway_ep = f"http://host.docker.internal:{DEFAULT_GATEWAY_PORT}"
    assert await aws.facts("s3", "uploads") == {"BUCKET": "uploads", "endpoint": s3_ep}
    # QUEUE_URL is the one fact re-pointed at the gateway (matches goaws.yaml's
    # own Host/Port); "endpoint" stays the backing's own direct port.
    assert await aws.facts("sqs", "jobs") == {
        "QUEUE_URL": f"{gateway_ep}/{ACCOUNT}/jobs", "endpoint": goaws_ep}
    assert await aws.facts("sns", "alerts") == {
        "TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:alerts", "endpoint": goaws_ep}
    assert await aws.facts("dynamodb", "tasks") == {"TABLE": "tasks", "endpoint": ddb_ep}


async def test_backing_ports_maps_service_to_running_backings_host_port(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    await aws.ensure_backing("sqs")  # goaws also serves sns from the same container
    assert await aws.backing_ports() == {
        "s3": rt.ports["odin-aws-rustfs-default"],
        "sqs": rt.ports["odin-aws-goaws-default"],
        "sns": rt.ports["odin-aws-goaws-default"],
    }


async def test_backing_ports_empty_when_nothing_running(rt, factory, tmp_path):
    assert await _aws(rt, factory, tmp_path).backing_ports() == {}


async def test_aws_env_yields_sqs_and_sns_from_one_goaws_plus_creds(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("sqs")  # only goaws runs
    env = await aws.aws_env()
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
async def test_gc_stops_backings_whose_kinds_are_all_inactive(rt, factory, tmp_path):
    await _aws(rt, factory, tmp_path).gc({"s3"})
    assert set(rt.stopped) == {
        "odin-aws-goaws-default", "odin-aws-dynalite-default", "odin-aws-registry-default",
        "odin-aws-goaws-default-mesh", "odin-aws-dynalite-default-mesh", "odin-aws-registry-default-mesh"}


async def test_gc_with_no_active_kinds_stops_everything(rt, factory, tmp_path):
    await _aws(rt, factory, tmp_path).gc(set())
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


async def test_backing_ports_second_call_within_ttl_makes_no_runtime_calls(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    first = await aws.backing_ports()
    counts = (len(rt.status_calls), len(rt.port_calls))
    assert await aws.backing_ports() == first
    assert (len(rt.status_calls), len(rt.port_calls)) == counts  # served from cache


async def test_backing_ports_requeries_after_the_ttl_expires(rt, factory, tmp_path, monkeypatch):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    await aws.backing_ports()
    status_count = len(rt.status_calls)
    now = time.monotonic()
    monkeypatch.setattr(backings.time, "monotonic", lambda: now + backings.PORTS_CACHE_TTL)
    assert await aws.backing_ports() == {"s3": rt.ports["odin-aws-rustfs-default"]}
    assert len(rt.status_calls) > status_count  # expired: swept the runtime for real


async def test_backing_ports_cache_invalidated_when_a_backing_starts(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    assert set(await aws.backing_ports()) == {"s3"}
    await aws.ensure_backing("sqs")  # goaws really boots: the s3-only table is stale now
    assert set(await aws.backing_ports()) == {"s3", "sqs", "sns"}


async def test_backing_ports_cache_invalidated_when_gc_stops_a_backing(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")
    await aws.ensure_backing("sqs")
    assert set(await aws.backing_ports()) == {"s3", "sqs", "sns"}
    await aws.gc({"s3"})  # stops goaws — the cache must not keep serving sqs/sns
    assert set(await aws.backing_ports()) == {"s3"}


async def test_gc_skips_the_docker_sweep_when_kinds_unchanged_and_nothing_started(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.gc({"s3"})
    swept = list(rt.stopped)
    await aws.gc({"s3"})  # same kinds, nothing ensured in between: zero docker calls
    assert rt.stopped == swept


async def test_gc_resweeps_when_the_active_kinds_change(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.gc({"s3", "sqs"})
    swept = len(rt.stopped)
    await aws.gc({"s3"})  # goaws just became inactive — must be swept away
    assert "odin-aws-goaws-default" in rt.stopped[swept:]


async def test_gc_resweeps_after_ensure_backing_actually_starts_a_container(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.gc({"s3"})            # sweep once, then the skip arms
    await aws.ensure_backing("s3")  # a real boot (e.g. crash recovery): re-arms the dirty flag
    swept = len(rt.stopped)
    await aws.gc({"s3"})            # same kinds, but a container just started: must re-sweep
    assert len(rt.stopped) > swept


async def test_noop_ensure_backing_on_a_running_container_keeps_gcs_skip_armed(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("s3")  # boots rustfs (dirty)
    await aws.gc({"s3"})            # sweeps, then the skip arms
    await aws.ensure_backing("s3")  # already running: NOT a state change
    swept = list(rt.stopped)
    await aws.gc({"s3"})            # unchanged kinds + nothing started: still skipped
    assert rt.stopped == swept


# --- V2b: the ecr registry:2 backing --------------------------------------------------


async def test_ensure_backing_ecr_runs_registry_with_dynamic_port(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("ecr")
    spec = rt.runs[0]
    assert spec.name == "odin-aws-registry-default"
    assert spec.image == "registry:2"
    assert spec.ports == {5000: 0}
    assert spec.env == {}
    assert spec.command == ()


async def test_ensure_backing_ecr_is_idempotent_while_running(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("ecr")
    await aws.ensure_backing("ecr")
    assert len(rt.runs) == 1


async def test_backing_ports_includes_ecr_when_running(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    await aws.ensure_backing("ecr")
    assert await aws.backing_ports() == {"ecr": rt.ports["odin-aws-registry-default"]}


async def test_gc_keeps_registry_running_while_ecr_is_active(rt, factory, tmp_path):
    # No ensure_backing() first (unlike the running-container assertions
    # above): ensure_backing's OWN pre-create "clear any exited remnant"
    # stop() would otherwise pollute rt.stopped before gc() ever runs,
    # matching test_gc_stops_backings_whose_kinds_are_all_inactive's pattern.
    await _aws(rt, factory, tmp_path).gc({"ecr"})
    assert "odin-aws-registry-default" not in rt.stopped


# --- field test 5 facts audit: an unreadable port never becomes an endpoint ---
#
# These drive the REAL ColimaRuntime (not FakeRuntime) with only the subprocess
# seam injected, so the whole real path runs: `docker inspect` fails ->
# PortUnreadable -> BackingUnavailable -> no `:0` anywhere.


class BrokenDockerRunner:
    """The container CLI, hiccuping. `status` still answers `running` (the
    container really is up), the port-map read fails -- the exact narrow window
    the audit is about, and the only one that reaches `facts()` at all."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, args, input=None):
        self.calls.append(args)
        if "{{.State.Status}}" in args:
            return _Proc(0, "running")
        return _Proc(1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")


def _broken_aws(tmp_path, factory):
    return BackingAws(ColimaRuntime(runner=BrokenDockerRunner()), env="default",
                      root=tmp_path, client_factory=factory)


async def test_facts_refuses_to_name_an_endpoint_it_could_not_read(tmp_path, factory):
    """THE hazard: `host_port` answered any CLI failure with 0, `facts()`
    interpolated it, and the resulting `http://host.docker.internal:0` went
    into `world.json` PERMANENTLY -- these facts are published once, on the
    starting->healthy transition, and never refreshed."""
    aws = _broken_aws(tmp_path, factory)
    with pytest.raises(backings.BackingUnavailable) as raised:
        await aws.facts("s3", "uploads")
    assert "Cannot connect to the Docker daemon" in str(raised.value)  # the REAL reason survives
    assert ":0" not in str(raised.value)


async def test_client_still_fails_typed_when_the_port_read_itself_fails(tmp_path, factory):
    # deprovision and friends swallow BackingUnavailable by name; a raw
    # PortUnreadable escaping here would turn a best-effort cleanup into a crash.
    with pytest.raises(backings.BackingUnavailable):
        await _broken_aws(tmp_path, factory).client("s3")


async def test_the_gateway_routing_table_omits_a_backing_it_cannot_read(tmp_path, factory):
    # Absent means the gateway answers service-unavailable (true). A 0 entry
    # meant it forwarded to port 0 and blamed the backing.
    assert await _broken_aws(tmp_path, factory).backing_ports() == {}


async def test_endpoint_vars_omit_a_backing_whose_port_cannot_be_read(tmp_path, factory):
    env = await _broken_aws(tmp_path, factory).aws_env()
    assert not [k for k in env if k.startswith("AWS_ENDPOINT_URL_")]
    assert env["AWS_ACCESS_KEY_ID"] == ACCESS_KEY  # creds are still knowable


# --- field test 6: WHY a backing is unavailable, from the real docker --------
#
# `docker rm -f` racing /apply-full's ensure phase was reproduced for real, and
# the exception it produced blamed a "gateway_port mismatch between this
# BackingAws and the container's creator" for a container that had simply been
# deleted. So the two answers real docker 28.4.0 gives were probed on this
# machine before this was coded against (honesty rule 1), and these are the
# transcripts:
#
#   $ docker run -d --name c -p 0:80 registry:2 && docker inspect -f '{{json .NetworkSettings.Ports}}' c
#   {"80/tcp":[{"HostIp":"0.0.0.0","HostPort":"33947"},{"HostIp":"::","HostPort":"33947"}]}   rc=0
#   $ docker create --name c … ; docker inspect …   ->  {}    rc=0   (State.Status "created")
#   $ docker run -d … ; docker stop c ; docker inspect …  ->  {}    rc=0   (State.Status "exited")
#   $ docker rm -f c ; docker inspect …  ->  (empty) "error: no such object: c"   rc=1
#
# i.e. an EMPTY port map means the container is not RUNNING; only rc=1 means the
# runtime could not be asked. Both fixtures below reproduce those exact bytes
# through the real ColimaRuntime, so the parse and the diagnosis are tested
# against docker's real contract rather than a fabricated one.


class _StateRunner:
    """Real docker's answers for a container in a given `State.Status`, with the
    empty port map that goes with every non-running state."""

    def __init__(self, state: str) -> None:
        self.state = state

    async def __call__(self, args, input=None):
        if "{{.State.Status}}" in args:
            if self.state == "absent":
                return _Proc(1, "", f"error: no such object: {args[-1]}")
            return _Proc(0, self.state)
        if "{{json .NetworkSettings.Ports}}" in args:
            return _Proc(0, "{}")
        return _Proc(0, "")


def _aws_with_state(tmp_path, factory, state: str):
    return BackingAws(ColimaRuntime(runner=_StateRunner(state)), env="applyfix",
                      root=tmp_path, client_factory=factory)


@pytest.mark.parametrize(("state", "expected"), [
    ("absent", "no container by that name exists"),
    ("created", "the container was created but never started"),
    ("exited", "the container exited"),
    ("running", "gateway_port mismatch"),
    ("dead", "odin has no reading for that container state"),
])
async def test_an_unavailable_backing_names_the_state_docker_really_reports(tmp_path, factory, state, expected):
    """The old message asserted "gateway_port mismatch ... ?" for all of these.
    It is only true for the RUNNING one -- a container that is running and still
    publishes nothing on the port this instance wants really is a mismatch --
    and it is actively misleading for the deleted case, which is the one a real
    `docker rm -f` produces. A state the map has no entry for says so rather
    than inheriting the running case's diagnosis."""
    with pytest.raises(backings.BackingUnavailable) as raised:
        await _aws_with_state(tmp_path, factory, state).client("s3")
    assert expected in str(raised.value)


async def test_an_unavailable_backing_carries_the_container_and_state_structurally(tmp_path, factory):
    """`server.py`'s failure verdict publishes `backing: {container, observed}`
    in its JSON body. Read off the exception, never scraped back out of its
    message."""
    with pytest.raises(backings.BackingUnavailable) as raised:
        await _aws_with_state(tmp_path, factory, "exited").client("s3")
    assert raised.value.container == "odin-aws-rustfs-applyfix"
    assert raised.value.observed == "exited"
    assert "odin-aws-rustfs-applyfix" in str(raised.value)


async def test_an_unreadable_port_does_not_go_on_to_ask_for_the_state(tmp_path, factory):
    """The unreadable branch keeps its own reason and asks the runtime nothing
    else: the runtime has just failed to answer a question, so a second question
    proves nothing and its failure would replace a real reason with a worse
    one. `BrokenDockerRunner` answers `running` for the status read, so a
    `_published_port` that consulted it would append the mismatch diagnosis to a
    "Cannot connect to the Docker daemon" failure."""
    with pytest.raises(backings.BackingUnavailable) as raised:
        await _broken_aws(tmp_path, factory).client("s3")
    assert "Cannot connect to the Docker daemon" in str(raised.value)
    assert "gateway_port mismatch" not in str(raised.value)
    assert raised.value.observed == "unreadable"
