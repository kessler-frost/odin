"""W2.7 -- gateway/models/rdsctl.py: `aws_db_instance` as a real Postgres
container.

Same method as V1a/V3a (tests/gateway/test_ec2net.py, test_ec2compute.py):
every request is a REAL boto3-signed capture (the `rds` fixture), and every
response round-trips through botocore's OWN `query`-protocol parser -- so the
wire shapes here are checked against botocore's rds model rather than against
my reading of it. Calls go straight to `rdsctl.pure_answer` with a FAKE
substrate injected, so the whole state machine (creating -> available,
deleting, failed, converge) is exercised with no Docker and no Postgres; the
real thing is proven once, in tests/simulate/test_rds_tf_e2e.py.
"""
from __future__ import annotations

import time
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway.classify import classify
from odin.gateway.models import rdsctl
from odin.gateway.stores import SynthStores
from odin.reconcile.assertions import PgReady

_SESSION = botocore.session.get_session()
ENV = "default"
DB = "appdb"
USER = "app"
PASSWORD = "apppass123"


class FakePostgresRds:
    """The `PostgresRds` shape (`create_db`/`delete_db`/`endpoint`/
    `container_name`/`set_password`) with no container and no database --
    deterministic and instant, so the background create thread's transitions
    can be observed with a short poll instead of a real Postgres boot."""

    def __init__(self, port: int = 54321, fail_create: bool = False, ready: bool = True) -> None:
        self.port = port
        self.fail_create = fail_create
        self.ready = ready
        self.created: list[tuple[str, str, str, str]] = []
        self.deleted: list[str] = []
        self.passwords: list[tuple[str, str]] = []
        self.up: set[str] = set()

    def container_name(self, db_id: str) -> str:
        return f"odin-rds-{ENV}-{db_id}"

    def create_db(self, db_id: str, user: str, password: str, db_name: str = "postgres") -> None:
        self.created.append((db_id, user, password, db_name))
        if self.fail_create:
            raise RuntimeError("docker run failed")
        self.up.add(db_id)

    def delete_db(self, db_id: str) -> None:
        self.deleted.append(db_id)
        self.up.discard(db_id)

    def endpoint(self, db_id: str) -> tuple[str, int] | None:
        return ("127.0.0.1", self.port) if db_id in self.up else None

    def set_password(self, db_id: str, user: str, current: str, new: str) -> None:
        self.passwords.append((db_id, new))


class FailingDelete(FakePostgresRds):
    def delete_db(self, db_id: str) -> None:
        raise RuntimeError("docker rm failed")


@pytest.fixture(autouse=True)
def fast_probe(monkeypatch):
    """`pg_ready_sync` answers instantly from the fake substrate's `ready`
    flag -- the ONE thing a unit test can't do for real. `_POLL_INTERVAL` and
    `_CREATE_TIMEOUT` shrink so the "never becomes ready" path finishes in
    well under a second instead of three minutes."""
    monkeypatch.setattr(rdsctl, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rdsctl, "_CREATE_TIMEOUT", 0.5)
    monkeypatch.setattr(
        rdsctl, "pg_ready_sync",
        lambda host, port, user, password, db="postgres": PgReady(ok=_READY.get("ok", True), error=None if _READY.get("ok", True) else "connection refused"),
    )


_READY: dict[str, bool] = {}


@pytest.fixture(autouse=True)
def reset_ready():
    _READY.clear()
    _READY["ok"] = True
    yield
    _READY.clear()


def _stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("rds")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


def _call(body: bytes, stores: SynthStores, rds, action: str, resource: str = DB) -> Response:
    return rdsctl.pure_answer(action, resource, ENV, body, stores, time.monotonic(), rds=rds)


def _captured(sink, client, call) -> tuple[str, str, bytes]:
    """(action, resource, body) for a REAL boto3-signed rds request -- the
    classify() pass doubles as the proof that the model's own dispatch key and
    the gateway's classification agree."""
    request = sink.call(lambda: call(client))
    action, resource = classify("rds", request.method, "/", {}, request.headers, request.body)
    return action, resource, request.body


def _create(sink, client, stores, rds, **kwargs) -> Response:
    action, resource, body = _captured(sink, client, lambda c: c.create_db_instance(
        DBInstanceIdentifier=kwargs.pop("identifier", DB),
        DBInstanceClass=kwargs.pop("instance_class", "db.t3.micro"),
        Engine="postgres",
        MasterUsername=USER,
        MasterUserPassword=PASSWORD,
        AllocatedStorage=kwargs.pop("storage", 20),
        DBName=kwargs.pop("db_name", "postgres"),
        Tags=kwargs.pop("tags", [{"Key": "odin:node", "Value": DB}]),
        **kwargs,
    ))
    assert action == "rds:CreateDBInstance"
    return _call(body, stores, rds, action, resource)


