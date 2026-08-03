"""V5a -- gateway/models/ecsctl.py: the ECS control-plane model (clusters,
task definitions, services, tasks), built to research-coverage.md §2e's
captured cluster/taskdef surface plus botocore's own ecs service model for
the never-captured service/task shapes (see the module's own docstring).

Same test method as V3a/V4a: every request is a REAL boto3-signed capture
(the `ecs` fixture), every response round-trips through botocore's own JSON
protocol parser, and a FAKE `TaskRuntime` is injected so these are "model
logic tested without containers" unit tests -- V5d's integration test is the
only one that boots real Colima containers.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from contextlib import suppress
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.aws.backings import REGION
from odin.compute.tasks import TaskContainerHandle
from odin.gateway.classify import classify
from odin.gateway.keys import KeyStore
from odin.gateway.models import ecsctl, efsctl, join, logsctl
from odin.gateway.stores import SynthStores
from odin.runtime.colima import CONTAINER_HOST
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
_CONTAINER_DEF = [{
    "name": "app",
    "image": "nginx:alpine",
    "essential": True,
    "portMappings": [{"containerPort": 80, "hostPort": 0, "protocol": "tcp"}],
}]


class FakeTaskRuntime:
    """The TaskRuntime shape (`run`/`status`/`exit_code`/`stop`/`logs`) with no
    Docker involved -- deterministic and near-instant, so the background
    convergence ecsctl.py spawns can be observed with a short poll instead of a
    real container boot.

    Every one of those five is `async def`, because every one of them is
    `async def` on the real `compute/tasks.py::TaskRuntime` (v0.7.7). This is
    load-bearing rather than cosmetic: `_launch_task` used to call
    `runtime.run(...)` WITHOUT awaiting it, so `handle` was a coroutine object
    and `handle.host_ports` was an AttributeError -- a sync fake would hide the
    fix by making the un-awaited call work again. The test-only helpers below
    (`print_line`/`mark_exited`/`vanish`) stay synchronous: they have no
    counterpart on the real class."""

    def __init__(self, fail_run: bool = False, block: asyncio.Event | None = None) -> None:
        self.fail_run = fail_run
        self.block = block
        self.ran: list[tuple] = []
        self.volumes: dict[str, str] = {}
        self.stopped: list[tuple] = []
        self._status: dict[tuple, str] = {}
        self._exit_codes: dict[tuple, int] = {}
        # Stands in for each container's own stdout/stderr, as `docker logs
        # --tail N` would report it (see `print_line`).
        self._logs: dict[tuple, str] = {}

    async def _like_a_real_docker_call(self) -> None:
        """Suspend where the real `TaskRuntime` suspends.

        Every method on the real class shells out to `docker` through
        `asyncio.create_subprocess_exec`, which yields to the event loop. A fake
        whose coroutines never suspend is not merely faster -- it silently
        changes what the concurrency tests measure, because awaiting a coroutine
        that never suspends is just a function call, so "concurrent" workers run
        strictly one after another. Measured on this fake WITHOUT this yield, by
        tracing `asyncio.current_task()` inside
        `test_concurrent_sweeps_and_scale_up_do_not_corrupt_the_store`: 720 sweep
        reads and 3 context switches -- each worker ran to completion before the
        next began, and the interleaving the test exists to survive never
        happened once."""
        await asyncio.sleep(0)

    async def run(
        self, env: str, task_id: str, container_def: dict, extra_env: dict[str, str] | None = None,
        cpu: str | int | None = None, memory: str | int | None = None,
        volumes: dict[str, str] | None = None,
    ) -> TaskContainerHandle:
        # RECORDED, not merely accepted -- a fake that swallowed this kwarg would
        # keep every test here green while proving nothing about the EFS mount
        # the caller resolves. `test_a_task_definitions_efs_volume_reaches_the_
        # container` reads it.
        self.volumes = dict(volumes or {})
        await self._like_a_real_docker_call()
        if self.block is not None:
            # `threading.Event.wait(timeout=5.0)` RETURNS on timeout and lets
            # the launch proceed; it never raises. `asyncio.timeout` + `suppress`
            # is the shape that keeps that behaviour exactly, where a bare
            # `asyncio.wait_for` would turn the 5s cap into a launch FAILURE and
            # quietly change what the gated tests are measuring.
            with suppress(TimeoutError):
                async with asyncio.timeout(5.0):
                    await self.block.wait()
        self.ran.append((env, task_id, container_def, extra_env, cpu, memory))
        if self.fail_run:
            raise RuntimeError("container failed to start")
        key = (env, task_id, container_def["name"])
        self._status[key] = "running"
        ports = {pm["containerPort"]: 10_000 + len(self.ran) for pm in container_def.get("portMappings") or []}
        return TaskContainerHandle(name=f"fake-{task_id}", host_ports=ports)

    async def status(self, env: str, task_id: str, container_name: str) -> str:
        await self._like_a_real_docker_call()
        return self._status.get((env, task_id, container_name), "absent")

    async def exit_code(self, env: str, task_id: str, container_name: str) -> int:
        await self._like_a_real_docker_call()
        return self._exit_codes.get((env, task_id, container_name), 0)

    async def stop(self, env: str, task_id: str, container_name: str) -> None:
        await self._like_a_real_docker_call()
        self.stopped.append((env, task_id, container_name))
        self._status[(env, task_id, container_name)] = "exited"

    async def logs(self, env: str, task_id: str, container_name: str, tail: int = 20) -> str:
        await self._like_a_real_docker_call()
        return self._logs.get((env, task_id, container_name), "")

    def print_line(self, env: str, task_id: str, container_name: str, line: str) -> None:
        """Test-only: the container writes one more line to its own stdout."""
        key = (env, task_id, container_name)
        self._logs[key] = f"{self._logs.get(key, '')}{line}\n"

    def mark_exited(self, env: str, task_id: str, container_name: str, exit_code: int = 1) -> None:
        """Test-only: simulate a container crashing/completing ON ITS OWN --
        distinct from `stop`, which is what ecsctl.py itself calls for a
        DELIBERATE stop (see ecsctl.py's `_stop_task` docstring)."""
        self._status[(env, task_id, container_name)] = "exited"
        self._exit_codes[(env, task_id, container_name)] = exit_code

    def vanish(self, env: str, task_id: str, container_name: str) -> None:
        """Test-only: the container is REMOVED (`docker rm -f`) rather than
        exited -- the real runtime reports `absent` for a name it can't
        inspect, and no exit code exists to read."""
        self._status[(env, task_id, container_name)] = "absent"
        self._exit_codes.pop((env, task_id, container_name), None)


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("ecs")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


@pytest.fixture
def keystore(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path)


async def _answer(stores, req, runtime=None, keystore=None, gateway_port=None) -> Response:
    path, query = split_url(req.url)
    classified = classify("ecs", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a recognized ECS action must never be unmappable"
    action, resource = classified
    response = await ecsctl.pure_answer(
        action, resource, ENV, req.body, stores, time.monotonic(), runtime,
        keystore=keystore, gateway_port=gateway_port,
    )
    assert response is not None, "ecsctl never falls through to None"
    return response


async def _create_cluster(stores, sink, ecs, runtime, name: str = "odin") -> dict:
    # `sink.call` stays SYNCHRONOUS everywhere below: `ecs` is a boto3 client,
    # whose operations are ordinary blocking calls against the capture sink.
    req = sink.call(lambda: ecs.create_cluster(clusterName=name))
    return _parse("CreateCluster", await _answer(stores, req, runtime))["cluster"]


async def _register_taskdef(stores, sink, ecs, runtime, family: str = "app", **kwargs) -> dict:
    kwargs.setdefault("containerDefinitions", _CONTAINER_DEF)
    req = sink.call(lambda: ecs.register_task_definition(family=family, **kwargs))
    return _parse("RegisterTaskDefinition", await _answer(stores, req, runtime))["taskDefinition"]


async def _create_service(stores, sink, ecs, runtime, keystore=None, gateway_port=None, **kwargs) -> dict:
    kwargs.setdefault("cluster", "odin")
    kwargs.setdefault("serviceName", "app")
    kwargs.setdefault("taskDefinition", "app")
    kwargs.setdefault("desiredCount", 1)
    req = sink.call(lambda: ecs.create_service(**kwargs))
    parsed = _parse("CreateService", await _answer(stores, req, runtime, keystore=keystore, gateway_port=gateway_port))
    return parsed["service"]


async def _describe_service(stores, sink, ecs, runtime, cluster: str = "odin", name: str = "app") -> dict:
    req = sink.call(lambda: ecs.describe_services(cluster=cluster, services=[name]))
    parsed = _parse("DescribeServices", await _answer(stores, req, runtime))
    (service,) = parsed["services"]
    return service


async def _wait_for_running_count(stores, sink, ecs, runtime, want: int, timeout: float = 2.0, **kwargs) -> dict:
    """Poll DescribeServices until the background convergence has brought the
    service to `want` running tasks.

    The `await asyncio.sleep` comes FIRST, before the first read (v0.7.7). The
    convergence is an asyncio task now, and unlike the daemon thread it replaced
    it has not run AT ALL until this coroutine yields -- so a read before the
    first yield sees a store nothing has touched yet. That is invisible for
    `want > 0` (the loop just goes round again) but silently fatal for
    `want == 0`, which the pre-convergence state satisfies trivially: measured,
    `test_wait_for_steady_services_names_the_service_the_counts_and_the_reason`
    then reached its assertions with no task record written and no reason to
    report."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        last = await _describe_service(stores, sink, ecs, runtime, **kwargs)
        if last["runningCount"] == want:
            return last
    raise AssertionError(f"service never reached runningCount={want} (last seen {last})")


async def _wait_for_stopped(runtime, task_id: str, timeout: float = 6.0) -> None:
    """Wait for a DELIBERATE stop of `task_id`. Retiring the previous
    revision is deliberately the LAST thing a rollout does, behind
    `ecsctl._ROLLOUT_STABILIZE_SECONDS` (field test 3), so it lands after the
    replacement has already reached RUNNING."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(stopped_id == task_id for _, stopped_id, _ in runtime.stopped):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} was never stopped (stopped: {runtime.stopped})")


# --- Cluster -----------------------------------------------------------------


async def test_create_cluster_is_active_immediately(sink, ecs, stores):
    cluster = await _create_cluster(stores, sink, ecs, FakeTaskRuntime())
    assert cluster["status"] == "ACTIVE"
    assert cluster["clusterName"] == "odin"
    assert cluster["runningTasksCount"] == 0


async def test_create_cluster_is_idempotent_on_existing_name(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    first = await _create_cluster(stores, sink, ecs, runtime)
    second = await _create_cluster(stores, sink, ecs, runtime)
    assert first["clusterArn"] == second["clusterArn"]


async def test_describe_clusters_unknown_name_is_a_failure_not_an_error(sink, ecs, stores):
    req = sink.call(lambda: ecs.describe_clusters(clusters=["ghost"]))
    parsed = _parse("DescribeClusters", await _answer(stores, req, FakeTaskRuntime()))
    assert parsed["clusters"] == []
    assert parsed["failures"][0]["reason"] == "MISSING"


async def test_delete_cluster(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    req = sink.call(lambda: ecs.delete_cluster(cluster="odin"))
    assert _parse("DeleteCluster", await _answer(stores, req, runtime))["cluster"]["clusterName"] == "odin"
    describe_req = sink.call(lambda: ecs.describe_clusters(clusters=["odin"]))
    assert _parse("DescribeClusters", await _answer(stores, describe_req, runtime))["clusters"] == []


async def test_delete_cluster_with_active_services_is_denied(sink, ecs, stores):
    runtime = FakeTaskRuntime(block=asyncio.Event())  # never released -- service stays 0 running, irrelevant here
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime)
    req = sink.call(lambda: ecs.delete_cluster(cluster="odin"))
    response = await _answer(stores, req, runtime)
    assert response.status_code == 400
    parsed = _parse("DeleteCluster", response, error=True)
    assert parsed["Error"]["Code"] == "ClusterContainsServicesException"


# --- TaskDefinition ------------------------------------------------------------


async def test_register_task_definition_starts_at_revision_1_and_increments(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    first = await _register_taskdef(stores, sink, ecs, runtime)
    second = await _register_taskdef(stores, sink, ecs, runtime)
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert first["taskDefinitionArn"].endswith(":1")
    assert second["taskDefinitionArn"].endswith(":2")


async def test_register_task_definition_echoes_container_definitions_verbatim(sink, ecs, stores):
    """THE drift-normalization proof (module docstring's mandate): the exact
    list boto3 sent must come back byte-for-byte identical, no field
    injection/reordering -- research §2e's captured non-zero-drift quirk."""
    runtime = FakeTaskRuntime()
    container_defs = [{
        "name": "app",
        "image": "nginx:alpine",
        "essential": True,
        "portMappings": [{"containerPort": 80, "hostPort": 0, "protocol": "tcp"}],
        "environment": [{"name": "FOO", "value": "bar"}],
    }]
    taskdef = await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=container_defs)
    assert taskdef["containerDefinitions"] == container_defs

    describe_req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="app:1"))
    described = _parse("DescribeTaskDefinition", await _answer(stores, describe_req, runtime))["taskDefinition"]
    assert described["containerDefinitions"] == container_defs


