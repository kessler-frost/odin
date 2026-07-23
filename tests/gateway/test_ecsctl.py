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

import threading
import time
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.compute.tasks import TaskContainerHandle
from odin.gateway.classify import classify
from odin.gateway.models import ecsctl
from odin.gateway.stores import SynthStores

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

    def run(self, env: str, task_id: str, container_def: dict) -> TaskContainerHandle:
        if self.block is not None:
            self.block.wait(timeout=5.0)
        self.ran.append((env, task_id, container_def))
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


def _answer(stores, req, runtime=None) -> Response:
    path, query = split_url(req.url)
    classified = classify("ecs", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a recognized ECS action must never be unmappable"
    action, resource = classified
    response = ecsctl.pure_answer(action, resource, ENV, req.body, stores, time.monotonic(), runtime)
    assert response is not None, "ecsctl never falls through to None"
    return response


def _create_cluster(stores, sink, ecs, runtime, name: str = "odin") -> dict:
    req = sink.call(lambda: ecs.create_cluster(clusterName=name))
    return _parse("CreateCluster", _answer(stores, req, runtime))["cluster"]


def _register_taskdef(stores, sink, ecs, runtime, family: str = "app", **kwargs) -> dict:
    kwargs.setdefault("containerDefinitions", _CONTAINER_DEF)
    req = sink.call(lambda: ecs.register_task_definition(family=family, **kwargs))
    return _parse("RegisterTaskDefinition", _answer(stores, req, runtime))["taskDefinition"]


def _create_service(stores, sink, ecs, runtime, **kwargs) -> dict:
    kwargs.setdefault("cluster", "odin")
    kwargs.setdefault("serviceName", "app")
    kwargs.setdefault("taskDefinition", "app")
    kwargs.setdefault("desiredCount", 1)
    req = sink.call(lambda: ecs.create_service(**kwargs))
    return _parse("CreateService", _answer(stores, req, runtime))["service"]


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

    assert any(task_id == old_task_id for _, task_id, _ in runtime.stopped)
    tasks_req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app"))
    (task_arn,) = _parse("ListTasks", _answer(stores, tasks_req, runtime))["taskArns"]
    describe_req = sink.call(lambda: ecs.describe_tasks(cluster="odin", tasks=[task_arn]))
    (task,) = _parse("DescribeTasks", _answer(stores, describe_req, runtime))["tasks"]
    assert task["taskDefinitionArn"].endswith(":2")


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


# --- Tasks: lazy sweep (spontaneous exit) --------------------------------------


def test_describe_tasks_lazily_marks_a_spontaneously_exited_container_stopped(sink, ecs, stores):
    runtime = FakeTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _wait_for_running_count(stores, sink, ecs, runtime, 1)
    _, task_id, _ = runtime.ran[0]

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
