"""The gateway's RDS model (task W2.7): `aws_db_instance` as a REAL Postgres
container.

Until W2.7 `rds` was the last drawable kind outside Terraform -- the
reconciler created its Postgres container directly, so the database was
invisible to `tofu plan`, to `tofu destroy`, and to `import-tf`, and odin's
"the canvas compiles to Terraform" promise had one silent exception. This
module closes it: the substrate is the SAME `aws/rds.py::PostgresRds` (same
`odin-rds-{env}-{id}` container, same `postgres:16-alpine` image, same
ephemeral host port), only the thing that DRIVES it changed, from the
reconciler's plan/execute loop to tofu's own CreateDBInstance call.

Closest analogue: `ec2compute.py`. Both model a substrate that takes real
wall-clock time to come up, and both do it the same way -- the create handler
mints the record in a TRANSITIONAL state (`creating`, ec2's `pending`) and
returns immediately, while a daemon thread finishes the real work. The
provider's own create waiter (`DBInstanceAvailable`: poll DescribeDBInstances
until `DBInstanceStatus == "available"`, botocore's own waiter model) is
built for exactly this latency, so no timing hack is needed anywhere.

`available` is gated on a REAL health assertion, not on `docker run`
returning: `_finish_create` waits for a published host port and then for
`reconcile.assertions.pg_ready_sync` to succeed twice in a row (a genuine
connection + `SELECT 1`; see `_wait_available` for why once isn't enough
against the postgres image's own init-then-restart dance). This is the same
probe that used to gate the reconciler's `healthy` phase, moved rather than
dropped -- and it now means a Postgres that boots but never accepts
connections FAILS THE APPLY (bounded by `_CREATE_TIMEOUT`) instead of being
reported as up.

Wire shape: RDS is botocore's `query` protocol (verified against botocore's
own `rds` service model: protocol `query`, xmlNamespace
`http://rds.amazonaws.com/doc/2014-10-31/`), i.e. form-encoded requests and
`<{Action}Response><{Action}Result>...` responses with a `resultWrapper` --
NOT the EC2 protocol's flatter envelope. Errors ride the same query envelope
SNS/IAM use (`gateway/errors.py`). Every response in
tests/gateway/test_rdsctl.py round-trips through botocore's own `QueryParser`,
the method V1a established.

MODELED SURFACE (what terraform-provider-aws drives for `aws_db_instance`):
CreateDBInstance / DescribeDBInstances / DeleteDBInstance / ModifyDBInstance,
plus tag CRUD (AddTagsToResource / ListTagsForResource /
RemoveTagsFromResource) so `tags` round-trip and apply -> plan is zero-drift.
Tags live in the shared `stores.tags` store keyed `"rds:{arn}"`, the same
convention ecr/ecsctl/logsctl already use.

ZERO-DRIFT NOTES (the fields that are here only because omitting them drifts):
the DB-instance shape emits explicit values for every `Optional`-without-
`Computed` attribute in the provider's own schema -- `AutoMinorVersionUpgrade`
(the one whose provider DEFAULT IS TRUE, so omitting it reads back as `false`
and drifts on every plan), plus `MultiAZ` / `PubliclyAccessible` /
`StorageEncrypted` / `CopyTagsToSnapshot` / `DeletionProtection` /
`IAMDatabaseAuthenticationEnabled` / `PerformanceInsightsEnabled` /
`CustomerOwnedIpEnabled` / `DedicatedLogVolume` / `MonitoringInterval`. The
`Endpoint` is the REAL published host port (see `_endpoint_xml`), so
`address`/`port`/`endpoint` in state point at something that actually
answers.

DELIBERATE LIMITS, each honest rather than silently wrong:
- **Postgres only.** The substrate is a Postgres container, so `agent/hcl.py`
  routes any other `engine` to `unsupported` rather than emitting HCL this
  module would fulfil with the wrong database.
- **`AllocatedStorage`/`DBInstanceClass` are metadata.** A local container has
  the host's disk and no instance sizing; both round-trip faithfully (so plans
  stay clean and an import preserves them) but changing them resizes nothing.
- **`MasterUserPassword` IS real.** ModifyDBInstance runs an actual
  `ALTER USER` (`PostgresRds.set_password`), because the DATABASE_URL fact
  odin publishes embeds that password -- storing a new one without applying it
  would make the fact a lie.
- **No snapshots.** `skip_final_snapshot` is meaningless locally and
  `FinalDBSnapshotIdentifier` is accepted-and-ignored; there is no
  DescribeDBSnapshots/CreateDBSnapshot surface at all.
- **The master password is stored** in `.odin/{env}/gateway/rdsctl.json`
  (mode 0600, like every other synth sidecar). It has to be: it's what the
  `DATABASE_URL` World fact is built from and what the drift sweep's health
  probe authenticates with. The Stack revision on disk already holds the same
  cleartext value, so this adds no new exposure class -- see SECURITY.md.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.aws.rds import POSTGRES_MAJOR, PostgresRds
from odin.fabric.models import FirewallRules
from odin.fabric.nebula import union_firewalls
from odin.gateway import errors
from odin.gateway.models import ec2net
from odin.gateway.models.ec2compute import membership_revision
from odin.gateway.stores import NO_CHANGE, SynthStores
from odin.reconcile.assertions import pg_ready_sync
from odin.runtime.colima import CONTAINER_HOST, ColimaRuntime

log = logging.getLogger("odin.gateway.rdsctl")

_RDS_NS = "http://rds.amazonaws.com/doc/2014-10-31/"
_REQUEST_ID = "00000000-0000-0000-0000-000000000000"

# How long `_finish_create` waits for the container to publish a port AND for
# Postgres to accept a real connection before giving up and reporting
# `failed`. Bounded so "never comes up" is a fast honest apply failure rather
# than the provider's own 40-minute default create wait (the same reasoning
# behind `_ECS_CONVERGE_TIMEOUT` in agent/hcl.py).
_CREATE_TIMEOUT = 180.0
_POLL_INTERVAL = 0.5
# Consecutive successful `pg_ready` probes required before reporting
# `available` -- see `_wait_available` for the postgres-entrypoint restart this
# exists to straddle.
_CONSECUTIVE_PROBES = 2

# The lifecycle states this module ever writes. `creating` -> `available` is
# what the provider's create waiter absorbs; `deleting` is what its delete
# waiter sees before the record disappears; `failed` is a boot that never
# passed `pg_ready` and the state the reality sweep corrects a vanished
# container to (all four are REAL DBInstanceStatus values).
AVAILABLE = "available"
CREATING = "creating"
DELETING = "deleting"
FAILED = "failed"

# Substrate facts echoed on the wire as the values they really are. Postgres
# runs on 5432 inside the container; the ADDRESS/PORT a caller gets is the
# host-published pair (see `_endpoint_xml`).
_LICENSE_MODEL = "postgresql-license"
_STORAGE_TYPE = "gp2"
_DEFAULT_INSTANCE_CLASS = "db.t3.micro"
_DEFAULT_ALLOCATED_STORAGE = 20
_DEFAULT_USERNAME = "app"
_DEFAULT_DB_NAME = "postgres"
# Real AWS's own default parameter/option group names for this engine major --
# the provider reads `parameter_group_name`/`option_group_name` from these
# (both Optional+Computed, so any consistent value plans clean).
_PARAMETER_GROUP = f"default.postgres{POSTGRES_MAJOR}"
_OPTION_GROUP = f"default:postgres-{POSTGRES_MAJOR}"
_CA_CERT = "rds-ca-rsa2048-g1"


def db_arn(identifier: str) -> str:
    return f"arn:aws:rds:{REGION}:{ACCOUNT}:db:{identifier}"


def _identifier_from_arn(value: str) -> str:
    """The bare instance identifier out of a `ResourceName` ARN (or the input
    unchanged when it isn't one) -- the same tolerance logsctl's
    `_group_from_arn` keeps for its own two ARN forms."""
    _prefix, sep, name = value.rpartition(":db:")
    return name if sep else value


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _key(identifier: str) -> str:
    return f"db:{identifier}"


def _record(stores: SynthStores, env: str, identifier: str) -> dict | None:
    return stores.rdsctl.get(env, _key(identifier))


def records(stores: SynthStores, env: str) -> list[dict]:
    """Every DB-instance record in `env` -- the public read the World
    projection (`reconcile/tf_status.py`) and the reality sweep
    (`reconcile/drift.py`) share, so neither has to know the store's key
    convention."""
    return [v for k, v in stores.rdsctl.items(env).items() if k.startswith("db:")]


# --- request parsing: the query protocol's form encoding -------------------


def _params(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _indexed(params: dict[str, str], prefix: str) -> list[dict[str, str]]:
    grouped: dict[int, dict[str, str]] = {}
    for key, value in params.items():
        if not key.startswith(f"{prefix}."):
            continue
        index, _, rest = key[len(prefix) + 1:].partition(".")
        if index.isdigit():
            grouped.setdefault(int(index), {})[rest] = value
    return [grouped[i] for i in sorted(grouped)]


def _request_tags(params: dict[str, str]) -> dict[str, str]:
    """`Tags.Tag.N.Key`/`.Value` -- RDS's `TagList` gives its member the
    locationName `Tag` (botocore's own model), so this is the list's real wire
    prefix, not the generic `.member.`."""
    return {t["Key"]: t.get("Value", "") for t in _indexed(params, "Tags.Tag") if "Key" in t}


def _vpc_sg_ids(params: dict[str, str]) -> list[str]:
    """`VpcSecurityGroupIds.VpcSecurityGroupId.N` -- `VpcSecurityGroupIdList`'s
    member carries the locationName `VpcSecurityGroupId` (botocore's own rds
    model), so that, not the generic `.member.`, is its wire prefix."""
    ids = _indexed(params, "VpcSecurityGroupIds.VpcSecurityGroupId")
    return [item[""] for item in ids if item.get("")]


def _tag_keys(params: dict[str, str]) -> list[str]:
    """`TagKeys.member.N` -- `KeyList`'s member has NO locationName, so it
    serializes with the query protocol's default `member` (again, botocore's
    model)."""
    return [item[""] for item in _indexed(params, "TagKeys.member") if "" in item]


def _int_param(params: dict[str, str], name: str, default: int) -> int:
    value = params.get(name, "")
    return int(value) if value.isdigit() else default


# --- wire building ---------------------------------------------------------


def _response(action: str, inner: str) -> Response:
    """A query-protocol response: the `{Action}Result` wrapper botocore's
    `QueryParser` looks for, plus the ResponseMetadata real RDS always
    sends."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{action}Response xmlns="{_RDS_NS}">'
        f"<{action}Result>{inner}</{action}Result>"
        f"<ResponseMetadata><RequestId>{_REQUEST_ID}</RequestId></ResponseMetadata>"
        f"</{action}Response>"
    )
    return Response(xml, media_type="text/xml")