async def test_describe_task_definition_bare_family_resolves_latest_active(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _register_taskdef(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="app"))
    described = _parse("DescribeTaskDefinition", await _answer(stores, req, runtime))["taskDefinition"]
    assert described["revision"] == 2


async def test_deregister_task_definition_marks_inactive_and_bare_family_skips_it(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _register_taskdef(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    deregister_req = sink.call(lambda: ecs.deregister_task_definition(taskDefinition="app:2"))
    deregistered = _parse("DeregisterTaskDefinition", await _answer(stores, deregister_req, runtime))["taskDefinition"]
    assert deregistered["status"] == "INACTIVE"

    req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="app"))
    described = _parse("DescribeTaskDefinition", await _answer(stores, req, runtime))["taskDefinition"]
    assert described["revision"] == 1  # rev 2 is INACTIVE -- "latest ACTIVE" skips it


async def test_a_register_never_overwrites_a_live_revision_it_cannot_see_a_counter_for(sink, ecs, stores):
    """The revision number comes off the `taskdef:` KEYS, never a separate
    `taskdef-rev:` counter that could disagree with them.

    Measured against the real handlers on the old code, after removing just
    the counter key -- the state a user's own repair of `gateway/ecsctl.json`
    leaves behind, and `stores.py::_data`'s docstring is what tells them to
    repair it:

        keys on disk          : ['taskdef:web:1']
        _latest_active_taskdef: None
        register #2 ->  .../task-definition/web:1
        taskdef:web:1 now     : [alpine:3.21]   <- revision 1 OVERWRITTEN
    """
    runtime = FakeTaskRuntime()
    first = await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{"name": "one"}])
    assert first["revision"] == 1
    # Whatever bookkeeping key a previous odin left behind, gone.
    stores.ecsctl.delete(ENV, "taskdef-rev:app")

    assert ecsctl._latest_active_taskdef(stores, ENV, "app")["revision"] == 1
    second = await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{"name": "two"}])

    assert second["revision"] == 2
    assert ecsctl._taskdef(stores, ENV, "app", 1)["container_definitions"] == [{"name": "one"}]


