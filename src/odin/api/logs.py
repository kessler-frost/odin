"""`GET /logs` -- resolve a canvas node label to its real backing
container(s)/VM and return their logs, or read one CloudWatch log GROUP
directly (`?group=`). Observability v1: today a running workload's logs are
simply unreachable and a crash's cause is discarded. This is the one route
both the CLI (`odin logs`) and the UI's Logs tab fetch-on-demand path hit.

Every outcome is an honest 200 -- an unknown node, a kind with no runnable
backing (vpc/subnet/sg/iam_role/ecr are real API/network primitives, not a
process), or a real backing that simply isn't running yet all answer with
`found`/`running` + a `message`, never a 500 (the same "absent is not an
exception" contract `aws/backings.py::BackingAws.exists` already keeps).

THREE things are a genuine error instead, and each fills the `error` field so
`cli/http.py::body_or_fail` turns it into a real non-zero exit the way every
other odin command does:
  - an UNKNOWN node;
  - a call naming NEITHER a node nor a group;
  - env state odin itself wrote and can no longer parse (a corrupt
    `.odin/<env>/gateway/<name>.json`). Probed: that makes every store-backed
    kind raise out of `gateway/stores.py::_data`. It is reported rather than
    swallowed BECAUSE the alternative is the worst answer available here -- an
    empty `lines` with `found=True` reads as "this container said nothing",
    when in truth odin never found out which container to ask. See
    `logs_route`.
A DEAD RUNTIME is deliberately not in that list: `ColimaRuntime.status`/`logs`
and `InstanceVm.status`/`logs` are `check=False` throughout, so a machine with
no `docker` on PATH answers `absent`/`""` rather than raising (probed) -- the
node then reports `not running`, which is exactly true.

Kind -> real backing:
- rds: the instance's own Postgres container (aws/rds.py::PostgresRds) --
  still resolved by node LABEL, which is the DBInstanceIdentifier tofu
  created it under (agent/hcl.py's `_rds`), so W2.7 moving rds onto
  Terraform changed nothing here.
- s3/sqs/sns/dynamodb: the env's shared backing container (aws/backings.py).
- ec2: the instance's own Lima VM -- no single process to `docker logs`, so
  this reads its systemd journal (compute/instances.py::InstanceVm.logs).
- lambda: the function's RIE container (compute/functions.py). Always on
  Colima regardless of the app's configured runtime -- FunctionRuntime's own
  default, unchanged here.
- ecs: the LIVE task containers backing the service (compute/tasks.py), plus
  the few most recently stopped ones -- v1's "the drawn node IS the
  service+taskdef pair" can still mean more than one real container when
  desiredCount > 1, or a crash-looping task that's been replaced. Bounded
  (`_ecs_task_containers`): unbounded history meant one docker call per task
  that had EVER run. Also always on Colima, matching TaskRuntime's own default.
- elasticache: the cluster's own `redis:7-alpine` container (aws/cache.py).
- logs: no container of its own -- an `aws_cloudwatch_log_group` node IS the
  SINK, so this reads the events stored under the group whose name is the
  node's label (`gateway/models/logsctl.py::stored_events`).
- vpc/subnet/sg/iam_role/ecr: no runnable backing at all.

`tail` is a budget for the WHOLE response, never per container: a node with
several backing containers honours `--tail N` exactly the way a single-source
one does (see `_from_containers`).

`?group=` bypasses node resolution entirely and reads ANY group in the env's
sink -- including the ones the substrates auto-create without anybody drawing
them (`/aws/lambda/{function}` from an Invoke, `/ecs/{service}` from the task
sweep). It's the read that makes those groups reachable: `odin logs --group
/aws/lambda/foo`. When both `node` and `group` arrive, the group wins (the
caller asked for a specific group) and `node` is echoed back unchanged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from odin.aws import cache as cache_compute
from odin.aws.backings import PROVISIONED, BackingAws
from odin.aws.rds import PostgresRds
from odin.compute import functions as lambda_compute
from odin.compute import instances as ec2_compute
from odin.compute import tasks as ecs_compute
from odin.gateway.models import cachectl, logsctl
from odin.gateway.stores import SynthStores
from odin.spec.store import StoreUnreadable
from odin.runtime.colima import ColimaRuntime
from odin.spec.store import SpecStore

log = logging.getLogger("odin.logs")

DEFAULT_TAIL = 100
NO_BACKING_KINDS = frozenset({"vpc", "subnet", "sg", "iam_role", "ecr"})
# How many already-STOPPED ECS tasks stay readable behind the live ones --
# see `_ecs_task_containers` for why they are bounded rather than dropped.
_MAX_STOPPED_SOURCES = 3


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


def _find_cache_cluster(stores: SynthStores, env: str, node: str) -> dict | None:
    for record in cachectl.clusters(stores, env):
        if _tagged_label(stores, env, f"elasticache:{record['arn']}", record["cache_cluster_id"]) == node:
            return record
    return None


def _ecs_task_containers(stores: SynthStores, env: str, cluster: str, service: str) -> list[tuple[str, str]]:
    """(task_id, container_def_name) for the task containers worth reading:
    every LIVE task most-recently-started first, then at most
    `_MAX_STOPPED_SOURCES` most-recently-stopped ones.

    Field test 3 (MED): this used to return every task record that had ever
    existed. After a few break/recover cycles that was 27 sources for 3 live
    tasks -- one `docker` call each, 0.849s against 0.044s for a single-source
    RDS node, and growing without bound as deployments accumulate (a STOPPED
    record is never deleted: only a DELIBERATE stop removes one, so every
    crash leaves a permanent source behind).

    The dead ones are BOUNDED rather than dropped, deliberately: a
    crash-looping service has zero live tasks, and those final lines are the
    entire diagnostic -- `odin logs` would be empty exactly when it matters
    most. Three is enough to see a loop repeating without the cost growing
    with the service's history."""
    prefix = f"task:{cluster}:"
    tasks = [
        t for key, t in stores.ecsctl.items(env).items()
        if key.startswith(prefix) and t["service_name"] == service
    ]
    live = sorted(
        (t for t in tasks if t["last_status"] != "STOPPED"),
        key=lambda t: t.get("started_at") or 0, reverse=True,
    )
    stopped = sorted(
        (t for t in tasks if t["last_status"] == "STOPPED"),
        key=lambda t: t.get("stopped_at") or t.get("started_at") or 0, reverse=True,
    )
    return [(t["task_id"], t["container_name"]) for t in live + stopped[:_MAX_STOPPED_SOURCES]]