def _empty_response(action: str) -> Response:
    """For the two tag mutations, whose botocore output shape is empty -- they
    carry NO result wrapper at all (AddTagsToResource/RemoveTagsFromResource
    have no `output` in the model)."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{action}Response xmlns="{_RDS_NS}">'
        f"<ResponseMetadata><RequestId>{_REQUEST_ID}</RequestId></ResponseMetadata>"
        f"</{action}Response>"
    )
    return Response(xml, media_type="text/xml")


def _not_found(identifier: str) -> Response:
    """`DBInstanceNotFound` is the REAL wire code, and load-bearing twice
    over: terraform-provider-aws's Read drops the resource from state on it
    (so a re-Apply plans a create), and its delete waiter treats it as
    "gone"."""
    return errors.synth_error("rds", "DBInstanceNotFound", f"DBInstance {identifier} not found.", 404)


def _tags_xml(tags: dict[str, str]) -> str:
    items = "".join(
        f"<Tag><Key>{escape(k)}</Key><Value>{escape(v)}</Value></Tag>" for k, v in tags.items()
    )
    return f"<TagList>{items}</TagList>"


def _endpoint_xml(record: dict) -> str:
    """The REAL, reachable endpoint: `host.docker.internal` (a container
    consumer's route to the Mac, the same address the DATABASE_URL fact has
    always used) plus the container's ACTUAL published host port. Omitted
    entirely while the instance is still `creating` -- real RDS does the same,
    and it's what keeps a mid-boot describe from advertising port 0."""
    port = record.get("endpoint_port")
    if not port:
        return ""
    return (
        "<Endpoint>"
        f"<Address>{escape(record['endpoint_address'])}</Address>"
        f"<Port>{port}</Port>"
        f"<HostedZoneId>{escape(record['hosted_zone_id'])}</HostedZoneId>"
        "</Endpoint>"
    )


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _vpc_sg_xml(record: dict) -> str:
    """The instance's assigned security groups, in real RDS's own response
    shape (`VpcSecurityGroupMembershipList`, whose member locationName is
    `VpcSecurityGroupMembership` -- botocore's model).

    ZERO-DRIFT, and the reason this isn't the empty `<VpcSecurityGroups/>` it
    used to be: terraform-provider-aws reads `vpc_security_group_ids` back out
    of this element, so an `aws_db_instance` that HAS groups would otherwise
    read back with none and plan a change on every apply. `active` is the only
    status real RDS ever reports here."""
    items = "".join(
        f"<VpcSecurityGroupMembership><VpcSecurityGroupId>{escape(gid)}</VpcSecurityGroupId>"
        "<Status>active</Status></VpcSecurityGroupMembership>"
        for gid in record.get("vpc_security_group_ids") or []
    )
    return f"<VpcSecurityGroups>{items}</VpcSecurityGroups>"


