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

import json
import threading
import time
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.aws.backings import REGION
from odin.compute.tasks import TaskContainerHandle
from odin.gateway.classify import classify
from odin.gateway.keys import KeyStore
from odin.gateway.models import ecsctl, logsctl
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
    """The TaskRuntime shape (`run`/`status`/`exit_code`/`stop`) with no
    Docker involved -- deterministic and near-instant, so the
    background-thread convergence ecsctl.py spawns can be observed with a
    short poll instead of a real container boot."""

    def __init__(self, fail_run: bool = False, block: threading.Event | None = None) -> None:
        self.fail_run = fail_run
        self.block = block
        self.ran: list[tuple] = []
        self.stopped: list[tuple] = []
        self._status: dict[tuple, str] = {}
        self._exit_codes: dict[tuple, int] = {}
        # Stands in for each container's own stdout/stderr, as `docker logs
        # --tail N` would report it (see `print_line`).
        self._logs: dict[tuple, str] = {}

    def run(
        self, env: str, task_id: str, container_def: dict, extra_env: dict[str, str] | None = None,
        cpu: str | int | None = None, memory: str | int | None = None,
    ) -> TaskContainerHandle:
        if self.block is not None:
            self.block.wait(timeout=5.0)
        self.ran.append((env, task_id, container_def, extra_env, cpu, memory))
        if self.fail_run:
            raise RuntimeError("container failed to start")
        key = (env, task_id, container_def["name"])
        self._status[key] = "running"
        ports = {pm["containerPort"]: 10_000 + len(self.ran) for pm in container_def.get("portMappings") or []}
        return TaskContainerHandle(name=f"fake-{task_id}", host_ports=ports)

    def status(self, env: str, task_id: str, container_name: str) -> str:
        return self._status.get((env, task_id, container_name), "absent")

    def exit_code(self, env: str, task_id: str, container_name: str) -> int:
        return self._exit_codes.get((env, task_id, container_name), 0)

    def stop(self, env: str, task_id: str, container_name: str) -> None:
        self.stopped.append((env, task_id, container_name))
        self._status[(env, task_id, container_name)] = "exited"

    def logs(self, env: str, task_id: str, container_name: str, tail: int = 20) -> str:
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


