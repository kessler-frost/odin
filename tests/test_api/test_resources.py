from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from odin.network.nebula_manager import NebulaManager
from odin.server import create_app
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry


@pytest.fixture
def app_deps(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    engine = MotoEngine()
    engine.start()
    registry = ResourceRegistry(registry_path)
    yield engine, registry
    engine.stop()


@pytest.fixture
def client(app_deps):
    engine, registry = app_deps
    app = create_app(engine=engine, registry=registry)
    return TestClient(app)


def test_get_state_empty(client):
    resp = client.get("/state")
    assert resp.status_code == 200
    assert resp.json()["resources"] == []


def test_get_state_with_resources(app_deps):
    engine, registry = app_deps
    registry.register("s3_test", service="s3", file_path=".odin/infra/s3_test.py")
    registry.update_status("s3_test", "live")
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)
    resp = client.get("/state")
    assert resp.status_code == 200
    resources = resp.json()["resources"]
    assert len(resources) == 1
    assert resources[0]["name"] == "s3_test"


def test_get_state_filtered(app_deps):
    engine, registry = app_deps
    registry.register("s3_a", service="s3", file_path=".odin/infra/s3_a.py")
    registry.register("ec2_b", service="ec2", file_path=".odin/infra/ec2_b.py")
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)
    resp = client.get("/state?service=s3")
    assert resp.status_code == 200
    resources = resp.json()["resources"]
    assert len(resources) == 1


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_deploy_resource(app_deps):
    engine, registry = app_deps
    registry.register("s3_deploy", service="s3", file_path=".odin/infra/s3_deploy.py")
    registry.update_status("s3_deploy", "validated")
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)

    resp = client.post("/deploy/s3_deploy")
    assert resp.status_code == 200
    assert resp.json()["status"] == "live"


def test_deploy_resource_not_found(client):
    resp = client.post("/deploy/nonexistent")
    assert resp.status_code == 404


def test_deploy_all(app_deps):
    engine, registry = app_deps
    registry.register("s3_a", service="s3", file_path=".odin/infra/s3_a.py")
    registry.update_status("s3_a", "validated")
    registry.register("iam_b", service="iam", file_path=".odin/infra/iam_b.py")
    registry.update_status("iam_b", "validated")
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)

    resp = client.post("/deploy")
    assert resp.status_code == 200
    assert len(resp.json()["deployed"]) == 2


def test_destroy_resource(app_deps):
    engine, registry = app_deps
    registry.register("s3_destroy", service="s3", file_path=".odin/infra/s3_destroy.py")
    registry.update_status("s3_destroy", "live")
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)

    resp = client.post("/destroy/s3_destroy")
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


def test_get_vm_ssh_not_found(client):
    resp = client.get("/vm/nonexistent/ssh")
    assert resp.status_code == 404


def test_server_accepts_nebula_manager(app_deps):
    """Verify create_app wires NebulaManager without errors."""
    engine, registry = app_deps
    nebula = NebulaManager()
    app = create_app(engine=engine, registry=registry, nebula_manager=nebula)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_server_accepts_container_manager(app_deps):
    """Verify create_app wires ContainerManager without errors."""

    from odin.compute.container_manager import ContainerManager

    engine, registry = app_deps
    container = ContainerManager()
    app = create_app(engine=engine, registry=registry, container_manager=container)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_invoke_lambda_endpoint(app_deps):
    from unittest.mock import AsyncMock, patch


    engine, registry = app_deps
    registry.register("lambda_my-func", service="lambda", file_path=".odin/infra/lambda_my-func.py")
    registry.update_status("lambda_my-func", "live", metadata={
        "function_name": "my-func",
        "handler": "index.handler",
        "container_host_vm": "odin-container-host-vpc_main",
        "container_name": "lambda-my-func",
    })
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)

    with patch.object(app.state.orchestrator, "invoke_lambda", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = '{"statusCode": 200}'
        resp = client.post(
            "/invoke/lambda_my-func",
            json={"payload": '{"key": "value"}'},
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == '{"statusCode": 200}'
        mock_invoke.assert_called_once_with("lambda_my-func", '{"key": "value"}')


def test_invoke_lambda_not_found(client):
    resp = client.post("/invoke/nonexistent", json={"payload": "{}"})
    assert resp.status_code == 404


def test_invoke_non_lambda_returns_400(app_deps):
    engine, registry = app_deps
    registry.register("s3_bucket", service="s3", file_path=".odin/infra/s3_bucket.py")
    registry.update_status("s3_bucket", "live")
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)
    resp = client.post("/invoke/s3_bucket", json={"payload": "{}"})
    assert resp.status_code == 400


def test_invoke_lambda_not_deployed_returns_400(app_deps):
    engine, registry = app_deps
    registry.register("lambda_pending", service="lambda", file_path=".odin/infra/lambda_pending.py")
    registry.update_status("lambda_pending", "validated")
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app)
    resp = client.post("/invoke/lambda_pending", json={"payload": "{}"})
    assert resp.status_code == 400