def _db_instance_xml(record: dict, tags: dict[str, str]) -> str:
    identifier = record["db_instance_identifier"]
    port = record.get("endpoint_port") or 0
    return "".join([
        f"<DBInstanceIdentifier>{escape(identifier)}</DBInstanceIdentifier>",
        f"<DBInstanceClass>{escape(record['db_instance_class'])}</DBInstanceClass>",
        f"<Engine>{escape(record['engine'])}</Engine>",
        f"<DBInstanceStatus>{record['status']}</DBInstanceStatus>",
        f"<MasterUsername>{escape(record['master_username'])}</MasterUsername>",
        f"<DBName>{escape(record['db_name'])}</DBName>",
        _endpoint_xml(record),
        f"<AllocatedStorage>{record['allocated_storage']}</AllocatedStorage>",
        f"<InstanceCreateTime>{record['instance_create_time']}</InstanceCreateTime>",
        f"<PreferredBackupWindow>{record['preferred_backup_window']}</PreferredBackupWindow>",
        f"<BackupRetentionPeriod>{record['backup_retention_period']}</BackupRetentionPeriod>",
        "<DBSecurityGroups/>",
        _vpc_sg_xml(record),
        "<DBParameterGroups><DBParameterGroup>"
        f"<DBParameterGroupName>{_PARAMETER_GROUP}</DBParameterGroupName>"
        "<ParameterApplyStatus>in-sync</ParameterApplyStatus>"
        "</DBParameterGroup></DBParameterGroups>",
        f"<AvailabilityZone>{record['availability_zone']}</AvailabilityZone>",
        f"<PreferredMaintenanceWindow>{record['preferred_maintenance_window']}</PreferredMaintenanceWindow>",
        "<PendingModifiedValues/>",
        f"<MultiAZ>{_bool(False)}</MultiAZ>",
        f"<EngineVersion>{escape(record['engine_version'])}</EngineVersion>",
        # The ONE Optional-without-Computed attribute whose provider default is
        # TRUE -- see the module docstring's zero-drift note.
        f"<AutoMinorVersionUpgrade>{_bool(True)}</AutoMinorVersionUpgrade>",
        f"<LicenseModel>{_LICENSE_MODEL}</LicenseModel>",
        "<OptionGroupMemberships><OptionGroupMembership>"
        f"<OptionGroupName>{_OPTION_GROUP}</OptionGroupName><Status>in-sync</Status>"
        "</OptionGroupMembership></OptionGroupMemberships>",
        f"<PubliclyAccessible>{_bool(False)}</PubliclyAccessible>",
        f"<StorageType>{_STORAGE_TYPE}</StorageType>",
        f"<DbInstancePort>{port}</DbInstancePort>",
        f"<StorageEncrypted>{_bool(False)}</StorageEncrypted>",
        f"<DbiResourceId>{record['dbi_resource_id']}</DbiResourceId>",
        f"<CACertificateIdentifier>{_CA_CERT}</CACertificateIdentifier>",
        f"<CopyTagsToSnapshot>{_bool(False)}</CopyTagsToSnapshot>",
        f"<MonitoringInterval>{0}</MonitoringInterval>",
        f"<DBInstanceArn>{db_arn(identifier)}</DBInstanceArn>",
        f"<IAMDatabaseAuthenticationEnabled>{_bool(False)}</IAMDatabaseAuthenticationEnabled>",
        f"<PerformanceInsightsEnabled>{_bool(False)}</PerformanceInsightsEnabled>",
        f"<DeletionProtection>{_bool(False)}</DeletionProtection>",
        f"<CustomerOwnedIpEnabled>{_bool(False)}</CustomerOwnedIpEnabled>",
        "<NetworkType>IPV4</NetworkType>",
        "<BackupTarget>region</BackupTarget>",
        f"<DedicatedLogVolume>{_bool(False)}</DedicatedLogVolume>",
        _tags_xml(tags),
    ])


