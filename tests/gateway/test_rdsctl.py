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

import threading
import time
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.fabric.models import FirewallRule
from odin.fabric.nebula import sg_rules_to_firewall
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
    `container_name`/`set_password`/`join_mesh`) with no container and no
    database -- deterministic and instant, so the background create thread's
    transitions can be observed with a short poll instead of a real Postgres
    boot.

    W2.6: `joined` records (db_id, firewall) so a test can assert WHICH
    compiled SG firewall gated the database, and `overlay` is the address a
    join hands back (None = this env has no mesh)."""

    def __init__(self, port: int = 54321, fail_create: bool = False, ready: bool = True,
                 overlay: str | None = None) -> None:
        self.port = port
        self.fail_create = fail_create
        self.ready = ready
        self.created: list[tuple[str, str, str, str]] = []
        self.deleted: list[str] = []
        self.passwords: list[tuple[str, str]] = []
        self.up: set[str] = set()
        self.overlay = overlay
        self.joined: list[tuple[str, object]] = []
        self.revisions: list[str] = []

    def join_mesh(self, db_id: str, firewall=None, revision: str = "") -> str | None:
        self.joined.append((db_id, firewall))
        self.revisions.append(revision)
        return self.overlay

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


class SlowStart(FakePostgresRds):
    """`create_db` returns BEFORE the container publishes a port -- the real
    substrate's normal case (image pull, then initdb), and therefore the
    "legitimately still coming up" window a test needs to hold open. The port
    appears when a test adds the id to `up`."""

    def create_db(self, db_id: str, user: str, password: str, db_name: str = "postgres") -> None:
        self.created.append((db_id, user, password, db_name))


_READY: dict[str, bool] = {}
_PROBES: dict[str, int] = {}


def _fake_probe(host, port, user, password, db="postgres") -> PgReady:
    """`pg_ready_sync`'s answer, counted. `flap` makes the SECOND probe fail
    once -- the postgres init-then-restart shape `_wait_available`'s
    consecutive-success rule exists for."""
    _PROBES["n"] = _PROBES.get("n", 0) + 1
    ok = _READY.get("ok", True)
    if ok and _READY.get("flap") and _PROBES["n"] == 2:
        ok = False
    return PgReady(ok=ok, error=None if ok else "connection refused")


@pytest.fixture(autouse=True)
def fast_probe(monkeypatch):
    """`pg_ready_sync` answers instantly from the `_READY` flags -- the ONE
    thing a unit test can't do for real. `_POLL_INTERVAL` and `_CREATE_TIMEOUT`
    shrink so the "never becomes ready" path finishes in well under a second
    instead of three minutes."""
    monkeypatch.setattr(rdsctl, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rdsctl, "_CREATE_TIMEOUT", 0.5)
    monkeypatch.setattr(rdsctl, "pg_ready_sync", _fake_probe)


@pytest.fixture(autouse=True)
def reset_ready():
    _READY.clear()
    _PROBES.clear()
    _READY["ok"] = True
    yield
    _READY.clear()
    _PROBES.clear()


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


async def _create(sink, client, stores, rds, **kwargs) -> Response:
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
    return await _call(body, stores, rds, action, resource)


async def _describe(sink, client, stores, rds, identifier: str | None = DB) -> Response:
    action, resource, body = _captured(sink, client, lambda c: (
        c.describe_db_instances(DBInstanceIdentifier=identifier) if identifier
        else c.describe_db_instances()
    ))
    return await _call(body, stores, rds, action, resource)


async def _await_status(sink, client, stores, rds, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        parsed = _parse("DescribeDBInstances", await _describe(sink, client, stores, rds))
        instance = parsed["DBInstances"][0]
        if instance["DBInstanceStatus"] == status:
            return instance
        assert time.monotonic() < deadline, f"never reached {status}: {instance['DBInstanceStatus']}"
        time.sleep(0.02)


# --- create: the transitional answer + the real waiter ----------------------


async def test_create_returns_creating_immediately_and_boots_in_the_background(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    parsed = _parse("CreateDBInstance", await _create(sink, rds, stores, fake))
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

    available = await _await_status(sink, rds, stores, fake, "available")
    assert available["Endpoint"] == {"Address": "host.docker.internal", "Port": 54321, "HostedZoneId": "Z2R2ITUGPM61AM"}
    assert available["DbInstancePort"] == 54321
    assert fake.created == [(DB, USER, PASSWORD, "postgres")]


async def test_available_is_gated_on_a_real_pg_ready_probe_not_on_docker_run(tmp_path, sink, rds):
    """The health assertion moved, it wasn't dropped: a container that starts
    but never accepts connections is `failed` with the probe's OWN error, so
    the apply fails instead of reporting a database that doesn't work."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _READY["ok"] = False
    await _create(sink, rds, stores, fake)
    failed = await _await_status(sink, rds, stores, fake, "failed")
    assert failed["DBInstanceStatus"] == "failed"
    record = rdsctl.records(stores, ENV)[0]
    assert "connection refused" in record["status_reason"]