def _answer(stores, req, runtime=None, keystore=None, gateway_port=None) -> Response:
    path, query = split_url(req.url)
    classified = classify("ecs", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a recognized ECS action must never be unmappable"
    action, resource = classified
    response = ecsctl.pure_answer(
        action, resource, ENV, req.body, stores, time.monotonic(), runtime,
        keystore=keystore, gateway_port=gateway_port,
    )
    assert response is not None, "ecsctl never falls through to None"
    return response


def _create_cluster(stores, sink, ecs, runtime, name: str = "odin") -> dict:
    req = sink.call(lambda: ecs.create_cluster(clusterName=name))
    return _parse("CreateCluster", _answer(stores, req, runtime))["cluster"]


def _register_taskdef(stores, sink, ecs, runtime, family: str = "app", **kwargs) -> dict:
    kwargs.setdefault("containerDefinitions", _CONTAINER_DEF)
    req = sink.call(lambda: ecs.register_task_definition(family=family, **kwargs))
    return _parse("RegisterTaskDefinition", _answer(stores, req, runtime))["taskDefinition"]


def _create_service(stores, sink, ecs, runtime, keystore=None, gateway_port=None, **kwargs) -> dict:
    kwargs.setdefault("cluster", "odin")
    kwargs.setdefault("serviceName", "app")
    kwargs.setdefault("taskDefinition", "app")
    kwargs.setdefault("desiredCount", 1)
    req = sink.call(lambda: ecs.create_service(**kwargs))
    return _parse("CreateService", _answer(stores, req, runtime, keystore=keystore, gateway_port=gateway_port))["service"]


def _describe_service(stores, sink, ecs, runtime, cluster: str = "odin", name: str = "app") -> dict:
    req = sink.call(lambda: ecs.describe_services(cluster=cluster, services=[name]))
    parsed = _parse("DescribeServices", _answer(stores, req, runtime))
    (service,) = parsed["services"]
    return service


def _wait_for_running_count(stores, sink, ecs, runtime, want: int, timeout: float = 2.0, **kwargs) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _describe_service(stores, sink, ecs, runtime, **kwargs)
        if last["runningCount"] == want:
            return last
        time.sleep(0.02)
    raise AssertionError(f"service never reached runningCount={want} (last seen {last})")


def _wait_for_stopped(runtime, task_id: str, timeout: float = 6.0) -> None:
    """Wait for a DELIBERATE stop of `task_id`. Retiring the previous
    revision is deliberately the LAST thing a rollout does, behind
    `ecsctl._ROLLOUT_STABILIZE_SECONDS` (field test 3), so it lands after the
    replacement has already reached RUNNING."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(stopped_id == task_id for _, stopped_id, _ in runtime.stopped):
            return
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} was never stopped (stopped: {runtime.stopped})")


# --- Cluster -----------------------------------------------------------------


def test_create_cluster_is_active_immediately(sink, ecs, stores):
    cluster = _create_cluster(stores, sink, ecs, FakeTaskRuntime())
    assert cluster["status"] == "ACTIVE"
    assert cluster["clusterName"] == "odin"
    assert cluster["runningTasksCount"] == 0


def test_create_cluster_is_idempotent_on_existing_name(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    first = _create_cluster(stores, sink, ecs, runtime)
    second = _create_cluster(stores, sink, ecs, runtime)
    assert first["clusterArn"] == second["clusterArn"]


def test_describe_clusters_unknown_name_is_a_failure_not_an_error(sink, ecs, stores):
    req = sink.call(lambda: ecs.describe_clusters(clusters=["ghost"]))
    parsed = _parse("DescribeClusters", _answer(stores, req, FakeTaskRuntime()))
    assert parsed["clusters"] == []
    assert parsed["failures"][0]["reason"] == "MISSING"


def test_delete_cluster(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    req = sink.call(lambda: ecs.delete_cluster(cluster="odin"))
    assert _parse("DeleteCluster", _answer(stores, req, runtime))["cluster"]["clusterName"] == "odin"
    describe_req = sink.call(lambda: ecs.describe_clusters(clusters=["odin"]))
    assert _parse("DescribeClusters", _answer(stores, describe_req, runtime))["clusters"] == []


def test_delete_cluster_with_active_services_is_denied(sink, ecs, stores):
    runtime = FakeTaskRuntime(block=threading.Event())  # never released -- service stays 0 running, irrelevant here
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime)
    req = sink.call(lambda: ecs.delete_cluster(cluster="odin"))
    response = _answer(stores, req, runtime)
    assert response.status_code == 400
    parsed = _parse("DeleteCluster", response, error=True)
    assert parsed["Error"]["Code"] == "ClusterContainsServicesException"


# --- TaskDefinition ------------------------------------------------------------


def test_register_task_definition_starts_at_revision_1_and_increments(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    first = _register_taskdef(stores, sink, ecs, runtime)
    second = _register_taskdef(stores, sink, ecs, runtime)
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert first["taskDefinitionArn"].endswith(":1")
    assert second["taskDefinitionArn"].endswith(":2")


def test_register_task_definition_echoes_container_definitions_verbatim(sink, ecs, stores):
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
    taskdef = _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=container_defs)
    assert taskdef["containerDefinitions"] == container_defs

    describe_req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="app:1"))
    described = _parse("DescribeTaskDefinition", _answer(stores, describe_req, runtime))["taskDefinition"]
    assert described["containerDefinitions"] == container_defs


def test_describe_task_definition_bare_family_resolves_latest_active(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _register_taskdef(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="app"))
    described = _parse("DescribeTaskDefinition", _answer(stores, req, runtime))["taskDefinition"]
    assert described["revision"] == 2


def test_deregister_task_definition_marks_inactive_and_bare_family_skips_it(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _register_taskdef(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    deregister_req = sink.call(lambda: ecs.deregister_task_definition(taskDefinition="app:2"))
    deregistered = _parse("DeregisterTaskDefinition", _answer(stores, deregister_req, runtime))["taskDefinition"]
    assert deregistered["status"] == "INACTIVE"

    req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="app"))
    described = _parse("DescribeTaskDefinition", _answer(stores, req, runtime))["taskDefinition"]
    assert described["revision"] == 1  # rev 2 is INACTIVE -- "latest ACTIVE" skips it


def test_describe_task_definition_unknown_is_client_exception(sink, ecs, stores):
    req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="ghost"))
    response = _answer(stores, req, FakeTaskRuntime())
    assert response.status_code == 400
    parsed = _parse("DescribeTaskDefinition", response, error=True)
    assert parsed["Error"]["Code"] == "ClientException"


# --- Service: create / converge / scale ---------------------------------------


def test_create_service_is_active_immediately_then_converges_running_count(sink, ecs, stores):
    block = threading.Event()
    runtime = FakeTaskRuntime(block=block)
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    service = _create_service(stores, sink, ecs, runtime, desiredCount=2)
    assert service["status"] == "ACTIVE"  # a service is a spec -- ACTIVE immediately
    assert service["desiredCount"] == 2
    assert service["runningCount"] == 0  # no containers exist yet at this instant

    block.set()
    final = _wait_for_running_count(stores, sink, ecs, runtime, 2)
    assert final["pendingCount"] == 0
    assert len(runtime.ran) == 2
    # A converged deployment reports COMPLETED (finding #3's honest state).
    assert final["deployments"][0]["rolloutState"] == "COMPLETED"
    assert final["events"] == []


def test_describe_services_reports_failed_deployment_when_a_task_cannot_start(sink, ecs, stores):
    """Field-test finding #3: a task that fails to start (bad image /
    crash-on-boot) must surface a FAILED deployment with the real reason in
    DescribeServices -- not the old hardcoded COMPLETED, which read a broken
    service as healthy and let a bad-image apply silently 'succeed'. Paired with
    the HCL's `wait_for_steady_state`, this is what makes apply fail honestly."""
    runtime = FakeTaskRuntime(fail_run=True)  # every launched container fails
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)

    deadline = time.monotonic() + 2.0
    service = None
    while time.monotonic() < deadline:
        service = _describe_service(stores, sink, ecs, runtime)
        if service["deployments"][0]["rolloutState"] == "FAILED":
            break
        time.sleep(0.02)
    assert service["runningCount"] == 0
    (deployment,) = service["deployments"]
    assert deployment["rolloutState"] == "FAILED", service
    assert "failed to start" in deployment["rolloutStateReason"]
    assert service["events"], "a failed deployment posts a service event"