def _tags_for(stores: SynthStores, env: str, identifier: str) -> dict[str, str]:
    return stores.tags.get(env, f"rds:{db_arn(identifier)}", {})


def _set_tags(stores: SynthStores, env: str, identifier: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"rds:{db_arn(identifier)}", tags)


def _instance_response(action: str, stores: SynthStores, env: str, record: dict) -> Response:
    identifier = record["db_instance_identifier"]
    inner = f"<DBInstance>{_db_instance_xml(record, _tags_for(stores, env, identifier))}</DBInstance>"
    return _response(action, inner)


# --- the record ------------------------------------------------------------


def _update(stores: SynthStores, env: str, identifier: str, **fields: object) -> None:
    """Field-wise update through the store's own mutate seam, so the create
    waiter's background thread and a concurrent Describe/Delete can't clobber
    each other's writes. A record that's already GONE (deleted while a boot
    was still in flight) is left gone -- never resurrected, the same guard
    `ec2compute._update_instance` keeps for its own terminal states."""

    def mutate(current: dict | None) -> dict | object:
        if current is None:
            return NO_CHANGE
        if current["status"] == DELETING and fields.get("status") not in (None, DELETING):
            # A slow boot finishing as `available` after a delete already won.
            return NO_CHANGE
        return {**current, **fields}

    stores.rdsctl.update(env, _key(identifier), mutate)


def _spawn(target: Callable[..., None], *args: object) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _substrate(env: str, stores: SynthStores, rds: PostgresRds | None) -> PostgresRds:
    """`ecsctl`'s `runtime or TaskRuntime()` precedent, env-scoped: the real
    substrate is built per call from the env in the request itself, so nothing
    has to be threaded down from `create_app` for production to work. Tests
    (and `create_app`'s own `rds=` seam) inject a stand-in with the same
    `create_db`/`delete_db`/`endpoint`/`container_name` shape.

    `stores.root` is the store root, and it is what the substrate's mesh
    sidecar needs (W2.6): the env's Nebula CA and overlay assignments live at
    `{stores.root}/{env}/nebula/`, written by `ec2net.py`'s own
    `ensure_network`. Passing it here is what makes the join work for a store
    that isn't the default `.odin` (every integration test's own root)."""
    return rds or PostgresRds(ColimaRuntime(), env, root=stores.root)