async def test_a_stale_counter_left_by_an_older_odin_cannot_pull_a_revision_backwards(sink, ecs, stores):
    # The other direction of the same shape: a counter that is present but
    # LOWER than the real revisions. Nothing reads it any more, so it cannot
    # steer a register onto a live key.
    runtime = FakeTaskRuntime()
    await _register_taskdef(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    stores.ecsctl.set(ENV, "taskdef-rev:app", 1)

    assert (await _register_taskdef(stores, sink, ecs, runtime))["revision"] == 3


async def test_two_concurrent_registers_for_one_family_get_two_distinct_revisions(stores):
    """No hand-edited store at all: choosing a revision and writing it used to
    be `get()` + `set()`, two separate lock acquisitions, so two registers in
    flight both saw the counter as 0, both claimed revision 1, and the loser's
    task definition simply vanished under the winner's. Measured on the old
    code:

        revisions claimed : {'B': 1, 'A': 1}
        taskdef:app:1     : [{'name': 'A'}]     <- B's registration is gone
        taskdef:app:2     : None

    The claim is now `stores.ecsctl.update`, which writes only while the key is
    still free, plus a re-derive on loss.

    HOW THE WINDOW IS OPENED, and why it changed in v0.7.7. It used to be two
    real threads held at a `threading.Barrier` inside the window between reading
    the store and writing to it -- necessary because simply starting two threads
    did not reproduce the bug (each register finishes before the other begins),
    and a version of this test without the barrier passed against a deliberately
    broken claim. That barrier cannot be carried over, and neither can the race
    it forced: `_register_task_definition` is now a coroutine with NO `await`
    anywhere between the read (`_taskdef_revisions` -> `items`) and the write
    (`update`), so on one event loop the two registers cannot interleave at all,
    and a `threading.Barrier` reached from an asyncio task would deadlock the
    loop rather than rendezvous with it.

    So the same window is opened at the same seam, deterministically: the FIRST
    `items` call from each racer is served the pre-write snapshot -- exactly the
    state the barrier held both threads in ("both saw the counter as 0"). B
    therefore claims revision 1 after A has already written it, loses the atomic
    claim, and must re-derive. `assert served == 2` is the fault injection
    proving the harness fired (honesty rule 4: verify the harness before
    believing the result), and the mutation still bites -- put the old
    `get()`+`set()` claim back and B overwrites A's revision 1."""
    original_items = stores.ecsctl.items
    arrivals = itertools.count()
    frozen: dict[str, dict] = {}
    served = 0

    def items_frozen_at_the_pre_write_view(env: str) -> dict:
        nonlocal served
        # Only the FIRST call from each racer sees the stale view; the loser's
        # retry must read the store as it really is or it could never converge.
        if next(arrivals) < 2:
            served += 1
            return frozen.setdefault(env, original_items(env))
        return original_items(env)

    stores.ecsctl.items = items_frozen_at_the_pre_write_view
    claimed: dict[str, int] = {}

    async def racer(tag: str) -> None:
        payload = {"family": "app", "containerDefinitions": [{"name": tag}]}
        response = await ecsctl._register_task_definition(payload, ENV, stores, FakeTaskRuntime())
        claimed[tag] = json.loads(response.body)["taskDefinition"]["revision"]

    await asyncio.gather(racer("A"), racer("B"))

    assert served == 2, "both registers must really have been held in the window"
    assert sorted(claimed.values()) == [1, 2]
    survivors = {
        ecsctl._taskdef(stores, ENV, "app", revision)["container_definitions"][0]["name"]
        for revision in (1, 2)
    }
    assert survivors == {"A", "B"}, "both registrations must survive"


async def test_register_task_definition_without_a_family_is_refused(sink, ecs, stores):
    """botocore's own ecs model marks `family` required and a real boto3 client
    refuses it client-side, so a request that arrives without one came from a
    raw HTTP client. Accepting it minted `taskdef::1` under the ARN
    `...task-definition/:1` -- a record keyed by nothing, which no later call
    can name and therefore no later call can delete."""
    response = await ecsctl._register_task_definition({"containerDefinitions": []}, ENV, stores, FakeTaskRuntime())

    assert response.status_code == 400
    assert json.loads(response.body)["__type"] == "InvalidParameterException"
    assert b"family" in response.body
    assert stores.ecsctl.items(ENV) == {}


async def test_create_service_without_a_service_name_is_refused(sink, ecs, stores):
    # Same shape, same reason: `serviceName` is required in botocore's model,
    # and accepting an empty one keyed the record `service:default:`.
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)

    response = await ecsctl._create_service({"cluster": "odin", "taskDefinition": "app"}, ENV, stores, runtime)

    assert response.status_code == 400
    assert json.loads(response.body)["__type"] == "InvalidParameterException"
    assert b"serviceName" in response.body
    assert not [key for key in stores.ecsctl.items(ENV) if key.startswith("service:")]


def test_a_not_found_message_names_the_empty_identifier_instead_of_trailing_off(stores):
    # `f"Service not found: {name}"` rendered "Service not found: " -- a
    # non-empty string that communicates nothing, so `errors.exc_text`'s
    # empty-string guard could not help either.
    assert b"Service not found: (none given)" in ecsctl._not_found_service("").body
    assert b"Unable to describe task definition: (none given)" in ecsctl._not_found_taskdef("").body
    assert b"Cluster not found: (none given)" in ecsctl._not_found_cluster("").body


async def test_describe_task_definition_unknown_is_client_exception(sink, ecs, stores):
    req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="ghost"))
    response = await _answer(stores, req, FakeTaskRuntime())
    assert response.status_code == 400
    parsed = _parse("DescribeTaskDefinition", response, error=True)
    assert parsed["Error"]["Code"] == "ClientException"


# --- Service: create / converge / scale ---------------------------------------


async def test_create_service_is_active_immediately_then_converges_running_count(sink, ecs, stores):
    block = asyncio.Event()
    runtime = FakeTaskRuntime(block=block)
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    service = await _create_service(stores, sink, ecs, runtime, desiredCount=2)
    assert service["status"] == "ACTIVE"  # a service is a spec -- ACTIVE immediately
    assert service["desiredCount"] == 2
    assert service["runningCount"] == 0  # no containers exist yet at this instant

    block.set()
    final = await _wait_for_running_count(stores, sink, ecs, runtime, 2)
    assert final["pendingCount"] == 0
    assert len(runtime.ran) == 2
    # A converged deployment reports COMPLETED (finding #3's honest state).
    assert final["deployments"][0]["rolloutState"] == "COMPLETED"
    assert final["events"] == []


async def test_describe_services_reports_failed_deployment_when_a_task_cannot_start(sink, ecs, stores):
    """Field-test finding #3: a task that fails to start (bad image /
    crash-on-boot) must surface a FAILED deployment with the real reason in
    DescribeServices -- not the old hardcoded COMPLETED, which read a broken
    service as healthy and let a bad-image apply silently 'succeed'. Paired with
    the HCL's `wait_for_steady_state`, this is what makes apply fail honestly."""
    runtime = FakeTaskRuntime(fail_run=True)  # every launched container fails
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)

    deadline = time.monotonic() + 2.0
    service = None
    while time.monotonic() < deadline:
        service = await _describe_service(stores, sink, ecs, runtime)
        if service["deployments"][0]["rolloutState"] == "FAILED":
            break
        await asyncio.sleep(0.02)
    assert service["runningCount"] == 0
    (deployment,) = service["deployments"]
    assert deployment["rolloutState"] == "FAILED", service
    assert "failed to start" in deployment["rolloutStateReason"]
    assert service["events"], "a failed deployment posts a service event"


async def test_update_service_scales_up(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=3))
    await _answer(stores, req, runtime)
    await _wait_for_running_count(stores, sink, ecs, runtime, 3)
    assert len(runtime.ran) == 3