def test_update_service_scales_up(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=3))
    _answer(stores, req, runtime)
    _wait_for_running_count(stores, sink, ecs, runtime, 3)
    assert len(runtime.ran) == 3


def test_concurrent_sweeps_and_scale_up_do_not_corrupt_the_store(sink, ecs, stores, tmp_path):
    """Release finding #3 -- `_update_task`'s old get()-then-set() pair, plus
    `_sweep_tasks` iterating the store's flat dict while ANOTHER thread
    mutates it (a service scale-up launching+updating several task
    records), is exactly the "dictionary changed size during iteration"
    class of bug. Many concurrent ListTasks calls (each sweeps) racing a
    scale-up must never raise, and the sidecar must stay valid JSON."""
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=6)
    _wait_for_running_count(stores, sink, ecs, runtime, 6)

    errors: list[Exception] = []

    # Capture the two request shapes ONCE, single-threaded: `sink.call`'s
    # index-based return (`requests[before]`) is not safe under concurrent
    # callers -- a racing thread's capture can land at `before` first, so the
    # scale-up thread could dispatch a ListTasks body and silently drop the
    # desiredCount=10 update (a rare flake under full-suite load). The
    # concurrency under test -- many dispatches sweeping the store while a
    # scale-up mutates it -- is preserved: every thread still re-dispatches
    # through classify + pure_answer on each iteration.
    list_req = sink.call(lambda: ecs.list_tasks(cluster="odin"))
    update_req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=10))

    def list_tasks_repeatedly() -> None:
        try:
            for _ in range(30):
                _answer(stores, list_req, runtime)
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    def scale_up() -> None:
        try:
            _answer(stores, update_req, runtime)
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    threads = [threading.Thread(target=list_tasks_repeatedly) for _ in range(4)] + [threading.Thread(target=scale_up)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    _wait_for_running_count(stores, sink, ecs, runtime, 10)
    sidecar = tmp_path / ENV / "gateway" / "ecsctl.json"
    json.loads(sidecar.read_text())  # raises if truncated/invalid


def test_update_service_scales_down_newest_task_first(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=3)
    _wait_for_running_count(stores, sink, ecs, runtime, 3)
    oldest_task_id = runtime.ran[0][1]

    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=1))
    _answer(stores, req, runtime)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

    assert len(runtime.stopped) == 2
    stopped_ids = {task_id for _, task_id, _ in runtime.stopped}
    assert oldest_task_id not in stopped_ids  # the oldest task survives; the two newest were culled

    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app"))
    remaining = _parse("ListTasks", _answer(stores, tasks_req, runtime))["taskArns"]
    assert len(remaining) == 1
    assert remaining[0].endswith(oldest_task_id)


