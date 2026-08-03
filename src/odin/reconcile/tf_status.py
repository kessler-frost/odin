"""Fix-wave 2b finding #1 -- a pure, read-only projection of the TF-owned
resource kinds (vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr/logs/secret/ssm/
elasticache/rds/alb/kms: the kinds
`agent/hcl.py` can build and only `tofu apply`/`tofu destroy` ever
creates/destroys -- s3/sqs/sns/dynamodb are excluded, those already get real
World entries via the reconciler's own PROVISIONED path in plan.py) from the
gateway's synth stores into `label -> (kind, phase, facts, verdict)`.

W2.7 added `rds`, one of the two projected kinds that carry real FACTS
rather than an empty dict (`alb`'s `ALB_ENDPOINT` is the other): an rds
node's `DATABASE_URL` /
`DATABASE_URL_VM` (see `_db_facts`) are what `fabric/` resolves every
`${{db.VAR}}` reference from, so they had to keep working byte-for-byte when
the database moved from the reconciler onto Terraform. Publishing them here,
off the gateway's own DB-instance record, is what makes that true.

Before this fix these kinds never entered World at all: the canvas showed
a permanently stale DRAFT badge even once tofu had a real VM/service/
function/role/repo/network up. `Reconciler.tick()` calls `project()` once
per tick (see reconcile/reconciler.py) and diffs the result against the
current World, emitting a WorldDelta per (label, kind, phase, verdict) and
pruning any label that's dropped out (the resource was destroyed -- by tofu,
never by this projection or the reconciler itself).

Observability v1 (w1): `verdict` carries WHY a TF-owned resource is
`crashed` -- ec2's real `StateReason`, lambda's real `StateReason`, ecs's
real `stoppedReason` + exit code -- the same fields the AWS API itself would
show, never invented text. Field test 3 added ONE non-`crashed` verdict, for
the same reason: an ecs service whose new deployment failed while the
PREVIOUS revision keeps serving (ecsctl's `_retire_stale`) is neither
`healthy` nor `crashed`, so it projects `error` and the verdict says which --
"N tasks serving the previous revision; deployment of <image> failed: <why>",
every part of it read from real task/taskdef records. `_ecs_services` also calls ecsctl's own
`sweep_tasks` once per projection: one of the TWO deliberate, idempotent
mutations this otherwise-pure module makes, syncing a service's task records against
their REAL container status (a task whose container already exited on its
own gets marked STOPPED with its real exit code + reason) so a crash-loop is
visible on the very next reconciler tick instead of only after some
unrelated `Describe*` call happens to run the sweep first. It never creates
or destroys anything TF-owned -- same non-negotiable as the rest of this
module.

Field test 5 gave lambda and rds their equivalent, `project()`'s own
`live_verdicts` overlay -- the two kinds that had none, so `/world` reported a
removed container `healthy` for the whole ~10-tick drift-sweep cadence. It is
the same one-bulk-`docker ps` read /apply-full makes, but READ-ONLY: it
overrides the phase (and withholds the facts) of a resource whose container
isn't running, and touches no record. Correcting one here would let the next
apply silently recreate a database nobody had been told was dead -- see
reconcile/drift.py's "WHY THE PROJECTION MAY NOT WRITE".

Label resolution is uniform across every kind: prefer the `odin:node` tag
`agent/hcl.py::_tags_block` stamps on every canvas-node-backed resource,
falling back to the resource's own AWS-native name field where one exists
(sg's GroupName, iam_role's RoleName, ecr's repositoryName, lambda's
FunctionName, ecs's serviceName, a log group's logGroupName, a secret's Name, an
SSM parameter's Name, elasticache's CacheClusterId -- all of which
already equal the canvas label by construction, per hcl.py's own builders and
`classify.py`'s LOGS note) -- vpc/subnet/ec2 have NO
such native field (real CreateVpc/CreateSubnet/RunInstances take no `Name`
argument), so the tag is their ONLY route back to a label; an untagged
vpc/subnet/ec2 (e.g. a resource applied before this feature existed) is
simply not projected yet, rather than guessing.
"""
from __future__ import annotations

import json
from pathlib import Path

from odin.aws.cache import container_name as cache_container_name
from odin.aws.rds import POSTGRES_PORT
from odin.aws.rds import container_name as db_container_name
from odin.compute.functions import container_name as function_container_name
from odin.compute.instances import HostsVerdict
from odin.compute.proxy import container_name as proxy_container_name
from odin.compute.tasks import TaskRuntime
from odin.fabric.nebula import NebulaManager
from odin.gateway.models import (
    cachectl,
    ecsctl,
    elbv2ctl,
    kmsctl,
    logsctl,
    rdsctl,
    route53ctl,
    ssmctl,
)
from odin.gateway.models.ecsctl import sweep_tasks, task_verdict
from odin.gateway.stores import SynthStores
from odin.reconcile import mesh_health
from odin.reconcile.drift import live_verdicts
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST
from odin.simulate.workspace import tf_dir
from odin.spec.models import ResourceObserved, World

# Deduplicated in v0.8.19. This literal had grown TWO overlapping lines --
# `"elasticache", "rds", "alb", "kms"` and `"elasticache", "rds", "alb", "ebs"`
# -- a merge artifact from the kms and ebs work landing in one release out of two
# worktrees, each adding its own kind to its own copy of the tail. A set literal
# makes that harmless to evaluate and invisible to every test, which is exactly
# why it survived: `test_tf_status.py` pins the resulting SET, and the set was
# right. Kept as one line per group so the next addition has nowhere to hide.
TF_OWNED_KINDS = frozenset({
    "vpc", "subnet", "sg", "ec2", "ecs", "lambda", "iam_role", "ecr", "logs", "secret", "ssm",
    "elasticache", "rds", "alb", "kms", "ebs", "route53",
})

# An EBS volume's own states (gateway/models/ec2compute.py's volume records)
# -> the World Phase enum. `available` is NOT crashed: a volume drawn with no
# attachment edge is a real, correctly-created free-standing disk, which is
# what AWS calls available too. `attaching`/`detaching` are `starting` because
# on this substrate they are a whole VM restart, so they are states a poller
# really sees rather than an instant.
_EBS_PHASE = {
    "available": "healthy", "in-use": "healthy",
    "attaching": "starting", "detaching": "starting",
}

# elbv2's own load-balancer state machine (gateway/models/elbv2ctl.py) -> the
# World Phase enum. `provisioning` is honest asynchrony (the real nginx
# container is coming up on a daemon thread); `failed` is the state a real
# `docker run` failure records, with the driver's own error as the verdict.
_ALB_PHASE = {"provisioning": "starting", "active": "healthy", "failed": "crashed"}