async def test_concurrent_sweeps_and_scale_up_do_not_corrupt_the_store(sink, ecs, stores, tmp_path):
    """Release finding #3 -- `_update_task`'s old get()-then-set() pair, plus
    `_sweep_tasks` iterating the store's flat dict while ANOTHER caller
    mutates it (a service scale-up launching+updating several task
    records), is exactly the "dictionary changed size during iteration"
    class of bug. Many concurrent ListTasks calls (each sweeps) racing a
    scale-up must never raise, and the sidecar must stay valid JSON.

    The five workers are asyncio tasks rather than threads (v0.7.7), and the
    overlap is MEASURED rather than assumed -- by tracing which task each
    `runtime` call came from, with the workers otherwise untouched:

        without FakeTaskRuntime._like_a_real_docker_call:
            720 sweep reads, 3 context switches   <- strictly serial
        with it:
            1188 entries, 1183 context switches, and all 4 of the scale-up's
            `run` calls land BETWEEN two sweep reads

    i.e. the fake had to suspend where the real runtime suspends before this was
    a concurrency test at all; see that method's docstring."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=6)
    await _wait_for_running_count(stores, sink, ecs, runtime, 6)

    errors: list[Exception] = []

    # Capture the two request shapes ONCE, single-threaded: `sink.call`'s
    # index-based return (`requests[before]`) is not safe under concurrent
    # callers -- a racing worker's capture can land at `before` first, so the
    # scale-up worker could dispatch a ListTasks body and silently drop the
    # desiredCount=10 update (a rare flake under full-suite load). The
    # concurrency under test -- many dispatches sweeping the store while a
    # scale-up mutates it -- is preserved: every worker still re-dispatches
    # through classify + pure_answer on each iteration.
    list_req = sink.call(lambda: ecs.list_tasks(cluster="odin"))
    update_req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=10))

    async def list_tasks_repeatedly() -> None:
        try:
            for _ in range(30):
                await _answer(stores, list_req, runtime)
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    async def scale_up() -> None:
        try:
            await _answer(stores, update_req, runtime)
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    await asyncio.gather(*[list_tasks_repeatedly() for _ in range(4)], scale_up())

    assert not errors
    await _wait_for_running_count(stores, sink, ecs, runtime, 10)
    sidecar = tmp_path / ENV / "gateway" / "ecsctl.json"
    json.loads(sidecar.read_text())  # raises if truncated/invalid


async def test_update_service_scales_down_newest_task_first(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=3)
    await _wait_for_running_count(stores, sink, ecs, runtime, 3)
    oldest_task_id = runtime.ran[0][1]

    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=1))
    await _answer(stores, req, runtime)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    assert len(runtime.stopped) == 2
    stopped_ids = {task_id for _, task_id, _ in runtime.stopped}
    assert oldest_task_id not in stopped_ids  # the oldest task survives; the two newest were culled

    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app"))
    remaining = _parse("ListTasks", await _answer(stores, tasks_req, runtime))["taskArns"]
    assert len(remaining) == 1
    assert remaining[0].endswith(oldest_task_id)


async def test_update_service_task_definition_replaces_stale_tasks(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    old_task_id = runtime.ran[0][1]

    await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:latest", "essential": True,
    }])  # rev 2
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    await _answer(stores, req, runtime)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    # Field test 3: the stale task is retired AFTER the replacement is up
    # (surge first, retire second), so the replacement reaching RUNNING no
    # longer implies the old one is already gone -- hence the wait.
    await _wait_for_stopped(runtime, old_task_id)
    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app"))
    (task_arn,) = _parse("ListTasks", await _answer(stores, tasks_req, runtime))["taskArns"]
    describe_req = sink.call(lambda: ecs.describe_tasks(cluster="odin", tasks=[task_arn]))
    (task,) = _parse("DescribeTasks", await _answer(stores, describe_req, runtime))["tasks"]
    assert task["taskDefinitionArn"].endswith(":2")


async def test_a_taskdef_update_reports_zero_running_until_the_new_revision_is_up(sink, ecs, stores):
    """Field-test 2 finding B1 (HIGH): a bad-image ECS *update* reported apply
    SUCCESS in 2.3s while taking the service to zero tasks and the load balancer
    to 503.

    Mechanism: terraform-provider-aws's `wait_for_steady_state` waiter keys on
    `len(deployments) == 1 && desiredCount == runningCount`. `runningCount` used
    to count EVERY running task regardless of revision, so at the instant
    UpdateService returned -- before the background reconcile had even started
    -- the three STALE tasks made `runningCount == desiredCount` and the waiter
    declared the deployment stable immediately. `runningCount` must therefore
    count only tasks on the service's CURRENT task definition, so an update is
    never "already steady" the moment it is requested.

    UpdateService's own response is that exact instant (`_update_service`
    renders it before spawning the reconcile), which makes this deterministic."""
    block = asyncio.Event()  # hold the reconcile task in `run`
    runtime = FakeTaskRuntime(block=block)
    block.set()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    await _create_service(stores, sink, ecs, runtime, desiredCount=3)
    await _wait_for_running_count(stores, sink, ecs, runtime, 3)

    await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:this-tag-does-not-exist-9z9z", "essential": True,
    }])  # rev 2
    block.clear()  # the reconcile spawned by UpdateService cannot make progress
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    updated = _parse("UpdateService", await _answer(stores, req, runtime))["service"]

    assert updated["desiredCount"] == 3
    assert updated["runningCount"] == 0, "three stale tasks must not read as the new revision"
    (deployment,) = updated["deployments"]
    assert deployment["taskDefinition"].endswith(":2")
    assert deployment["runningCount"] == 0
    assert deployment["rolloutState"] != "COMPLETED", updated
    block.set()


async def test_a_taskdef_update_that_cannot_start_keeps_reporting_a_failed_deployment(sink, ecs, stores):
    """The other half of B1: once the replacement tasks genuinely fail, the
    service must KEEP reporting short-of-desired for as long as it is short --
    that is what turns the provider's bounded `timeouts.update` into a real,
    honest apply failure instead of a 2.3s success."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    await _create_service(stores, sink, ecs, runtime, desiredCount=2)
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)

    await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:this-tag-does-not-exist-9z9z", "essential": True,
    }])  # rev 2
    runtime.fail_run = True  # the new image cannot be pulled
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    await _answer(stores, req, runtime)

    deadline = time.monotonic() + 2.0
    service = None
    while time.monotonic() < deadline:
        service = await _describe_service(stores, sink, ecs, runtime)
        if service["deployments"][0]["rolloutState"] == "FAILED":
            break
        await asyncio.sleep(0.02)
    (deployment,) = service["deployments"]
    assert deployment["rolloutState"] == "FAILED", service
    assert service["runningCount"] != service["desiredCount"], "would read as steady state"
    assert "failed to start" in deployment["rolloutStateReason"]
    assert service["events"], "a failed deployment posts a real service event"


async def test_a_failed_taskdef_update_keeps_the_previous_revision_serving(sink, ecs, stores):
    """FIELD TEST 3, the flagship claim. Measured before this fix: three
    healthy tasks went to ZERO about four seconds into the apply, because
    `_reconcile_service_tasks` stopped every stale task BEFORE launching a
    single replacement -- so a deployment that could never succeed still
    destroyed the one that had.

    With `minimumHealthyPercent` honored (surge first, retire second), zero
    replacements RUNNING means zero stale tasks retired: the previous
    revision keeps serving for the whole failed apply. Nothing about the
    failure is softened -- see the two asserts on `runningCount` /
    `rolloutState` at the end, which are v0.7.1's loud-failure behavior
    unchanged."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    await _create_service(stores, sink, ecs, runtime, desiredCount=3)
    await _wait_for_running_count(stores, sink, ecs, runtime, 3)
    serving = {entry[1] for entry in runtime.ran}

    await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:this-tag-does-not-exist-9z9z", "essential": True,
    }])  # rev 2
    runtime.fail_run = True  # the new image cannot be pulled
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    await _answer(stores, req, runtime)

    # Well past `_ROLLOUT_STABILIZE_SECONDS`: the retirement decision has been
    # made and declined, not merely not-yet-reached.
    await asyncio.sleep(ecsctl._ROLLOUT_STABILIZE_SECONDS + 1.0)
    stopped = {task_id for _, task_id, _ in runtime.stopped}
    assert not (serving & stopped), f"the previous revision was retired anyway: {serving & stopped}"
    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app", desiredStatus="RUNNING"))
    running = _parse("ListTasks", await _answer(stores, tasks_req, runtime))["taskArns"]
    assert len(running) == 3, "all three previous-revision tasks must still be serving"

    # ... and the apply still fails loudly, on the same clock as v0.7.1.
    service = await _describe_service(stores, sink, ecs, runtime)
    assert service["runningCount"] == 0, "current-revision accounting must stay revision-blind-free"
    assert service["deployments"][0]["rolloutState"] == "FAILED", service


async def test_a_zero_percent_floor_retires_the_previous_revision_immediately(sink, ecs, stores):
    """The floor is genuinely READ, not assumed: a service that asks for
    `minimumHealthyPercent = 0` gets the old take-everything-down-first
    behavior, which is what proves the 100% default is doing the work."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    await _create_service(
        stores, sink, ecs, runtime, desiredCount=2,
        deploymentConfiguration={"minimumHealthyPercent": 0, "maximumPercent": 100},
    )
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)
    old = {entry[1] for entry in runtime.ran}

    await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:this-tag-does-not-exist-9z9z", "essential": True,
    }])  # rev 2
    runtime.fail_run = True
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    await _answer(stores, req, runtime)

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and not old <= {t for _, t, _ in runtime.stopped}:
        await asyncio.sleep(0.02)
    assert old <= {task_id for _, task_id, _ in runtime.stopped}, "a 0% floor keeps nothing serving"