def test_update_service_task_definition_replaces_stale_tasks(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    old_task_id = runtime.ran[0][1]

    _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:latest", "essential": True,
    }])  # rev 2
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    _answer(stores, req, runtime)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

    # Field test 3: the stale task is retired AFTER the replacement is up
    # (surge first, retire second), so the replacement reaching RUNNING no
    # longer implies the old one is already gone -- hence the wait.
    _wait_for_stopped(runtime, old_task_id)
    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app"))
    (task_arn,) = _parse("ListTasks", _answer(stores, tasks_req, runtime))["taskArns"]
    describe_req = sink.call(lambda: ecs.describe_tasks(cluster="odin", tasks=[task_arn]))
    (task,) = _parse("DescribeTasks", _answer(stores, describe_req, runtime))["tasks"]
    assert task["taskDefinitionArn"].endswith(":2")


def test_a_taskdef_update_reports_zero_running_until_the_new_revision_is_up(sink, ecs, stores):
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
    block = threading.Event()  # hold the reconcile thread in `run`
    runtime = FakeTaskRuntime(block=block)
    block.set()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    _create_service(stores, sink, ecs, runtime, desiredCount=3)
    _wait_for_running_count(stores, sink, ecs, runtime, 3)

    _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:this-tag-does-not-exist-9z9z", "essential": True,
    }])  # rev 2
    block.clear()  # the reconcile spawned by UpdateService cannot make progress
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    updated = _parse("UpdateService", _answer(stores, req, runtime))["service"]

    assert updated["desiredCount"] == 3
    assert updated["runningCount"] == 0, "three stale tasks must not read as the new revision"
    (deployment,) = updated["deployments"]
    assert deployment["taskDefinition"].endswith(":2")
    assert deployment["runningCount"] == 0
    assert deployment["rolloutState"] != "COMPLETED", updated
    block.set()


def test_a_taskdef_update_that_cannot_start_keeps_reporting_a_failed_deployment(sink, ecs, stores):
    """The other half of B1: once the replacement tasks genuinely fail, the
    service must KEEP reporting short-of-desired for as long as it is short --
    that is what turns the provider's bounded `timeouts.update` into a real,
    honest apply failure instead of a 2.3s success."""
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    _create_service(stores, sink, ecs, runtime, desiredCount=2)
    _wait_for_running_count(stores, sink, ecs, runtime, 2)

    _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:this-tag-does-not-exist-9z9z", "essential": True,
    }])  # rev 2
    runtime.fail_run = True  # the new image cannot be pulled
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    _answer(stores, req, runtime)

    deadline = time.monotonic() + 2.0
    service = None
    while time.monotonic() < deadline:
        service = _describe_service(stores, sink, ecs, runtime)
        if service["deployments"][0]["rolloutState"] == "FAILED":
            break
        time.sleep(0.02)
    (deployment,) = service["deployments"]
    assert deployment["rolloutState"] == "FAILED", service
    assert service["runningCount"] != service["desiredCount"], "would read as steady state"
    assert "failed to start" in deployment["rolloutStateReason"]
    assert service["events"], "a failed deployment posts a real service event"


def test_a_successful_taskdef_update_still_reaches_steady_state(sink, ecs, stores):
    """The counterweight to the two above: current-revision-only accounting must
    still CONVERGE, or every healthy update would hang until its timeout."""
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)  # rev 1
    _create_service(stores, sink, ecs, runtime, desiredCount=2)
    _wait_for_running_count(stores, sink, ecs, runtime, 2)

    _register_taskdef(stores, sink, ecs, runtime, containerDefinitions=[{
        "name": "app", "image": "nginx:1.27-alpine", "essential": True,
    }])  # rev 2
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", taskDefinition="app:2"))
    _answer(stores, req, runtime)

    final = _wait_for_running_count(stores, sink, ecs, runtime, 2)
    (deployment,) = final["deployments"]
    assert deployment["rolloutState"] == "COMPLETED", final
    assert deployment["taskDefinition"].endswith(":2")


def test_delete_service_stops_all_its_tasks(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=2)
    _wait_for_running_count(stores, sink, ecs, runtime, 2)

    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    deleted = _parse("DeleteService", _answer(stores, req, runtime))["service"]
    assert deleted["status"] == "INACTIVE"
    assert len(runtime.stopped) == 2

    # LOAD-BEARING (V5d): a real tofu destroy's own delete-waiter polls
    # DescribeServices expecting to see status="INACTIVE" on a successfully
    # DESCRIBED service -- not a "MISSING" failure, which the real Go-SDK
    # provider treats as "not ready yet" and retries forever (see
    # ecsctl.py's `_INACTIVE_SERVICE_SWEEP_SECONDS` docstring). So the
    # record must still describe cleanly, INACTIVE, right after delete.
    describe_req = sink.call(lambda: ecs.describe_services(cluster="odin", services=["app"]))
    parsed = _parse("DescribeServices", _answer(stores, describe_req, runtime))
    assert parsed["failures"] == []
    (described,) = parsed["services"]
    assert described["status"] == "INACTIVE"
    assert described["runningCount"] == 0