def _tail_lines(text: str, budget: int) -> str:
    """The last `budget` lines of `text`. The driver's own `--tail` is a
    PER-CONTAINER bound, so it alone cannot make `--tail N` mean N lines when
    a node has several containers -- this is what closes the gap exactly."""
    return "\n".join(text.splitlines()[-budget:])


async def _from_containers(
    env: str, node: str, kind: str, runtime, names: list[str], tail: int, absent_message: str | None = None,
) -> LogsResponse:
    """Read logs off every name in `names` -- an EXITED container's logs are
    exactly the diagnostic a crash needs, so anything short of `absent`
    (never existed / already removed) still gets its tail read.

    `tail` is a budget for the WHOLE response, not per container (field test
    3, MED: `--tail 1` on a 3-task service returned 8 lines, while RDS honoured
    it exactly). It is spent newest-source-first, which is the order `names`
    already arrives in -- a crash-loop's latest attempt is the one being asked
    about. The `==> name <==` headers are attribution, not content: they are
    never counted against the budget, and a single-source node has none at all,
    so `odin logs db --tail 2` and `odin logs web --tail 2` mean the same
    thing. Every name stays in `sources` regardless of whether the budget
    reached it -- the inventory of what was consulted must not shrink just
    because the output was trimmed."""
    blocks: list[str] = []
    budget = tail
    any_running = any_present = False
    for name in names:
        status = await runtime.status(name)
        if status == "absent":
            continue
        any_present = True
        any_running = any_running or status == "running"
        text = _tail_lines(await runtime.logs(name, budget), budget) if budget > 0 else ""
        budget -= len(text.splitlines())
        if text:
            blocks.append(f"==> {name} <==\n{text}" if len(names) > 1 else text)
    # Joined with a single newline, not a blank line between blocks: every line
    # of the response is then either a `==>` header or one real log line, which
    # is what makes "--tail N gives N lines" exactly checkable.
    lines = "\n".join(blocks)
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