def _describe(sink, client, stores, rds, identifier: str | None = DB) -> Response:
    action, resource, body = _captured(sink, client, lambda c: (
        c.describe_db_instances(DBInstanceIdentifier=identifier) if identifier
        else c.describe_db_instances()
    ))
    return _call(body, stores, rds, action, resource)


def _await_status(sink, client, stores, rds, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        parsed = _parse("DescribeDBInstances", _describe(sink, client, stores, rds))
        instance = parsed["DBInstances"][0]
        if instance["DBInstanceStatus"] == status:
            return instance
        assert time.monotonic() < deadline, f"never reached {status}: {instance['DBInstanceStatus']}"
        time.sleep(0.02)


# --- create: the transitional answer + the real waiter ----------------------


def test_create_returns_creating_immediately_and_boots_in_the_background(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    parsed = _parse("CreateDBInstance", _create(sink, rds, stores, fake))
    instance = parsed["DBInstance"]
    # The create ANSWER is `creating` with no endpoint at all -- exactly what
    # the provider's own DBInstanceAvailable waiter is built to poll through.
    assert instance["DBInstanceStatus"] == "creating"
    assert "Endpoint" not in instance
    assert instance["DBInstanceIdentifier"] == DB
    assert instance["Engine"] == "postgres"
    assert instance["MasterUsername"] == USER
    assert instance["AllocatedStorage"] == 20
    assert instance["DBInstanceArn"] == f"arn:aws:rds:us-east-1:000000000000:db:{DB}"

    available = _await_status(sink, rds, stores, fake, "available")
    assert available["Endpoint"] == {"Address": "host.docker.internal", "Port": 54321, "HostedZoneId": "Z2R2ITUGPM61AM"}
    assert available["DbInstancePort"] == 54321
    assert fake.created == [(DB, USER, PASSWORD, "postgres")]


def test_available_is_gated_on_a_real_pg_ready_probe_not_on_docker_run(tmp_path, sink, rds):
    """The health assertion moved, it wasn't dropped: a container that starts
    but never accepts connections is `failed` with the probe's OWN error, so
    the apply fails instead of reporting a database that doesn't work."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _READY["ok"] = False
    _create(sink, rds, stores, fake)
    failed = _await_status(sink, rds, stores, fake, "failed")
    assert failed["DBInstanceStatus"] == "failed"
    record = rdsctl.records(stores, ENV)[0]
    assert "connection refused" in record["status_reason"]


def test_a_container_that_never_starts_lands_failed_with_the_real_reason(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds(fail_create=True)
    _create(sink, rds, stores, fake)
    _await_status(sink, rds, stores, fake, "failed")
    assert "docker run failed" in rdsctl.records(stores, ENV)[0]["status_reason"]


def test_creating_the_same_identifier_twice_is_an_already_exists_error(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    duplicate = _create(sink, rds, stores, fake)
    assert duplicate.status_code == 400
    parsed = _parse("CreateDBInstance", duplicate, error=True)
    assert parsed["Error"]["Code"] == "DBInstanceAlreadyExists"


# --- describe --------------------------------------------------------------


def test_describe_an_unknown_instance_is_db_instance_not_found(tmp_path, sink, rds):
    """The exact wire code terraform-provider-aws's Read drops state on, and
    its delete waiter treats as "gone"."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    response = _describe(sink, rds, stores, fake, identifier="nope")
    assert response.status_code == 404
    assert _parse("DescribeDBInstances", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


def test_describe_without_an_identifier_lists_every_instance(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    _create(sink, rds, stores, fake, identifier="other", tags=[{"Key": "odin:node", "Value": "other"}])
    parsed = _parse("DescribeDBInstances", _describe(sink, rds, stores, fake, identifier=None))
    assert [i["DBInstanceIdentifier"] for i in parsed["DBInstances"]] == [DB, "other"]


def test_zero_drift_fields_carry_the_provider_defaults_that_would_otherwise_flap(tmp_path, sink, rds):
    """Every `Optional`-without-`Computed` attribute in the provider's own
    aws_db_instance schema is emitted explicitly -- AutoMinorVersionUpgrade
    above all, whose provider DEFAULT is true (omitting it reads back false and
    drifts every plan)."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    instance = _await_status(sink, rds, stores, fake, "available")
    assert instance["AutoMinorVersionUpgrade"] is True
    for field in (
        "MultiAZ", "PubliclyAccessible", "StorageEncrypted", "CopyTagsToSnapshot",
        "DeletionProtection", "IAMDatabaseAuthenticationEnabled",
        "PerformanceInsightsEnabled", "CustomerOwnedIpEnabled", "DedicatedLogVolume",
    ):
        assert instance[field] is False, field
    assert instance["MonitoringInterval"] == 0
    assert instance["DBParameterGroups"][0]["DBParameterGroupName"] == "default.postgres16"
    assert instance["OptionGroupMemberships"][0]["OptionGroupName"] == "default:postgres-16"


# --- tags ------------------------------------------------------------------


def test_create_tags_round_trip_through_list_tags_and_the_describe_taglist(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake, tags=[{"Key": "odin:node", "Value": DB}, {"Key": "team", "Value": "core"}])
    action, resource, body = _captured(sink, rds, lambda c: c.list_tags_for_resource(
        ResourceName=rdsctl.db_arn(DB),
    ))
    assert action == "rds:ListTagsForResource"
    # The tag calls carry an ARN, reduced to the same bare identifier the
    # policy layer keys on.
    assert resource == DB
    parsed = _parse("ListTagsForResource", _call(body, stores, fake, action, resource))
    assert {t["Key"]: t["Value"] for t in parsed["TagList"]} == {"odin:node": DB, "team": "core"}
    instance = _parse("DescribeDBInstances", _describe(sink, rds, stores, fake))["DBInstances"][0]
    assert {t["Key"]: t["Value"] for t in instance["TagList"]} == {"odin:node": DB, "team": "core"}


def test_add_and_remove_tags_mutate_the_shared_tag_store(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    action, resource, body = _captured(sink, rds, lambda c: c.add_tags_to_resource(
        ResourceName=rdsctl.db_arn(DB), Tags=[{"Key": "env", "Value": "dev"}],
    ))
    assert action == "rds:AddTagsToResource"
    assert _call(body, stores, fake, action, resource).status_code == 200
    assert stores.tags.get(ENV, f"rds:{rdsctl.db_arn(DB)}") == {"odin:node": DB, "env": "dev"}

    action, resource, body = _captured(sink, rds, lambda c: c.remove_tags_from_resource(
        ResourceName=rdsctl.db_arn(DB), TagKeys=["env"],
    ))
    assert action == "rds:RemoveTagsFromResource"
    assert _call(body, stores, fake, action, resource).status_code == 200
    assert stores.tags.get(ENV, f"rds:{rdsctl.db_arn(DB)}") == {"odin:node": DB}


def test_tagging_an_unknown_instance_is_not_found(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    action, resource, body = _captured(sink, rds, lambda c: c.list_tags_for_resource(
        ResourceName=rdsctl.db_arn("ghost"),
    ))
    response = _call(body, stores, fake, action, resource)
    assert _parse("ListTagsForResource", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


# --- modify ----------------------------------------------------------------


def test_modify_records_metadata_changes_and_echoes_them_back(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    _await_status(sink, rds, stores, fake, "available")
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier=DB, AllocatedStorage=50, DBInstanceClass="db.t3.small", ApplyImmediately=True,
    ))
    assert action == "rds:ModifyDBInstance"
    parsed = _parse("ModifyDBInstance", _call(body, stores, fake, action, resource))
    assert parsed["DBInstance"]["AllocatedStorage"] == 50
    assert parsed["DBInstance"]["DBInstanceClass"] == "db.t3.small"
    # Metadata only -- the container is untouched (a documented limit, not a
    # silent resize).
    assert fake.created == [(DB, USER, PASSWORD, "postgres")]


def test_modify_password_runs_a_real_alter_user(tmp_path, sink, rds):
    """The DATABASE_URL fact embeds this password, so storing a new one without
    applying it would publish a credential that doesn't work."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    _await_status(sink, rds, stores, fake, "available")
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier=DB, MasterUserPassword="newpass456", ApplyImmediately=True,
    ))
    assert _call(body, stores, fake, action, resource).status_code == 200
    assert fake.passwords == [(DB, "newpass456")]
    assert rdsctl.records(stores, ENV)[0]["master_password"] == "newpass456"


def test_modify_password_failure_is_a_real_error_not_a_silent_drift(tmp_path, sink, rds):
    class RefusingRds(FakePostgresRds):
        def set_password(self, db_id, user, current, new):
            raise RuntimeError("connection refused")

    stores, fake = _stores(tmp_path), RefusingRds()
    _create(sink, rds, stores, fake)
    _await_status(sink, rds, stores, fake, "available")
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier=DB, MasterUserPassword="newpass456", ApplyImmediately=True,
    ))
    response = _call(body, stores, fake, action, resource)
    assert _parse("ModifyDBInstance", response, error=True)["Error"]["Code"] == "InvalidDBInstanceState"
    assert rdsctl.records(stores, ENV)[0]["master_password"] == PASSWORD


