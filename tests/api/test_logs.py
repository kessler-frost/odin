"""w1 observability -- GET /logs: resolve a canvas node label to its real
backing container(s)/VM and return their logs. `fetch_logs` is tested
directly (fast, precise per-kind resolution); a couple of TestClient smoke
tests lock the route's wiring end to end.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.api.logs import NO_BACKING_KINDS, fetch_logs
from odin.gateway.stores import SynthStores
from odin.server import create_app
from odin.spec.models import ResourceDesired, Stack
from odin.spec.store import SpecStore

ENV = "default"


class FakeRuntime:
    def __init__(self):
        self._status: dict[str, str] = {}
        self._logs: dict[str, str] = {}

    def set(self, name: str, status: str, logs: str = "") -> None:
        self._status[name] = status
        self._logs[name] = logs

    def status(self, name: str) -> str:
        return self._status.get(name, "absent")

    def logs(self, name: str, tail: int = 20) -> str:
        return self._logs.get(name, "")


def _store(tmp_path, resources=()):
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=resources))
    return store


# --- unknown node: the one real error ---------------------------------------


def test_unknown_node_is_an_honest_error_not_found(tmp_path):
    store = _store(tmp_path)
    stores = SynthStores(tmp_path)
    result = fetch_logs(store, stores, FakeRuntime(), ENV, "nope")
    assert result.found is False
    assert "nope" in result.error


# --- rds -----------------------------------------------------------------


def test_rds_logs_from_its_direct_postgres_container(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="db", kind="rds"),))
    stores = SynthStores(tmp_path)
    rt = FakeRuntime()
    rt.set("odin-rds-default-db", "running", "PostgreSQL init complete")

    result = fetch_logs(store, stores, rt, ENV, "db")

    assert result.found and result.running
    assert result.sources == ["odin-rds-default-db"]
    assert result.lines == "PostgreSQL init complete"
    assert result.message is None


def test_rds_not_running_is_honest_not_a_500(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="db", kind="rds"),))
    stores = SynthStores(tmp_path)
    result = fetch_logs(store, stores, FakeRuntime(), ENV, "db")  # container never booted
    assert result.found is True
    assert result.running is False
    assert "not running" in result.message


# --- s3/sqs/sns/dynamodb: the shared per-env backing container -------------


def test_provisioned_kind_reads_the_shared_backing_container(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="uploads", kind="s3"),))
    stores = SynthStores(tmp_path)
    rt = FakeRuntime()
    rt.set("odin-aws-rustfs-default", "running", "listening on :9000")

    result = fetch_logs(store, stores, rt, ENV, "uploads")

    assert result.sources == ["odin-aws-rustfs-default"]
    assert result.lines == "listening on :9000"


def test_exited_container_still_returns_its_last_logs(tmp_path):
    # A crash's whole diagnostic value is in the logs -- absent (never
    # existed) is the only status that skips reading them.
    store = _store(tmp_path, (ResourceDesired(id="db", kind="rds"),))
    stores = SynthStores(tmp_path)
    rt = FakeRuntime()
    rt.set("odin-rds-default-db", "exited", "FATAL: out of memory")

    result = fetch_logs(store, stores, rt, ENV, "db")

    assert result.running is False
    assert result.lines == "FATAL: out of memory"
    assert "not running" in result.message


# --- ec2: resolves via the odin:node tag, reads the VM's journal -----------


def test_ec2_resolves_instance_by_tag_and_reads_vm_logs(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="server", kind="ec2"),))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-1", {"instance_id": "i-1", "state_name": "running"})
    stores.tags.set(ENV, "ec2:i-1", {"odin:node": "server"})

    class FakeVm:
        def status(self, name):
            return "running"

        def logs(self, name, tail=20):
            return f"journal for {name}"

    monkeypatch.setattr("odin.api.logs.ec2_compute.InstanceVm", FakeVm)
    result = fetch_logs(store, stores, FakeRuntime(), ENV, "server")

    assert result.found and result.running
    assert result.sources == ["odin-ec2-default-i-1"]
    assert result.lines == "journal for odin-ec2-default-i-1"


def test_ec2_no_instance_yet_is_honest_not_found(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="server", kind="ec2"),))
    stores = SynthStores(tmp_path)
    result = fetch_logs(store, stores, FakeRuntime(), ENV, "server")
    assert result.found is True
    assert "no EC2 instance" in result.message


# --- lambda: resolves via tag or FunctionName, always reads via Colima -----


def test_lambda_resolves_by_function_name_and_reads_rie_container(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="fn1", kind="lambda"),))
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", {
        "function_name": "fn1", "function_arn": "arn:aws:lambda:us-east-1:000000000000:function:fn1",
        "state": "Active",
    })

    class FakeColima:
        def status(self, name):
            return "running"

        def logs(self, name, tail=20):
            return "RIE listening"

    monkeypatch.setattr("odin.api.logs.ColimaRuntime", FakeColima)
    result = fetch_logs(store, stores, FakeRuntime(), ENV, "fn1")

    assert result.sources == ["odin-lambda-default-fn1"]
    assert result.lines == "RIE listening"


# --- ecs: every task container for the service ------------------------------


def test_ecs_reads_logs_from_every_task_container(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", {
        "cluster_name": "odin", "service_name": "app", "status": "ACTIVE", "desired_count": 2,
    })
    stores.ecsctl.set(ENV, "task:odin:t1", {
        "cluster_name": "odin", "service_name": "app", "task_id": "t1aaaaaa", "container_name": "web",
        "last_status": "RUNNING", "started_at": 2.0,
    })
    stores.ecsctl.set(ENV, "task:odin:t2", {
        "cluster_name": "odin", "service_name": "app", "task_id": "t2bbbbbb", "container_name": "web",
        "last_status": "STOPPED", "started_at": 1.0,
    })

    class FakeColima:
        def status(self, name):
            return "running" if "t1aaaaaa" in name else "exited"

        def logs(self, name, tail=20):
            return f"log:{name}"

    monkeypatch.setattr("odin.api.logs.ColimaRuntime", FakeColima)
    result = fetch_logs(store, stores, FakeRuntime(), ENV, "app")

    assert result.running is True  # at least one task is up
    assert len(result.sources) == 2
    assert result.sources[0].startswith("odin-ecs-default-t1aaaaaa")  # newest task first
    assert "==>" in result.lines  # multi-source: headered blocks


def test_ecs_no_service_yet_is_honest_not_found(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = SynthStores(tmp_path)
    result = fetch_logs(store, stores, FakeRuntime(), ENV, "app")
    assert result.found is True
    assert "no ECS service" in result.message


# --- kinds with no runnable backing at all ----------------------------------


def test_no_backing_kinds_answer_honestly(tmp_path):
    assert NO_BACKING_KINDS == {"vpc", "subnet", "sg", "iam_role", "ecr"}
    for kind in NO_BACKING_KINDS:
        store = _store(tmp_path, (ResourceDesired(id="thing", kind=kind),))
        stores = SynthStores(tmp_path)
        result = fetch_logs(store, stores, FakeRuntime(), ENV, "thing")
        assert result.found is True
        assert "no logs available" in result.message


# --- route wiring (TestClient) ----------------------------------------------


def test_logs_route_returns_200_never_500_for_an_unknown_node(tmp_path):
    from tests.api.test_apply import FakeRds
    from tests.api.test_apply import FakeRuntime as ServerFakeRuntime

    app = create_app(runtime=ServerFakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        resp = client.get("/logs", params={"node": "ghost"})
        assert resp.status_code == 200
        assert resp.json()["error"]


def test_logs_route_missing_node_param_is_an_honest_error(tmp_path):
    from tests.api.test_apply import FakeRds
    from tests.api.test_apply import FakeRuntime as ServerFakeRuntime

    app = create_app(runtime=ServerFakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        resp = client.get("/logs")
        assert resp.status_code == 200
        assert resp.json()["error"]