def test_delete_service_lets_the_same_name_be_recreated_immediately(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _answer(stores, req, runtime)

    recreated = _create_service(stores, sink, ecs, runtime, desiredCount=1)
    assert recreated["status"] == "ACTIVE"


def test_delete_cluster_after_service_delete_is_allowed(sink, ecs, stores):
    """An INACTIVE (recently-deleted) service must NOT block cluster
    deletion -- only a still-ACTIVE one does (see
    test_delete_cluster_with_active_services_is_denied)."""
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _answer(stores, req, runtime)

    req = sink.call(lambda: ecs.delete_cluster(cluster="odin"))
    response = _answer(stores, req, runtime)
    assert response.status_code == 200


# --- Workload creds injection (odin:node tag -> per-node keystore creds) -------


def test_create_service_with_odin_node_tag_injects_workload_creds(sink, ecs, stores, keystore):
    """A service tagged `odin:node` (agent/hcl.py's `_tags_block` stamp)
    launches its REAL task containers with the four AWS-SDK env vars layered
    on via `extra_env` -- so the container can call odin's own gateway AS
    ITSELF -- while the stored taskdef stays byte-for-byte untouched."""
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

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


def test_update_service_scale_up_injects_the_same_stable_creds(sink, ecs, stores, keystore):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=2))
    _answer(stores, req, runtime, keystore=keystore, gateway_port=4266)
    _wait_for_running_count(stores, sink, ecs, runtime, 2)

    access_key, _ = keystore.issue(ENV, "myservice")
    assert len(runtime.ran) == 2
    for _, _, _, extra_env, _, _ in runtime.ran:
        assert extra_env["AWS_ACCESS_KEY_ID"] == access_key  # stable identity, never a second mint


def test_create_service_without_keystore_keeps_prior_behavior(sink, ecs, stores):
    """REGRESSION: today's callers pass no keystore/gateway_port -- the tag
    may be present, but no creds are injected and nothing crashes."""
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, tags=[{"key": "odin:node", "value": "myservice"}])
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

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


def test_task_containers_launch_with_the_nodes_env_and_resolved_refs(sink, ecs, stores, keystore, tmp_path):
    """Field test 2, "the product hole": an ECS node's `env` -- static entries
    AND `${{producer.ATTR}}` refs -- was silently dropped, so there was no
    canvas-driven way to hand a container its connection strings. It now rides
    the same launch-time seam the issued credentials already use, so nothing
    resolved ever enters the taskdef (and therefore tofu state)."""
    _seed_db_and_stack(stores, tmp_path, {"DATABASE_URL": "${{appdb.DATABASE_URL}}", "APP_TIER": "web"})
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

    (_, _, container_def, extra_env, _, _) = runtime.ran[0]
    assert extra_env["DATABASE_URL"] == f"postgresql://app:s3cret@{CONTAINER_HOST}:33366/shop"
    assert extra_env["APP_TIER"] == "web"
    # The issued creds still win, and the taskdef is still byte-for-byte clean:
    # a resolved connection string carries the DB PASSWORD, and the taskdef is
    # echoed into tofu state verbatim.
    assert extra_env["AWS_ACCESS_KEY_ID"] == keystore.issue(ENV, "myservice")[0]
    assert container_def.get("environment") is None


def test_odins_own_aws_vars_win_over_a_canvas_that_names_them(sink, ecs, stores, keystore, tmp_path):
    _seed_db_and_stack(stores, tmp_path, {"AWS_DEFAULT_REGION": "eu-west-1"})
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

    (_, _, _, extra_env, _, _) = runtime.ran[0]
    assert extra_env["AWS_DEFAULT_REGION"] == REGION, "the gateway wiring must not be overridable"


def test_an_unresolvable_ref_fails_the_task_with_a_naming_reason(sink, ecs, stores, keystore, tmp_path):
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
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(
        stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266,
        tags=[{"key": "odin:node", "value": "myservice"}],
    )

    deadline = time.monotonic() + 2.0
    service = None
    while time.monotonic() < deadline:
        service = _describe_service(stores, sink, ecs, runtime)
        if service["deployments"][0]["rolloutState"] == "FAILED":
            break
        time.sleep(0.02)
    (deployment,) = service["deployments"]
    assert deployment["rolloutState"] == "FAILED", service
    assert "DATABASE_URL" in deployment["rolloutStateReason"], deployment
    assert "appdb" in deployment["rolloutStateReason"], deployment
    assert not runtime.ran, "no container may be started with a hole in its environment"