def test_modify_an_unknown_instance_is_not_found(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier="ghost", AllocatedStorage=50,
    ))
    response = _call(body, stores, fake, action, resource)
    assert _parse("ModifyDBInstance", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


# --- delete ----------------------------------------------------------------


def _delete(sink, client, stores, rds, identifier: str = DB) -> Response:
    action, resource, body = _captured(sink, client, lambda c: c.delete_db_instance(
        DBInstanceIdentifier=identifier, SkipFinalSnapshot=True,
    ))
    assert action == "rds:DeleteDBInstance"
    return _call(body, stores, rds, action, resource)


def test_delete_reports_deleting_then_the_record_disappears(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    _await_status(sink, rds, stores, fake, "available")
    parsed = _parse("DeleteDBInstance", _delete(sink, rds, stores, fake))
    assert parsed["DBInstance"]["DBInstanceStatus"] == "deleting"
    deadline = time.monotonic() + 5.0
    while rdsctl.records(stores, ENV):
        assert time.monotonic() < deadline, "the record never disappeared"
        time.sleep(0.02)
    assert fake.deleted == [DB]
    # The delete waiter's success condition: the instance is genuinely gone.
    assert _describe(sink, rds, stores, fake).status_code == 404
    assert stores.tags.get(ENV, f"rds:{rdsctl.db_arn(DB)}") == {}


def test_a_failed_container_delete_keeps_the_record_deleting_rather_than_lying(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FailingDelete()
    _create(sink, rds, stores, FakePostgresRds())
    _delete(sink, rds, stores, fake)
    deadline = time.monotonic() + 5.0
    while True:
        record = rdsctl.records(stores, ENV)[0]
        if record.get("status_reason"):
            break
        assert time.monotonic() < deadline, "the delete failure was never recorded"
        time.sleep(0.02)
    assert record["status"] == "deleting"
    assert "docker rm failed" in record["status_reason"]


def test_deleting_an_unknown_instance_is_not_found(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    response = _delete(sink, rds, stores, fake, identifier="ghost")
    assert _parse("DeleteDBInstance", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


# --- drift + converge ------------------------------------------------------


def test_mark_instance_failed_then_converge_recreates_the_container(tmp_path, sink, rds):
    """The crash-recovery contract W2.2's `converge_*` pattern established, for
    rds: the reality sweep marks the record `failed` (honest, and what the
    canvas shows), and an Apply's converge is what actually brings the real
    Postgres back -- tofu's own plan is empty, since the config never changed."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    _await_status(sink, rds, stores, fake, "available")

    fake.up.discard(DB)  # the container is gone (docker kill / rm)
    rdsctl.mark_instance_failed(stores, ENV, DB, "container removed outside odin")
    failed = _parse("DescribeDBInstances", _describe(sink, rds, stores, fake))["DBInstances"][0]
    assert failed["DBInstanceStatus"] == "failed"

    rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    recovered = _await_status(sink, rds, stores, fake, "available")
    assert recovered["Endpoint"]["Port"] == 54321
    assert fake.created == [(DB, USER, PASSWORD, "postgres"), (DB, USER, PASSWORD, "postgres")]
    assert rdsctl.records(stores, ENV)[0]["status_reason"] is None


def test_converge_leaves_available_and_creating_instances_alone(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _create(sink, rds, stores, fake)
    _await_status(sink, rds, stores, fake, "available")
    rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    assert fake.created == [(DB, USER, PASSWORD, "postgres")]


# --- dispatch --------------------------------------------------------------


def test_an_unmodeled_action_is_a_protocol_correct_invalid_action(tmp_path):
    stores = _stores(tmp_path)
    response = rdsctl.pure_answer(
        "rds:CreateDBSnapshot", DB, ENV, b"Action=CreateDBSnapshot", stores, 0.0, rds=FakePostgresRds(),
    )
    assert response.status_code == 400
    parsed = _parse("CreateDBInstance", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidAction"