def _stored_line(event: dict, with_stream: bool) -> str:
    """One stored event as one human line: an ISO-8601 UTC timestamp (the
    stored `timestamp` is CloudWatch's own epoch MILLISECONDS), the stream
    name when the group has more than one (a `/ecs/{service}` group has one
    stream per task -- without the name, two tasks' output would interleave
    unattributably), then the message verbatim."""
    when = datetime.fromtimestamp(event["timestamp"] / 1000, timezone.utc).isoformat(timespec="milliseconds")
    stream = f" [{event['stream']}]" if with_stream else ""
    return f"{when}{stream} {event['message']}"


def fetch_group_logs(
    stores: SynthStores, env: str, group: str, tail: int = DEFAULT_TAIL,
    node: str = "", kind: str | None = None,
) -> LogsResponse:
    """One log GROUP's stored events, newest LAST (the order a terminal reads
    naturally), as one text block. `running` is `True` whenever the group
    exists: a log group is a sink, not a process -- there is nothing here that
    could be "up", and reporting `False` for a group that's holding real
    events would read as a failure that isn't one. `sources` is the stream
    names the rendered events came from."""
    events = logsctl.stored_events(stores, env, group, tail)
    exists = logsctl.group_exists(stores, env, group)
    streams = sorted({event["stream"] for event in events})
    message = None
    if not exists:
        message = f"no log group {group!r} in env {env!r} yet — nothing has been ingested into it"
    elif not events:
        message = f"log group {group!r} exists but has no events yet"
    return LogsResponse(
        env=env, node=node, kind=kind, found=True, running=exists, sources=streams,
        lines="\n".join(_stored_line(event, len(streams) > 1) for event in events),
        message=message,
    )