# EC2's real instance-state machine (gateway/models/ec2compute.py's own
# `_STATE_CODES` keys) -> the World Phase enum. `stopped` (an intentional
# Stop, the VM still exists) reads "crashed"; `shutting-down` stays visible
# (`starting`) because a delete can fail and the VM outlive it. `terminated`
# only ever REACHES this mapping when the record is `drifted` (the reality
# sweep found its VM deleted outside odin) -- every other terminated record is
# excluded from the projection entirely in `_ec2_instances` below, so this row
# never resurrects the phantom the v0.5.2 fix removed.
_EC2_PHASE = {
    "pending": "starting", "running": "healthy", "stopping": "starting",
    "stopped": "crashed", "shutting-down": "starting", "terminated": "crashed",
}

# Lambda's two independent state machines (lambdactl.py's module docstring)
# -- only `State` (Pending/Active/Failed) matters for World; `LastUpdateStatus`
# is a redeploy-in-progress concept World's Phase enum has no room for and a
# function stays "healthy" (its container is still serving the OLD code)
# through a redeploy regardless.
_LAMBDA_PHASE = {"Pending": "starting", "Active": "healthy", "Failed": "crashed"}

# RDS's own DBInstanceStatus values (gateway/models/rdsctl.py) -> Phase.
# `deleting` reads `starting` for the SAME reason ec2's `shutting-down` does:
# a delete can fail and the container outlive it, so the node stays visible
# until the record actually disappears (and the prune in reconciler.py clears
# it then).
_RDS_PHASE = {
    "creating": "starting", "available": "healthy",
    "deleting": "starting", "failed": "crashed",
}

# label -> (kind, phase, facts, verdict) -- verdict is populated only for a
# "crashed" phase, from whatever real reason the underlying model recorded.
#
# `facts` is `dict[str, str]`: every VALUE must be a string. That is enforced
# at the emit boundary (`Reconciler._assert_string_facts`, which carries the
# argument) because these dicts round-trip through `world.json` on every tick,
# and a value that doesn't survive that unchanged -- a tuple read back as a
# list -- compares unequal to itself forever and storms one delta per tick.
# Anything numeric gets `str()`d by its builder, as `cachectl.facts` does for
# `port`.
Projected = dict[str, tuple[str, str, dict, str | None]]


def _label(tags: dict[str, str], natural: str | None) -> str | None:
    return tags.get("odin:node") or natural


def _crash_verdict(reason: str | None, *, kind: str, identifier: str, status: str, container: str) -> str:
    """The verdict a `crashed` projection carries: the recorded reason, or --
    when the record kept none -- what odin DOES know, never nothing at all.

    THE HOLE THIS CLOSES. Four sites here read `(record.get("<x>_reason") or
    None)`, and that `or None` deliberately turned an empty reason into no
    verdict: a node went RED on the canvas with no explanation, while its
    kind, its identifier and its container name were all sitting right here.
    Measured against a real server before this existed -- four `crashed`
    resources, `"verdict": null` in `/world`, and `odin world` printing three
    columns of phase with not one word of why.

    IT IS REACHABLE, not theoretical. Three of the four writers store
    `str(exc)` verbatim -- `cachectl._finish_create`,
    `lambdactl._finish_deploy`, `elbv2ctl._converge_safely` -- and a real
    `StopIteration`, a cancelled `Future`, a bare `KeyError()`/`TimeoutError()`
    all stringify to `""` (probed, not assumed). Driving those three real
    failure paths with such an exception put `reason=''` on all three records.
    rds is the exception: both of ITS writers f-string-wrap the reason, so
    today only the `.get()` default (a record with no such key) can reach the
    fallback there -- it is included anyway, because the bug class here is a
    field DEFAULTING into a lie, and the fix is the shape, not the instance.

    Same rule the rest of the tree already keeps one layer down:
    `drift.py::_NO_PROBE_ERROR`, `mesh_health.py`'s `"<Type>, raised with no
    message"`, `server.py::_failure_body`'s `detail`. This is the projection's
    own.

    NO REMEDY IS PROMISED, deliberately: /apply-full converges lambda and rds
    (`converge_functions` / `converge_db_instances`) but NOT elasticache and
    NOT alb, so "re-Apply to recreate" would be false for half the kinds this
    serves. The container name is the honest handle instead -- it is what the
    user can read logs off, and what `odin logs --env <env> <label>` resolves
    to for every kind here except alb.
    """
    return reason or (
        f"odin's {kind} record for {identifier!r} is {status!r} and the failure was recorded with "
        f"NO message, so this is the whole of what odin knows about it: the container behind it is "
        f"{container}. Nothing here diagnosed the cause -- odin is reporting the gap rather than "
        f"inventing a reason."
    )


