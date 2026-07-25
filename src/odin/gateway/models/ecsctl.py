"""The gateway's ECS control-plane model (task V5a): clusters, task
definitions, services, and tasks, built to the captured `aws_ecs_cluster` +
`aws_ecs_task_definition` surface in
docs/superpowers/research/research-coverage.md §2e (JSON protocol,
`X-Amz-Target: AmazonEC2ContainerServiceV20141113.*`; cluster ACTIVE
immediately after an internal ~10s waiter; taskdef `family:revision` counter)
and the MiniStack ECS digest it references (§2.6: "real -- RunTask calls
`docker_client.containers.run(...)`", the taskdef/service/task record
triple, the per-family revision counter, lazy RUNNING->STOPPED projection
from real container status) -- adopted as a DESIGN, never a dependency
(NORTHSTAR directive 5). `aws_ecs_service`/RunTask were never captured live
(research: "not captured live"), so their shape here is derived from
botocore's own ecs `service-2.json` (verified field-by-field against
`botocore.session.get_session().get_service_model("ecs")`), not a capture.

Like ec2compute/iamctl/ecr/lambdactl, ECS's CONTROL PLANE has no backing to
forward to: this module is the whole answer for every `ecs:*` action. Unlike
those, service/task STATE is REAL and asynchronous like ec2compute's
instances: a cluster and a service are both ACTIVE immediately (they're
specs, not real infrastructure themselves), but `runningCount`/`lastStatus`
answer from REAL Colima containers (`compute/tasks.py::TaskRuntime`), never
a stored fiction.

GATEWAY-INTERNAL RECONCILE (the brief's own design note, not the main
Reconciler loop): CreateService/UpdateService spawn a background thread
(`_reconcile_service_tasks`, the exact `_spawn`-a-daemon-thread shape
ec2compute/lambdactl already use) that converges the service's REAL task
containers toward `desiredCount` -- launch when short, newest-task-first
scale-down when over (the digest's own ordering), and replace any task
still running a STALE `taskDefinition` revision. This does NOT live in
`reconcile/reconciler.py`'s main tick: the gateway model owns ALL of ECS's
state end to end, and it's the AWS provider's own repeated `Describe*` READ
calls during `apply`/`plan` (its create/update waiters) that drive
convergence checks forward -- there is no separate "AWS resource" for the
main reconciler to observe or own here, exactly like ec2/iam/ecr/lambda
before it. A SEPARATE, purely-observational "lazy sweep"
(`_sweep_tasks`, called from every `DescribeServices`/`DescribeTasks`/
`ListTasks`) promotes a task from `PROVISIONING` to `RUNNING` once its
container is actually up, and DEMOTES `RUNNING` to `STOPPED` (with
`stoppedReason`+the container's real exit code) the moment its container has
exited ON ITS OWN -- the digest's `_maybe_mark_stopped`, ported as a
real-container status check instead of a stored Docker-events subscription.
A task stopped this way is NOT automatically replaced until the next
mutating call reconciles the service again (a documented v1 limitation:
no background scheduler loop watches for a spontaneous crash between
API calls, matching "TF's read calls drive convergence", not a timer).

TASK-DEFINITION DRIFT (the brief's explicit mandate, research §2e's ONE
non-zero-drift capture): `containerDefinitions` is stored and echoed back
BYTE-FOR-BYTE as the client submitted it (the parsed JSON structure from the
wire, no field injection/reordering/default-filling of any kind). Real
AWS/MiniStack both auto-populate a bunch of default fields on every
container definition (`essential`, zeroed `cpu`, empty `environment`/
`mountPoints`/`volumesFrom`/`systemControls` arrays, ...) that the TF AWS
provider's own `container_definitions` equivalence check is SUPPOSED to
tolerate -- but research's own capture found MiniStack's still didn't (a
real, structural `1 to add / 1 to destroy` on every subsequent plan). Rather
than reverse-engineer MiniStack's exact (and possibly buggy) default-filling
to match, this module sidesteps the whole class of drift: it never adds
anything the client didn't send, so RegisterTaskDefinition's response IS the
client's own submitted JSON, structurally identical on every read -- the
provider's own comparison (whatever normalization it applies) is comparing a
value against ITSELF, which is trivially equal regardless of the exact
algorithm. If V5d's real `tofu plan` still finds drift despite this, that's
the honest fallback the brief allows: document it as a known re-register
(research observed the same class of issue against MiniStack).

Persistence: one `JsonStore` at `.odin/{env}/gateway/ecsctl.json`
(`stores.ecsctl`), flat keys `"cluster:{name}"`, `"taskdef-rev:{family}"`
(the per-family revision counter), `"taskdef:{family}:{revision}"`,
`"service:{cluster}:{name}"`, `"task:{cluster}:{task_id}"` -- four disjoint
prefixes (`"taskdef:"` never collides with `"taskdef-rev:"`, `"task:"` never
matches `"taskdef..."` -- the 5th character disagrees) sharing one flat
namespace, the exact convention ec2net/ec2compute already use for their own
multi-kind stores. Service TAGS live in the shared `stores.tags` store
(lambdactl's exact convention), keyed `"ecs:{serviceArn}"`, so a `tags`
block on `aws_ecs_service` round-trips (DescribeServices echoes it;
`TagResource`/`UntagResource`/`ListTagsForResource` are modeled) instead of
drifting on every subsequent `tofu plan`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterable
from typing import NamedTuple

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.compute.tasks import TaskRuntime, container_name
from odin.gateway import errors
from odin.gateway.keys import KeyStore, workload_env
from odin.gateway.models import elbv2ctl, logsctl
from odin.gateway.stores import NO_CHANGE, SynthStores
from odin.gateway.wiring import node_env
from odin.runtime.colima import CONTAINER_HOST

log = logging.getLogger("odin.gateway.ecsctl")

_DEFAULT_LAUNCH_TYPE = "EC2"
_DEFAULT_NETWORK_MODE = "bridge"
_DEFAULT_COMPATIBILITIES = ["EC2"]

# The digest's real-AWS-observed stop reason for a container that exited on
# its own (research §2e / §2.6's `_maybe_mark_stopped`) -- distinct from a
# deliberately-issued stop (scale-down, stale-taskdef replacement, service
# delete), which never goes through the lazy sweep at all (this module
# already knows the outcome the moment it issues the stop).
_ESSENTIAL_CONTAINER_EXITED = "Essential container in task exited"

# How many trailing lines of a task container's output ONE sweep reads
# (`docker logs --tail N`) -- see `_ship_task_logs`: bounded so a chatty
# container can't turn a `Describe*` into an unbounded read, generous enough
# that a crash's traceback fits inside one window.
_LOG_TAIL_LINES = 200

# DeleteService keeps the record around, `status="INACTIVE"`, for this long
# before actually purging it (mirrors ec2compute.py's
# `_TERMINATED_SWEEP_SECONDS` grace-window pattern) -- LOAD-BEARING (V5d):
# real terraform-provider-aws's post-delete poll (aws-sdk-go-v2, not
# botocore's own `ServicesInactive` waiter, whose `failures[].reason ==
# "MISSING"` acceptor is a FAILURE state, verified against botocore's own
# ecs waiters-2.json) treats a service that's simply GONE as "not ready yet"
# and retries forever -- it needs to see `status: "INACTIVE"` on a
# successfully-described service to consider the delete complete. Deleting
# the record outright (this module's first cut) hung a real `tofu destroy`
# indefinitely; this grace window is what makes the delete observable.
_INACTIVE_SERVICE_SWEEP_SECONDS = 60.0

# Trailing keystore/gateway_port are threaded to EVERY handler even where
# unused (the same convention `runtime` itself follows): only the service
# handlers act on them (workload-creds injection -- `workload_env`).
_Handler = Callable[[dict, str, SynthStores, TaskRuntime, KeyStore | None, int | None], Response]


# --- keys / arns -------------------------------------------------------


def _cluster_key(name: str) -> str:
    return f"cluster:{name}"


def _taskdef_counter_key(family: str) -> str:
    return f"taskdef-rev:{family}"


def _taskdef_key(family: str, revision: int) -> str:
    return f"taskdef:{family}:{revision}"


def _service_key(cluster: str, name: str) -> str:
    return f"service:{cluster}:{name}"


def _task_key(cluster: str, task_id: str) -> str:
    return f"task:{cluster}:{task_id}"


def _cluster_arn(name: str) -> str:
    return f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{name}"


def _service_arn(cluster: str, name: str) -> str:
    return f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/{cluster}/{name}"


def _task_arn(cluster: str, task_id: str) -> str:
    return f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{cluster}/{task_id}"


def _taskdef_arn(family: str, revision: int) -> str:
    return f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{family}:{revision}"


def _strip_id(value: str | None, default: str = "default") -> str:
    """A bare name from either a bare id or a full ARN -- the last `/`
    segment, matching ecr.py's `_resource_repo`/ec2net's id-param handling."""
    return value.rsplit("/", 1)[-1] if value else default


# --- store access --------------------------------------------------------


def _cluster(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.ecsctl.get(env, _cluster_key(name))


def _all_clusters(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.ecsctl.items(env).items() if k.startswith("cluster:")]


def _taskdef(stores: SynthStores, env: str, family: str, revision: int) -> dict | None:
    return stores.ecsctl.get(env, _taskdef_key(family, revision))


def _service(stores: SynthStores, env: str, cluster: str, name: str) -> dict | None:
    return stores.ecsctl.get(env, _service_key(cluster, name))


def _services_for_cluster(stores: SynthStores, env: str, cluster: str) -> list[dict]:
    prefix = f"service:{cluster}:"
    return [v for k, v in stores.ecsctl.items(env).items() if k.startswith(prefix)]


def _active_services_for_cluster(stores: SynthStores, env: str, cluster: str) -> list[dict]:
    return [s for s in _services_for_cluster(stores, env, cluster) if s["status"] == "ACTIVE"]


def _sweep_inactive_services(stores: SynthStores, env: str) -> None:
    now = time.time()
    for service in _all_services(stores, env):
        deleted_at = service.get("deleted_at")
        if deleted_at is not None and now - deleted_at > _INACTIVE_SERVICE_SWEEP_SECONDS:
            stores.ecsctl.delete(env, _service_key(service["cluster_name"], service["service_name"]))


def _all_services(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.ecsctl.items(env).items() if k.startswith("service:")]


def _all_tasks(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.ecsctl.items(env).items() if k.startswith("task:")]


def _tasks_for_cluster(stores: SynthStores, env: str, cluster: str) -> list[dict]:
    prefix = f"task:{cluster}:"
    return [v for k, v in stores.ecsctl.items(env).items() if k.startswith(prefix)]


def _tasks_for_service(stores: SynthStores, env: str, cluster: str, service_name: str) -> list[dict]:
    return [t for t in _tasks_for_cluster(stores, env, cluster) if t["service_name"] == service_name]


def _update_task(stores: SynthStores, env: str, cluster: str, task_id: str, **fields: object) -> None:
    def mutate(task: dict | None) -> dict | object:
        if task is None:  # deleted (or never existed) while a background op was mid-flight
            return NO_CHANGE
        task = dict(task)
        task.update(fields)
        return task

    stores.ecsctl.update(env, _task_key(cluster, task_id), mutate)


def _resolve_taskdef_ref(stores: SynthStores, env: str, ref: str) -> dict | None:
    """`ref` is a family, a `family:revision`, or a full taskDefinitionArn
    (any of which the ECS API itself accepts for `taskDefinition` params) --
    a bare family resolves to its latest ACTIVE revision (real AWS's own
    "no revision given" semantics), never just the highest-numbered one
    (DeregisterTaskDefinition can leave the highest revision INACTIVE)."""
    if not ref:
        return None
    bare = ref.rsplit("/", 1)[-1]
    family, _, revision_str = bare.rpartition(":")
    if family and revision_str.isdigit():
        return _taskdef(stores, env, family, int(revision_str))
    return _latest_active_taskdef(stores, env, bare)


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, f"ecs:{arn}", {})


def _latest_active_taskdef(stores: SynthStores, env: str, family: str) -> dict | None:
    latest = stores.ecsctl.get(env, _taskdef_counter_key(family), 0)
    for revision in range(latest, 0, -1):
        taskdef = _taskdef(stores, env, family, revision)
        if taskdef is not None and taskdef["status"] == "ACTIVE":
            return taskdef
    return None


# --- wire building --------------------------------------------------------


def _tag_list(tags: dict[str, str]) -> list[dict]:
    """The ECS `Tag` wire shape: a LIST of lowercase {"key","value"} dicts
    (botocore's ecs service-2.json) -- the exact inverse of
    `_create_service`/`_tag_resource`'s parse, so tags round-trip
    symmetrically."""
    return [{"key": k, "value": v} for k, v in tags.items()]


def _json(payload: dict) -> Response:
    body = {k: v for k, v in payload.items() if v is not None}
    return Response(json.dumps(body), media_type="application/x-amz-json-1.0")


def _not_found_cluster(name: str) -> Response:
    return errors.synth_error("ecs", "ClusterNotFoundException", f"Cluster not found: {name}", 400)


def _not_found_service(name: str) -> Response:
    return errors.synth_error("ecs", "ServiceNotFoundException", f"Service not found: {name}", 400)


def _not_found_taskdef(ref: str) -> Response:
    return errors.synth_error("ecs", "ClientException", f"Unable to describe task definition: {ref}", 400)


def _cluster_wire(stores: SynthStores, env: str, cluster: dict) -> dict:
    tasks = _tasks_for_cluster(stores, env, cluster["cluster_name"])
    running = sum(1 for t in tasks if t["last_status"] == "RUNNING")
    pending = sum(1 for t in tasks if t["last_status"] == "PROVISIONING")
    return {
        "clusterArn": cluster["cluster_arn"],
        "clusterName": cluster["cluster_name"],
        "status": "ACTIVE",
        "registeredContainerInstancesCount": 0,  # v1 never models real ECS container instances
        "runningTasksCount": running,
        "pendingTasksCount": pending,
        "activeServicesCount": len(_active_services_for_cluster(stores, env, cluster["cluster_name"])),
        "statistics": [],
        "tags": [],
        "settings": cluster["settings"],
        "capacityProviders": [],
        "defaultCapacityProviderStrategy": [],
    }


def _taskdef_wire(taskdef: dict) -> dict:
    return {
        "taskDefinitionArn": _taskdef_arn(taskdef["family"], taskdef["revision"]),
        # VERBATIM -- see the module docstring's "TASK-DEFINITION DRIFT" note.
        "containerDefinitions": taskdef["container_definitions"],
        "family": taskdef["family"],
        "taskRoleArn": taskdef.get("task_role_arn"),
        "executionRoleArn": taskdef.get("execution_role_arn"),
        "networkMode": taskdef["network_mode"],
        "revision": taskdef["revision"],
        "volumes": taskdef["volumes"],
        "status": taskdef["status"],
        "requiresAttributes": [],
        "placementConstraints": [],
        "compatibilities": taskdef["requires_compatibilities"],
        "requiresCompatibilities": taskdef["requires_compatibilities"],
        "cpu": taskdef.get("cpu"),
        "memory": taskdef.get("memory"),
        "registeredAt": taskdef["registered_at"],
        "deregisteredAt": taskdef.get("deregistered_at"),
        "registeredBy": f"arn:aws:iam::{ACCOUNT}:root",
    }


def _on_current_revision(service: dict, tasks: list[dict]) -> list[dict]:
    """The subset of `tasks` running the service's CURRENT task definition.

    Field-test 2 finding B1 (HIGH): a bad-image ECS *update* reported apply
    SUCCESS in 2.3 seconds while destroying all three healthy tasks and leaving
    the load balancer serving 503. The v0.5.4 guard (`wait_for_steady_state` +
    a bounded `timeouts`) genuinely covered CREATE but was inert on UPDATE,
    because terraform-provider-aws's steady-state waiter keys on
    `len(deployments) == 1 && desiredCount == runningCount` -- and a
    revision-blind `runningCount` still counted the three STALE tasks at the
    instant UpdateService returned, so the waiter declared the deployment
    stable before the reconcile thread had even started. Counting only the
    current revision is also what real ECS's PRIMARY deployment reports, and it
    is the only self-consistent choice for a model that renders exactly ONE
    deployment record: an update is never "already steady" the moment it is
    requested, and it stays short-of-desired for as long as it genuinely is,
    which is what turns the bounded `timeouts.update` into a real apply
    failure. (Deliberate deviation, recorded: real AWS's SERVICE-level
    runningCount also counts draining old-revision tasks, but it distinguishes
    them with a second deployment record, which odin does not model.)"""
    return [t for t in tasks if t["task_definition_arn"] == service["task_definition_arn"]]


def _rollout(service: dict, tasks: list[dict], deployment_id: str) -> tuple[str, str]:
    """The deployment's honest `(rolloutState, rolloutStateReason)` from the
    REAL task outcomes (field-test finding #3). A STOPPED task in the store is
    always a genuine failure/spontaneous-exit -- a DELIBERATE stop
    (scale-down/replace/delete) deletes the record outright (`_stop_task`),
    never leaves it STOPPED -- so its presence while short of desired means the
    deployment can't converge. Before this the state was hardcoded COMPLETED, so
    a bad image / crash-on-start read as a healthy deployment and the failure
    was never surfaced (apply silently 'succeeded'). runningCount==desiredCount
    wins first, so a lingering STOPPED task from an already-recovered service
    never reads as FAILED.

    `tasks` is already narrowed to the CURRENT revision by the caller
    (`_on_current_revision`) -- a stale task from the previous deployment is
    neither progress toward this one nor a failure of it."""
    running = sum(1 for t in tasks if t["last_status"] == "RUNNING")
    stopped = [t for t in tasks if t["last_status"] == "STOPPED"]
    if running == service["desired_count"]:
        return "COMPLETED", f"ECS deployment {deployment_id} completed."
    if stopped:
        reason = next((t["stopped_reason"] for t in stopped if t.get("stopped_reason")), "a task failed to start")
        return "FAILED", f"ECS deployment circuit breaker: tasks failed to start: {reason}"
    return "IN_PROGRESS", f"ECS deployment {deployment_id} in progress."


def _service_wire(stores: SynthStores, env: str, service: dict) -> dict:
    arn = _service_arn(service["cluster_name"], service["service_name"])
    all_tasks = _tasks_for_service(stores, env, service["cluster_name"], service["service_name"])
    # CURRENT-REVISION ONLY -- see `_on_current_revision` for why (finding B1:
    # this is the whole reason a bad-image UPDATE now fails apply instead of
    # reading as instantly steady).
    tasks = _on_current_revision(service, all_tasks)
    running = sum(1 for t in tasks if t["last_status"] == "RUNNING")
    pending = sum(1 for t in tasks if t["last_status"] == "PROVISIONING")
    deployment_id = f"ecs-svc/{service['service_name']}"
    rollout_state, rollout_reason = _rollout(service, tasks, deployment_id)
    # A failed deployment surfaces a real service event too (real ECS posts
    # "unable to..." events here) -- honest in DescribeServices even though the
    # v5 provider's create waiter keys on runningCount, not rolloutState.
    events = []
    if rollout_state == "FAILED":
        events = [{
            "id": str(uuid.uuid4()), "createdAt": time.time(),
            "message": f"(service {service['service_name']}) {rollout_reason}",
        }]
    deployment = {
        "id": deployment_id,
        "status": "PRIMARY",
        "taskDefinition": service["task_definition_arn"],
        "desiredCount": service["desired_count"],
        "pendingCount": pending,
        "runningCount": running,
        "createdAt": service["created_at"],
        "updatedAt": service["created_at"],
        "launchType": service["launch_type"],
        "rolloutState": rollout_state,
        "rolloutStateReason": rollout_reason,
    }
    return {
        "serviceArn": arn,
        "serviceName": service["service_name"],
        "clusterArn": _cluster_arn(service["cluster_name"]),
        # W2.5: echoed VERBATIM from what CreateService submitted (the same
        # byte-for-byte rule this module's docstring sets for
        # containerDefinitions) -- an `aws_ecs_service` with a `load_balancer`
        # block drifts on every subsequent plan if this stays hardcoded `[]`.
        "loadBalancers": service.get("load_balancers") or [],
        "serviceRegistries": [],
        "status": service["status"],
        "desiredCount": service["desired_count"],
        "runningCount": running,
        "pendingCount": pending,
        "launchType": service["launch_type"],
        "taskDefinition": service["task_definition_arn"],
        "deploymentConfiguration": {"maximumPercent": 200, "minimumHealthyPercent": 100},
        "deployments": [deployment],
        "events": events,
        "createdAt": service["created_at"],
        "placementConstraints": [],
        "placementStrategy": [],
        "schedulingStrategy": "REPLICA",
        "enableECSManagedTags": False,
        "propagateTags": "NONE",
        # Echoed on EVERY describe (real AWS gates this behind
        # `include=["TAGS"]`; always answering is harmless to the parser and
        # covers the TF provider's DescribeServices-with-TAGS read path) --
        # THE fix for the recorded "tags block drifts on a subsequent plan"
        # v1 limit.
        "tags": _tag_list(_tags_for(stores, env, arn)),
        # Real AWS's own default (verified live, V5d): the TF provider's
        # schema treats this as Computed with that default, so omitting it
        # entirely reads as "unset" and drifts on every subsequent plan.
        "availabilityZoneRebalancing": "DISABLED",
    }


def _task_wire(task: dict) -> dict:
    container = {
        "containerArn": f"{task['task_arn']}/{task['container_name']}",
        "taskArn": task["task_arn"],
        "name": task["container_name"],
        "lastStatus": task["last_status"],
        "exitCode": task["exit_code"],
        "reason": task["stopped_reason"],
        "networkBindings": [
            {"bindIP": "0.0.0.0", "containerPort": cport, "hostPort": hport, "protocol": "tcp"}
            for cport, hport in task["host_ports"].items()
        ],
    }
    return {
        "taskArn": task["task_arn"],
        "clusterArn": _cluster_arn(task["cluster_name"]),
        "taskDefinitionArn": task["task_definition_arn"],
        "overrides": {"containerOverrides": []},
        "lastStatus": task["last_status"],
        "desiredStatus": task["desired_status"],
        "cpu": task.get("cpu"),
        "memory": task.get("memory"),
        "containers": [container],
        "startedAt": task["started_at"],
        "stoppedAt": task["stopped_at"],
        "stoppedReason": task["stopped_reason"],
        "group": f"service:{task['service_name']}",
        "launchType": "EC2",
        "version": 1,
    }


# --- W2.5: the service scheduler's load-balancer half --------------------
# Real ECS (not terraform) is what registers a TASK with the target groups
# named in its service's `loadBalancers` block, and deregisters it when the
# task goes away. These two helpers are odin's equivalent, called from the
# same three places a task's real lifecycle turns over: `_launch_task`,
# `_stop_task` (a deliberate stop) and the lazy sweep / drift correction (a
# container that died on its own). A service with no `loadBalancers` runs the
# loop body zero times, so this costs nothing for the ECS-without-an-ALB case.


def _task_targets(
    stores: SynthStores, env: str, cluster_name: str, service_name: str, host_ports: dict | None,
) -> list[tuple[str, int]]:
    """`[(targetGroupArn, published host port), ...]` for one task: each of the
    service's load balancers, paired with the REAL host port Docker published
    for the container port that load balancer names. An odin ECS task is a
    bridge-mode container, so its address is (`host.docker.internal`, that
    published port) -- see elbv2ctl.py's TARGET ADDRESSES note. `host_ports`
    keys are ints in memory but strings once the store has round-tripped
    through JSON, hence the `str()` normalization."""
    service = _service(stores, env, cluster_name, service_name)
    published = {str(port): host_port for port, host_port in (host_ports or {}).items()}
    return [
        (lb["targetGroupArn"], int(published[str(lb.get("containerPort"))]))
        for lb in (service or {}).get("load_balancers") or []
        if lb.get("targetGroupArn") and published.get(str(lb.get("containerPort")))
    ]


def _register_task_targets(
    stores: SynthStores, env: str, cluster_name: str, service_name: str, host_ports: dict | None,
) -> None:
    for target_group_arn, host_port in _task_targets(stores, env, cluster_name, service_name, host_ports):
        elbv2ctl.register_target(stores, env, target_group_arn, CONTAINER_HOST, host_port)


def _deregister_task_targets(stores: SynthStores, env: str, task: dict) -> None:
    targets = _task_targets(stores, env, task["cluster_name"], task["service_name"], task.get("host_ports"))
    for target_group_arn, host_port in targets:
        elbv2ctl.deregister_target(stores, env, target_group_arn, CONTAINER_HOST, host_port)


# --- lazy sweep: real container status -> task lastStatus (research §2.6's
# `_maybe_mark_stopped`, module docstring's "GATEWAY-INTERNAL RECONCILE") ---


def _ship_task_logs(stores: SynthStores, env: str, task: dict, runtime: TaskRuntime) -> None:
    """Ship one task container's log tail into `/ecs/{service}` -- the group
    an `awslogs` log-configuration would name in real ECS, so a crash's output
    survives the container that produced it.

    ONE STREAM PER REAL TASK CONTAINER, named after the container itself
    (`odin-ecs-{env}-{task_id8}-{container}`) -- odin's honest deviation from
    AWS's `{prefix}/{container}/{taskId}` stream naming, and what makes
    `ingest_tail`'s cursor stable: the cursor counts how many lines of THIS
    container's output have been ingested, and a task container is never
    reused (a replacement task gets a new task_id, hence a new stream), so a
    re-read of the same tail appends nothing.

    Cost: `sweep_tasks` runs on every reconciler tick (`reconcile/
    tf_status.py`) and every ECS `Describe*`/`ListTasks`, so this is exactly
    ONE bounded `docker logs --tail N` per task per sweep, with the cursor --
    not a growing log group -- absorbing the repetition. No try/except: the
    read is `TaskRuntime.logs` -> the driver's `check=False` CLI call, which
    answers "" for an already-removed container instead of raising.
    """
    logsctl.ingest_tail(
        stores, env, f"/ecs/{task['service_name']}",
        container_name(env, task["task_id"], task["container_name"]),
        runtime.logs(env, task["task_id"], task["container_name"], _LOG_TAIL_LINES),
    )


def sweep_tasks(stores: SynthStores, env: str, runtime: TaskRuntime) -> None:
    for task in _all_tasks(stores, env):
        # Before (and independent of) the RUNNING-only check below: a task
        # that already exited on its own still has its FINAL lines sitting in
        # a stopped container, and those lines ARE the crash diagnostic.
        _ship_task_logs(stores, env, task, runtime)
        if task["last_status"] != "RUNNING":
            continue
        status = runtime.status(env, task["task_id"], task["container_name"])
        if status not in ("exited", "dead", "removing"):
            continue
        exit_code = runtime.exit_code(env, task["task_id"], task["container_name"])
        _update_task(
            stores, env, task["cluster_name"], task["task_id"],
            last_status="STOPPED", stopped_at=time.time(), exit_code=exit_code,
            stopped_reason=_ESSENTIAL_CONTAINER_EXITED,
        )
        # W2.5: a task that died on its own must leave its load balancer's
        # upstream list too, or the proxy keeps a dead server in rotation. Runs
        # once, on the RUNNING->STOPPED transition (this loop only reaches here
        # for a task the store still calls RUNNING), never every sweep.
        _deregister_task_targets(stores, env, task)


def mark_task_stopped(stores: SynthStores, env: str, cluster_name: str, task_id: str, reason: str) -> None:
    """Public seam for W2.2's reality sweep (`reconcile/drift.py`): a task
    whose container was REMOVED outside odin, marked STOPPED with `reason`.
    Same terminal shape `sweep_tasks` gives a container that exited on its
    own -- minus an `exit_code`, because a container that no longer exists
    never reported one (an invented 0/137 would be a lie). Keeping the store
    honest is also what makes the recovery real: `converge_services` (an
    Apply) relaunches a STOPPED task, while a RUNNING record whose container
    is gone would have it wait forever for a task nothing will replace."""
    _update_task(
        stores, env, cluster_name, task_id,
        last_status="STOPPED", stopped_at=time.time(), stopped_reason=reason,
    )
    task = stores.ecsctl.get(env, _task_key(cluster_name, task_id))
    if task is not None:  # W2.5: a vanished container leaves the LB's rotation too
        _deregister_task_targets(stores, env, task)


# --- background completion: the reconcile-on-mutation shape (module
# docstring's "GATEWAY-INTERNAL RECONCILE" -- the same daemon-thread pattern
# ec2compute/lambdactl already use for real, possibly-slow substrate work) --


def _launch_task(
    stores: SynthStores, env: str, cluster_name: str, service_name: str, taskdef: dict, runtime: TaskRuntime,
    extra_env: dict[str, str] | None = None, node_label: str = "",
) -> None:
    container_def = taskdef["container_definitions"][0]  # v1: single-container taskdefs (V5c)
    task_id = uuid.uuid4().hex
    task = {
        "cluster_name": cluster_name,
        "task_id": task_id,
        "task_arn": _task_arn(cluster_name, task_id),
        "task_definition_arn": _taskdef_arn(taskdef["family"], taskdef["revision"]),
        "service_name": service_name,
        "container_name": container_def.get("name", "app"),
        "last_status": "PROVISIONING",
        "desired_status": "RUNNING",
        "started_at": None,
        "stopped_at": None,
        "stopped_reason": None,
        "exit_code": None,
        "cpu": taskdef.get("cpu"),
        "memory": taskdef.get("memory"),
        "host_ports": {},
    }
    stores.ecsctl.set(env, _task_key(cluster_name, task_id), task)
    try:
        # CANVAS WIRING (field test 2, the product hole): the node's own `env`
        # map -- static entries plus every `${{producer.ATTR}}` ref resolved to a
        # live value -- delivered into the REAL container here rather than baked
        # into the taskdef, which would put resolved connection strings
        # (passwords included) into terraform.tfstate and drift on every plan
        # (see gateway/wiring.py for the full rationale). Resolved BEFORE the
        # issued credentials are layered on, so odin's own four AWS_* vars
        # always win over a canvas that happens to name one of them.
        # An `UnresolvedRef` lands in the SAME `except` as a failed
        # `docker run`: the task goes STOPPED with the real reason, which is
        # what makes a bad ref a `crashed` node with a naming verdict AND a
        # failed apply (the service never reaches steady state).
        launch_env = {**node_env(stores, env, node_label), **(extra_env or {})} if node_label else extra_env
        handle = runtime.run(
            env, task_id, container_def, extra_env=launch_env,
            cpu=taskdef.get("cpu"), memory=taskdef.get("memory"),
        )
    except Exception as exc:
        # Deliberately broad: this runs on a daemon thread with no caller to
        # propagate an exception to -- see ec2compute.py's `_finish_boot` for
        # the identical "silent hang is forbidden" reasoning.
        log.warning("task container failed for %s/%s (env %s): %s", service_name, task_id, env, exc)
        _update_task(
            stores, env, cluster_name, task_id,
            last_status="STOPPED", stopped_at=time.time(), stopped_reason=str(exc),
        )
        return
    _update_task(
        stores, env, cluster_name, task_id,
        last_status="RUNNING", started_at=time.time(), host_ports=handle.host_ports,
    )
    # W2.5: the task is now genuinely serving, so it joins its service's target
    # groups -- which re-renders the load balancer's nginx config and reloads
    # it. Synchronous on purpose: this whole function already runs on a daemon
    # thread, and a task must be in rotation before anything reports it healthy.
    _register_task_targets(stores, env, cluster_name, service_name, handle.host_ports)


def _stop_task(stores: SynthStores, env: str, task: dict, runtime: TaskRuntime) -> None:
    """A DELIBERATE stop (scale-down / stale-taskdef replacement / service
    delete) -- unlike `_sweep_tasks`'s lazy discovery of a spontaneous exit,
    this module already knows the outcome the moment it issues the stop, so
    the task record is removed outright rather than parked in a transitional
    state nothing will ever sweep away."""
    # W2.5: out of the load balancer's rotation FIRST, so the proxy stops
    # sending it traffic before the container actually dies.
    _deregister_task_targets(stores, env, task)
    try:
        runtime.stop(env, task["task_id"], task["container_name"])
    except Exception as exc:
        log.warning("stopping task container %s (env %s) failed: %s", task["task_id"], env, exc)
    stores.ecsctl.delete(env, _task_key(task["cluster_name"], task["task_id"]))


def _reconcile_service_tasks(
    stores: SynthStores, env: str, cluster_name: str, service_name: str, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> None:
    with _lock_for_service(stores, env, cluster_name, service_name):
        service = _service(stores, env, cluster_name, service_name)
        if service is None or service["status"] != "ACTIVE":  # deleted while this reconcile was queued/racing
            return
        taskdef = _resolve_taskdef_ref(stores, env, service["task_definition_arn"])
        if taskdef is None:  # taskdef deregistered out from under a live service -- nothing to converge to
            return
        # Workload-creds injection: an `odin:node`-tagged service's tasks get
        # the four AWS-SDK env vars (`workload_env`) layered into the REAL
        # container's env at launch -- never into the stored taskdef (the
        # module docstring's byte-for-byte TASK-DEFINITION DRIFT mandate).
        extra_env: dict[str, str] = {}
        if keystore is not None and gateway_port is not None and service.get("node_label"):
            extra_env = workload_env(keystore, env, service["node_label"], gateway_port)
        live = [t for t in _tasks_for_service(stores, env, cluster_name, service_name) if t["last_status"] != "STOPPED"]
        stale = [t for t in live if t["task_definition_arn"] != service["task_definition_arn"]]
        fresh = [t for t in live if t not in stale]
        for task in stale:
            _stop_task(stores, env, task, runtime)

        desired = service["desired_count"]
        node_label = service.get("node_label") or ""
        if len(fresh) < desired:
            for _ in range(desired - len(fresh)):
                _launch_task(stores, env, cluster_name, service_name, taskdef, runtime, extra_env, node_label)
        elif len(fresh) > desired:
            # Newest-task-first scale-down (the digest's own ordering).
            excess = sorted(fresh, key=lambda t: t["started_at"] or 0, reverse=True)[: len(fresh) - desired]
            for task in excess:
                _stop_task(stores, env, task, runtime)


def _spawn(target: Callable[..., None], *args: object) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def converge_services(
    stores: SynthStores, env: str, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> list[threading.Thread]:
    """Re-converge every ACTIVE service's REAL task containers toward
    `desiredCount` -- the exact `_reconcile_service_tasks` pass CreateService/
    UpdateService already spawn, driven by an APPLY (server.py's /apply-full)
    rather than by an AWS mutation.

    This is what makes "re-Apply" the honest recovery for the drift W2.2's
    reality sweep reports. A task is NOT a terraform resource: nothing about
    an `aws_ecs_service`'s config changes when its container is destroyed out
    of band, so tofu's plan is empty and tofu will never fix it -- in real
    AWS the SERVICE SCHEDULER (not terraform) replaces a lost task, and this
    is odin's equivalent, deliberately triggered by the user's Apply instead
    of a background timer (the module docstring's "TF's read calls drive
    convergence, not a timer" limit stays: no new scheduler loop). Idempotent
    -- a service already at desiredCount launches nothing.

    Returns the spawned threads so the caller can WAIT for the convergence it
    just asked for (`wait_for_steady_services`) instead of guessing at it.

    The leading `sweep_tasks` is load-bearing (field test 3): a container that
    exited on its own still reads RUNNING in the store until something sweeps
    it, so without this the reconcile below counted a dead task as live and
    launched nothing -- the Apply-is-the-recovery promise silently did nothing
    for the most common failure there is. One sweep per Apply, the same
    idempotent pass every World projection already runs."""
    sweep_tasks(stores, env, runtime)
    return [
        _spawn(
            _reconcile_service_tasks, stores, env, service["cluster_name"],
            service["service_name"], runtime, keystore, gateway_port,
        )
        for service in _all_services(stores, env) if service["status"] == "ACTIVE"
    ]


# --- post-apply verification: an Apply may not report success at zero tasks --

# Field test 3 (HIGH): `odin apply` exited 0 with `status: applied / tf: ok`
# while the service sat at 0 of 3 tasks with every task failing. The v0.7.1
# guard (`wait_for_steady_state` + a bounded `timeouts` on the tf resource) is
# only ever evaluated when tofu actually UPDATES the service -- so any apply
# tofu sees as a NO-OP (a re-apply on an already-broken service; an edit that
# only touches the launch-time `env` map, which is deliberately not in the
# task definition) skipped the check entirely and scored a full outage green.
# Nothing in tofu can close that, by definition: tofu has nothing to do. So
# odin verifies it itself, right after its own convergence pass.
_STEADY_POLL_SECONDS = 0.5
_STEADY_TIMEOUT_ENV = "ODIN_ECS_STEADY_TIMEOUT"


def steady_timeout() -> float:
    """The post-apply convergence budget, in seconds. Deliberately the SAME 60s
    as `agent/hcl.py`'s `timeouts.update` (the tf-side twin of this check) --
    one number for "how long may a task legitimately take to come up", not two.
    `ODIN_ECS_STEADY_TIMEOUT` overrides, matching every other odin timeout."""
    return float(os.environ.get(_STEADY_TIMEOUT_ENV, "60"))


class ServiceShortfall(NamedTuple):
    """One service that is below its desired task count: WHICH service, WHAT
    was observed, and the real underlying reason when odin knows one (field
    test 3's caveat: the failing image named only in /world and events was
    never in the apply's own output)."""

    node: str
    running: int
    desired: int
    reason: str | None


def task_verdict(task: dict) -> str:
    """A STOPPED task's real reason as one human line -- the `docker` error
    naming a bad image, an `UnresolvedRef` naming a broken `${{...}}`, or the
    essential-container-exited reason plus its exit code. Shared with
    `reconcile/tf_status.py`, which renders the same string as the node's
    World verdict: the apply output and World must not disagree."""
    reason = task.get("stopped_reason") or "task stopped"
    exit_code = task.get("exit_code")
    return f"{reason} (exit {exit_code})" if exit_code is not None else reason


def _shortfall(stores: SynthStores, env: str, service: dict) -> tuple[ServiceShortfall, int] | None:
    """`(shortfall, pending)` when this service is below desired, else None.
    CURRENT-REVISION tasks only, for the same reason `_on_current_revision`
    records: a stale task from the previous deployment is neither progress
    toward this one nor a failure of it."""
    tasks = _on_current_revision(service, _tasks_for_service(
        stores, env, service["cluster_name"], service["service_name"],
    ))
    running = sum(1 for t in tasks if t["last_status"] == "RUNNING")
    if running >= service["desired_count"]:
        return None
    stopped = [t for t in tasks if t["last_status"] == "STOPPED"]
    latest = max(stopped, key=lambda t: t.get("stopped_at") or 0, default=None)
    return ServiceShortfall(
        node=service.get("node_label") or service["service_name"],
        running=running, desired=service["desired_count"],
        reason=task_verdict(latest) if latest is not None else None,
    ), sum(1 for t in tasks if t["last_status"] not in ("RUNNING", "STOPPED"))


def wait_for_steady_services(
    stores: SynthStores, env: str, runtime: TaskRuntime,
    converging: Iterable[threading.Thread] = (), timeout: float | None = None,
) -> list[ServiceShortfall]:
    """Every ACTIVE service still short of its desired task count once the
    Apply's convergence has had its bounded chance -- empty means the whole
    env's ECS surface is genuinely at desired count, which is the only state
    an Apply may report success in.

    Bounded three ways, so this never becomes a hang and never fails a service
    that is legitimately still starting:
      1. it JOINS `converging` (the threads `converge_services` just spawned),
         so a slow first image pull is waited on rather than raced;
      2. it returns the instant nothing is left PENDING -- a service short of
         desired with no task still coming up cannot converge without another
         Apply, so waiting out the budget would only make the failure slower;
      3. it returns at `steady_timeout()` regardless.
    A freshly created or scaled-up service never reaches here short: tofu's own
    `wait_for_steady_state` already blocked on it, and a tofu failure has
    already failed the apply before this runs.

    `sweep_tasks` each pass for the same reason every World projection runs it:
    a task whose container already exited still reads RUNNING in the store, and
    counting that as healthy is precisely the lie this closes."""
    deadline = time.monotonic() + (steady_timeout() if timeout is None else timeout)
    for thread in converging:
        thread.join(max(0.0, deadline - time.monotonic()))
    while True:
        sweep_tasks(stores, env, runtime)
        short = [
            found for service in _all_services(stores, env) if service["status"] == "ACTIVE"
            for found in [_shortfall(stores, env, service)] if found is not None
        ]
        settled = all(pending == 0 for _, pending in short)
        if not short or settled or time.monotonic() >= deadline:
            return [shortfall for shortfall, _ in short]
        time.sleep(_STEADY_POLL_SECONDS)


# Per-(SynthStores, env, cluster, service) lock, serializing
# `_reconcile_service_tasks` (spawned off CreateService/UpdateService)
# against `_delete_service`'s own stop pass -- LOAD-BEARING (V5d): real
# terraform-provider-aws's own Delete for aws_ecs_service calls
# UpdateService(desiredCount=0) immediately before DeleteService (verified
# live, TF_LOG=DEBUG trace), so a background reconcile thread from THAT
# UpdateService and DeleteService's own synchronous stop loop were racing to
# `docker rm` the SAME container -- the loser's exception is caught (the
# module's own "best-effort teardown" contract, `_stop_task`'s docstring),
# so the record still went away while the REAL container silently survived.
# Without this lock two concurrent stops of the same task are possible;
# with it, at most one ever runs. Keyed through a `WeakKeyDictionary` on the
# `SynthStores` instance itself (the same instance-scoping
# `aws/backings.py::BackingAws._ensure_lock` uses, not a bare module
# global) -- a bare `(env, cluster, service)` global leaked ACROSS
# independent `SynthStores` instances (every test reuses env="default"),
# found the hard way when an unrelated test's never-released FakeTaskRuntime
# block deadlocked every later test sharing that key.
#
# NOT superseded by `JsonStore`'s own per-env lock (release finding #3):
# that lock only makes each INDIVIDUAL get/set/update call atomic (and
# `_update_task` below now goes through it via `stores.ecsctl.update`) -- it
# is released between calls, so it cannot serialize the WHOLE multi-step
# reconcile-vs-delete section this lock covers, which spans several store
# calls interleaved with REAL `docker stop`/`docker rm` calls via `runtime`.
# The two locks protect different things at different granularities and
# both stay.
_service_locks: "weakref.WeakKeyDictionary[SynthStores, dict[tuple[str, str, str], threading.Lock]]" = weakref.WeakKeyDictionary()
_service_locks_guard = threading.Lock()


def _lock_for_service(stores: SynthStores, env: str, cluster_name: str, service_name: str) -> threading.Lock:
    key = (env, cluster_name, service_name)
    with _service_locks_guard:
        per_store = _service_locks.setdefault(stores, {})
        return per_store.setdefault(key, threading.Lock())


# --- Cluster ---------------------------------------------------------------


def _create_cluster(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    name = payload.get("clusterName") or "default"
    existing = _cluster(stores, env, name)
    if existing is not None:  # real CreateCluster is idempotent on an existing name
        return _json({"cluster": _cluster_wire(stores, env, existing)})
    cluster = {
        "cluster_name": name,
        "cluster_arn": _cluster_arn(name),
        "settings": payload.get("settings") or [{"name": "containerInsights", "value": "disabled"}],
    }
    stores.ecsctl.set(env, _cluster_key(name), cluster)
    return _json({"cluster": _cluster_wire(stores, env, cluster)})


def _describe_clusters(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    names = payload.get("clusters")
    failures: list[dict] = []
    if names:
        found = {c["cluster_name"]: c for c in _all_clusters(stores, env)}
        selected = []
        for raw in names:
            name = _strip_id(raw)
            if name in found:
                selected.append(found[name])
            else:
                failures.append({"arn": _cluster_arn(name), "reason": "MISSING"})
    else:
        selected = _all_clusters(stores, env)
    return _json({"clusters": [_cluster_wire(stores, env, c) for c in selected], "failures": failures})


def _delete_cluster(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    name = _strip_id(payload.get("cluster"))
    cluster = _cluster(stores, env, name)
    if cluster is None:
        return _not_found_cluster(name)
    if _active_services_for_cluster(stores, env, name):
        return errors.synth_error(
            "ecs", "ClusterContainsServicesException",
            f"The Cluster cannot be deleted while Services are active: {name}", 400,
        )
    wire = _cluster_wire(stores, env, cluster)
    stores.ecsctl.delete(env, _cluster_key(name))
    return _json({"cluster": wire})


# --- TaskDefinition ----------------------------------------------------------


def _register_task_definition(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    family = payload.get("family", "")
    counter_key = _taskdef_counter_key(family)
    revision = stores.ecsctl.get(env, counter_key, 0) + 1
    stores.ecsctl.set(env, counter_key, revision)
    taskdef = {
        "family": family,
        "revision": revision,
        "container_definitions": payload.get("containerDefinitions") or [],  # verbatim, see module docstring
        "network_mode": payload.get("networkMode") or _DEFAULT_NETWORK_MODE,
        "requires_compatibilities": payload.get("requiresCompatibilities") or list(_DEFAULT_COMPATIBILITIES),
        "cpu": payload.get("cpu"),
        "memory": payload.get("memory"),
        "task_role_arn": payload.get("taskRoleArn"),
        "execution_role_arn": payload.get("executionRoleArn"),
        "volumes": payload.get("volumes") or [],
        "status": "ACTIVE",
        "registered_at": time.time(),
        "deregistered_at": None,
    }
    stores.ecsctl.set(env, _taskdef_key(family, revision), taskdef)
    return _json({"taskDefinition": _taskdef_wire(taskdef), "tags": []})


def _describe_task_definition(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    ref = payload.get("taskDefinition", "")
    taskdef = _resolve_taskdef_ref(stores, env, ref)
    if taskdef is None:
        return _not_found_taskdef(ref)
    return _json({"taskDefinition": _taskdef_wire(taskdef), "tags": []})


def _deregister_task_definition(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    ref = payload.get("taskDefinition", "")
    taskdef = _resolve_taskdef_ref(stores, env, ref)
    if taskdef is None:
        return _not_found_taskdef(ref)
    taskdef["status"] = "INACTIVE"
    taskdef["deregistered_at"] = time.time()
    stores.ecsctl.set(env, _taskdef_key(taskdef["family"], taskdef["revision"]), taskdef)
    return _json({"taskDefinition": _taskdef_wire(taskdef)})


# --- Service -----------------------------------------------------------------


def _create_service(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    cluster_name = _strip_id(payload.get("cluster"))
    cluster = _cluster(stores, env, cluster_name)
    if cluster is None:
        return _not_found_cluster(cluster_name)
    service_name = payload.get("serviceName", "")
    existing = _service(stores, env, cluster_name, service_name)
    if existing is not None and existing["status"] == "ACTIVE":
        return errors.synth_error(
            "ecs", "InvalidParameterException",
            f"Creation of service was not idempotent. {service_name} already exists", 400,
        )
    taskdef = _resolve_taskdef_ref(stores, env, payload.get("taskDefinition", ""))
    if taskdef is None:
        return _not_found_taskdef(payload.get("taskDefinition", ""))
    # ECS `Tag` wire shape on CreateService: a LIST of lowercase
    # {"key":..., "value":...} dicts (botocore's ecs service-2.json), NOT a
    # map. `odin:node` is agent/hcl.py::_tags_block's canvas-label stamp --
    # stored on the record so LATER reconcile passes (UpdateService, scale-up)
    # still know which keystore identity this service's tasks run as.
    tags = {t["key"]: t.get("value") for t in payload.get("tags") or []}
    service = {
        "cluster_name": cluster_name,
        "service_name": service_name,
        "node_label": tags.get("odin:node"),
        # W2.5: `[{targetGroupArn, containerName, containerPort}, ...]`, stored
        # verbatim. This is what makes odin's ECS behave like real ECS's own
        # SERVICE SCHEDULER: launching a task registers it with these target
        # groups, stopping one deregisters it (`_task_targets` below) -- so a
        # load balancer's upstream list tracks the real task fleet, and
        # terraform never sees (or manages) an individual task target.
        "load_balancers": payload.get("loadBalancers") or [],
        "task_definition_arn": _taskdef_arn(taskdef["family"], taskdef["revision"]),
        "desired_count": int(payload.get("desiredCount") or 0),
        "launch_type": payload.get("launchType") or _DEFAULT_LAUNCH_TYPE,
        "created_at": time.time(),
        "status": "ACTIVE",
        "deleted_at": None,
    }
    stores.ecsctl.set(env, _service_key(cluster_name, service_name), service)
    # The FULL tag dict is persisted (shared `stores.tags`, lambdactl's
    # convention), not just the `odin:node` extraction above -- and set
    # unconditionally, so recreating a name always overwrites any stale tags
    # a deleted prior incarnation left behind. UpdateService has no `tags`
    # param on the real wire (botocore's ecs service-2.json); later edits
    # arrive via TagResource/UntagResource only.
    stores.tags.set(env, f"ecs:{_service_arn(cluster_name, service_name)}", tags)
    # No tasks exist yet at this instant (CreateService just minted the
    # record), so rendering the response before spawning the reconcile
    # thread -- the same ordering ec2compute/lambdactl document for their
    # own async creates -- carries no race here; it's kept for consistency.
    response = _json({"service": _service_wire(stores, env, service)})
    _spawn(_reconcile_service_tasks, stores, env, cluster_name, service_name, runtime, keystore, gateway_port)
    return response


def _describe_services(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    sweep_tasks(stores, env, runtime)
    _sweep_inactive_services(stores, env)
    cluster_name = _strip_id(payload.get("cluster"))
    names = payload.get("services") or []
    selected, failures = [], []
    for raw in names:
        name = _strip_id(raw)
        service = _service(stores, env, cluster_name, name)
        if service is None:
            failures.append({"arn": _service_arn(cluster_name, name), "reason": "MISSING"})
        else:
            selected.append(service)
    return _json({"services": [_service_wire(stores, env, s) for s in selected], "failures": failures})


def _update_service(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    cluster_name = _strip_id(payload.get("cluster"))
    service_name = _strip_id(payload.get("service"), default="")
    service = _service(stores, env, cluster_name, service_name)
    if service is None or service["status"] != "ACTIVE":
        return _not_found_service(service_name)
    if "desiredCount" in payload:
        service["desired_count"] = int(payload["desiredCount"])
    if payload.get("taskDefinition"):
        taskdef = _resolve_taskdef_ref(stores, env, payload["taskDefinition"])
        if taskdef is None:
            return _not_found_taskdef(payload["taskDefinition"])
        service["task_definition_arn"] = _taskdef_arn(taskdef["family"], taskdef["revision"])
    stores.ecsctl.set(env, _service_key(cluster_name, service_name), service)
    response = _json({"service": _service_wire(stores, env, service)})
    _spawn(_reconcile_service_tasks, stores, env, cluster_name, service_name, runtime, keystore, gateway_port)
    return response


def _delete_service(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    cluster_name = _strip_id(payload.get("cluster"))
    service_name = _strip_id(payload.get("service"), default="")
    service = _service(stores, env, cluster_name, service_name)
    if service is None or service["status"] != "ACTIVE":
        return _not_found_service(service_name)
    # A real docker stop/rm is fast (no cold pull involved), so -- unlike
    # CreateService/UpdateService's convergence pass -- stopping the real
    # containers is done synchronously here: by the time this responds,
    # they're genuinely gone, matching "force=true" AWS semantics without
    # needing to model the `force` parameter at all (v1 simplification).
    # The same per-service lock `_reconcile_service_tasks` takes -- real
    # terraform-provider-aws issues UpdateService(desiredCount=0) right
    # before this call, so without the lock its background reconcile thread
    # and this stop loop can race to `docker rm` the same container (see
    # `_lock_for_service`'s docstring -- V5d found this for real).
    with _lock_for_service(stores, env, cluster_name, service_name):
        for task in _tasks_for_service(stores, env, cluster_name, service_name):
            _stop_task(stores, env, task, runtime)
        # The RECORD, unlike the containers, is NOT deleted outright -- see
        # `_INACTIVE_SERVICE_SWEEP_SECONDS`'s docstring: a real `tofu
        # destroy` hangs forever polling DescribeServices for a service
        # that's simply gone, so this module keeps it around,
        # `status="INACTIVE"`, for the provider's own delete-waiter to
        # actually observe.
        service["status"] = "INACTIVE"
        service["deleted_at"] = time.time()
        stores.ecsctl.set(env, _service_key(cluster_name, service_name), service)
    return _json({"service": _service_wire(stores, env, service)})


# --- Tasks ---------------------------------------------------------------


def _list_tasks(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    sweep_tasks(stores, env, runtime)
    cluster_name = _strip_id(payload.get("cluster"))
    tasks = _tasks_for_cluster(stores, env, cluster_name)
    service_name = payload.get("serviceName")
    if service_name:
        tasks = [t for t in tasks if t["service_name"] == service_name]
    family = payload.get("family")
    if family:
        tasks = [t for t in tasks if t["task_definition_arn"].rsplit("/", 1)[-1].rpartition(":")[0] == family]
    wants_stopped = payload.get("desiredStatus") == "STOPPED"
    tasks = [t for t in tasks if (t["last_status"] == "STOPPED") == wants_stopped]
    return _json({"taskArns": [t["task_arn"] for t in tasks], "nextToken": None})


def _describe_tasks(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    sweep_tasks(stores, env, runtime)
    cluster_name = _strip_id(payload.get("cluster"))
    by_arn = {t["task_arn"]: t for t in _tasks_for_cluster(stores, env, cluster_name)}
    selected, failures = [], []
    for ref in payload.get("tasks") or []:
        arn = ref if ref.startswith("arn:") else _task_arn(cluster_name, ref)
        task = by_arn.get(arn)
        if task is None:
            failures.append({"arn": arn, "reason": "MISSING"})
        else:
            selected.append(task)
    return _json({"tasks": [_task_wire(t) for t in selected], "failures": failures})


# --- Tags (TagResource/UntagResource/ListTagsForResource, shared stores.tags,
# JSON-body params only -- ECS's JSON protocol carries `resourceArn`/`tags`/
# `tagKeys` in the body, never query params like Lambda's REST shape) --------


def _tagged_service(stores: SynthStores, env: str, arn: str) -> dict | None:
    """v1 tags SERVICES only -- the one ECS resource odin's own HCL stamps a
    `tags` block on (agent/hcl.py::_tags_block deliberately skips the shared
    cluster and the taskdef, and the TF provider submits the serviceArn for
    `aws_ecs_service` tag ops). Resolve `arn:...:service/{cluster}/{name}`
    back to its record; any other ARN shape answers None -> not-found."""
    parts = arn.rsplit(":", 1)[-1].split("/")
    if len(parts) == 3 and parts[0] == "service":
        return _service(stores, env, parts[1], parts[2])
    return None


def _tag_resource(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    arn = payload.get("resourceArn", "")
    if _tagged_service(stores, env, arn) is None:
        return _not_found_service(arn)
    new_tags = {t["key"]: t.get("value") for t in payload.get("tags") or []}
    stores.tags.set(env, f"ecs:{arn}", {**_tags_for(stores, env, arn), **new_tags})
    return _json({})


def _untag_resource(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    arn = payload.get("resourceArn", "")
    if _tagged_service(stores, env, arn) is None:
        return _not_found_service(arn)
    removed = set(payload.get("tagKeys") or [])
    kept = {k: v for k, v in _tags_for(stores, env, arn).items() if k not in removed}
    stores.tags.set(env, f"ecs:{arn}", kept)
    return _json({})


def _list_tags_for_resource(
    payload: dict, env: str, stores: SynthStores, runtime: TaskRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    arn = payload.get("resourceArn", "")
    if _tagged_service(stores, env, arn) is None:
        return _not_found_service(arn)
    return _json({"tags": _tag_list(_tags_for(stores, env, arn))})


# --- dispatch ----------------------------------------------------------------


_HANDLERS: dict[str, _Handler] = {
    "CreateCluster": _create_cluster,
    "DescribeClusters": _describe_clusters,
    "DeleteCluster": _delete_cluster,
    "RegisterTaskDefinition": _register_task_definition,
    "DescribeTaskDefinition": _describe_task_definition,
    "DeregisterTaskDefinition": _deregister_task_definition,
    "CreateService": _create_service,
    "DescribeServices": _describe_services,
    "UpdateService": _update_service,
    "DeleteService": _delete_service,
    "ListTasks": _list_tasks,
    "DescribeTasks": _describe_tasks,
    "TagResource": _tag_resource,
    "UntagResource": _untag_resource,
    "ListTagsForResource": _list_tags_for_resource,
}


def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    runtime: TaskRuntime | None = None,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response | None:
    """The whole ECS control-plane answer -- same no-backing contract as
    ec2compute/iamctl/ecr/lambdactl. `runtime` is the injectable
    `TaskRuntime` (or a test's fake stand-in with the same `run`/`status`/
    `exit_code`/`stop` shape); production callers (gateway/synth.py) never
    pass one, so a real `TaskRuntime()` is used, mirroring
    ec2compute.py's `vm or InstanceVm()` default. `keystore` + `gateway_port`
    (threaded down from create_gateway_app via synth.pure_answer) let an
    `odin:node`-tagged service's tasks launch with their own gateway creds
    injected (`workload_env`); absent either, no injection happens."""
    op = action.removeprefix("ecs:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("ecs", "InvalidParameterException", f"The action {op} is not valid.", 400)
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    return handler(payload, env, stores, runtime or TaskRuntime(), keystore, gateway_port)