async def test_available_needs_two_consecutive_probes_so_a_restarting_postgres_isnt_ready(tmp_path, sink, rds):
    """The postgres image inits behind a TEMPORARY server, then shuts it down
    and starts the real one. A single lucky probe against that temp server would
    publish a DATABASE_URL that stops answering a moment later, so `available`
    requires two successes in a row."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _READY["ok"] = True
    _READY["flap"] = True  # ok, then not ok, then ok forever (see the fixture)
    await _create(sink, rds, stores, fake)
    available = await _await_status(sink, rds, stores, fake, "available")
    assert available["DBInstanceStatus"] == "available"
    # The flap means the FIRST success was discarded -- more than the two
    # minimum probes were needed to get a clean consecutive pair.
    assert _PROBES["n"] > rdsctl._CONSECUTIVE_PROBES


async def test_a_container_that_never_starts_lands_failed_with_the_real_reason(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds(fail_create=True)
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "failed")
    assert "docker run failed" in rdsctl.records(stores, ENV)[0]["status_reason"]


async def test_creating_the_same_identifier_twice_is_an_already_exists_error(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    duplicate = await _create(sink, rds, stores, fake)
    assert duplicate.status_code == 400
    parsed = _parse("CreateDBInstance", duplicate, error=True)
    assert parsed["Error"]["Code"] == "DBInstanceAlreadyExists"


# --- describe --------------------------------------------------------------


async def test_describe_an_unknown_instance_is_db_instance_not_found(tmp_path, sink, rds):
    """The exact wire code terraform-provider-aws's Read drops state on, and
    its delete waiter treats as "gone"."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    response = await _describe(sink, rds, stores, fake, identifier="nope")
    assert response.status_code == 404
    assert _parse("DescribeDBInstances", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


async def test_describe_without_an_identifier_lists_every_instance(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _create(sink, rds, stores, fake, identifier="other", tags=[{"Key": "odin:node", "Value": "other"}])
    parsed = _parse("DescribeDBInstances", await _describe(sink, rds, stores, fake, identifier=None))
    assert [i["DBInstanceIdentifier"] for i in parsed["DBInstances"]] == [DB, "other"]


async def test_zero_drift_fields_carry_the_provider_defaults_that_would_otherwise_flap(tmp_path, sink, rds):
    """Every `Optional`-without-`Computed` attribute in the provider's own
    aws_db_instance schema is emitted explicitly -- AutoMinorVersionUpgrade
    above all, whose provider DEFAULT is true (omitting it reads back false and
    drifts every plan)."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    instance = await _await_status(sink, rds, stores, fake, "available")
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


async def test_create_tags_round_trip_through_list_tags_and_the_describe_taglist(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake, tags=[{"Key": "odin:node", "Value": DB}, {"Key": "team", "Value": "core"}])
    action, resource, body = _captured(sink, rds, lambda c: c.list_tags_for_resource(
        ResourceName=rdsctl.db_arn(DB),
    ))
    assert action == "rds:ListTagsForResource"
    # The tag calls carry an ARN, reduced to the same bare identifier the
    # policy layer keys on.
    assert resource == DB
    parsed = _parse("ListTagsForResource", await _call(body, stores, fake, action, resource))
    assert {t["Key"]: t["Value"] for t in parsed["TagList"]} == {"odin:node": DB, "team": "core"}
    instance = _parse("DescribeDBInstances", await _describe(sink, rds, stores, fake))["DBInstances"][0]
    assert {t["Key"]: t["Value"] for t in instance["TagList"]} == {"odin:node": DB, "team": "core"}


async def test_add_and_remove_tags_mutate_the_shared_tag_store(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    action, resource, body = _captured(sink, rds, lambda c: c.add_tags_to_resource(
        ResourceName=rdsctl.db_arn(DB), Tags=[{"Key": "env", "Value": "dev"}],
    ))
    assert action == "rds:AddTagsToResource"
    assert await _call(body, stores, fake, action, resource).status_code == 200
    assert stores.tags.get(ENV, f"rds:{rdsctl.db_arn(DB)}") == {"odin:node": DB, "env": "dev"}

    action, resource, body = _captured(sink, rds, lambda c: c.remove_tags_from_resource(
        ResourceName=rdsctl.db_arn(DB), TagKeys=["env"],
    ))
    assert action == "rds:RemoveTagsFromResource"
    assert await _call(body, stores, fake, action, resource).status_code == 200
    assert stores.tags.get(ENV, f"rds:{rdsctl.db_arn(DB)}") == {"odin:node": DB}


async def test_tagging_an_unknown_instance_is_not_found(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    action, resource, body = _captured(sink, rds, lambda c: c.list_tags_for_resource(
        ResourceName=rdsctl.db_arn("ghost"),
    ))
    response = await _call(body, stores, fake, action, resource)
    assert _parse("ListTagsForResource", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


# --- modify ----------------------------------------------------------------


async def test_modify_records_metadata_changes_and_echoes_them_back(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier=DB, AllocatedStorage=50, DBInstanceClass="db.t3.small", ApplyImmediately=True,
    ))
    assert action == "rds:ModifyDBInstance"
    parsed = _parse("ModifyDBInstance", await _call(body, stores, fake, action, resource))
    assert parsed["DBInstance"]["AllocatedStorage"] == 50
    assert parsed["DBInstance"]["DBInstanceClass"] == "db.t3.small"
    # Metadata only -- the container is untouched (a documented limit, not a
    # silent resize).
    assert fake.created == [(DB, USER, PASSWORD, "postgres")]


async def test_modify_password_runs_a_real_alter_user(tmp_path, sink, rds):
    """The DATABASE_URL fact embeds this password, so storing a new one without
    applying it would publish a credential that doesn't work."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier=DB, MasterUserPassword="newpass456", ApplyImmediately=True,
    ))
    assert await _call(body, stores, fake, action, resource).status_code == 200
    assert fake.passwords == [(DB, "newpass456")]
    assert rdsctl.records(stores, ENV)[0]["master_password"] == "newpass456"