def _db_firewall(stores: SynthStores, env: str, group_ids: list[str]) -> FirewallRules | None:
    """The compiled Nebula firewall an rds instance's Postgres container is
    gated by: the UNION of its ASSIGNED security groups' rules (AWS's
    permissive-only SG semantics). `ec2compute._instance_firewall`'s twin,
    reading the very same `ec2net.compiled_firewall` bytes -- so a database and
    an EC2 VM in one security group are gated by identical rules.

    None means "not gated" (nebula's allow-all default): the node named no
    security group, or none of the groups it named has a compiled firewall
    yet. Deliberately NOT deny-everything -- an rds node that worked before
    anyone drew an SG has to keep working. And deliberately no VPC-default
    fallback, unlike ec2's: RunInstances with no groups really does inherit the
    VPC default in AWS, whereas CreateDBInstance's own default is the DB subnet
    group's VPC default, which odin doesn't model -- so inventing one here
    would gate a database by rules its canvas never showed."""
    compiled = [
        f for gid in group_ids
        if (f := ec2net.compiled_firewall(stores, env, gid)) is not None
    ]
    return union_firewalls(compiled) if compiled else None


def _join_mesh(stores: SynthStores, env: str, identifier: str, rds: PostgresRds) -> None:
    """Put this instance's real Postgres container on the env's Nebula overlay
    behind its assigned SGs' compiled firewall, and record the overlay IP so
    `reconcile/tf_status.py::_db_facts` can publish `DATABASE_URL_MESH`.

    W2.6 put this in the reconciler's rds observe pass; W2.7 retired that pass
    entirely, so it lives HERE now -- with the model that owns the container's
    lifecycle. The group ids are read from the record rather than passed in, so
    a re-ensure always compiles the CURRENT assignment.

    Strictly additive, and never able to fail an apply: this runs AFTER the
    instance is already `available`, and `MeshSidecar.ensure` swallows its own
    failures (returning None) rather than raising, so a database whose mesh
    wiring didn't come up is still a working database on its published host
    port."""
    record = _record(stores, env, identifier)
    if record is None:
        return
    firewall = _db_firewall(stores, env, record.get("vpc_security_group_ids") or [])
    # Field test 4: the database is the ADMITTING member, so it is the one that
    # must re-check flows it ALREADY admitted when a client's group is revoked.
    # `membership_revision` is what makes its reload count -- see
    # `fabric/nebula.py::FIREWALL_REVISION_KEY`.
    revision = membership_revision(stores, env)
    _update(stores, env, identifier, overlay_ip=rds.join_mesh(identifier, firewall, revision))


def _wait_available(rds: PostgresRds, identifier: str, user: str, password: str, deadline: float) -> tuple[int, str | None]:
    """Poll until the container publishes a host port AND `pg_ready_sync`
    succeeds TWICE IN A ROW against it. Returns `(port, error)` -- `error` is
    None on success, otherwise the LAST real failure text (never an invented
    one), so `failed`'s reason says what actually went wrong.

    Two consecutive successes, not one, because of how the postgres image
    actually boots (observed against the real container): the entrypoint runs
    initdb behind a TEMPORARY server, applies `POSTGRES_DB`/`POSTGRES_USER`,
    then SHUTS THAT SERVER DOWN and starts the real one. A single probe can
    land on the temporary server and report ready a moment before the
    restart -- so `available` would briefly name a database that is about to
    stop answering, and any consumer handed the DATABASE_URL fact in that
    window would fail. `_CONSECUTIVE_PROBES` samples spread over
    `_POLL_INTERVAL` straddle the restart instead of landing inside it."""
    error = "timed out waiting for a published port"
    streak = 0
    while time.monotonic() < deadline:
        endpoint = rds.endpoint(identifier)
        if endpoint is not None:
            probe = pg_ready_sync(endpoint[0], endpoint[1], user, password)
            streak = streak + 1 if probe.ok else 0
            if streak >= _CONSECUTIVE_PROBES:
                return endpoint[1], None
            if not probe.ok:
                error = probe.error or "pg_ready failed"
        time.sleep(_POLL_INTERVAL)
    return 0, error