async def test_deployment_configuration_is_echoed_from_what_was_submitted(sink, ecs, stores):
    """It is what the scheduler reads, so DescribeServices must not report a
    number it ignored (the pre-fix hardcoded 200/100 echo was true only by
    coincidence)."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    created = await _create_service(
        stores, sink, ecs, runtime, desiredCount=2,
        deploymentConfiguration={"minimumHealthyPercent": 50, "maximumPercent": 150},
    )
    assert created["deploymentConfiguration"] == {"minimumHealthyPercent": 50, "maximumPercent": 150}

    # Absent on a later call, the service keeps what it has (real ECS's rule).
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=3))
    updated = _parse("UpdateService", await _answer(stores, req, runtime))["service"]
    assert updated["deploymentConfiguration"] == {"minimumHealthyPercent": 50, "maximumPercent": 150}


async def test_default_deployment_configuration_matches_real_aws(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    created = await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    assert created["deploymentConfiguration"] == {"minimumHealthyPercent": 100, "maximumPercent": 200}


async def test_a_successful_taskdef_update_still_reaches_steady_state(sink, ecs, stores):
    """The counterweight to the two above: current-revision-only accounting must
    still CONVERGE, or every healthy update would hang until its timeout."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    await _create_service(stores, sink, ecs, runtime, desiredCount=2)
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)
    old = [entry[1] for entry in runtime.ran]

    await _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:1.27-alpine", "essential": True,
    }])  # rev 2
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    await _answer(stores, req, runtime)

    final = await _wait_for_running_count(stores, sink, ecs, runtime, 2)
    (deployment,) = final["deployments"]
    assert deployment["rolloutState"] == "COMPLETED", final
    assert deployment["taskDefinition"].endswith(":2")
    # Field test 3: a GOOD update still fully retires the previous revision --
    # keeping the old tasks serving is a failure path, never a leak.
    for task_id in old:
        await _wait_for_stopped(runtime, task_id)


async def test_delete_service_stops_all_its_tasks(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=2)
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)

    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    deleted = _parse("DeleteService", await _answer(stores, req, runtime))["service"]
    assert deleted["status"] == "INACTIVE"
    assert len(runtime.stopped) == 2

    # LOAD-BEARING (V5d): a real tofu destroy's own delete-waiter polls
    # DescribeServices expecting to see status="INACTIVE" on a successfully
    # DESCRIBED service -- not a "MISSING" failure, which the real Go-SDK
    # provider treats as "not ready yet" and retries forever (see
    # ecsctl.py's `_INACTIVE_SERVICE_SWEEP_SECONDS` docstring). So the
    # record must still describe cleanly, INACTIVE, right after delete.
    describe_req = sink.call(lambda: ecs.describe_services(cluster="odin", services=["app"]))
    parsed = _parse("DescribeServices", await _answer(stores, describe_req, runtime))
    assert parsed["failures"] == []
    (described,) = parsed["services"]
    assert described["status"] == "INACTIVE"
    assert described["runningCount"] == 0


async def test_delete_service_lets_the_same_name_be_recreated_immediately(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    await _answer(stores, req, runtime)

    recreated = await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    assert recreated["status"] == "ACTIVE"


async def test_delete_cluster_after_service_delete_is_allowed(sink, ecs, stores):
    """An INACTIVE (recently-deleted) service must NOT block cluster
    deletion -- only a still-ACTIVE one does (see
    test_delete_cluster_with_active_services_is_denied)."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    await _answer(stores, req, runtime)

    req = sink.call(lambda: ecs.delete_cluster(cluster="odin"))
    response = await _answer(stores, req, runtime)
    assert response.status_code == 200


# --- Workload creds injection (odin:node tag -> per-node keystore creds) -------


async def test_create_service_with_odin_node_tag_injects_workload_creds(sink, ecs, stores, keystore):
    """A service tagged `odin:node` (iac/hcl.py's `_tags_block` stamp)
    launches its REAL task containers with the four AWS-SDK env vars layered
    on via `extra_env` -- so the container can call odin's own gateway AS
    ITSELF -- while the stored taskdef stays byte-for-byte untouched."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    assert stores.ecsctl.get(ENV, "service:odin:app")["node_label"] == "myservice"
    (_, _, container_def, extra_env, _, _) = runtime.ran[0]
    access_key, secret_key = keystore.issue(ENV, "myservice")
    assert extra_env == {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_ENDPOINT_URL": f"http://{CONTAINER_HOST}:4266",
        "AWS_DEFAULT_REGION": REGION,
    }
    # Zero-drift mandate: the creds ride in via extra_env ONLY -- the taskdef's
    # own containerDefinitions (stored + echoed verbatim) must not gain them.
    assert container_def.get("environment") is None


async def test_update_service_scale_up_injects_the_same_stable_creds(sink, ecs, stores, keystore):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=2))
    await _answer(stores, req, runtime, keystore=keystore, gateway_port=4266)
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)

    access_key, _ = keystore.issue(ENV, "myservice")
    assert len(runtime.ran) == 2
    for _, _, _, extra_env, _, _ in runtime.ran:
        assert extra_env["AWS_ACCESS_KEY_ID"] == access_key  # stable identity, never a second mint