def _vpc_subnet_sg(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.ec2net.items(env).items():
        kind, _, resource_id = key.partition(":")
        if kind not in ("vpc", "subnet", "sg"):
            continue
        tags = stores.tags.get(env, f"ec2:{resource_id}", {})
        natural = record.get("group_name") if kind == "sg" else None
        label = _label(tags, natural)
        if label:
            out[label] = (kind, "healthy", {}, None)
    return out


def _iam_roles(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.iamctl.items(env).items():
        if not key.startswith("role:"):
            continue
        tags = stores.tags.get(env, f"iam:{record['arn']}", {})
        label = _label(tags, record["role_name"])
        if label:
            out[label] = ("iam_role", "healthy", {}, None)
    return out


def _ecr_repos(stores: SynthStores, env: str) -> Projected:
    """A repository, and the address a workload must name to run its image.

    THE THIRD KIND THAT PROJECTS FACTS (`rds` and `alb` are the others, for the
    same reason): the whole point of an ECR node is that something pulls from
    it, and `repositoryUri` carries a port odin publishes dynamically, so it
    cannot be reconstructed by anyone reading the canvas.

    It projected `{}` until v0.8.14, which made the address undiscoverable from
    the product entirely -- not in `/world`, not in `odin world`, not reachable
    by a `${{repo.REPOSITORY_URI}}` reference. A user was expected to type an
    image address the UI never showed them. Nothing here is secret: it is a
    loopback host:port, unlike `_secrets`/`_ssm_parameters`, which project no
    facts at all.
    """
    out: Projected = {}
    for key, record in stores.ecr.items(env).items():
        if not key.startswith("repo:"):
            continue
        tags = stores.tags.get(env, f"ecr:{record['repository_arn']}", {})
        label = _label(tags, record["repository_name"])
        uri = record.get("repository_uri")
        if label:
            out[label] = ("ecr", "healthy", {"REPOSITORY_URI": uri} if uri else {}, None)
    return out


def _log_groups(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.logsctl.items(env).items():
        if not key.startswith("group:"):
            continue
        # An `auto` group was created by SUBSTRATE log shipping (a lambda
        # invoke's `/aws/lambda/{fn}`, an ecs task sweep -- logsctl.py's
        # `ensure_group`), never by tofu from a canvas `logs` node. Projecting
        # it would strand a phantom World resource the canvas never drew and
        # nothing can ever prune: no Stack resource matches that label, so it
        # would sit in /world forever -- the same failure mode as a projected
        # `terminated` instance below. A real CreateLogGroup ADOPTS an auto
        # group and clears the flag (logsctl.py's deviation 2), so the moment
        # the canvas does own the group it starts projecting.
        if record.get("auto"):
            continue
        name = record["log_group_name"]
        tags = stores.tags.get(env, f"logs:{logsctl.group_arn(name)}", {})
        label = _label(tags, name)
        if label:
            out[label] = ("logs", "healthy", {}, None)
    return out


def _secrets(stores: SynthStores, env: str) -> Projected:
    """W2.4: a `secret` node exists once tofu's CreateSecret landed. NO FACTS
    ARE PROJECTED -- deliberately: `facts` ride the WorldDelta onto the
    event stream and into `.odin/{env}/world.json`, and a secret's value must
    never travel either. Existence + phase is the whole honest projection; the
    value leaves only through a `GetSecretValue` an IAM edge allowed."""
    out: Projected = {}
    for key, record in stores.secretsctl.items(env).items():
        if not key.startswith("secret:"):
            continue
        tags = stores.tags.get(env, f"secretsmanager:{record['arn']}", {})
        label = _label(tags, record["name"])
        if label:
            out[label] = ("secret", "healthy", {}, None)
    return out


def _ssm_parameters(stores: SynthStores, env: str) -> Projected:
    """W2.4, same no-facts rule as `_secrets` above (a SecureString parameter's
    value is a secret, and a String one is nobody's business either)."""
    out: Projected = {}
    for key, record in stores.ssmctl.items(env).items():
        if not key.startswith("param:"):
            continue
        tags = stores.tags.get(env, f"ssm:{ssmctl.canonical_name(record['name'])}", {})
        label = _label(tags, record["name"])
        if label:
            out[label] = ("ssm", "healthy", {}, None)
    return out


def _kms_keys(stores: SynthStores, env: str) -> Projected:
    """W2.9: a `kms` node exists once tofu's CreateKey landed. NO FACTS ARE
    PROJECTED, on `_secrets`' rule -- a key id is not itself a secret, but there
    is nothing here a consumer could use: odin publishes no
    `${{key.SOMETHING}}`, the key is named by the LABEL a canvas already knows,
    and the one thing a fact could carry that the canvas does not (the ARN) is
    reconstructible from the label by anyone who wants it. Existence + phase is
    the whole honest projection.

    THE `odin:node` TAG IS THE ONLY ROUTE BACK TO A LABEL HERE, with no
    AWS-native fallback -- the one thing that makes this unlike `_secrets` /
    `_ssm_parameters`, whose `Name` equals the label by construction. A key's
    `key_id` does NOT: `kmsctl` mints the env's default key as `odin-default`
    (`DEFAULT_KEY_ID`, created on first use to seal any secret no kms node was
    drawn for) and a uuid for any untagged CreateKey. Falling back to `key_id`
    would project `odin-default` as a World resource no canvas node matches, no
    Stack revision can prune and `plan()` would call "observed but no longer
    desired" on every tick -- the phantom `_log_groups` skips an `auto` group to
    avoid, and the same one the v0.5.2 terminated-instance fix removed. So an
    untagged key is simply not projected, exactly as an untagged vpc/subnet/ec2
    is not.

    No phase but `healthy`: a key is metadata plus 32 bytes on disk, with no
    container to be down and no state machine to fail. `ScheduleKeyDeletion`
    deletes the record outright (kmsctl's deviation 2 -- it is immediate), so
    the reconciler's own prune is what removes the node from World.
    """
    out: Projected = {}
    for key, record in stores.kmsctl.items(env).items():
        if not key.startswith("key:"):
            continue
        tags = stores.tags.get(env, f"kms:{record['arn']}", {})
        label = tags.get(kmsctl.NODE_TAG)
        if label:
            out[label] = ("kms", "healthy", {}, None)
    return out


# v0.8.19: the record types `route53ctl` mints for a zone at creation and that
# NOBODY drew. Real CreateHostedZone makes an SOA and an NS pair, which is why a
# fresh zone's `ResourceRecordSetCount` is 2 and not 0. They must not count as
# drawn records here, or every zone would look like it serves something.
_ROUTE53_AUTO_TYPES = frozenset({"SOA", "NS"})


def _route53_zones(stores: SynthStores, env: str) -> Projected:
    """A `route53` node exists once tofu's CreateHostedZone landed.

    THE LABEL COMES FROM THE `odin:node` TAG, like every other primary, and the
    first version of this function got that wrong in a way worth recording.

    A hosted zone's id IS its domain name (`route53ctl`'s deviation 1 -- the id
    is DERIVED from the name rather than minted, which is what lets `classify.py`
    recover the IAM resource from the path with no store access), and
    `agent/hcl.py::_route53` emits `name = <label>`. So `record["zone_id"]`
    EQUALS the canvas label for every zone odin's canvas authored, and reading it
    directly looked like a free simplification -- this docstring used to claim it
    needed no tag "by construction rather than by luck".

    That is true for the zones odin drew and false for every other one. A zone in
    a hand-written project, or any zone created without the tag, would then be
    projected as a World resource that no canvas node matches, no Stack revision
    can prune, and `plan()` would call "observed but no longer desired" on every
    tick -- the phantom `_kms_keys` refuses to create by declining to fall back
    to `key_id`, and `_log_groups` avoids by skipping an `auto` group. Caught by
    the gateway author reviewing this projector, not by me writing it.

    So: untagged zone, not projected -- exactly as an untagged vpc/subnet/ec2/kms
    is not.

    THE PHASE IS NOT ALWAYS `healthy`, and what decides it is OBSERVED, never
    inferred. odin resolves a name with a hosts entry, and which address works
    depends on who is asking: a container reaches an instance's `private_ip`, a
    VM cannot reach another VM's at all (stock Lima `vz` NATs each VM into its
    own address space -- 100% loss, before nebula is involved), so a VM is
    served the Nebula overlay address or nothing.

    THIS PROJECTOR USED TO WORK THAT OUT FOR ITSELF, and deleting that is the
    point of the current shape. It counted `stores.ec2compute` records, read the
    overlay assignments, and PREDICTED what a hosts push would do. Meanwhile
    `compute/instances.py::HostsVerdict` recorded what a push actually DID. Two
    sources for one fact is how they drift, and then whichever happens to win
    decides what the user is told -- exactly the defect the ECS work hit when
    `container_gone_reason` was extracted to unify two writers while their
    ARGUMENTS stayed divergent, so the race went on deciding the answer anyway.
    Extracting a shared helper would have repeated that. The inference is GONE:
    this reads `hosts_action`/`hosts_names` off the instance records the
    resolver writes, and if nothing wrote them there is nothing to report.

    That last clause is deliberate and is the honest failure mode. A zone whose
    instances carry no verdict reads `healthy` -- because "no VM reported a
    problem" is what the substrate is saying. It is NOT a claim that resolution
    was proven; that proof is `tests/test_compute/test_hosts_resolution_e2e.py`,
    which asks `getent hosts` inside a real container and a real VM.
    """
    out: Projected = {}
    unresolvable = sorted({
        name
        for record in stores.ec2compute.items(env).values()
        if record.get("hosts_action") and not HostsVerdict(
            vm=record.get("instance_id", ""),
            action=record["hosts_action"],
            names=tuple(record.get("hosts_names") or ()),
        ).healthy
        for name in (record.get("hosts_names") or ())
    })
    for key, record in stores.route53ctl.items(env).items():
        if not key.startswith("zone:"):
            continue
        zone_id = record.get("zone_id")
        label = stores.tags.get(env, f"{route53ctl.SERVICE}:{zone_id}", {}).get(route53ctl.NODE_TAG)
        if not label:
            continue
        # Only the names belonging to THIS zone: two zones in one env fail
        # independently, and telling a user their `internal.test` zone is broken
        # because `example.com` could not be pushed is a different lie. Keyed on
        # the ZONE ID, which is what a record's name is actually suffixed with --
        # the label is what the canvas calls the node, and the two are equal for
        # a canvas-authored zone but must not be assumed equal here.
        mine = [name for name in unresolvable if name.endswith(f".{zone_id}")]
        verdict = _hosts_verdict_for(stores, env, mine) if mine else None
        out[label] = ("route53", "crashed" if verdict else "healthy", {}, verdict)
    return out


def _hosts_verdict_for(stores: SynthStores, env: str, names: list[str]) -> str:
    """The REAL reason, taken from the resolver's own `HostsVerdict.reason`
    rather than re-worded here -- `compute/instances.py` owns that text, it is
    keyed off an outcome map with no optimistic default, and an unmapped action
    reports itself as the bug. Re-deriving the sentence in this file is the
    second-writer problem in prose."""
    reasons = sorted({
        HostsVerdict(
            vm=record.get("instance_id", ""),
            action=record["hosts_action"],
            names=tuple(record.get("hosts_names") or ()),
        ).reason
        for record in stores.ec2compute.items(env).values()
        if record.get("hosts_action")
        and any(name in names for name in (record.get("hosts_names") or ()))
    })
    return " | ".join(reason for reason in reasons if reason)


# The two rds facts that name the OVERLAY (SG-gated) address rather than the
# published host port -- withheld together the moment that path stops
# answering, so odin never hands out an endpoint it hasn't verified
# (reconcile/mesh_health.py).
_DB_MESH_KEYS = ("DATABASE_URL_MESH", "endpoint_mesh")


def _db_facts(record: dict) -> dict:
    """The rds facts the Fabric resolves `${{db.DATABASE_URL}}` from -- the
    EXACT four keys (and the exact two host forms) the reconciler's own
    `_observe_rds` published before W2.7, because existing canvases reference
    them by name:

      - `DATABASE_URL` / `endpoint` on `host.docker.internal` -- what a
        CONTAINER consumer needs (inside a container, "localhost" is the
        container, not the Mac; host.docker.internal is the host, same as AWS).
      - `DATABASE_URL_VM` / `endpoint_vm` on `host.lima.internal` -- v0.5.4
        finding #5: an EC2 Lima VM does NOT resolve host.docker.internal, so
        an ec2 node consumes `${{db.DATABASE_URL_VM}}` while containers keep
        `${{db.DATABASE_URL}}` (per-consumer-type ref routing is still
        deferred; two facts remain the honest, smaller answer).

    NEITHER of those two is gated by a security group -- they are the same raw
    published host port under two host aliases (ROADMAP's residual-gap
    paragraph, which field test 2 MEDIUM-5 rightly pointed out never said
    "...and that includes Lima VMs"). The mesh form below is the ONLY governed
    path, and the only one whose address survives a container recreation
    unchanged, so a VM consumer in an env WITH a mesh should be pointed at it.
    `_VM` stays published anyway: removing it would break existing canvases,
    and it is the right answer for an env with no VPC drawn.

    Both point at the SAME real Postgres, on the container's actually-published
    host port, with the instance's real master credentials and its real
    `db_name` (a `POSTGRES_DB` the substrate genuinely creates -- so the path
    in this URL is a database that exists, not a label).

    W2.6 adds a THIRD form, and only when the instance really is on the env's
    Nebula overlay (`rdsctl._join_mesh` recorded an `overlay_ip`; an env with
    no VPC drawn has no mesh, and then no mesh key is published at all rather
    than an empty placeholder):

      - `DATABASE_URL_MESH` / `endpoint_mesh` on the overlay IP and
        `POSTGRES_PORT` -- the address a drawn security group actually GATES.
        The port is the container's own 5432, not the published host port,
        because the mesh sidecar shares the container's network namespace.

    It rides ALONGSIDE the two host forms, never instead of them: the gateway's
    forwarding, the create waiter's probe, host-side clients and every existing
    `${{db.DATABASE_URL}}` reference all keep using the published port.
    """
    port = record.get("endpoint_port")
    if not port:
        return {}
    user, password, db = record["master_username"], record["master_password"], record["db_name"]
    addr, vm_addr = f"{CONTAINER_HOST}:{port}", f"{LIMA_HOST}:{port}"
    overlay_ip = record.get("overlay_ip")
    mesh_addr = f"{overlay_ip}:{POSTGRES_PORT}" if overlay_ip else None
    mesh = {
        "DATABASE_URL_MESH": f"postgresql://{user}:{password}@{mesh_addr}/{db}",
        "endpoint_mesh": mesh_addr,
    } if mesh_addr else {}
    return {
        "DATABASE_URL": f"postgresql://{user}:{password}@{addr}/{db}",
        "endpoint": addr,
        "DATABASE_URL_VM": f"postgresql://{user}:{password}@{vm_addr}/{db}",
        "endpoint_vm": vm_addr,
        **mesh,
    }


async def _db_instances(stores: SynthStores, env: str) -> Projected:
    """W2.7: rds joins the projection instead of being provisioned+observed by
    the reconciler. Facts are published ONLY for an `available` instance -- a
    `failed` one has no working endpoint, and advertising the last known
    DATABASE_URL for a dead database is exactly the kind of stale-green lie the
    reality sweep exists to kill. The verdict then carries WHY.

    `mesh_health.gate` extends that same rule to the ONE fact whose path no
    other probe in odin ever checks: `DATABASE_URL_MESH` names the SG-gated
    overlay address, and `pg_ready` only ever proves the published HOST port
    (field test 2 -- a database `healthy` for minutes on a mesh endpoint that
    had been dead since its container was recreated). It costs nothing for an
    instance with no mesh fact."""
    out: Projected = {}
    for record in rdsctl.records(stores, env):
        identifier = record["db_instance_identifier"]
        tags = stores.tags.get(env, f"rds:{rdsctl.db_arn(identifier)}", {})
        label = _label(tags, identifier)
        if not label:
            continue
        phase = _RDS_PHASE.get(record["status"], "starting")
        facts = _db_facts(record) if record["status"] == rdsctl.AVAILABLE else {}
        container = db_container_name(env, identifier)
        verdict = _crash_verdict(
            record.get("status_reason"), kind="rds", identifier=identifier,
            status=record["status"], container=container,
        ) if phase == "crashed" else None
        out[label] = await mesh_health.gate(
            ("rds", phase, facts, verdict), root=stores.root, env=env,
            member=container,  # PostgresRds.mesh_member == the container name
            overlay_ip=record.get("overlay_ip"), mesh_keys=_DB_MESH_KEYS,
            sidecar_target=container, sidecar_port=POSTGRES_PORT,
        )
    return out


def _load_balancers(stores: SynthStores, env: str) -> Projected:
    """W2.5: an `alb` node exists once tofu's CreateLoadBalancer landed, and is
    `healthy` once its REAL nginx proxy container is up (elbv2ctl flips the
    record to `active` from the thread that ran `docker run`, so this reads a
    measured state rather than asserting one).

    ONE OF THE TWO KINDS THAT PROJECT FACTS (`rds` is the other, see
    `_db_facts`): a load balancer's whole point is an
    address, and `DNSName` can't carry the dynamic host port odin publishes the
    proxy on (elbv2ctl.py's `_DNS_NAME` note). So the genuinely reachable
    `http://127.0.0.1:{port}` rides out as a fact -- onto the event stream, into
    `.odin/{env}/world.json`, and resolvable by another node's
    `${{lb.ALB_ENDPOINT}}` reference through the fabric. Nothing secret is in
    it (contrast `_secrets`/`_ssm_parameters`, which project no facts at all).
    """
    out: Projected = {}
    for key, record in stores.elbv2ctl.items(env).items():
        if not key.startswith("lb:"):
            continue
        tags = stores.tags.get(env, f"{elbv2ctl.SERVICE}:{record['arn']}", {})
        label = _label(tags, record["name"])
        if not label:
            continue
        phase = _ALB_PHASE.get(record["state"], "starting")
        endpoint = elbv2ctl.endpoint_url(record)
        facts = {"ALB_ENDPOINT": endpoint} if endpoint else {}
        verdict = _crash_verdict(
            record.get("state_reason"), kind="alb", identifier=record["name"],
            status=record["state"], container=proxy_container_name(env, record["name"]),
        ) if phase == "crashed" else None
        out[label] = ("alb", phase, facts, verdict)
    return out


def _ec2_verdict(record: dict) -> str | None:
    """`state_reason` is `{"code": ..., "message": ...} | None`
    (gateway/models/ec2compute.py) -- the real Server.* reason a boot/stop
    failure recorded, or None when nothing failed (a deliberate Stop via a
    healthy path carries no reason either -- honestly reported as no verdict,
    never an invented one)."""
    reason = record.get("state_reason")
    if not reason:
        return None
    return f"{reason['code']}: {reason['message']}"


# An ec2 node's facts (field test 2 LOW-13: it published NONE, so a VM's own
# addresses were only findable by hand-reading `.odin/<env>/nebula/
# overlay.json`, while rds published three). Both are plain addresses, no
# credentials, no ports invented:
#   PRIVATE_IP -- the VM's real private address, what DescribeInstances
#                 reports as privateIpAddress. Host-reachable, NOT SG-gated.
#   MESH_IP    -- its Nebula overlay address: sticky across recreation, and
#                 the ONE path a drawn security group actually gates. Withheld
#                 (like rds's `*_MESH`) when the env's lighthouse isn't up, so
#                 odin never hands out an address no peer can reach.
_EC2_MESH_KEYS = ("MESH_IP",)


def _ec2_facts(record: dict, overlay: dict[str, str]) -> dict:
    private_ip, overlay_ip = record.get("private_ip"), overlay.get(record["instance_id"])
    return {
        **({"PRIVATE_IP": private_ip} if private_ip else {}),
        **({"MESH_IP": overlay_ip} if overlay_ip else {}),
    }


def _overlay_assignments(stores: SynthStores, env: str) -> dict[str, str]:
    """host_id -> overlay IP for this env, read ONCE per projection (one small
    JSON read, and none at all for an env with no mesh). Read-only: no mkdir,
    no allocation -- `NebulaManager.load_overlay`'s own contract."""
    overlay = NebulaManager(Path(stores.root) / env / "nebula").load_overlay()
    hosts = overlay.subnets.get("hosts") if overlay else None
    return dict(hosts.assignments) if hosts else {}


async def _ec2_instances(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    overlay = _overlay_assignments(stores, env)
    instances = [r for k, r in stores.ec2compute.items(env).items() if k.startswith("instance:")]
    # Terminated records FIRST, so a live instance sharing their label
    # overwrites them: a re-Apply that recovers drift mints a NEW instance
    # while the drifted one is still inside its 60s lazy-sweep window
    # (ec2compute's `_sweep_terminated`), and the recovered node must never
    # read `crashed` off the corpse it replaced. `sorted` is stable, so
    # same-state records keep store order.
    for record in sorted(instances, key=lambda r: r["state_name"] != "terminated"):
        # Release sweep finding #2: a `terminated` instance is GONE (VM deleted
        # by tofu destroy / empty-canvas Apply / a boot failure). Exclude it so
        # the reconciler prunes it from World immediately (the ECS INACTIVE
        # precedent). This projection reads the store directly and never
        # triggers ec2compute's Describe-driven lazy sweep, so projecting a
        # terminated record would strand a phantom `crashed` node in /world
        # forever, breaking the "empty canvas + Apply => /world empty" promise.
        #
        # A `drifted` record is the ONE exception (W2.2 honesty fix): the
        # reality sweep marked it terminated because its VM was deleted
        # outside odin, and that terminated record is exactly what makes the
        # next Apply recreate the VM (ec2compute's `mark_instance_terminated`).
        # Dropping it here would trade one dishonesty for another -- odin
        # would quietly forget a node the user still has on the canvas instead
        # of showing WHY it's down -- so it projects `crashed` + the real
        # StateReason, and the recovery apply's own describes are what
        # eventually sweep the record away.
        if record["state_name"] == "terminated" and not record.get("drifted"):
            continue
        tags = stores.tags.get(env, f"ec2:{record['instance_id']}", {})
        label = tags.get("odin:node")  # no AWS-native name field to fall back to
        if not label:
            continue
        phase = _EC2_PHASE.get(record["state_name"], "starting")
        verdict = _ec2_verdict(record) if phase == "crashed" else None
        facts = _ec2_facts(record, overlay) if phase == "healthy" else {}
        out[label] = await mesh_health.gate(
            ("ec2", phase, facts, verdict), root=stores.root, env=env,
            member=record["instance_id"],  # the nebula host_id `InstanceVm` signs (compute/instances.py)
            overlay_ip=facts.get("MESH_IP"), mesh_keys=_EC2_MESH_KEYS,
        )
    return out


def _ebs_volumes(stores: SynthStores, env: str) -> Projected:
    """Every EBS volume the gateway holds, by its canvas label.

    The one non-obvious phase is `available` WITH a `last_error`, and it is the
    whole reason this projection is worth having. A volume in that state was
    asked to attach and did not: the disk is real and healthy, but the thing
    the user DREW -- a line from a volume to an instance -- is not in force.
    Reporting `healthy` there is the decorative-edge bug in status form, so it
    reads `crashed` and carries the real reason out as the verdict. A volume
    that was never asked to attach has no `last_error` and is honestly green.
    """
    out: Projected = {}
    # Keyed on the STORE KEY PREFIX, exactly as `_ec2_instances` above does,
    # and NOT on which fields a value happens to carry. A shape sniff was the
    # first thing written here and a mutation test killed it: it read
    # `"state" in record and "volume_id" in record`, and loosening either half
    # changed nothing, because no other family in this store carries a
    # `volume_id` today. A guard whose two halves are both currently redundant
    # is a guard nothing can prove -- `volume:` is the fact that actually
    # distinguishes these records, so that is what this reads.
    for key, record in stores.ec2compute.items(env).items():
        if not key.startswith("volume:"):
            continue
        label = stores.tags.get(env, f"ec2:{record['volume_id']}", {}).get("odin:node")
        if not label:
            continue
        error = record.get("last_error")
        phase = "crashed" if (record["state"] == "available" and error) else _EBS_PHASE.get(record["state"], "starting")
        facts = {
            "VOLUME_ID": record["volume_id"],
            "SIZE_GIB": str(record["size"]),
            # The name of the REAL artifact, so a user can go and look at it
            # with `limactl disk ls` rather than take odin's word for it.
            "LIMA_DISK": record.get("disk") or "",
        }
        out[label] = ("ebs", phase, facts, error if phase == "crashed" else None)
    return out


def _invocation_verdict(record: dict) -> str | None:
    """Field test 2 finding #4: a Lambda reported `healthy` while failing every
    single invocation (the canvas code defined `handler`, the entry point looked
    for `lambda_handler`, so every call raised `Runtime.HandlerNotFound`).

    The PHASE stays `healthy`, deliberately: the deploy genuinely succeeded and
    the function really is deployed and serving requests -- calling that
    `crashed` would be a different lie, and it would make an Apply look like it
    failed when it didn't. What was actually missing is that odin KNOWS the
    invocations failed (`lambdactl._invoke` records the FunctionError the RIE
    reported) and said nothing. So the invocation outcome rides out as the
    verdict, which is exactly the channel a phase-less truth belongs in: it
    reaches the event stream, `world.json`, `events.jsonl` and M8's evidence
    bundle, and `Reconciler._emit` already suppresses everything but a CHANGE,
    so a function failing in a loop costs one delta, not one per invocation.

    Only the LAST invocation's outcome, and no counters in the text: a
    function that starts working again clears the verdict on its own, and a
    cold function nobody has invoked yet (no key at all) says nothing rather
    than raising a false alarm."""
    error = record.get("last_invocation_error")
    if not error:
        return None
    return f"the last invocation failed ({error}) — the deploy succeeded, the handler did not"


def _lambda_functions(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.lambdactl.items(env).items():
        if not key.startswith("fn:"):
            continue
        tags = stores.tags.get(env, f"lambda:{record['function_arn']}", {})
        label = _label(tags, record["function_name"])
        if label:
            phase = _LAMBDA_PHASE.get(record["state"], "starting")
            verdict = (
                _crash_verdict(
                    record.get("state_reason"), kind="lambda", identifier=record["function_name"],
                    status=record["state"], container=function_container_name(env, record["function_name"]),
                ) if phase == "crashed"
                else _invocation_verdict(record)
            )
            out[label] = ("lambda", phase, {}, verdict)
    return out


# ElastiCache's cluster statuses (gateway/models/cachectl.py) -> World Phase.
# `deleting` maps to `starting` for the same reason ec2's `shutting-down` does:
# a delete can fail and the container outlive it, so the node stays visible
# until the record is actually gone (which is what prunes it from World).
# `create-failed` is odin's own status for "the Redis container never came up"
# -- see cachectl.py's docstring for why it isn't one of AWS's.
_CACHE_PHASE = {
    cachectl.STATUS_CREATING: "starting",
    cachectl.STATUS_AVAILABLE: "healthy",
    cachectl.STATUS_DELETING: "starting",
    cachectl.STATUS_CREATE_FAILED: "crashed",
}


def _cache_clusters(stores: SynthStores, env: str) -> Projected:
    """W2.8. Publishes real FACTS the way `_db_instances` does: an `available`
    cluster's `REDIS_URL`/`REDIS_URL_VM` endpoints, so a consumer's
    `${{cache.REDIS_URL}}` ref resolves through the Fabric off World exactly
    the way rds's `DATABASE_URL` does (`cachectl.facts`).

    "The way `_db_instances` does" now includes the GATE, which is the whole
    point of the pattern and was the one half missing (field test 5's facts
    audit). This projected in EVERY phase, so a `deleting` cluster kept
    advertising a live `REDIS_URL` -- precisely the stale-green lie
    `_db_instances`'s docstring says its gate exists to prevent, and which
    `_ec2_instances` gates for too. `cachectl.facts`'s own docstring already
    said "the facts an AVAILABLE cluster publishes"; only the call site
    disagreed.

    Gated on the record STATUS rather than the derived phase, following rds
    (they are equivalent today -- `available` is the only status
    `_CACHE_PHASE` maps to `healthy` -- and the status form fails SAFE if that
    ever stops being true: a new status publishes nothing until someone
    decides it should)."""
    out: Projected = {}
    for record in cachectl.clusters(stores, env):
        tags = stores.tags.get(env, f"elasticache:{record['arn']}", {})
        label = _label(tags, record["cache_cluster_id"])
        if not label:
            continue
        phase = _CACHE_PHASE.get(record["status"], "starting")
        facts = cachectl.facts(record) if record["status"] == cachectl.STATUS_AVAILABLE else {}
        verdict = _crash_verdict(
            record.get("status_reason"), kind="elasticache", identifier=record["cache_cluster_id"],
            status=record["status"], container=cache_container_name(env, record["cache_cluster_id"]),
        ) if phase == "crashed" else None
        out[label] = ("elasticache", phase, facts, verdict)
    return out


def _ecs_tasks_for(stores: SynthStores, env: str, cluster_name: str, service_name: str) -> list[dict]:
    prefix = f"task:{cluster_name}:"
    return [
        task for key, task in stores.ecsctl.items(env).items()
        if key.startswith(prefix) and task["service_name"] == service_name
    ]


def _serving_previous_verdict(stores: SynthStores, env: str, service: dict, previous: int, failed: dict) -> str:
    """The verdict for a service that a FAILED deployment left serving its
    PREVIOUS revision -- concrete about both halves, because either half alone
    misleads: "N tasks serving the previous revision" without the failure
    reads like a healthy service, and the failure without the N reads like an
    outage. Names the image the failed deployment asked for (the typo'd tag
    IS the diagnosis in the field-test case) plus the real stop reason."""
    image = ecsctl.service_image(stores, env, service)
    plural = "task" if previous == 1 else "tasks"
    target = f" of {image}" if image else ""
    return f"{previous} {plural} serving the previous revision; deployment{target} failed: {task_verdict(failed)}"


async def _ecs_services(stores: SynthStores, env: str, runtime: TaskRuntime | None = None) -> Projected:
    out: Projected = {}
    # Keep task state honest against real containers BEFORE reading it below
    # -- without this, a task whose container already exited on its own
    # keeps reading "RUNNING" from the store until some unrelated Describe*
    # call happens to sweep it, and a crash-looping service shows "starting"
    # forever (the exact bug this fix closes).
    await sweep_tasks(stores, env, runtime or TaskRuntime())
    for key, record in stores.ecsctl.items(env).items():
        # An INACTIVE service is mid-delete (ecsctl.py's own grace-window
        # sweep, `_INACTIVE_SERVICE_SWEEP_SECONDS`) -- World must drop it
        # immediately, not wait for that sweep to actually purge the record.
        if not key.startswith("service:") or record["status"] != "ACTIVE":
            continue
        label = record.get("node_label") or record["service_name"]
        tasks = _ecs_tasks_for(stores, env, record["cluster_name"], record["service_name"])
        # REVISION-AWARE (field test 3). `minimumHealthyPercent` now keeps the
        # previous revision serving through a failed deployment
        # (ecsctl.py's `_retire_stale`), which would read as plain `healthy`
        # under a revision-blind count -- "the service is fine" while the
        # deployment the operator just asked for is dead. So the projection
        # splits by revision using ecsctl's OWN rule: only tasks on the
        # revision the service currently points at count toward desired.
        current = ecsctl.on_current_revision(record, tasks)
        running = sum(1 for t in current if t["last_status"] == "RUNNING")
        previous = sum(1 for t in tasks if t["last_status"] == "RUNNING" and t not in current)
        # A STOPPED task record surviving in the store is ALWAYS a real
        # failure: a deliberate stop (scale-down / stale-taskdef replacement
        # / service delete) deletes the record outright (ecsctl.py's
        # `_stop_task`) rather than leaving it STOPPED -- so every STOPPED
        # record here came from either the lazy sweep catching a spontaneous
        # container exit, or a launch that failed outright.
        failed = [t for t in current if t["last_status"] == "STOPPED"]
        latest = max(failed, key=lambda t: t.get("stopped_at") or 0) if failed else None
        if running == record["desired_count"]:
            out[label] = ("ecs", "healthy", {}, None)

        elif latest is not None and previous:
            # NOT `healthy` (the requested revision is not running) and NOT
            # `crashed` (traffic is still being served) -- `error` is the
            # Phase that already means a terminal failure needing operator
            # action, which a deployment that will never converge on its own
            # is. The UI renders it red and surfaces the verdict on hover.
            out[label] = ("ecs", "error", {}, _serving_previous_verdict(stores, env, record, previous, latest))
        elif latest is not None:
            out[label] = ("ecs", "crashed", {}, task_verdict(latest))
        else:
            out[label] = ("ecs", "starting", {}, None)
    return out


async def project(
    stores: SynthStores, env: str, ecs_runtime: TaskRuntime | None = None, containers=None,
) -> Projected:
    """`label -> (kind, phase, facts, verdict)` for every currently-existing
    TF-owned resource in the env's synth stores -- a snapshot of what tofu has
    created, save for the two record syncs below. `ecs_runtime`/`containers` are
    injectable seams purely for tests; every real caller leaves them default (a
    real `TaskRuntime()` / `ColimaRuntime()`, matching ecsctl.py's own
    `runtime or TaskRuntime()` precedent).

    `live_verdicts` LAST, and it is the field-test-5 fix: a lambda or rds whose
    container is not running right now reads `crashed` with the real reason and
    NO FACTS, whatever its record says. Without it these two kinds were honest
    only on the drift sweep's ~10-tick cadence, so `/world` reported a
    `docker rm -f`'d function `healthy` -- and published a dead database's
    DATABASE_URL -- for that whole window. One bulk `docker ps` per projection
    (none at all for an env with no lambda/rds), off the SAME read /apply-full
    makes, so the projection and an apply cannot disagree about liveness.

    READ-ONLY, unlike `_ecs_services`' task sync: correcting an rds record here
    would let the very next apply silently delete and recreate a database
    nothing had reported dead yet (`reconcile/drift.py`'s own note). The
    projection reports; only an apply writes."""
    out: Projected = {}
    out.update(_vpc_subnet_sg(stores, env))
    out.update(_iam_roles(stores, env))
    out.update(_ecr_repos(stores, env))
    out.update(_log_groups(stores, env))
    out.update(_secrets(stores, env))
    out.update(_ssm_parameters(stores, env))
    out.update(_kms_keys(stores, env))
    out.update(await _db_instances(stores, env))
    out.update(_load_balancers(stores, env))
    out.update(await _ec2_instances(stores, env))
    out.update(_ebs_volumes(stores, env))
    out.update(_lambda_functions(stores, env))
    out.update(await _ecs_services(stores, env, ecs_runtime))
    out.update(_cache_clusters(stores, env))
    # Takes no overlay snapshot at all any more. It used to read
    # `_overlay_assignments` a second time to PREDICT whether a hosts push could
    # work; it now reads what the push RECORDED, so the mesh question is asked
    # once, by the component that acts on the answer.
    out.update(_route53_zones(stores, env))
    # The live container check, applied over whatever the records claimed. Facts
    # go with it: a database that isn't running must stop advertising a
    # DATABASE_URL nothing can connect to -- the stale-green fact `_db_facts`
    # exists to prevent, which a phase-only override would leave behind.
    out.update({
        label: (out[label][0], "crashed", {}, verdict)
        for label, verdict in (await live_verdicts(stores, env, containers)).items()
        if label in out
    })
    return out


# --- the resources a failed apply leaves in tofu's state and nowhere else ---

_TF_STATE = "terraform.tfstate"

# The AWS-shaped kinds that live INSIDE a shared per-env backing container
# (rustfs / goaws / dynalite) rather than getting a container of their own, and
# whose World entries therefore come from the reconciler's PROVISIONED observe
# path -- not from `project()` above. tofu resource type -> (odin kind, the
# attribute carrying the resource's own name, which equals the canvas label by
# construction: see agent/hcl.py's `_s3`/`_sqs`/`_sns`/`_dynamodb`).
_BACKED_TF_TYPES = {
    "aws_s3_bucket": ("s3", "bucket"),
    "aws_sqs_queue": ("sqs", "name"),
    "aws_sns_topic": ("sns", "name"),
    "aws_dynamodb_table": ("dynamodb", "name"),
}

# The DIAGNOSIS half is deliberately the same words `server.
# on_backing_unavailable` puts on the `backing_unavailable` event: one down
# backing, one vocabulary, whichever surface an operator is looking at. The
# ADVICE half is not shared any more, because the two are sent at different
# moments -- that event fires live, possibly from inside a tofu run that is
# 503-ing right now; this is a read-model overlay on `GET /world`, after the
# fact.
#
# WHY "Apply to start it" alone was not good enough, and what replaces it.
# The state this reports is USUALLY REACHED BY A FAILED APPLY (see
# `stranded_in_tf_state`), so the old sentence prescribed the very command
# that had just failed, with nothing to tell the user their retry had changed
# nothing. Both halves of the loop were measured against a real server on a
# real env:
#   * a SUCCESSFUL apply really does clear it -- `/apply` of a canvas with the
#     s3 node booted the backing and the resource read `healthy` on the same
#     tick, with the verdict gone (12 consecutive 1s samples).
#   * the moment the committed Stack stops naming the kind -- which is exactly
#     what a FAILED apply leaves, since `/apply-full` skips `store.apply(stack)`
#     on `tf_failed` -- the next tick's `BackingAws.gc` stops the backing and
#     this verdict is back. Measured at the first poll after the apply
#     returned, and held for all 12 samples.
# So a user who Applies, fails, and re-reads this has been told nothing new;
# the sentence now says so, and names the apply's own error as the thing to
# fix.
#
# `odin destroy` is deliberately NOT offered as the way out. Under exactly
# this condition it wedges: `/destroy` calls `ensure_backings(last_applied)`,
# the last applied Stack is the one WITHOUT these resources (that is why the
# backing is down), so it boots nothing and every AWS call tofu's destroy
# makes 503-retries with backoff -- `simulate/runner.py::_WEDGED_DESTROY_HINT`
# and `server.py::_BACKING_ADVICE` document that same trap for `odin tf plan`.
_STRANDED_VERDICT = (
    "this resource exists in the env's tofu state, but no {kind} backing container is running "
    "for this env -- every AWS call to it answers ServiceUnavailable. Apply again: an apply that "
    "SUCCEEDS starts the backing and this clears on the same tick. An apply that FAILS does not, "
    "and a failed apply is the usual way an env gets here -- it never commits the desired state, "
    "so the next reconciler tick stops the backing again and this exact message is back within "
    "about one tick (~1s at the default poll interval). If you are reading it a second time, "
    "re-applying is not what changes it: fix the error your last apply printed."
)


def _tf_state(root: Path, env: str) -> dict:
    """tofu's own state for this env, or `{}` when there is nothing to read.

    STRICT, in the same direction as `ec2compute.tf_forgotten_instances`: a
    state file that is missing, empty or unparseable is NO evidence, never an
    error. tofu rewrites state IN PLACE (open, truncate, write -- see
    util.ensure_private_file), so a reader that lands mid-write sees a
    truncated file; `/world` is polled throughout an apply and must not 500
    because it caught tofu at that instant.
    """
    state = tf_dir(root, env) / _TF_STATE
    text = state.read_text().strip() if state.is_file() else ""
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def stranded_in_tf_state(
    root: Path, env: str, world: World, reachable_kinds: frozenset[str] | set[str],
) -> tuple[ResourceObserved, ...]:
    """The resources tofu really created that odin can currently see NOWHERE
    else -- reported with an honest phase instead of vanishing.

    Field test 3, P2-5. A 22-node canvas whose ECS node never came up failed
    the apply, and a failed apply does not commit the desired state
    (`/apply-full`'s `tf_failed` branch, release finding #2). So the env's
    Stack stayed empty: plan() never provisioned the 12 s3/sqs/sns/dynamodb
    nodes, the trailing tick's `gc({})` stopped the very backing containers
    `ensure_backings` had just booted, and the nodes had **no badge at all** in
    `/world` -- not pending, not crashed, absent -- while tofu's state listed
    them and every call to them answered `ServiceUnavailable`. A resource that
    exists and is unreachable must not be invisible; that is "odin can't see
    itself" again.

    Three conditions, and all three matter:

    * IN TOFU'S STATE. tofu is the only thing that creates or destroys these,
      so its state is the witness that the resource EXISTS. A resource that
      genuinely no longer exists (tofu destroy, an empty-canvas Apply) leaves
      the state and stops being reported here the same instant -- this cannot
      strand a phantom the way projecting a `terminated` EC2 record did in
      v0.5.2, because nothing here outlives the state entry.
    * NOT IN WORLD. World always wins. The moment the reconciler observes the
      resource for real, this says nothing about it at all.
    * ITS BACKING IS UNREACHABLE. `reachable_kinds` is the gateway's OWN
      routing table (`GatewayState.backing_port`) -- the exact thing that
      decides between forwarding a call and answering `ServiceUnavailable`.
      So this reports only what the gateway would genuinely refuse, and stays
      silent through the ordinary window of a HEALTHY apply, where the backing
      is up from `ensure_backings` and the resource is simply not observed yet.

    Nothing here is written to World and no WorldDelta is emitted: it is a
    read-model overlay on `GET /world`, computed per request. That is what
    keeps it away from the draft-flap v0.7.1 killed -- plan() would call any
    such entry "observed but no longer desired" on the very next tick and
    prune it straight back out, one stream event per tick, forever.
    """
    out: list[ResourceObserved] = []
    for resource in _tf_state(root, env).get("resources", []):
        backed = _BACKED_TF_TYPES.get(resource.get("type"))
        if backed is None or backed[0] in reachable_kinds:
            continue
        kind, name_attr = backed
        for instance in resource.get("instances", []):
            attributes = instance.get("attributes") or {}
            label = _label(attributes.get("tags") or {}, attributes.get(name_attr))
            if label and world.get(label) is None:
                out.append(ResourceObserved(
                    id=label, kind=kind, phase="crashed", verdict=_STRANDED_VERDICT.format(kind=kind),
                ))
    return tuple(out)