def test_create_service_without_tags_launches_with_no_injected_creds(sink, ecs, stores, keystore):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, keystore=keystore, gateway_port=4266)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)

    assert stores.ecsctl.get(ENV, "service:odin:app")["node_label"] is None
    (_, _, _, extra_env, _, _) = runtime.ran[0]
    assert not extra_env


# --- Tags: TagResource/UntagResource/ListTagsForResource + describe echo -------


def _service_setup_with_tags(sink, ecs, stores, tags: list[dict] | None = None) -> tuple[FakeTaskRuntime, str]:
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    kwargs = {"tags": tags} if tags is not None else {}
    service = _create_service(stores, sink, ecs, runtime, **kwargs)
    return runtime, service["serviceArn"]


def test_describe_services_echoes_create_service_tags(sink, ecs, stores):
    """THE recorded-drift kill: a `tags` block on `aws_ecs_service` must come
    back from DescribeServices exactly as submitted (the wire's own
    list-of-lowercase-{key,value} shape), so a subsequent `tofu plan` sees no
    diff -- ROADMAP's old 'tags aren't echoed back' v1 limit."""
    tags = [{"key": "odin:node", "value": "myservice"}, {"key": "team", "value": "platform"}]
    runtime, _ = _service_setup_with_tags(sink, ecs, stores, tags)
    service = _describe_service(stores, sink, ecs, runtime)
    assert service["tags"] == tags


def test_tag_untag_list_round_trip(sink, ecs, stores):
    runtime, arn = _service_setup_with_tags(sink, ecs, stores, [{"key": "team", "value": "platform"}])

    tag_req = sink.call(lambda: ecs.tag_resource(resourceArn=arn, tags=[
        {"key": "env", "value": "prod"}, {"key": "team", "value": "core"},
    ]))
    assert _answer(stores, tag_req, runtime).status_code == 200

    list_req = sink.call(lambda: ecs.list_tags_for_resource(resourceArn=arn))
    listed = _parse("ListTagsForResource", _answer(stores, list_req, runtime))["tags"]
    assert listed == [{"key": "team", "value": "core"}, {"key": "env", "value": "prod"}]  # merged, last write wins

    untag_req = sink.call(lambda: ecs.untag_resource(resourceArn=arn, tagKeys=["team"]))
    assert _answer(stores, untag_req, runtime).status_code == 200

    relist_req = sink.call(lambda: ecs.list_tags_for_resource(resourceArn=arn))
    assert _parse("ListTagsForResource", _answer(stores, relist_req, runtime))["tags"] == [{"key": "env", "value": "prod"}]
    assert _describe_service(stores, sink, ecs, runtime)["tags"] == [{"key": "env", "value": "prod"}]


