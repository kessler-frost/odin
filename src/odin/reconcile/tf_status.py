"""Fix-wave 2b finding #1 -- a pure, read-only projection of the TF-owned
resource kinds (vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr/elasticache: the kinds
`agent/hcl.py` can build and only `tofu apply`/`tofu destroy` ever
creates/destroys -- s3/sqs/sns/dynamodb are excluded, those already get real
World entries via the reconciler's own PROVISIONED path in plan.py) from the
gateway's synth stores into `label -> (kind, phase, facts, verdict)`.

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
show, never invented text. `_ecs_services` also calls ecsctl's own
`sweep_tasks` once per projection: the ONE deliberate, idempotent mutation
this otherwise-pure module makes, syncing a service's task records against
their REAL container status (a task whose container already exited on its
own gets marked STOPPED with its real exit code + reason) so a crash-loop is
visible on the very next reconciler tick instead of only after some
unrelated `Describe*` call happens to run the sweep first. It never creates
or destroys anything TF-owned -- same non-negotiable as the rest of this
module.

Label resolution is uniform across every kind: prefer the `odin:node` tag
`agent/hcl.py::_tags_block` stamps on every canvas-node-backed resource,
falling back to the resource's own AWS-native name field where one exists
(sg's GroupName, iam_role's RoleName, ecr's repositoryName, lambda's
FunctionName, ecs's serviceName, elasticache's CacheClusterId -- all of which
already equal the canvas label by construction, per hcl.py's own builders)
-- vpc/subnet/ec2 have NO
such native field (real CreateVpc/CreateSubnet/RunInstances take no `Name`
argument), so the tag is their ONLY route back to a label; an untagged
vpc/subnet/ec2 (e.g. a resource applied before this feature existed) is
simply not projected yet, rather than guessing.
"""
from __future__ import annotations

from odin.compute.tasks import TaskRuntime
from odin.gateway.models import cachectl
from odin.gateway.models.ecsctl import sweep_tasks
from odin.gateway.stores import SynthStores

TF_OWNED_KINDS = frozenset({"vpc", "subnet", "sg", "ec2", "ecs", "lambda", "iam_role", "ecr", "elasticache"})

# EC2's real instance-state machine (gateway/models/ec2compute.py's own
# `_STATE_CODES` keys) -> the World Phase enum. `terminated` is deliberately
# absent: a terminated instance is GONE (its Lima VM deleted) and is EXCLUDED
# from the projection in `_ec2_instances` below, not mapped to a phase -- see
# that function's own note (release sweep finding #2). `stopped` (an
# intentional Stop, the VM still exists) does read "crashed"; `shutting-down`
# stays visible (`starting`) because a delete can fail and the VM outlive it.
_EC2_PHASE = {
    "pending": "starting", "running": "healthy", "stopping": "starting",
    "stopped": "crashed", "shutting-down": "starting",
}

# Lambda's two independent state machines (lambdactl.py's module docstring)
# -- only `State` (Pending/Active/Failed) matters for World; `LastUpdateStatus`
# is a redeploy-in-progress concept World's Phase enum has no room for and a
# function stays "healthy" (its container is still serving the OLD code)
# through a redeploy regardless.
_LAMBDA_PHASE = {"Pending": "starting", "Active": "healthy", "Failed": "crashed"}

# label -> (kind, phase, facts, verdict) -- verdict is populated only for a
# "crashed" phase, from whatever real reason the underlying model recorded.
Projected = dict[str, tuple[str, str, dict, str | None]]


def _label(tags: dict[str, str], natural: str | None) -> str | None:
    return tags.get("odin:node") or natural


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
    out: Projected = {}
    for key, record in stores.ecr.items(env).items():
        if not key.startswith("repo:"):
            continue
        tags = stores.tags.get(env, f"ecr:{record['repository_arn']}", {})
        label = _label(tags, record["repository_name"])
        if label:
            out[label] = ("ecr", "healthy", {}, None)
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


