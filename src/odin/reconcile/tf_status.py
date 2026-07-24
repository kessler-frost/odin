"""Fix-wave 2b finding #1 -- a pure, read-only projection of the TF-owned
resource kinds (vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr: the kinds
`agent/hcl.py` can build and only `tofu apply`/`tofu destroy` ever
creates/destroys -- s3/sqs/sns/dynamodb are excluded, those already get real
World entries via the reconciler's own PROVISIONED path in plan.py) from the
gateway's synth stores into `label -> (kind, phase, facts)`.

Before this fix these 8 kinds never entered World at all: the canvas showed
a permanently stale DRAFT badge even once tofu had a real VM/service/
function/role/repo/network up. `Reconciler.tick()` calls `project()` once
per tick (see reconcile/reconciler.py) and diffs the result against the
current World, emitting a WorldDelta per (label, kind, phase) and pruning
any label that's dropped out (the resource was destroyed -- by tofu, never
by this projection or the reconciler itself).

Label resolution is uniform across every kind: prefer the `odin:node` tag
`agent/hcl.py::_tags_block` stamps on every canvas-node-backed resource,
falling back to the resource's own AWS-native name field where one exists
(sg's GroupName, iam_role's RoleName, ecr's repositoryName, lambda's
FunctionName, ecs's serviceName -- all of which already equal the canvas
label by construction, per hcl.py's own builders) -- vpc/subnet/ec2 have NO
such native field (real CreateVpc/CreateSubnet/RunInstances take no `Name`
argument), so the tag is their ONLY route back to a label; an untagged
vpc/subnet/ec2 (e.g. a resource applied before this feature existed) is
simply not projected yet, rather than guessing.
"""
from __future__ import annotations

from odin.gateway.stores import SynthStores

TF_OWNED_KINDS = frozenset({"vpc", "subnet", "sg", "ec2", "ecs", "lambda", "iam_role", "ecr"})

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

Projected = dict[str, tuple[str, str, dict]]  # label -> (kind, phase, facts)


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
            out[label] = (kind, "healthy", {})
    return out


def _iam_roles(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.iamctl.items(env).items():
        if not key.startswith("role:"):
            continue
        tags = stores.tags.get(env, f"iam:{record['arn']}", {})
        label = _label(tags, record["role_name"])
        if label:
            out[label] = ("iam_role", "healthy", {})
    return out


def _ecr_repos(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.ecr.items(env).items():
        if not key.startswith("repo:"):
            continue
        tags = stores.tags.get(env, f"ecr:{record['repository_arn']}", {})
        label = _label(tags, record["repository_name"])
        if label:
            out[label] = ("ecr", "healthy", {})
    return out


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
        out[label] = ("ec2", phase, {})
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
            out[label] = ("lambda", phase, {})
    return out


def _ecs_running_count(stores: SynthStores, env: str, cluster_name: str, service_name: str) -> int:
    prefix = f"task:{cluster_name}:"
    return sum(
        1 for key, task in stores.ecsctl.items(env).items()
        if key.startswith(prefix) and task["service_name"] == service_name and task["last_status"] == "RUNNING"
    )


def _ecs_services(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
    for key, record in stores.ecsctl.items(env).items():
        # An INACTIVE service is mid-delete (ecsctl.py's own grace-window
        # sweep, `_INACTIVE_SERVICE_SWEEP_SECONDS`) -- World must drop it
        # immediately, not wait for that sweep to actually purge the record.
        if not key.startswith("service:") or record["status"] != "ACTIVE":
            continue
        label = record.get("node_label") or record["service_name"]
        running = _ecs_running_count(stores, env, record["cluster_name"], record["service_name"])
        phase = "healthy" if running == record["desired_count"] else "starting"
        out[label] = ("ecs", phase, {})
    return out


def project(stores: SynthStores, env: str) -> Projected:
    """`label -> (kind, phase, facts)` for every currently-existing TF-owned
    resource in the env's synth stores -- a pure snapshot of what tofu has
    created, read-only (never mutates any store)."""
    out: Projected = {}
    out.update(_vpc_subnet_sg(stores, env))
    out.update(_iam_roles(stores, env))
    out.update(_ecr_repos(stores, env))
    out.update(_ec2_instances(stores, env))
    out.update(_lambda_functions(stores, env))
    out.update(_ecs_services(stores, env))
    return out