def test_tag_ops_on_unknown_service_arn_are_not_found(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    ghost = ecsctl._service_arn("odin", "ghost")
    req = sink.call(lambda: ecs.list_tags_for_resource(resourceArn=ghost))
    response = _answer(stores, req, runtime)
    assert response.status_code == 400
    parsed = _parse("ListTagsForResource", response, error=True)
    assert parsed["Error"]["Code"] == "ServiceNotFoundException"


def test_recreate_service_overwrites_stale_tags(sink, ecs, stores):
    """Create-with-tags -> delete -> recreate WITHOUT tags must describe as
    untagged: CreateService's tag write is authoritative, never a merge with
    a deleted prior incarnation's leftovers (which would itself be drift)."""
    runtime, _ = _service_setup_with_tags(sink, ecs, stores, [{"key": "team", "value": "platform"}])
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    delete_req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _answer(stores, delete_req, runtime)

    recreated = _create_service(stores, sink, ecs, runtime)
    assert recreated["tags"] == []
    assert _describe_service(stores, sink, ecs, runtime)["tags"] == []


# --- Tasks: lazy sweep (spontaneous exit) --------------------------------------


def test_describe_tasks_lazily_marks_a_spontaneously_exited_container_stopped(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]

    runtime.mark_exited(ENV, task_id, "app", exit_code=137)

    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app", desiredStatus="RUNNING"))
    tasks_req_body = sink.call(lambda: ecs.describe_tasks(cluster="odin", tasks=[f"arn:aws:ecs:us-east-1:000000000000:task/odin/{task_id}"]))
    (task,) = _parse("DescribeTasks", _answer(stores, tasks_req_body, runtime))["tasks"]
    assert task["lastStatus"] == "STOPPED"
    assert task["containers"][0]["exitCode"] == 137
    assert task["stoppedReason"]

    running_tasks = _parse("ListTasks", _answer(stores, tasks_req, runtime))["taskArns"]
    assert running_tasks == []

    service = _describe_service(stores, sink, ecs, runtime)
    assert service["runningCount"] == 0


# --- W2.2: an Apply re-converges a service whose task is gone. A task is not
# a TF resource, so tofu's plan for an unchanged `aws_ecs_service` is empty
# and only this pass can bring the container back. --------------------------


def test_mark_task_stopped_records_the_drift_reason_with_no_invented_exit_code(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]

    ecsctl.mark_task_stopped(stores, ENV, "odin", task_id, "container gone — re-Apply to recreate")

    task = stores.ecsctl.get(ENV, f"task:odin:{task_id}")
    assert task["last_status"] == "STOPPED"
    assert task["stopped_reason"] == "container gone — re-Apply to recreate"
    assert task["exit_code"] is None  # a container that no longer exists never reported one
    assert _describe_service(stores, sink, ecs, runtime)["runningCount"] == 0


def test_converge_services_relaunches_a_task_whose_container_is_gone(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _, _, _, _ = runtime.ran[0]
    ecsctl.mark_task_stopped(stores, ENV, "odin", task_id, "removed outside odin")

    ecsctl.converge_services(stores, ENV, runtime)  # what an Apply now does

    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    assert len(runtime.ran) == 2, "the missing task must be relaunched, not left short"


def test_converge_services_is_a_no_op_at_desired_count(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=2)
    _wait_for_running_count(stores, sink, ecs, runtime, 2)

    ecsctl.converge_services(stores, ENV, runtime)
    _wait_for_running_count(stores, sink, ecs, runtime, 2)

    assert len(runtime.ran) == 2  # idempotent: every Apply must not stack up containers
    assert runtime.stopped == []


def test_converge_services_leaves_a_deleted_service_alone(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    delete_req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _parse("DeleteService", _answer(stores, delete_req, runtime))
    launched = len(runtime.ran)

    ecsctl.converge_services(stores, ENV, runtime)  # an empty-canvas Apply's teardown

    time.sleep(0.1)
    assert len(runtime.ran) == launched, "an INACTIVE service must never be re-launched"


# --- W2.1 piece 3: the sweep ships each task's tail into /ecs/{service} ---------


def _running_service(sink, ecs, stores) -> tuple[FakeTaskRuntime, str]:
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    return runtime, runtime.ran[0][1]


def _shipped(stores) -> list[dict]:
    return logsctl.stored_events(stores, ENV, "/ecs/app", 100)


def test_sweep_ships_a_task_container_tail_into_the_service_log_group(sink, ecs, stores):
    runtime, task_id = _running_service(sink, ecs, stores)
    runtime.print_line(ENV, task_id, "app", "nginx: ready to accept connections")

    _describe_service(stores, sink, ecs, runtime)  # every Describe* sweeps

    events = _shipped(stores)
    assert [e["message"] for e in events] == ["nginx: ready to accept connections"]
    # One stream per real task container (ecsctl.py's `_ship_task_logs`).
    assert {e["stream"] for e in events} == {f"odin-ecs-default-{task_id[:8]}-app"}
    assert logsctl.group_exists(stores, ENV, "/ecs/app")  # auto-created by ingestion


def test_resweeping_the_same_tail_never_duplicates_events(sink, ecs, stores):
    runtime, task_id = _running_service(sink, ecs, stores)
    runtime.print_line(ENV, task_id, "app", "started")

    for _ in range(3):  # a Describe* per reconciler tick, over and over
        _describe_service(stores, sink, ecs, runtime)
    assert [e["message"] for e in _shipped(stores)] == ["started"]

    runtime.print_line(ENV, task_id, "app", "handled a request")
    _describe_service(stores, sink, ecs, runtime)
    assert [e["message"] for e in _shipped(stores)] == ["started", "handled a request"]


def test_sweep_captures_the_final_lines_of_a_task_that_already_exited(sink, ecs, stores):
    """The crash diagnostic: shipping runs BEFORE the RUNNING-only status
    check, so a container that died on its own still hands over its last
    output -- and keeps handing over nothing new on later sweeps."""
    runtime, task_id = _running_service(sink, ecs, stores)
    runtime.print_line(ENV, task_id, "app", "FATAL: config missing")
    runtime.mark_exited(ENV, task_id, "app", exit_code=1)

    service = _describe_service(stores, sink, ecs, runtime)
    assert service["runningCount"] == 0  # the sweep also demoted it, as before
    assert [e["message"] for e in _shipped(stores)] == ["FATAL: config missing"]

    _describe_service(stores, sink, ecs, runtime)
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
    monkeypatch.setattr(
        ecsctl.elbv2ctl, "register_target",
        lambda stores, env, arn, target_id, port: calls["register"].append((env, arn, target_id, port)),
    )
    monkeypatch.setattr(
        ecsctl.elbv2ctl, "deregister_target",
        lambda stores, env, arn, target_id, port: calls["deregister"].append((env, arn, target_id, port)),
    )
    return calls


def _lb_service(sink, ecs, stores, desired: int = 1) -> FakeTaskRuntime:
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=desired, loadBalancers=_LOAD_BALANCERS)
    _wait_for_running_count(stores, sink, ecs, runtime, desired)
    return runtime


def test_describe_services_echoes_the_load_balancers_it_was_created_with(sink, ecs, stores, target_calls):
    """Hardcoding `loadBalancers: []` (this module's own first cut) drifts an
    `aws_ecs_service` with a `load_balancer` block on every subsequent plan."""
    runtime = _lb_service(sink, ecs, stores)
    service = _describe_service(stores, sink, ecs, runtime)
    assert service["loadBalancers"] == _LOAD_BALANCERS


def test_launching_a_task_registers_its_real_published_port_as_a_target(sink, ecs, stores, target_calls):
    _lb_service(sink, ecs, stores, desired=2)
    # Registration TRAILS the running count: a task is running for a moment
    # before it joins the rotation (real ECS behaves the same way), so waiting
    # on `runningCount` alone raced and saw only the first port ~1 run in 4.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(target_calls["register"]) < 2:
        time.sleep(0.02)
    # The FakeTaskRuntime publishes containerPort 80 on 10001 / 10002.
    assert sorted(target_calls["register"]) == [
        (ENV, _TG_ARN, CONTAINER_HOST, 10_001),
        (ENV, _TG_ARN, CONTAINER_HOST, 10_002),
    ]
    assert target_calls["deregister"] == []


def test_a_service_with_no_load_balancers_never_touches_elbv2(sink, ecs, stores, target_calls):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    assert target_calls == {"register": [], "deregister": []}


def test_scaling_down_deregisters_the_stopped_task(sink, ecs, stores, target_calls):
    runtime = _lb_service(sink, ecs, stores, desired=2)
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=1))
    _parse("UpdateService", _answer(stores, req, runtime))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not target_calls["deregister"]:
        time.sleep(0.02)
    # Newest-task-first scale-down, so the SECOND task's port leaves rotation.
    assert target_calls["deregister"] == [(ENV, _TG_ARN, CONTAINER_HOST, 10_002)]