async def fetch_logs(
    store: SpecStore, stores: SynthStores, runtime, env: str, node: str, tail: int = DEFAULT_TAIL,
) -> LogsResponse:
    kind = _resource_kind(store, env, node)
    if kind is None:
        return LogsResponse(env=env, node=node, error=f"no such node {node!r} in env {env!r}")

    if kind == "rds":
        name = PostgresRds(runtime, env).container_name(node)
        return await _from_containers(env, node, kind, runtime, [name], tail)

    if kind in PROVISIONED:
        name = BackingAws(runtime, env).container_name(kind)
        return await _from_containers(env, node, kind, runtime, [name], tail)

    if kind == "ec2":
        instance = _find_ec2_instance(stores, env, node)
        if instance is None:
            return LogsResponse(env=env, node=node, kind=kind, found=True, message=f"no EC2 instance backs node {node!r} yet")
        name = ec2_compute.vm_name(env, instance["instance_id"])
        vm = ec2_compute.InstanceVm()
        # ONE real status read, and the message attributes each half to
        # whoever said it (field test 2, LOW-12): this used to report `is not
        # running (state: running)` for a deleted VM -- the "not running" came
        # from the live `limactl` check, the "running" from odin's own record,
        # and the sentence contradicted itself. Both are worth showing (a
        # disagreement between them IS the diagnosis), neither may be printed
        # as if it were the other's answer.
        state = await vm.status(name)
        running = state == "running"
        lines = await vm.logs(name, tail) if state != "absent" else ""
        message = None if running else (
            f"{name} is not running (VM state: {state}; odin's record says {instance['state_name']})"
        )
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
        return await _from_containers(
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
        return await _from_containers(env, node, kind, colima, names, tail)

    if kind == "elasticache":
        cluster = _find_cache_cluster(stores, env, node)
        if cluster is None:
            return LogsResponse(env=env, node=node, kind=kind, found=True, message=f"no cache cluster backs node {node!r} yet")
        name = cache_compute.container_name(env, cluster["cache_cluster_id"])
        # Always Colima, matching RedisCache's own default runtime (the same
        # reasoning the lambda/ecs branches above record).
        return await _from_containers(
            env, node, kind, ColimaRuntime(), [name], tail,
            absent_message=f"{name} is not running (cluster status: {cluster['status']})",
        )

    if kind == "logs":
        # A log group's identity IS its name, and odin's canonical resource id
        # is the node label -- so the group this node backs is the one named
        # after it, no tag lookup needed (unlike ec2/lambda/ecs above, whose
        # real ids are server-minted).
        return fetch_group_logs(stores, env, node, tail, node=node, kind=kind)

    return LogsResponse(env=env, node=node, kind=kind, found=True, message=f"no logs available for kind {kind!r}")


def _corrupt_state_files(root: Path, env: str) -> list[str]:
    """Which of this env's gateway state files no longer parse as JSON.

    Run ONLY after a read has already failed, so it costs a healthy request
    nothing. It is what makes the error actionable: the raised
    `JSONDecodeError` carries a line and column but NOT the path, and
    `SynthStores` has ten of these files per env, so without this the user is
    told a byte offset in a file odin declines to name."""
    return sorted(
        str(path) for path in sorted((root / env / "gateway").glob("*.json"))
        if _unparseable(path)
    )


def _unparseable(path: Path) -> bool:
    try:
        json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    return False


def _unreadable_state_error(root: Path, env: str, exc: Exception) -> str:
    # `StoreUnreadable` NAMES its file (gateway/stores.py reads through
    # `spec/store.py::_load` now), so the scan below is only needed for a raw
    # `OSError`/`ValueError` from some other read -- which is why it stayed
    # rather than being deleted outright.
    named = str(getattr(exc, "path", "")) or (
        ", ".join(_corrupt_state_files(root, env)) or f"somewhere under {root / env / 'gateway'}"
    )
    # Accurate for BOTH paths this guard covers: a node read resolves a
    # container, a `?group=` read resolves a sink, and neither could happen.
    return (
        f"odin could not read the gateway state for env {env!r}, so this request could not be "
        f"resolved: {named} -- {type(exc).__name__}: {exc}. Nothing was read, so an empty result "
        f"here would have been a lie."
    )


def create_logs_router(store: SpecStore, stores: SynthStores, runtime) -> APIRouter:
    router = APIRouter()

    @router.get("/logs")
    async def logs_route(
        env: str = "default", node: str = "", group: str = "", tail: int = DEFAULT_TAIL,
    ) -> LogsResponse:
        if not node and not group:
            return LogsResponse(env=env, node=node, error="node or group is required")
        # THE one guard, at the boundary that owns the "every outcome is an
        # honest 200" contract. Probed: a truncated `.odin/<env>/gateway/
        # <name>.json` makes EVERY store-backed kind -- ecs, lambda, ec2,
        # elasticache, logs, and `?group=` -- fail out of
        # `gateway/stores.py::_data`. That read is no longer unguarded: it
        # raises `StoreUnreadable`, which NAMES the file and carries the
        # recovery for its role, and this clause catches that type as well as a
        # raw OSError/ValueError from anything that does not go through
        # `_load`. It must
        # not become a 500 (this route promises not to), and it must NOT
        # become an empty-but-successful log either: that is the false green
        # honesty rule 2 is about. It becomes the `error` field this route
        # already reserves for a genuine failure, which `cli/http.py::
        # body_or_fail` turns into a real non-zero exit.
        try:
            if group:  # an explicit group read wins over node resolution (module docstring)
                return fetch_group_logs(stores, env, group, tail, node=node)
            return await fetch_logs(store, stores, runtime, env, node, tail)
        except (StoreUnreadable, OSError, ValueError) as exc:
            log.warning("could not read gateway state for env %s: %s", env, exc)
            return LogsResponse(env=env, node=node, error=_unreadable_state_error(stores.root, env, exc))

    return router
