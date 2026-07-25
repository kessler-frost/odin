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
from odin.gateway.models import ecsctl
from odin.gateway.stores import SynthStores
from odin.runtime.colima import CONTAINER_HOST

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