def test_deleting_the_service_deregisters_every_task(sink, ecs, stores, target_calls):
    runtime = _lb_service(sink, ecs, stores, desired=2)
    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    _parse("DeleteService", _answer(stores, req, runtime))
    assert sorted(target_calls["deregister"]) == [
        (ENV, _TG_ARN, CONTAINER_HOST, 10_001),
        (ENV, _TG_ARN, CONTAINER_HOST, 10_002),
    ]


def test_a_task_that_dies_on_its_own_leaves_the_rotation(sink, ecs, stores, target_calls):
    """A dead container left in the upstream list is a real load-balancer bug --
    the sweep that demotes it to STOPPED must also take it out."""
    runtime = _lb_service(sink, ecs, stores)
    task_id = runtime.ran[0][1]
    runtime.mark_exited(ENV, task_id, "app", exit_code=137)
    service = _describe_service(stores, sink, ecs, runtime)
    assert service["runningCount"] == 0
    assert target_calls["deregister"] == [(ENV, _TG_ARN, CONTAINER_HOST, 10_001)]
    # Only once -- a later sweep sees a task that's already STOPPED.
    _describe_service(stores, sink, ecs, runtime)
    assert len(target_calls["deregister"]) == 1


def test_drift_marking_a_task_stopped_also_deregisters_it(sink, ecs, stores, target_calls):
    runtime = _lb_service(sink, ecs, stores)
    task_id = runtime.ran[0][1]
    ecsctl.mark_task_stopped(stores, ENV, "odin", task_id, "container removed outside odin")
    assert target_calls["deregister"] == [(ENV, _TG_ARN, CONTAINER_HOST, 10_001)]