def _finish_create(stores: SynthStores, env: str, identifier: str, user: str, password: str, db_name: str, rds: PostgresRds) -> None:
    # Deliberately broad, for `ec2compute._finish_boot`'s exact reason: this
    # runs on a daemon thread with no caller to propagate to, and an uncaught
    # exception would strand the instance `creating` forever -- the provider's
    # create waiter would then spin until ITS timeout with no explanation.
    # Any failure becomes a real, provider-visible `failed` status instead.
    try:
        rds.create_db(identifier, user, password, db_name)
    except Exception as exc:
        log.warning("rds create failed for %s (env %s): %s", identifier, env, exc)
        _update(stores, env, identifier, status=FAILED, status_reason=f"container did not start: {exc}")
        return
    port, error = _wait_available(rds, identifier, user, password, time.monotonic() + _CREATE_TIMEOUT)
    if error is not None:
        log.warning("rds %s (env %s) never became ready: %s", identifier, env, error)
        _update(stores, env, identifier, status=FAILED, status_reason=f"Postgres never became ready: {error}")
        return
    _update(
        stores, env, identifier, status=AVAILABLE, status_reason=None,
        endpoint_address=CONTAINER_HOST, endpoint_port=port,
    )
    # W2.6, deliberately AFTER `available`: the database really is up on its
    # published host port at this point, so the provider's create waiter is
    # never held behind mesh wiring (which, on a machine that hasn't built the
    # nebula sidecar image yet, does real work). The gated overlay address
    # follows a moment later as an extra fact.
    _join_mesh(stores, env, identifier, rds)


def _finish_delete(stores: SynthStores, env: str, identifier: str, rds: PostgresRds) -> None:
    try:
        rds.delete_db(identifier)
    except Exception as exc:
        # Honesty over a clean-looking teardown (ec2compute's own VM-delete
        # rule): the record STAYS `deleting` with the failure recorded, so a
        # caller polling DescribeDBInstances is told the truth and the next
        # Apply's delete tries again -- never a record claiming the container
        # is gone when it might not be.
        log.error("rds container delete failed for %s (env %s): %s", identifier, env, exc)
        _update(stores, env, identifier, status_reason=f"container delete failed: {exc}")
        return
    stores.rdsctl.delete(env, _key(identifier))
    _set_tags(stores, env, identifier, {})


# --- handlers --------------------------------------------------------------


def _create_db_instance(params: dict[str, str], env: str, stores: SynthStores, now: float, rds: PostgresRds) -> Response:
    identifier = params.get("DBInstanceIdentifier", "")
    if not identifier:
        return errors.synth_error("rds", "InvalidParameterValue", "DBInstanceIdentifier is required", 400)
    if _record(stores, env, identifier) is not None:
        return errors.synth_error(
            "rds", "DBInstanceAlreadyExists", f"DB instance already exists: {identifier}", 400,
        )
    user = params.get("MasterUsername") or _DEFAULT_USERNAME
    password = params.get("MasterUserPassword") or ""
    record = {
        "db_instance_identifier": identifier,
        "db_instance_class": params.get("DBInstanceClass") or _DEFAULT_INSTANCE_CLASS,
        "engine": params.get("Engine") or "postgres",
        "engine_version": params.get("EngineVersion") or POSTGRES_MAJOR,
        "status": CREATING,
        "status_reason": None,
        "master_username": user,
        # Stored because the DATABASE_URL World fact and the drift sweep's
        # health probe both need it -- see the module docstring's note.
        "master_password": password,
        "db_name": params.get("DBName") or _DEFAULT_DB_NAME,
        # W2.6: the drawn security groups, arriving the same way an EC2
        # instance's do -- through terraform (`vpc_security_group_ids`). They
        # are echoed back for zero-drift (`_vpc_sg_xml`) and they gate the real
        # container's overlay membership (`_db_firewall`).
        "vpc_security_group_ids": _vpc_sg_ids(params),
        # Filled in by `_join_mesh` once the container is on the env's overlay;
        # stays None for an env with no Nebula network (no VPC drawn).
        "overlay_ip": None,
        "allocated_storage": _int_param(params, "AllocatedStorage", _DEFAULT_ALLOCATED_STORAGE),
        "backup_retention_period": _int_param(params, "BackupRetentionPeriod", 0),
        "preferred_backup_window": "04:00-04:30",
        "preferred_maintenance_window": "sun:05:00-sun:05:30",
        "availability_zone": f"{REGION}a",
        "instance_create_time": _now_iso(),
        "dbi_resource_id": f"db-{secrets.token_hex(13).upper()[:26]}",
        "hosted_zone_id": "Z2R2ITUGPM61AM",
        "endpoint_address": CONTAINER_HOST,
        "endpoint_port": 0,
    }
    stores.rdsctl.set(env, _key(identifier), record)
    _set_tags(stores, env, identifier, _request_tags(params))
    # Render the `creating` answer BEFORE spawning the boot (JsonStore hands
    # back the same dict it was given, so a fast fake substrate could
    # otherwise flip it to `available` mid-render -- ec2compute's own
    # documented race).
    response = _instance_response("CreateDBInstance", stores, env, record)
    _spawn(_finish_create, stores, env, identifier, user, password, record["db_name"], rds)
    return response


