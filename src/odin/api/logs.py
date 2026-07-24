"""`GET /logs` -- resolve a canvas node label to its real backing
container(s)/VM and return their logs. Observability v1: today a running
workload's logs are simply unreachable and a crash's cause is discarded.
This is the one route both the CLI (`odin logs`) and the UI's Logs tab
fetch-on-demand path hit.

Every outcome is an honest 200 -- an unknown node, a kind with no runnable
backing (vpc/subnet/sg/iam_role/ecr are real API/network primitives, not a
process), or a real backing that simply isn't running yet all answer with
`found`/`running` + a `message`, never a 500 (the same "absent is not an
exception" contract `aws/backings.py::BackingAws.exists` already keeps). An
UNKNOWN node is the one genuine error (an `error` field, so
`cli/http.py::body_or_fail` treats it as a hard failure the same way every
other odin command already does).

Kind -> real backing:
- rds: the direct Postgres container (aws/rds.py::PostgresRds).
- s3/sqs/sns/dynamodb: the env's shared backing container (aws/backings.py).
- ec2: the instance's own Lima VM -- no single process to `docker logs`, so
  this reads its systemd journal (compute/instances.py::InstanceVm.logs).
- lambda: the function's RIE container (compute/functions.py). Always on
  Colima regardless of the app's configured runtime -- FunctionRuntime's own
  default, unchanged here.
- ecs: EVERY task container currently backing the service (compute/tasks.py)
  -- v1's "the drawn node IS the service+taskdef pair" can still mean more
  than one real container when desiredCount > 1, or a crash-looping task
  that's been replaced. Also always on Colima, matching TaskRuntime's own
  default.
- vpc/subnet/sg/iam_role/ecr: no runnable backing at all.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from odin.aws.backings import PROVISIONED, BackingAws
from odin.aws.rds import PostgresRds
from odin.compute import functions as lambda_compute
from odin.compute import instances as ec2_compute
from odin.compute import tasks as ecs_compute
from odin.gateway.stores import SynthStores
from odin.runtime.colima import ColimaRuntime
from odin.spec.store import SpecStore

DEFAULT_TAIL = 100
NO_BACKING_KINDS = frozenset({"vpc", "subnet", "sg", "iam_role", "ecr"})


class LogsResponse(BaseModel):
    env: str
    node: str
    kind: str | None = None
    found: bool = False
    running: bool = False
    sources: list[str] = []
    lines: str = ""
    message: str | None = None
    error: str | None = None


def _resource_kind(store: SpecStore, env: str, node: str) -> str | None:
    """The desired Stack is the primary source (the canvas's own record);
    the observed World is the fallback -- a node just removed from the
    canvas, still winding down, still deserves its last logs."""
    stack = store.get_stack(env)
    desired = next((r.kind for r in stack.resources if r.id == node), None)
    if desired is not None:
        return desired
    observed = store.current_world(env).get(node)
    return observed.kind if observed is not None else None


def _tagged_label(stores: SynthStores, env: str, resource_key: str, natural: str | None = None) -> str | None:
    """tf_status.py's `_label` resolution, used here to find the record a
    label resolves to (not to build the label from a record) -- the exact
    same tag-namespace rules, so a label visible in World always resolves to
    the SAME underlying record here."""
    tags = stores.tags.get(env, resource_key, {})
    return tags.get("odin:node") or natural


def _find_ec2_instance(stores: SynthStores, env: str, node: str) -> dict | None:
    for key, record in stores.ec2compute.items(env).items():
        if not key.startswith("instance:"):
            continue
        if _tagged_label(stores, env, f"ec2:{record['instance_id']}") == node:
            return record
    return None


def _find_lambda_function(stores: SynthStores, env: str, node: str) -> dict | None:
    for key, record in stores.lambdactl.items(env).items():
        if not key.startswith("fn:"):
            continue
        label = _tagged_label(stores, env, f"lambda:{record['function_arn']}", record["function_name"])
        if label == node:
            return record
    return None


def _find_ecs_service(stores: SynthStores, env: str, node: str) -> dict | None:
    for key, record in stores.ecsctl.items(env).items():
        if not key.startswith("service:") or record["status"] != "ACTIVE":
            continue
        label = record.get("node_label") or record["service_name"]
        if label == node:
            return record
    return None


def _ecs_task_containers(stores: SynthStores, env: str, cluster: str, service: str) -> list[tuple[str, str]]:
    """(task_id, container_def_name) for every task this service currently
    owns, most-recently-started first -- a crash-loop's newest attempt (the
    one the user actually cares about) reads first."""
    prefix = f"task:{cluster}:"
    tasks = [
        t for key, t in stores.ecsctl.items(env).items()
        if key.startswith(prefix) and t["service_name"] == service
    ]
    tasks.sort(key=lambda t: t.get("started_at") or 0, reverse=True)
    return [(t["task_id"], t["container_name"]) for t in tasks]


def _from_containers(
    env: str, node: str, kind: str, runtime, names: list[str], tail: int, absent_message: str | None = None,
) -> LogsResponse:
    """Read logs off every name in `names` -- an EXITED container's logs are
    exactly the diagnostic a crash needs, so anything short of `absent`
    (never existed / already removed) still gets its tail read."""
    blocks: list[str] = []
    any_running = any_present = False
    for name in names:
        status = runtime.status(name)
        if status == "absent":
            continue
        any_present = True
        any_running = any_running or status == "running"
        text = runtime.logs(name, tail)
        blocks.append(f"==> {name} <==\n{text}" if len(names) > 1 else text)
    lines = "\n\n".join(b for b in blocks if b)
    if not any_present:
        message = absent_message or f"not running: {', '.join(names)}"
    elif not any_running:
        message = "container exists but is not running (showing its last logs)"
    else:
        message = None
    return LogsResponse(
        env=env, node=node, kind=kind, found=True, running=any_running,
        sources=names, lines=lines, message=message,
    )


def fetch_logs(
    store: SpecStore, stores: SynthStores, runtime, env: str, node: str, tail: int = DEFAULT_TAIL,
) -> LogsResponse:
    kind = _resource_kind(store, env, node)
    if kind is None:
        return LogsResponse(env=env, node=node, error=f"no such node {node!r} in env {env!r}")

    if kind == "rds":
        name = PostgresRds(runtime, env).container_name(node)
        return _from_containers(env, node, kind, runtime, [name], tail)

    if kind in PROVISIONED:
        name = BackingAws(runtime, env).container_name(kind)
        return _from_containers(env, node, kind, runtime, [name], tail)

    if kind == "ec2":
        instance = _find_ec2_instance(stores, env, node)
        if instance is None:
            return LogsResponse(env=env, node=node, kind=kind, found=True, message=f"no EC2 instance backs node {node!r} yet")
        name = ec2_compute.vm_name(env, instance["instance_id"])
        vm = ec2_compute.InstanceVm()
        running = vm.status(name) == "running"
        lines = vm.logs(name, tail) if vm.status(name) != "absent" else ""
        message = None if running else f"{name} is not running (state: {instance['state_name']})"
        return LogsResponse(
            env=env, node=node, kind=kind, found=True, running=running,
            sources=[name], lines=lines, message=message,
        )

    if kind == "lambda":
        fn = _find_lambda_function(stores, env, node)
        if fn is None:
            return LogsResponse(env=env, node=node, kind=kind, found=True, message=f"no Lambda function backs node {node!r} yet")
        name = lambda_compute.container_name(env, fn["function_name"])
        colima = ColimaRuntime()
        return _from_containers(
            env, node, kind, colima, [name], tail,
            absent_message=f"{name} is not running (function state: {fn['state']})",
        )

    if kind == "ecs":
        service = _find_ecs_service(stores, env, node)
        if service is None:
            return LogsResponse(env=env, node=node, kind=kind, found=True, message=f"no ECS service backs node {node!r} yet")
        containers = _ecs_task_containers(stores, env, service["cluster_name"], service["service_name"])
        if not containers:
            return LogsResponse(env=env, node=node, kind=kind, found=True, message="no tasks have run for this service yet")
        colima = ColimaRuntime()
        names = [ecs_compute.container_name(env, task_id, cdef_name) for task_id, cdef_name in containers]
        return _from_containers(env, node, kind, colima, names, tail)

    return LogsResponse(env=env, node=node, kind=kind, found=True, message=f"no logs available for kind {kind!r}")


def create_logs_router(store: SpecStore, stores: SynthStores, runtime) -> APIRouter:
    router = APIRouter()

    @router.get("/logs")
    def logs_route(env: str = "default", node: str = "", tail: int = DEFAULT_TAIL) -> LogsResponse:
        if not node:
            return LogsResponse(env=env, node=node, error="node is required")
        return fetch_logs(store, stores, runtime, env, node, tail)

    return router