async def test_modify_password_failure_is_a_real_error_not_a_silent_drift(tmp_path, sink, rds):
    class RefusingRds(FakePostgresRds):
        def set_password(self, db_id, user, current, new):
            raise RuntimeError("connection refused")

    stores, fake = _stores(tmp_path), RefusingRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier=DB, MasterUserPassword="newpass456", ApplyImmediately=True,
    ))
    response = await _call(body, stores, fake, action, resource)
    assert _parse("ModifyDBInstance", response, error=True)["Error"]["Code"] == "InvalidDBInstanceState"
    assert rdsctl.records(stores, ENV)[0]["master_password"] == PASSWORD


async def test_modify_an_unknown_instance_is_not_found(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    action, resource, body = _captured(sink, rds, lambda c: c.modify_db_instance(
        DBInstanceIdentifier="ghost", AllocatedStorage=50,
    ))
    response = await _call(body, stores, fake, action, resource)
    assert _parse("ModifyDBInstance", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


# --- delete ----------------------------------------------------------------


async def _delete(sink, client, stores, rds, identifier: str = DB) -> Response:
    action, resource, body = _captured(sink, client, lambda c: c.delete_db_instance(
        DBInstanceIdentifier=identifier, SkipFinalSnapshot=True,
    ))
    assert action == "rds:DeleteDBInstance"
    return await _call(body, stores, rds, action, resource)


async def test_delete_reports_deleting_then_the_record_disappears(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    parsed = _parse("DeleteDBInstance", await _delete(sink, rds, stores, fake))
    assert parsed["DBInstance"]["DBInstanceStatus"] == "deleting"
    deadline = time.monotonic() + 5.0
    while rdsctl.records(stores, ENV):
        assert time.monotonic() < deadline, "the record never disappeared"
        time.sleep(0.02)
    assert fake.deleted == [DB]
    # The delete waiter's success condition: the instance is genuinely gone.
    assert await _describe(sink, rds, stores, fake).status_code == 404
    assert stores.tags.get(ENV, f"rds:{rdsctl.db_arn(DB)}") == {}


async def test_a_failed_container_delete_keeps_the_record_deleting_rather_than_lying(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FailingDelete()
    await _create(sink, rds, stores, FakePostgresRds())
    await _delete(sink, rds, stores, fake)
    deadline = time.monotonic() + 5.0
    while True:
        record = rdsctl.records(stores, ENV)[0]
        if record.get("status_reason"):
            break
        assert time.monotonic() < deadline, "the delete failure was never recorded"
        time.sleep(0.02)
    assert record["status"] == "deleting"
    assert "docker rm failed" in record["status_reason"]


async def test_deleting_an_unknown_instance_is_not_found(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    response = await _delete(sink, rds, stores, fake, identifier="ghost")
    assert _parse("DeleteDBInstance", response, error=True)["Error"]["Code"] == "DBInstanceNotFound"


# --- drift + converge ------------------------------------------------------


async def test_mark_instance_failed_then_converge_recreates_the_container(tmp_path, sink, rds):
    """The crash-recovery contract W2.2's `converge_*` pattern established, for
    rds: the reality sweep marks the record `failed` (honest, and what the
    canvas shows), and an Apply's converge is what actually brings the real
    Postgres back -- tofu's own plan is empty, since the config never changed."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")

    fake.up.discard(DB)  # the container is gone (docker kill / rm)
    rdsctl.mark_instance_failed(stores, ENV, DB, "container removed outside odin")
    failed = _parse("DescribeDBInstances", await _describe(sink, rds, stores, fake))["DBInstances"][0]
    assert failed["DBInstanceStatus"] == "failed"

    rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    recovered = await _await_status(sink, rds, stores, fake, "available")
    assert recovered["Endpoint"]["Port"] == 54321
    assert fake.created == [(DB, USER, PASSWORD, "postgres"), (DB, USER, PASSWORD, "postgres")]
    assert rdsctl.records(stores, ENV)[0]["status_reason"] is None


async def test_converge_leaves_available_and_creating_instances_alone(tmp_path, sink, rds):
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    assert fake.created == [(DB, USER, PASSWORD, "postgres")]


# --- W2.6: the database on the mesh, gated by its drawn security group ------
#
# These four replace the reconciler-side tests that proved the same behaviour
# before W2.7 moved rds onto Terraform: the SG now arrives as
# `vpc_security_group_ids` (the ec2 route) instead of being read off the canvas
# node, and the join happens here, where the container's lifecycle lives.


def _seed_sg(stores: SynthStores, group_id: str, port: int, source: dict) -> None:
    """One SG record shaped exactly as `ec2net.py` stores it once tofu has
    created the group -- with its firewall compiled by nebula's OWN compiler
    rather than hand-written, so this can't drift from what the SG model really
    writes (`ec2net._compiled_firewall`)."""
    firewall = sg_rules_to_firewall([{"IpProtocol": "tcp", "FromPort": port, "ToPort": port, **source}])
    stores.ec2net.set(ENV, f"sg:{group_id}", {
        "group_id": group_id, "group_name": group_id, "vpc_id": "vpc-1",
        "firewall": firewall.model_dump(),
    })


async def test_create_records_and_echoes_its_assigned_security_groups(tmp_path, sink, rds):
    """ZERO-DRIFT: the provider reads `vpc_security_group_ids` back out of
    DescribeDBInstances' `VpcSecurityGroups`, so groups that went in have to
    come back out -- otherwise an `aws_db_instance` with an SG plans a change
    on every single apply."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake, VpcSecurityGroupIds=["sg-db", "sg-ops"])
    instance = await _await_status(sink, rds, stores, fake, "available")
    assert instance["VpcSecurityGroups"] == [
        {"VpcSecurityGroupId": "sg-db", "Status": "active"},
        {"VpcSecurityGroupId": "sg-ops", "Status": "active"},
    ]


async def test_the_database_joins_the_mesh_behind_its_assigned_sgs_compiled_firewall(tmp_path, sink, rds):
    """The W2.6 payoff, for a TF-owned database: the SGs a canvas drew for it
    compile into the firewall its overlay membership is gated by -- the UNION
    of every assigned group, byte-identical to what an EC2 VM in those same
    groups gets (both read `ec2net.compiled_firewall`)."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _seed_sg(stores, "sg-db", 5432, {"UserIdGroupPairs": [{"GroupId": "sg-web"}]})
    _seed_sg(stores, "sg-ops", 22, {"IpRanges": [{"CidrIp": "10.0.0.0/8"}]})
    await _create(sink, rds, stores, fake, VpcSecurityGroupIds=["sg-db", "sg-ops"])
    await _await_status(sink, rds, stores, fake, "available")

    (db_id, firewall) = fake.joined[-1]
    assert db_id == DB
    assert firewall is not None, "a drawn db-sg must gate the database's overlay membership"
    assert FirewallRule(port="5432", proto="tcp", group="sg-web") in firewall.inbound
    assert FirewallRule(port="22", proto="tcp", cidr="10.0.0.0/8") in firewall.inbound


async def test_a_database_with_no_security_groups_joins_the_mesh_ungated(tmp_path, sink, rds):
    """No SG assigned -> `None` -> the sidecar's allow-all default. Joining the
    mesh must never silently become "deny everything" just because nothing was
    drawn -- that would break a canvas that worked yesterday."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    assert fake.joined == [(DB, None)]


async def test_the_gated_overlay_address_lands_on_the_record(tmp_path, sink, rds):
    """`overlay_ip` on the record is what `tf_status._db_facts` publishes
    `DATABASE_URL_MESH` from -- and it stays None for an env with no Nebula
    network (no VPC drawn), which is what keeps the mesh facts absent rather
    than empty."""
    stores, joined = _stores(tmp_path), FakePostgresRds(overlay="10.42.1.4")
    await _create(sink, rds, stores, joined)
    await _await_status(sink, rds, stores, joined, "available")
    assert rdsctl.records(stores, ENV)[0]["overlay_ip"] == "10.42.1.4"

    meshless_stores, meshless = _stores(tmp_path / "no-mesh"), FakePostgresRds()
    await _create(sink, rds, meshless_stores, meshless)
    await _await_status(sink, rds, meshless_stores, meshless, "available")
    assert rdsctl.records(meshless_stores, ENV)[0]["overlay_ip"] is None


async def test_ensure_db_mesh_repushes_an_edited_sgs_rules_and_ignores_failed_instances(tmp_path, sink, rds):
    """An SG edit reaches the gateway only through an Apply (security groups are
    TF-owned) and nebula reads its firewall only at startup, so an Apply is
    when the recompiled rules must be pushed -- `ensure_db_mesh`. A `failed`
    instance is skipped: `converge_db_instances` re-creates it, and that boot
    joins on its own."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    _seed_sg(stores, "sg-db", 5432, {"UserIdGroupPairs": [{"GroupId": "sg-web"}]})
    await _create(sink, rds, stores, fake, VpcSecurityGroupIds=["sg-db"])
    await _await_status(sink, rds, stores, fake, "available")

    _seed_sg(stores, "sg-db", 5432, {"UserIdGroupPairs": [{"GroupId": "sg-batch"}]})  # the canvas edited it
    rdsctl.ensure_db_mesh(stores, ENV, substrate=fake)
    assert fake.joined[-1][1].inbound == [FirewallRule(port="5432", proto="tcp", group="sg-batch")]

    rdsctl.mark_instance_failed(stores, ENV, DB, "container removed outside odin")
    before = len(fake.joined)
    rdsctl.ensure_db_mesh(stores, ENV, substrate=fake)
    assert len(fake.joined) == before


# --- dispatch --------------------------------------------------------------


def test_an_unmodeled_action_is_a_protocol_correct_invalid_action(tmp_path):
    stores = _stores(tmp_path)
    response = rdsctl.pure_answer(
        "rds:CreateDBSnapshot", DB, ENV, b"Action=CreateDBSnapshot", stores, 0.0, rds=FakePostgresRds(),
    )
    assert response.status_code == 400
    parsed = _parse("CreateDBInstance", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidAction"


# --- the post-apply verification (`ecsctl.wait_for_steady_services`' twin) ---


async def test_wait_for_available_instances_reports_a_database_that_never_came_back(tmp_path, sink, rds):
    """THE hole this closes: `converge_db_instances` starts a re-create and
    returns, so /apply-full scored `applied` the instant the thread was spawned
    even when Postgres never accepted a connection. The wait JOINS that thread
    and reports what really happened -- naming the instance and the LAST REAL
    probe failure, never an invented one."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    fake.up.discard(DB)
    rdsctl.mark_instance_failed(stores, ENV, DB, "container removed outside odin")
    _READY["ok"] = False  # ...and this time Postgres never answers

    booting = rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    faults = rdsctl.wait_for_available_instances(stores, ENV, booting)

    assert faults == [rdsctl.DatabaseFault(
        node=DB, status="failed",
        reason="Postgres never became ready: connection refused",
    )], faults


async def test_wait_for_available_instances_names_the_docker_failure_when_that_is_the_reason(tmp_path, sink, rds):
    """The other real reason, and the higher-quality one: the container never
    started at all, so the apply names the `docker` error itself.

    The `RuntimeError: ` prefix is `errors.exc_text`, which this writer now
    shares with ec2compute/lambdactl/ecsctl instead of spelling `str(exc)`
    itself -- so a no-message exception can no longer persist `container did
    not start: ` as an instance's whole explanation. See
    tests/gateway/test_empty_reasons.py."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    rdsctl.mark_instance_failed(stores, ENV, DB, "container removed outside odin")
    fake.fail_create = True

    booting = rdsctl.converge_db_instances(stores, ENV, substrate=fake)
    faults = rdsctl.wait_for_available_instances(stores, ENV, booting)

    assert faults == [rdsctl.DatabaseFault(
        node=DB, status="failed", reason="container did not start: RuntimeError: docker run failed",
    )], faults


async def test_wait_for_available_instances_is_one_store_read_when_everything_is_available(tmp_path, sink, rds):
    """The happy path may not slow down: nothing is `creating`, so the wait
    returns on its first pass without sleeping or polling once."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")

    started = time.monotonic()
    faults = rdsctl.wait_for_available_instances(stores, ENV, [])
    elapsed = time.monotonic() - started

    assert faults == []
    assert elapsed < 0.1, f"a healthy env cost {elapsed:.3f}s -- it must cost one store read"


async def test_wait_for_available_instances_waits_for_a_database_that_is_still_creating(tmp_path, sink, rds):
    """The trap the ECS version avoids, for rds: a database that is merely
    STILL BOOTING must never fail an apply -- a FRESH one legitimately takes
    time. The wait blocks on `creating` rather than judging it at that
    instant."""
    stores, fake = _stores(tmp_path), SlowStart()
    await _create(sink, rds, stores, fake)
    # The create thread is genuinely in flight: `_wait_available` polls until
    # the substrate says the container publishes a port.
    assert rdsctl.records(stores, ENV)[0]["status"] == "creating"

    threading.Timer(0.05, lambda: fake.up.add(DB)).start()
    faults = rdsctl.wait_for_available_instances(stores, ENV, [], timeout=5.0)

    assert faults == [], "a database that was still coming up must not fail the apply"
    assert rdsctl.records(stores, ENV)[0]["status"] == "available"


def test_available_timeout_defaults_to_outlasting_the_boot_it_verifies(monkeypatch):
    """The budget must be LONGER than `_finish_create`'s own `_CREATE_TIMEOUT`,
    or the verification would hard-stop while the thread it is verifying is
    still probing and report `creating` instead of the real reason. And it is
    deliberately far longer than ECS's 60s: a database is slower to become
    ready than a container."""
    monkeypatch.delenv("ODIN_RDS_AVAILABLE_TIMEOUT", raising=False)
    monkeypatch.setattr(rdsctl, "_CREATE_TIMEOUT", 180.0)
    assert rdsctl.available_timeout() > 180.0

    monkeypatch.setenv("ODIN_RDS_AVAILABLE_TIMEOUT", "7")
    assert rdsctl.available_timeout() == 7.0


# --- a request that named NO instance ---------------------------------------
#
# logsctl/secretsctl's defect in this module: only `CreateDBInstance` checked
# the identifier it had just read. Measured against the real handlers with
# `DBInstanceIdentifier` omitted, five ops answered
#
#   404 DBInstanceNotFound "DBInstance  not found."
#
# -- a sentence with a hole in it (note the double space) that reads as though
# odin looked something up and came back empty-handed, when the truth is that
# it was handed no name to look up. A real boto3 client refuses to send one
# (`modify_db_instance()` -> `ParamValidationError: Missing required parameter
# in input: "DBInstanceIdentifier"`), so this is the raw-client path -- the
# same finding, and the same conclusion, ecsctl's `_missing_parameter` records.


@pytest.mark.parametrize("op,body,expected", [
    ("ModifyDBInstance", b"Action=ModifyDBInstance", "DBInstanceIdentifier"),
    ("DeleteDBInstance", b"Action=DeleteDBInstance", "DBInstanceIdentifier"),
    ("CreateDBInstance", b"Action=CreateDBInstance&Engine=postgres", "DBInstanceIdentifier"),
    ("ModifyDBInstance", b"Action=ModifyDBInstance&DBInstanceIdentifier=", "DBInstanceIdentifier"),
    ("ModifyDBInstance", b"Action=ModifyDBInstance&DBInstanceIdentifier=%20", "DBInstanceIdentifier"),
    ("ListTagsForResource", b"Action=ListTagsForResource", "ResourceName"),
    ("AddTagsToResource", b"Action=AddTagsToResource", "ResourceName"),
    ("RemoveTagsFromResource", b"Action=RemoveTagsFromResource", "ResourceName"),
])
def test_a_request_that_named_no_db_instance_says_so_instead_of_blaming_the_name(
    op, body, expected, tmp_path,
):
    stores = _stores(tmp_path)
    response = rdsctl.pure_answer(f"rds:{op}", "", ENV, body, stores, 0.0, rds=FakePostgresRds())
    text = response.body.decode()

    assert response.status_code == 400, text
    assert "<Code>InvalidParameterValue</Code>" in text
    assert f"<Message>{expected} is required</Message>" in text
    assert "DBInstance  not found" not in text, "a message with a hole in it is the bug"
    assert stores.rdsctl.items(ENV) == {}


async def test_an_identifier_less_describe_is_still_a_legitimate_list(tmp_path, sink, rds):
    """The gate must not turn the LIST call into an error -- terraform's own
    refresh drives an unfiltered DescribeDBInstances (and botocore marks
    nothing required on it)."""
    stores, fake = _stores(tmp_path), FakePostgresRds()
    await _create(sink, rds, stores, fake)
    await _await_status(sink, rds, stores, fake, "available")
    listed = _parse("DescribeDBInstances", await _describe(sink, rds, stores, fake, identifier=None))
    assert [i["DBInstanceIdentifier"] for i in listed["DBInstances"]] == [DB]


def test_a_named_instance_that_is_missing_still_gets_the_real_not_found_code(tmp_path):
    """`DBInstanceNotFound` is load-bearing twice over (the provider's Read
    drops the resource from state on it, and its delete waiter treats it as
    gone), so the gate must not swallow the genuine case."""
    stores = _stores(tmp_path)
    response = rdsctl.pure_answer(
        "rds:DeleteDBInstance", "nope", ENV, b"Action=DeleteDBInstance&DBInstanceIdentifier=nope",
        stores, 0.0, rds=FakePostgresRds(),
    )
    assert response.status_code == 404
    assert "<Code>DBInstanceNotFound</Code>" in response.body.decode()
    assert "DBInstance nope not found." in response.body.decode()
