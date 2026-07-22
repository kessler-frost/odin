"""BackingAws: shared per-env backing containers (RustFS/goaws/dynalite).

Unit-only — a FakeRuntime stands in for Colima and a fake client factory for
boto3. Real containers are exercised in the integration pass (Task 4).
"""
from __future__ import annotations

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

    def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.statuses[spec.name] = "running"
        self.ports[spec.name] = 51000 + len(self.runs)

    def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.statuses.pop(name, None)
        self.ports.pop(name, None)

    def status(self, name: str) -> str:
        return self.statuses.get(name, "absent")

    def host_port(self, name: str, container_port: int) -> int:
        return self.ports.get(name, 0)

    def logs(self, name: str, tail: int = 20) -> str:
        return f"fake logs of {name}"


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


def test_ensure_backing_s3_runs_rustfs_with_creds_and_dynamic_port(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    spec = rt.runs[0]
    assert spec.name == "allfather-aws-rustfs-default"
    assert spec.image == "rustfs/rustfs:latest"
    assert spec.env == {"RUSTFS_ACCESS_KEY": ACCESS_KEY, "RUSTFS_SECRET_KEY": SECRET_KEY}
    assert spec.ports == {9000: 0}
    assert spec.labels == {"allfather-env": "default"}
    # remnant-clear contract: stop() before run, same as PostgresRds.create_db
    assert rt.stopped == ["allfather-aws-rustfs-default"]


def test_ensure_backing_is_idempotent_while_running(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("s3")
    aws.ensure_backing("s3")
    assert len(rt.runs) == 1


def test_sqs_and_sns_share_one_goaws_container_with_mounted_config(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path, env="staging")
    aws.ensure_backing("sqs")
    aws.ensure_backing("sns")
    assert len(rt.runs) == 1
    spec = rt.runs[0]
    assert spec.name == "allfather-aws-goaws-staging"
    assert spec.image == "admiralpiett/goaws:v0.5.4"
    assert spec.command == ("-config", "/conf/goaws.yaml", "Local")
    assert spec.ports == {4100: 0}
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


def test_ensure_backing_timeout_raises_with_logs(rt, factory, tmp_path, monkeypatch):
    monkeypatch.setattr(backings, "READY_TIMEOUT", 0.0)
    with pytest.raises(RuntimeError, match="fake logs of allfather-aws-dynalite-default"):
        _aws(rt, factory, tmp_path).ensure_backing("dynamodb")


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
    s3_ep = f"http://host.docker.internal:{rt.ports['allfather-aws-rustfs-default']}"
    goaws_ep = f"http://host.docker.internal:{rt.ports['allfather-aws-goaws-default']}"
    ddb_ep = f"http://host.docker.internal:{rt.ports['allfather-aws-dynalite-default']}"
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
        "s3": rt.ports["allfather-aws-rustfs-default"],
        "sqs": rt.ports["allfather-aws-goaws-default"],
        "sns": rt.ports["allfather-aws-goaws-default"],
    }


def test_backing_ports_empty_when_nothing_running(rt, factory, tmp_path):
    assert _aws(rt, factory, tmp_path).backing_ports() == {}


def test_aws_env_yields_sqs_and_sns_from_one_goaws_plus_creds(rt, factory, tmp_path):
    aws = _aws(rt, factory, tmp_path)
    aws.ensure_backing("sqs")  # only goaws runs
    env = aws.aws_env()
    goaws_ep = f"http://host.docker.internal:{rt.ports['allfather-aws-goaws-default']}"
    assert env["AWS_ENDPOINT_URL_SQS"] == goaws_ep
    assert env["AWS_ENDPOINT_URL_SNS"] == goaws_ep
    assert "AWS_ENDPOINT_URL_S3" not in env
    assert "AWS_ENDPOINT_URL_DYNAMODB" not in env
    assert env["AWS_ACCESS_KEY_ID"] == ACCESS_KEY
    assert env["AWS_SECRET_ACCESS_KEY"] == SECRET_KEY
    assert env["AWS_DEFAULT_REGION"] == REGION


def test_gc_stops_backings_whose_kinds_are_all_inactive(rt, factory, tmp_path):
    _aws(rt, factory, tmp_path).gc({"s3"})
    assert set(rt.stopped) == {
        "allfather-aws-goaws-default", "allfather-aws-dynalite-default"}


def test_gc_with_no_active_kinds_stops_everything(rt, factory, tmp_path):
    _aws(rt, factory, tmp_path).gc(set())
    assert set(rt.stopped) == {
        "allfather-aws-rustfs-default",
        "allfather-aws-goaws-default",
        "allfather-aws-dynalite-default",
    }
