"""Fix-wave 2b finding #1 -- a pure, read-only projection of the TF-owned
resource kinds (vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr/logs/secret/ssm/
elasticache/rds/alb: the kinds
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

from odin.compute.tasks import TaskRuntime
from odin.gateway.models import cachectl, elbv2ctl, logsctl, rdsctl, ssmctl
from odin.gateway.models.ecsctl import sweep_tasks
from odin.gateway.stores import SynthStores
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST

TF_OWNED_KINDS = frozenset({
    "vpc", "subnet", "sg", "ec2", "ecs", "lambda", "iam_role", "ecr", "logs", "secret", "ssm",
    "elasticache", "rds", "alb",
})

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
    WebSocket and into `.odin/{env}/world.json`, and a secret's value must
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

    Both point at the SAME real Postgres, on the container's actually-published
    host port, with the instance's real master credentials and its real
    `db_name` (a `POSTGRES_DB` the substrate genuinely creates -- so the path
    in this URL is a database that exists, not a label).
    """
    port = record.get("endpoint_port")
    if not port:
        return {}
    user, password, db = record["master_username"], record["master_password"], record["db_name"]
    addr, vm_addr = f"{CONTAINER_HOST}:{port}", f"{LIMA_HOST}:{port}"
    return {
        "DATABASE_URL": f"postgresql://{user}:{password}@{addr}/{db}",
        "endpoint": addr,
        "DATABASE_URL_VM": f"postgresql://{user}:{password}@{vm_addr}/{db}",
        "endpoint_vm": vm_addr,
    }


def _db_instances(stores: SynthStores, env: str) -> Projected:
    """W2.7: rds joins the projection instead of being provisioned+observed by
    the reconciler. Facts are published ONLY for an `available` instance -- a
    `failed` one has no working endpoint, and advertising the last known
    DATABASE_URL for a dead database is exactly the kind of stale-green lie the
    reality sweep exists to kill. The verdict then carries WHY."""
    out: Projected = {}
    for record in rdsctl.records(stores, env):
        identifier = record["db_instance_identifier"]
        tags = stores.tags.get(env, f"rds:{rdsctl.db_arn(identifier)}", {})
        label = _label(tags, identifier)
        if not label:
            continue
        phase = _RDS_PHASE.get(record["status"], "starting")
        facts = _db_facts(record) if record["status"] == rdsctl.AVAILABLE else {}
        verdict = (record.get("status_reason") or None) if phase == "crashed" else None
        out[label] = ("rds", phase, facts, verdict)
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
    `http://127.0.0.1:{port}` rides out as a fact -- onto the WebSocket, into
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
        verdict = (record.get("state_reason") or None) if phase == "crashed" else None
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


def _ec2_instances(stores: SynthStores, env: str) -> Projected:
    out: Projected = {}
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
    """W2.8. Publishes real FACTS the way `_db_instances` does: an `available`
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
    out.update(_log_groups(stores, env))
    out.update(_secrets(stores, env))
    out.update(_ssm_parameters(stores, env))
    out.update(_db_instances(stores, env))
    out.update(_load_balancers(stores, env))
    out.update(_ec2_instances(stores, env))
    out.update(_lambda_functions(stores, env))
    out.update(_ecs_services(stores, env, ecs_runtime))
    out.update(_cache_clusters(stores, env))
    return out