def _ec2_instances(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.ec2compute.items(env).items():
        if not key.startswith("instance:"):
            continue
        # Release sweep finding #2: a `terminated` instance is GONE (VM deleted
        # by tofu destroy / empty-canvas Apply / a boot failure). Exclude it so
        # the reconciler prunes it from World immediately (the ECS INACTIVE
        # precedent). This projection reads the store directly and never
        # triggers ec2compute's Describe-driven lazy sweep, so projecting a
        # terminated record would strand a phantom `crashed` node in /world
        # forever, breaking the "empty canvas + Apply => /world empty" promise.
        if record["state_name"] == "terminated":
            continue
        tags = stores.tags.get(env, f"ec2:{record['instance_id']}", {})
        label = tags.get("odin:node")  # no AWS-native name field to fall back to
        if not label:
            continue
        phase = _EC2_PHASE.get(record["state_name"], "starting")
        verdict = _ec2_verdict(record) if phase == "crashed" else None
        out[label] = ("ec2", phase, {}, verdict)
    return out


def _lambda_functions(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.lambdactl.items(env).items():
        if not key.startswith("fn:"):
            continue
        tags = stores.tags.get(env, f"lambda:{record['function_arn']}", {})
        label = _label(tags, record["function_name"])
        if label:
            phase = _LAMBDA_PHASE.get(record["state"], "starting")
            verdict = (record.get("state_reason") or None) if phase == "crashed" else None
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
    """W2.8. The ONLY projection here that publishes real FACTS: an `available`
    cluster's `REDIS_URL`/`REDIS_URL_VM` endpoints, so a consumer's
    `${{cache.REDIS_URL}}` ref resolves through the Fabric off World exactly
    the way rds's `DATABASE_URL` does (`cachectl.facts`)."""
    out: Projected = {}
    for record in cachectl.clusters(stores, env):
        tags = stores.tags.get(env, f"elasticache:{record['arn']}", {})
        label = _label(tags, record["cache_cluster_id"])
        if not label:
            continue
        phase = _CACHE_PHASE.get(record["status"], "starting")
        verdict = (record.get("status_reason") or None) if phase == "crashed" else None
        out[label] = ("elasticache", phase, cachectl.facts(record), verdict)
    return out


def _ecs_tasks_for(stores: SynthStores, env: str, cluster_name: str, service_name: str) -> list[dict]:
    prefix = f"task:{cluster_name}:"
    return [
        task for key, task in stores.ecsctl.items(env).items()
        if key.startswith(prefix) and task["service_name"] == service_name
    ]


def _ecs_verdict(task: dict) -> str:
    reason = task.get("stopped_reason") or "task stopped"
    exit_code = task.get("exit_code")
    return f"{reason} (exit {exit_code})" if exit_code is not None else reason


def _ecs_services(stores: SynthStores, env: str, runtime: TaskRuntime | None = None) -> Projected:
    out: Projected = {}
    # Keep task state honest against real containers BEFORE reading it below
    # -- without this, a task whose container already exited on its own
    # keeps reading "RUNNING" from the store until some unrelated Describe*
    # call happens to sweep it, and a crash-looping service shows "starting"
    # forever (the exact bug this fix closes).
    sweep_tasks(stores, env, runtime or TaskRuntime())
    for key, record in stores.ecsctl.items(env).items():
        # An INACTIVE service is mid-delete (ecsctl.py's own grace-window
        # sweep, `_INACTIVE_SERVICE_SWEEP_SECONDS`) -- World must drop it
        # immediately, not wait for that sweep to actually purge the record.
        if not key.startswith("service:") or record["status"] != "ACTIVE":
            continue
        label = record.get("node_label") or record["service_name"]
        tasks = _ecs_tasks_for(stores, env, record["cluster_name"], record["service_name"])
        running = sum(1 for t in tasks if t["last_status"] == "RUNNING")
        # A STOPPED task record surviving in the store is ALWAYS a real
        # failure: a deliberate stop (scale-down / stale-taskdef replacement
        # / service delete) deletes the record outright (ecsctl.py's
        # `_stop_task`) rather than leaving it STOPPED -- so every STOPPED
        # record here came from either the lazy sweep catching a spontaneous
        # container exit, or a launch that failed outright.
        failed = [t for t in tasks if t["last_status"] == "STOPPED"]
        if running == record["desired_count"]:
            out[label] = ("ecs", "healthy", {}, None)
        elif failed:
            latest = max(failed, key=lambda t: t.get("stopped_at") or 0)
            out[label] = ("ecs", "crashed", {}, _ecs_verdict(latest))
        else:
            out[label] = ("ecs", "starting", {}, None)
    return out


def project(stores: SynthStores, env: str, ecs_runtime: TaskRuntime | None = None) -> Projected:
    """`label -> (kind, phase, facts, verdict)` for every currently-existing
    TF-owned resource in the env's synth stores -- a pure snapshot of what
    tofu has created, save for `_ecs_services`'s own task-state sync (see
    module docstring). `ecs_runtime` is an injectable seam purely for tests;
    every real caller leaves it default (a real `TaskRuntime()`, matching
    ecsctl.py's own `runtime or TaskRuntime()` precedent)."""
    out: Projected = {}
    out.update(_vpc_subnet_sg(stores, env))
    out.update(_iam_roles(stores, env))
    out.update(_ecr_repos(stores, env))
    out.update(_ec2_instances(stores, env))
    out.update(_lambda_functions(stores, env))
    out.update(_ecs_services(stores, env, ecs_runtime))
    out.update(_cache_clusters(stores, env))
    return out