async def test_create_service_without_keystore_keeps_prior_behavior(sink, ecs, stores):
    """REGRESSION: today's callers pass no keystore/gateway_port -- the tag
    may be present, but no creds are injected and nothing crashes."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, tags=[{"key": "odin:node", "value": "myservice"}])
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    (_, _, _, extra_env, _, _) = runtime.ran[0]
    assert not extra_env


# --- Canvas WIRING: the node's own `env` map reaches the real container -------


def _seed_db_and_stack(stores, tmp_path, env_map: dict) -> None:
    """A real, `available` rds record in the gateway's own store plus an applied
    Stack whose ecs node carries `env_map` -- the two inputs
    `gateway/wiring.py` resolves against."""
    stores.rdsctl.set(ENV, "db:appdb", {
        "db_instance_identifier": "appdb", "master_username": "app", "master_password": "s3cret",
        "db_name": "shop", "status": "available", "endpoint_port": 33366,
    })
    SpecStore(tmp_path).apply(canvas_to_stack({
        "nodes": [
            {"id": "n1", "type": "rds", "data": {"label": "appdb"}},
            {"id": "n2", "type": "ecs", "data": {"label": "myservice", "image": "nginx:alpine", "env": env_map}},
        ],
        "edges": [],
    }, env=ENV))


async def test_task_containers_launch_with_the_nodes_env_and_resolved_refs(sink, ecs, stores, keystore, tmp_path):
    """Field test 2, "the product hole": an ECS node's `env` -- static entries
    AND `${{producer.ATTR}}` refs -- was silently dropped, so there was no
    canvas-driven way to hand a container its connection strings. It now rides
    the same launch-time seam the issued credentials already use, so nothing
    resolved ever enters the taskdef (and therefore tofu state)."""
    _seed_db_and_stack(stores, tmp_path, {"DATABASE_URL": "${{appdb.DATABASE_URL}}", "APP_TIER": "web"})
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    (_, _, container_def, extra_env, _, _) = runtime.ran[0]
    assert extra_env["DATABASE_URL"] == f"postgresql://app:s3cret@{CONTAINER_HOST}:33366/shop"
    assert extra_env["APP_TIER"] == "web"
    # The issued creds still win, and the taskdef is still byte-for-byte clean:
    # a resolved connection string carries the DB PASSWORD, and the taskdef is
    # echoed into tofu state verbatim.
    assert extra_env["AWS_ACCESS_KEY_ID"] == keystore.issue(ENV, "myservice")[0]
    assert container_def.get("environment") is None


async def test_odins_own_aws_vars_win_over_a_canvas_that_names_them(sink, ecs, stores, keystore, tmp_path):
    _seed_db_and_stack(stores, tmp_path, {"AWS_DEFAULT_REGION": "eu-west-1"})
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    (_, _, _, extra_env, _, _) = runtime.ran[0]
    assert extra_env["AWS_DEFAULT_REGION"] == REGION, "the gateway wiring must not be overridable"


async def test_an_unresolvable_ref_fails_the_task_with_a_naming_reason(sink, ecs, stores, keystore, tmp_path):
    """Never an empty string: the task goes STOPPED with the real reason, which
    makes the node `crashed` with a naming verdict AND (since the service is
    short of desired) fails the apply."""
    stores.rdsctl.set(ENV, "db:appdb", {
        "db_instance_identifier": "appdb", "master_username": "app", "master_password": "s3cret",
        "db_name": "shop", "status": "creating", "endpoint_port": 0,  # NOT available yet
    })
    SpecStore(tmp_path).apply(canvas_to_stack({
        "nodes": [
            {"id": "n1", "type": "rds", "data": {"label": "appdb"}},
            {"id": "n2", "type": "ecs", "data": {
                "label": "myservice", "image": "nginx:alpine",
                "env": {"DATABASE_URL": "${{appdb.DATABASE_URL}}"}}},
        ],
        "edges": [],
    }, env=ENV))
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )

    deadline = time.monotonic() + 2.0
    service = None
    while time.monotonic() < deadline:
        service = await _describe_service(stores, sink, ecs, runtime)
        if service["deployments"][0]["rolloutState"] == "FAILED":
            break
        await asyncio.sleep(0.02)
    (deployment,) = service["deployments"]
    assert deployment["rolloutState"] == "FAILED", service
    assert "DATABASE_URL" in deployment["rolloutStateReason"], deployment
    assert "appdb" in deployment["rolloutStateReason"], deployment
    assert not runtime.ran, "no container may be started with a hole in its environment"


async def test_create_service_without_tags_launches_with_no_injected_creds(sink, ecs, stores, keystore):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    assert stores.ecsctl.get(ENV, "service:odin:app")["node_label"] is None
    (_, _, _, extra_env, _, _) = runtime.ran[0]
    assert not extra_env


# --- Tags: TagResource/UntagResource/ListTagsForResource + describe echo -------


async def _service_setup_with_tags(sink, ecs, stores, tags: list[dict] | None = None) -> tuple[FakeTaskRuntime, str]:
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    kwargs = {"tags": tags} if tags is not None else {}
    service = await _create_service(stores, sink, ecs, runtime, **kwargs)
    return runtime, service["serviceArn"]


async def test_describe_services_echoes_create_service_tags(sink, ecs, stores):
    """THE recorded-drift kill: a `tags` block on `aws_ecs_service` must come
    back from DescribeServices exactly as submitted (the wire's own
    list-of-lowercase-{key,value} shape), so a subsequent `tofu plan` sees no
    diff -- ROADMAP's old 'tags aren't echoed back' v1 limit."""
    tags = [{"key": "odin:node", "value": "myservice"}, {"key": "team", "value": "platform"}]
    runtime, _ = await _service_setup_with_tags(sink, ecs, stores, tags)
    service = await _describe_service(stores, sink, ecs, runtime)
    assert service["tags"] == tags


async def test_tag_untag_list_round_trip(sink, ecs, stores):
    runtime, arn = await _service_setup_with_tags(sink, ecs, stores, [{"key": "team", "value": "platform"}])

    tag_req = sink.call(lambda: ecs.tag_resource(resourceArn=arn, tags=[
        {"key": "env", "value": "prod"}, {"key": "team", "value": "core"},
    ]))
    assert (await _answer(stores, tag_req, runtime)).status_code == 200

    list_req = sink.call(lambda: ecs.list_tags_for_resource(resourceArn=arn))
    listed = _parse("ListTagsForResource", await _answer(stores, list_req, runtime))["tags"]
    assert listed == [{"key": "team", "value": "core"}, {"key": "env", "value": "prod"}]  # merged, last write wins

    untag_req = sink.call(lambda: ecs.untag_resource(resourceArn=arn, tagKeys=["team"]))
    assert (await _answer(stores, untag_req, runtime)).status_code == 200

    relist_req = sink.call(lambda: ecs.list_tags_for_resource(resourceArn=arn))
    assert _parse("ListTagsForResource", await _answer(stores, relist_req, runtime))["tags"] == [{"key": "env", "value": "prod"}]
    assert (await _describe_service(stores, sink, ecs, runtime))["tags"] == [{"key": "env", "value": "prod"}]


async def test_tag_ops_on_unknown_service_arn_are_not_found(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    ghost = ecsctl._service_arn("odin", "ghost")
    req = sink.call(lambda: ecs.list_tags_for_resource(resourceArn=ghost))
    response = await _answer(stores, req, runtime)
    assert response.status_code == 400
    parsed = _parse("ListTagsForResource", response, error=True)
    assert parsed["Error"]["Code"] == "ServiceNotFoundException"


async def test_recreate_service_overwrites_stale_tags(sink, ecs, stores):
    """Create-with-tags -> delete -> recreate WITHOUT tags must describe as
    untagged: CreateService's tag write is authoritative, never a merge with
    a deleted prior incarnation's leftovers (which would itself be drift)."""
    runtime, _ = await _service_setup_with_tags(sink, ecs, stores, [{"key": "team", "value": "platform"}])
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    delete_req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    await _answer(stores, delete_req, runtime)

    recreated = await _create_service(stores, sink, ecs, runtime)
    assert recreated["tags"] == []
    assert (await _describe_service(stores, sink, ecs, runtime))["tags"] == []


# --- Tasks: lazy sweep (spontaneous exit) --------------------------------------


async def test_describe_tasks_lazily_marks_a_spontaneously_exited_container_stopped(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]

    runtime.mark_exited(ENV, task_id, "app", exit_code=137)

    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app", desiredStatus="RUNNING"))
    tasks_req_body = sink.call(lambda: ecs.describe_tasks(cluster="odin", tasks=[f"arn:aws:ecs:us-east-1:000000000000:task/odin/{task_id}"]))
    (task,) = _parse("DescribeTasks", await _answer(stores, tasks_req_body, runtime))["tasks"]
    assert task["lastStatus"] == "STOPPED"
    assert task["containers"][0]["exitCode"] == 137
    assert task["stoppedReason"]

    running_tasks = _parse("ListTasks", await _answer(stores, tasks_req, runtime))["taskArns"]
    assert running_tasks == []

    service = await _describe_service(stores, sink, ecs, runtime)
    assert service["runningCount"] == 0


# --- W2.2: an Apply re-converges a service whose task is gone. A task is not
# a TF resource, so tofu's plan for an unchanged `aws_ecs_service` is empty
# and only this pass can bring the container back. --------------------------


async def test_mark_task_stopped_records_the_drift_reason_with_no_invented_exit_code(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]

    await ecsctl.mark_task_stopped(stores, ENV, "odin", task_id, "container gone — re-Apply to recreate")

    task = stores.ecsctl.get(ENV, f"task:odin:{task_id}")
    assert task["last_status"] == "STOPPED"
    assert task["stopped_reason"] == "container gone — re-Apply to recreate"
    assert task["exit_code"] is None  # a container that no longer exists never reported one
    assert (await _describe_service(stores, sink, ecs, runtime))["runningCount"] == 0


async def test_converge_services_relaunches_a_task_whose_container_is_gone(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]
    await ecsctl.mark_task_stopped(stores, ENV, "odin", task_id, "removed outside odin")

    # `converge_services` returns the background tasks it started, so the Apply
    # can WAIT for the convergence it asked for -- `join` is the same primitive
    # `wait_for_steady_services` uses, and awaiting it is stricter than polling.
    await join(await ecsctl.converge_services(stores, ENV, runtime), 5.0)  # what an Apply now does

    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    assert len(runtime.ran) == 2, "the missing task must be relaunched, not left short"


async def test_converge_services_is_a_no_op_at_desired_count(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=2)
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)

    await join(await ecsctl.converge_services(stores, ENV, runtime), 5.0)
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)

    assert len(runtime.ran) == 2  # idempotent: every Apply must not stack up containers
    assert runtime.stopped == []