def _describe_db_instances(params: dict[str, str], env: str, stores: SynthStores, now: float, rds: PostgresRds) -> Response:
    identifier = params.get("DBInstanceIdentifier", "")
    if identifier:
        record = _record(stores, env, identifier)
        if record is None:
            return _not_found(identifier)
        selected = [record]
    else:
        selected = sorted(records(stores, env), key=lambda r: r["db_instance_identifier"])
    items = "".join(
        f"<DBInstance>{_db_instance_xml(r, _tags_for(stores, env, r['db_instance_identifier']))}</DBInstance>"
        for r in selected
    )
    return _response("DescribeDBInstances", f"<DBInstances>{items}</DBInstances>")


def _delete_db_instance(params: dict[str, str], env: str, stores: SynthStores, now: float, rds: PostgresRds) -> Response:
    identifier = params.get("DBInstanceIdentifier", "")
    record = _record(stores, env, identifier)
    if record is None:
        return _not_found(identifier)
    # SkipFinalSnapshot/FinalDBSnapshotIdentifier are accepted and ignored --
    # there are no snapshots to take locally (module docstring). Reported as
    # `deleting` immediately; the provider's delete waiter polls until the
    # record is gone (DBInstanceNotFound), which `_finish_delete` makes true.
    _update(stores, env, identifier, status=DELETING)
    deleting = {**record, "status": DELETING}
    response = _instance_response("DeleteDBInstance", stores, env, deleting)
    _spawn(_finish_delete, stores, env, identifier, rds)
    return response


def _modify_db_instance(params: dict[str, str], env: str, stores: SynthStores, now: float, rds: PostgresRds) -> Response:
    identifier = params.get("DBInstanceIdentifier", "")
    record = _record(stores, env, identifier)
    if record is None:
        return _not_found(identifier)
    password = params.get("MasterUserPassword") or ""
    if password and password != record["master_password"]:
        # A REAL `ALTER USER`, not a stored intention -- the DATABASE_URL fact
        # embeds this password (module docstring). A failure is reported as a
        # real RDS error so the apply fails instead of drifting.
        try:
            rds.set_password(identifier, record["master_username"], record["master_password"], password)
        except Exception as exc:
            return errors.synth_error(
                "rds", "InvalidDBInstanceState",
                f"could not change the master password on {identifier}: {exc}", 400,
            )
    changes: dict[str, object] = {"master_password": password or record["master_password"]}
    for param, field in (
        ("DBInstanceClass", "db_instance_class"),
        ("EngineVersion", "engine_version"),
        ("PreferredBackupWindow", "preferred_backup_window"),
        ("PreferredMaintenanceWindow", "preferred_maintenance_window"),
    ):
        if params.get(param):
            changes[field] = params[param]
    if params.get("AllocatedStorage", "").isdigit():
        changes["allocated_storage"] = int(params["AllocatedStorage"])
    if params.get("BackupRetentionPeriod", "").isdigit():
        changes["backup_retention_period"] = int(params["BackupRetentionPeriod"])
    # Only when the provider actually sent groups -- the query protocol can't
    # express an empty list, so "no VpcSecurityGroupIds in this request" means
    # "unchanged", exactly as it does for every scalar above (and as real RDS
    # treats it). The recompiled firewall reaches the container on the next
    # `ensure_db_mesh`.
    if sg_ids := _vpc_sg_ids(params):
        changes["vpc_security_group_ids"] = sg_ids
    _update(stores, env, identifier, **changes)
    return _instance_response("ModifyDBInstance", stores, env, {**record, **changes})


def _tagged(params: dict[str, str], env: str, stores: SynthStores) -> tuple[str, dict | None]:
    identifier = _identifier_from_arn(params.get("ResourceName", ""))
    return identifier, _record(stores, env, identifier)


def _list_tags_for_resource(params: dict[str, str], env: str, stores: SynthStores, now: float, rds: PostgresRds) -> Response:
    identifier, record = _tagged(params, env, stores)
    if record is None:
        return _not_found(identifier)
    return _response("ListTagsForResource", _tags_xml(_tags_for(stores, env, identifier)))


def _add_tags_to_resource(params: dict[str, str], env: str, stores: SynthStores, now: float, rds: PostgresRds) -> Response:
    identifier, record = _tagged(params, env, stores)
    if record is None:
        return _not_found(identifier)
    _set_tags(stores, env, identifier, {**_tags_for(stores, env, identifier), **_request_tags(params)})
    return _empty_response("AddTagsToResource")


def _remove_tags_from_resource(params: dict[str, str], env: str, stores: SynthStores, now: float, rds: PostgresRds) -> Response:
    identifier, record = _tagged(params, env, stores)
    if record is None:
        return _not_found(identifier)
    remove = set(_tag_keys(params))
    kept = {k: v for k, v in _tags_for(stores, env, identifier).items() if k not in remove}
    _set_tags(stores, env, identifier, kept)
    return _empty_response("RemoveTagsFromResource")


# --- drift + recovery seams (W2.2's shape, for this kind) -------------------


def mark_instance_failed(stores: SynthStores, env: str, identifier: str, reason: str) -> None:
    """Public seam for the reality sweep (`reconcile/drift.py`): this
    instance's Postgres container is GONE (killed or removed outside odin), so
    the record says `failed` with `reason`.

    `failed` is a REAL DBInstanceStatus, and the honest one. It is deliberately
    NOT `deleted`/absent: an `aws_db_instance`'s CONFIG hasn't changed, and the
    provider's schema exposes `status` as read-only Computed (verified against
    the v5.100.0 provider schema), so tofu's plan is empty either way and
    dropping the record would only make odin forget a node the user still has
    on the canvas. `converge_db_instances` below is what makes the
    "re-Apply to recreate" verdict TRUE -- exactly the shape
    `lambdactl.mark_function_failed`/`converge_functions` established for the
    same reason."""
    _update(stores, env, identifier, status=FAILED, status_reason=reason)


def converge_db_instances(
    stores: SynthStores, env: str, substrate: PostgresRds | None = None,
) -> None:
    """Re-create the REAL Postgres container of every `failed` instance -- the
    same `_finish_create` pass CreateDBInstance spawns, driven by an APPLY
    (server.py's /apply-full) rather than by an AWS mutation.
    `lambdactl.converge_functions`' twin: a database container is odin's
    EXECUTION SUBSTRATE for a resource whose terraform config is unchanged, so
    tofu can never be the fixer here; real RDS's own control plane replaces
    failed storage without terraform's involvement either.

    Idempotent, and never two boots at once: the record is claimed back to
    `creating` in the store BEFORE the thread is spawned, so a second Apply
    arriving mid-boot sees `creating` and skips (`ec2compute._claim_delete_retry`'s
    claim-then-act shape). `available`/`creating`/`deleting` records are left
    completely alone.

    `substrate` is per-ENV, so it's built here rather than passed in from the
    request path: one `PostgresRds` covers every record in this env."""
    rds = substrate or PostgresRds(ColimaRuntime(), env, root=stores.root)
    for record in records(stores, env):
        if record["status"] != FAILED:
            continue
        identifier = record["db_instance_identifier"]
        _update(stores, env, identifier, status=CREATING, endpoint_port=0)
        log.info("converging rds %s (env %s): re-creating its container", identifier, env)
        _spawn(
            _finish_create, stores, env, identifier,
            record["master_username"], record["master_password"], record["db_name"], rds,
        )


def ensure_db_mesh(
    stores: SynthStores, env: str, substrate: PostgresRds | None = None,
) -> None:
    """Re-ensure every `available` instance's Nebula mesh membership -- run on
    each Apply (server.py's /apply-full), beside `converge_db_instances`.

    Two things need it, and an Apply is the honest cadence for both. An SG
    EDIT: security groups are TF-owned, so a changed `db-sg` reaches the
    gateway ONLY through an apply, and nebula reads its firewall only at
    startup -- so this is exactly when the recompiled rules must be pushed into
    the sidecar. And a DEAD SIDECAR: the companion container can be killed
    while the database itself keeps running, which no create path would ever
    notice.

    Cheap and idempotent by construction (`MeshSidecar.ensure`): an unchanged
    firewall with a running sidecar is a couple of file reads plus one
    container-status call; a firewall-only change is a SIGHUP that drops no
    tunnel, and only a deeper change replaces the daemon. `failed` instances
    are `converge_db_instances`' business -- their re-create joins the mesh on
    its own.

    Runs AFTER `ec2compute.ensure_instance_mesh` (server.py), because a
    database is the ADMITTING member of the SG rules that matter and it has to
    see a revoked client's NEW certificate before it re-checks the flows it
    already granted -- field test 4, `membership_revision`."""
    rds = substrate or PostgresRds(ColimaRuntime(), env, root=stores.root)
    for record in records(stores, env):
        if record["status"] == AVAILABLE:
            _join_mesh(stores, env, record["db_instance_identifier"], rds)


# --- dispatch --------------------------------------------------------------

_Handler = Callable[[dict[str, str], str, SynthStores, float, PostgresRds], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateDBInstance": _create_db_instance,
    "DescribeDBInstances": _describe_db_instances,
    "DeleteDBInstance": _delete_db_instance,
    "ModifyDBInstance": _modify_db_instance,
    "ListTagsForResource": _list_tags_for_resource,
    "AddTagsToResource": _add_tags_to_resource,
    "RemoveTagsFromResource": _remove_tags_from_resource,
}


def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    rds: PostgresRds | None = None,
) -> Response:
    """The whole `rds:*` answer -- same no-backing contract as
    ec2net/iamctl/ecr/logsctl: an unmodeled action gets a protocol-correct
    error, never a 503 and never a silent forward. `rds` is the injectable
    substrate seam (see `_substrate`)."""
    op = action.removeprefix("rds:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("rds", "InvalidAction", f"The action {op} is not valid.", 400)
    return handler(_params(body), env, stores, now, _substrate(env, stores, rds))