async def test_converge_services_leaves_a_deleted_service_alone(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    delete_req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _parse("DeleteService", await _answer(stores, delete_req, runtime))
    launched = len(runtime.ran)

    # An empty-canvas Apply's teardown.
    await join(await ecsctl.converge_services(stores, ENV, runtime), 5.0)

    await asyncio.sleep(0.1)
    assert len(runtime.ran) == launched, "an INACTIVE service must never be re-launched"


async def test_converge_services_relaunches_a_task_whose_container_exited(sink, ecs, stores):
    """Field test 3 (HIGH): the Apply's own convergence pass must SEE reality
    before it decides there is nothing to do -- a container that exited on its
    own still reads RUNNING in the store until something sweeps it, so without
    the sweep at the head of `converge_services` the Apply looked at a full
    task list and launched nothing while the service was really at zero."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]
    runtime.mark_exited(ENV, task_id, "app", exit_code=137)  # crashed on its own

    await join(await ecsctl.converge_services(stores, ENV, runtime), 5.0)

    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    assert len(runtime.ran) == 2, "the exited task must be swept STOPPED, then relaunched"


# --- Field test 3 (HIGH): a no-op Apply must not report success at zero tasks ---


async def test_wait_for_steady_services_is_silent_at_desired_count(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=2)
    await _wait_for_running_count(stores, sink, ecs, runtime, 2)

    started = time.monotonic()
    assert await ecsctl.wait_for_steady_services(stores, ENV, runtime) == []
    assert time.monotonic() - started < 1.0, "a healthy service must not cost the apply a wait"


async def test_wait_for_steady_services_names_the_service_the_counts_and_the_reason(sink, ecs, stores):
    """THE field-test-3 bug: an Apply tofu sees as a no-op, on a service that
    is already short of desired. The shortfall must name the node, what it
    observed (running vs desired) and the real underlying reason."""
    runtime = FakeTaskRuntime(fail_run=True)
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=3)
    await _wait_for_running_count(stores, sink, ecs, runtime, 0)

    started = time.monotonic()
    (short,) = await ecsctl.wait_for_steady_services(stores, ENV, runtime)
    elapsed = time.monotonic() - started

    assert short.node == "app"
    assert (short.running, short.desired) == (0, 3)
    assert "container failed to start" in (short.reason or ""), short
    assert elapsed < 10, f"nothing is pending -- this must fail fast, took {elapsed:.1f}s"


async def test_wait_for_steady_services_waits_out_a_slow_start(sink, ecs, stores):
    """A task legitimately takes seconds to come up: the wait must join the
    convergence it is verifying instead of failing a service that is still
    launching."""
    block = asyncio.Event()
    runtime = FakeTaskRuntime(block=block)
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)

    async def release_after(delay: float) -> None:
        """`threading.Timer(0.3, block.set)` was the thread-era spelling."""
        await asyncio.sleep(delay)
        block.set()

    releaser = asyncio.create_task(release_after(0.3))

    converging = await ecsctl.converge_services(stores, ENV, runtime)
    assert await ecsctl.wait_for_steady_services(stores, ENV, runtime, converging) == []
    await releaser


async def test_wait_for_steady_services_is_bounded(sink, ecs, stores, monkeypatch):
    """A service that never converges fails the apply inside the budget rather
    than hanging it -- `ODIN_ECS_STEADY_TIMEOUT` is the knob."""
    monkeypatch.setenv("ODIN_ECS_STEADY_TIMEOUT", "0.5")
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    # A task stuck PROVISIONING forever: pending, so the fast path can't fire.
    task_key = next(k for k in stores.ecsctl.items(ENV) if k.startswith("task:"))
    stores.ecsctl.set(ENV, task_key, {**stores.ecsctl.get(ENV, task_key), "last_status": "PROVISIONING"})

    started = time.monotonic()
    (short,) = await ecsctl.wait_for_steady_services(stores, ENV, runtime)
    assert time.monotonic() - started < 5.0
    assert (short.running, short.desired) == (0, 1)
    assert short.reason is None, "nothing has failed yet -- inventing a reason would be a lie"


async def test_wait_for_steady_services_ignores_a_deleted_service(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    delete_req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _parse("DeleteService", await _answer(stores, delete_req, runtime))

    assert await ecsctl.wait_for_steady_services(stores, ENV, runtime) == []


# --- W2.1 piece 3: the sweep ships each task's tail into /ecs/{service} ---------


async def _running_service(sink, ecs, stores) -> tuple[FakeTaskRuntime, str]:
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    return runtime, runtime.ran[0][1]


def _shipped(stores) -> list[dict]:
    return logsctl.stored_events(stores, ENV, "/ecs/app", 100)


async def test_sweep_ships_a_task_container_tail_into_the_service_log_group(sink, ecs, stores):
    runtime, task_id = await _running_service(sink, ecs, stores)
    runtime.print_line(ENV, task_id, "app", "nginx: ready to accept connections")

    await _describe_service(stores, sink, ecs, runtime)  # every Describe* sweeps

    events = _shipped(stores)
    assert [e["message"] for e in events] == ["nginx: ready to accept connections"]
    # One stream per real task container (ecsctl.py's `_ship_task_logs`).
    assert {e["stream"] for e in events} == {f"odin-ecs-default-{task_id[:8]}-app"}
    assert logsctl.group_exists(stores, ENV, "/ecs/app")  # auto-created by ingestion


async def test_resweeping_the_same_tail_never_duplicates_events(sink, ecs, stores):
    runtime, task_id = await _running_service(sink, ecs, stores)
    runtime.print_line(ENV, task_id, "app", "started")

    for _ in range(3):  # a Describe* per reconciler tick, over and over
        await _describe_service(stores, sink, ecs, runtime)
    assert [e["message"] for e in _shipped(stores)] == ["started"]

    runtime.print_line(ENV, task_id, "app", "handled a request")
    await _describe_service(stores, sink, ecs, runtime)
    assert [e["message"] for e in _shipped(stores)] == ["started", "handled a request"]


async def test_sweep_captures_the_final_lines_of_a_task_that_already_exited(sink, ecs, stores):
    """The crash diagnostic: shipping runs BEFORE the RUNNING-only status
    check, so a container that died on its own still hands over its last
    output -- and keeps handing over nothing new on later sweeps."""
    runtime, task_id = await _running_service(sink, ecs, stores)
    runtime.print_line(ENV, task_id, "app", "FATAL: config missing")
    runtime.mark_exited(ENV, task_id, "app", exit_code=1)

    service = await _describe_service(stores, sink, ecs, runtime)
    assert service["runningCount"] == 0  # the sweep also demoted it, as before
    assert [e["message"] for e in _shipped(stores)] == ["FATAL: config missing"]

    await _describe_service(stores, sink, ecs, runtime)
    assert len(_shipped(stores)) == 1


# --- W2.5: the service scheduler's load-balancer half ---------------------------
# Real ECS (never terraform) registers a TASK with the target groups its service
# names, and deregisters it when the task goes away. These tests pin the contract
# ecsctl OWNS -- which target group, and which REAL published host port -- with
# elbv2ctl's own side stubbed out, so no nginx container is involved. elbv2ctl's
# half (upstream rendering, proxy reload) is tested in test_elbv2ctl.py.

_TG_ARN = "arn:aws:elasticloadbalancing:us-east-1:000000000000:targetgroup/web-lb-tg/abc123"
_LOAD_BALANCERS = [{"targetGroupArn": _TG_ARN, "containerName": "app", "containerPort": 80}]


@pytest.fixture
def target_calls(monkeypatch) -> dict[str, list[tuple]]:
    calls: dict[str, list[tuple]] = {"register": [], "deregister": []}

    # `async def`, not a lambda: `_register_task_targets`/`_deregister_task_targets`
    # AWAIT these, and a sync stand-in would make the await a TypeError on None.
    async def register(stores, env, arn, target_id, port) -> None:
        calls["register"].append((env, arn, target_id, port))

    async def deregister(stores, env, arn, target_id, port) -> None:
        calls["deregister"].append((env, arn, target_id, port))

    monkeypatch.setattr(ecsctl.elbv2ctl, "register_target", register)
    monkeypatch.setattr(ecsctl.elbv2ctl, "deregister_target", deregister)
    return calls


async def _lb_service(sink, ecs, stores, desired: int = 1) -> FakeTaskRuntime:
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=desired, loadBalancers=_LOAD_BALANCERS)
    await _wait_for_running_count(stores, sink, ecs, runtime, desired)
    return runtime


async def test_describe_services_echoes_the_load_balancers_it_was_created_with(sink, ecs, stores, target_calls):
    """Hardcoding `loadBalancers: []` (this module's own first cut) drifts an
    `aws_ecs_service` with a `load_balancer` block on every subsequent plan."""
    runtime = await _lb_service(sink, ecs, stores)
    service = await _describe_service(stores, sink, ecs, runtime)
    assert service["loadBalancers"] == _LOAD_BALANCERS


async def test_launching_a_task_registers_its_real_published_port_as_a_target(sink, ecs, stores, target_calls):
    await _lb_service(sink, ecs, stores, desired=2)
    # Registration TRAILS the running count: a task is running for a moment
    # before it joins the rotation (real ECS behaves the same way), so waiting
    # on `runningCount` alone raced and saw only the first port ~1 run in 4.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(target_calls["register"]) < 2:
        await asyncio.sleep(0.02)
    # The FakeTaskRuntime publishes containerPort 80 on 10001 / 10002.
    assert sorted(target_calls["register"]) == [
        (ENV, _TG_ARN, CONTAINER_HOST, 10_001),
        (ENV, _TG_ARN, CONTAINER_HOST, 10_002),
    ]
    assert target_calls["deregister"] == []


async def test_a_service_with_no_load_balancers_never_touches_elbv2(sink, ecs, stores, target_calls):
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    assert target_calls == {"register": [], "deregister": []}


async def test_scaling_down_deregisters_the_stopped_task(sink, ecs, stores, target_calls):
    runtime = await _lb_service(sink, ecs, stores, desired=2)
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=1))
    _parse("UpdateService", await _answer(stores, req, runtime))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not target_calls["deregister"]:
        await asyncio.sleep(0.02)
    # Newest-task-first scale-down, so the SECOND task's port leaves rotation.
    assert target_calls["deregister"] == [(ENV, _TG_ARN, CONTAINER_HOST, 10_002)]


async def test_deleting_the_service_deregisters_every_task(sink, ecs, stores, target_calls):
    runtime = await _lb_service(sink, ecs, stores, desired=2)
    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _parse("DeleteService", await _answer(stores, req, runtime))
    assert sorted(target_calls["deregister"]) == [
        (ENV, _TG_ARN, CONTAINER_HOST, 10_001),
        (ENV, _TG_ARN, CONTAINER_HOST, 10_002),
    ]


async def test_a_task_that_dies_on_its_own_leaves_the_rotation(sink, ecs, stores, target_calls):
    """A dead container left in the upstream list is a real load-balancer bug --
    the sweep that demotes it to STOPPED must also take it out."""
    runtime = await _lb_service(sink, ecs, stores)
    task_id = runtime.ran[0][1]
    runtime.mark_exited(ENV, task_id, "app", exit_code=137)
    service = await _describe_service(stores, sink, ecs, runtime)
    assert service["runningCount"] == 0
    assert target_calls["deregister"] == [(ENV, _TG_ARN, CONTAINER_HOST, 10_001)]
    # Only once -- a later sweep sees a task that's already STOPPED.
    await _describe_service(stores, sink, ecs, runtime)
    assert len(target_calls["deregister"]) == 1


async def test_drift_marking_a_task_stopped_also_deregisters_it(sink, ecs, stores, target_calls):
    runtime = await _lb_service(sink, ecs, stores)
    task_id = runtime.ran[0][1]
    await ecsctl.mark_task_stopped(stores, ENV, "odin", task_id, "container removed outside odin")
    assert target_calls["deregister"] == [(ENV, _TG_ARN, CONTAINER_HOST, 10_001)]


async def test_sweep_marks_a_task_whose_container_vanished(sink, ecs, stores):
    """Field test 4: one of three serving containers was `docker rm -f`'d 20s
    into a 63s apply and `/world` still said 3 for 57s. The drift sweep is the
    only other thing that notices a vanished container, and during an apply it
    serves a cache -- so this passive read has to catch it."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]

    runtime.vanish(ENV, task_id, "app")  # gone, not exited
    await ecsctl.sweep_tasks(stores, ENV, runtime)

    task = stores.ecsctl.get(ENV, f"task:odin:{task_id}")
    assert task["last_status"] == "STOPPED"
    assert task["exit_code"] is None, "a container that no longer exists never reported one"
    # The SHARED wording -- reconcile/drift.py's reality sweep races this
    # passive path for the identical event, and they must not disagree about
    # what the user is told (a test asserting one of two sentences flaked).
    #
    # SPELLED OUT IN FULL, DELIBERATELY. This used to read
    # `== ecsctl.container_gone_reason(task["container_name"])`, which derives
    # the expectation from the same expression the source uses -- so it stayed
    # green for months while the source passed the TASK DEFINITION's name
    # ("web") where drift.py passed the real container. The literal below is
    # the user-facing contract: the verdict names something `docker inspect`
    # would find. Pairs with tests/reconcile/test_drift.py's assertion on the
    # other writer; together they pin the two to ONE string.
    assert task["stopped_reason"] == (
        f"container odin-ecs-{ENV}-{task_id[:8]}-{task['container_name']} "
        "removed outside odin — re-Apply to recreate"
    )
    assert (await _describe_service(stores, sink, ecs, runtime))["runningCount"] == 0


# --- efs: `taskdef["volumes"]` finally gets READ -----------------------------
#
# It has been stored (`_register_task_definition`) and echoed
# (`_taskdef_json`) since ECS landed, and read by absolutely nothing. These
# tests are about the join that reads it reaching a REAL container spec, and
# about the two ways it must refuse rather than mount an empty directory.


async def _a_file_system(stores, tmp_path, file_system_id: str = "fs-00000000000000001") -> str:
    """A file system record plus its REAL directory -- the same pair
    `efsctl._create_file_system` writes, built directly so this file needs no
    EFS client of its own."""
    directory = efsctl.host_dir(tmp_path, ENV, file_system_id)
    directory.mkdir(parents=True)
    stores.efsctl.set(ENV, f"fs:{file_system_id}", {
        "file_system_id": file_system_id, "creation_token": "shared", "host_dir": str(directory),
        "created_at": 1.0, "performance_mode": "generalPurpose", "throughput_mode": "bursting",
        "size_bytes": 0,
    })
    return str(directory)


_EFS_VOLUMES = [{"name": "shared", "efsVolumeConfiguration": {"fileSystemId": "fs-00000000000000001"}}]
_MOUNTING_CONTAINER_DEF = [{
    **_CONTAINER_DEF[0],
    "mountPoints": [{"sourceVolume": "shared", "containerPath": "/mnt/efs", "readOnly": False}],
}]


async def test_a_task_definitions_efs_volume_reaches_the_container(stores, sink, ecs, tmp_path):
    """The whole ECS half, through the real handlers: a taskdef registered with
    an `efsVolumeConfiguration` and a container that names it in `mountPoints`
    launches a container bind-mounting the REAL host directory."""
    directory = await _a_file_system(stores, tmp_path)
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(
        stores, sink, ecs, runtime,
        containerDefinitions=_MOUNTING_CONTAINER_DEF, volumes=_EFS_VOLUMES,
    )
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    assert runtime.volumes == {directory: "/mnt/efs"}


async def test_the_stored_container_definition_is_not_mutated_by_the_mount(stores, sink, ecs, tmp_path):
    """ecsctl's zero-drift mandate: `containerDefinitions` is stored VERBATIM.
    The join reads `mountPoints` and builds a SEPARATE volume map, so what tofu
    reads back is byte-for-byte what it sent."""
    await _a_file_system(stores, tmp_path)
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(
        stores, sink, ecs, runtime,
        containerDefinitions=_MOUNTING_CONTAINER_DEF, volumes=_EFS_VOLUMES,
    )

    stored = stores.ecsctl.get(ENV, "taskdef:app:1")
    assert stored["container_definitions"] == _MOUNTING_CONTAINER_DEF
    assert stored["volumes"] == _EFS_VOLUMES


async def test_a_task_whose_file_system_is_gone_stops_with_the_real_reason(stores, sink, ecs, tmp_path):
    """THE refusal, at the level a user sees it. The task does not come up
    holding an empty directory and reporting success -- it goes STOPPED with
    the reason, which is what fails the apply and puts a verdict on the canvas.

    Mutation-test: make `efsctl._mount_source` return the path without the
    `is_dir()` check and this fails (the task reaches RUNNING)."""
    directory = await _a_file_system(stores, tmp_path)
    Path(directory).rmdir()
    assert not Path(directory).exists(), "the injection did nothing -- this test proves nothing"

    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(
        stores, sink, ecs, runtime,
        containerDefinitions=_MOUNTING_CONTAINER_DEF, volumes=_EFS_VOLUMES,
    )
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 0)

    assert not runtime.ran, "the container was STARTED over a missing file system"
    (task,) = [v for k, v in stores.ecsctl.items(ENV).items() if k.startswith("task:")]
    assert task["last_status"] == "STOPPED"
    assert directory in task["stopped_reason"]
    assert "empty directory" in task["stopped_reason"]


async def test_a_taskdef_with_no_efs_volume_mounts_nothing(stores, sink, ecs):
    """The other half of the ratchet: the ordinary ECS path must acquire no
    volume at all."""
    runtime = FakeTaskRuntime()
    await _create_cluster(stores, sink, ecs, runtime)
    await _register_taskdef(stores, sink, ecs, runtime)
    await _create_service(stores, sink, ecs, runtime, desiredCount=1)
    await _wait_for_running_count(stores, sink, ecs, runtime, 1)

    assert runtime.volumes == {}
