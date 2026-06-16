"""App-level API tests. The agent is stubbed out; Moto + tofu are real."""
import pytest
from fastapi.testclient import TestClient

from odin.agent.client import OdinAgent
from odin.server import create_app

pytestmark = pytest.mark.tofu

VPC_HCL = 'resource "aws_vpc" "v" {\n  cidr_block = "10.50.0.0/16"\n}\n'


@pytest.fixture
def app_client(moto_engine, registry, tmp_path, monkeypatch):
    async def _noop_start(self):
        return

    monkeypatch.setattr(OdinAgent, "start", _noop_start)
    tf_dir = tmp_path / "tf"
    monkeypatch.setattr("odin.server.TF_DIR", tf_dir)
    monkeypatch.setattr("odin.server.CANVAS_PATH", tmp_path / "canvas.json")
    app = create_app(engine=moto_engine, registry=registry)
    with TestClient(app) as client:
        yield client, registry, tf_dir


def test_health(app_client):
    client, _, _ = app_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["agent"] is False  # start() patched to a no-op


def test_state_empty(app_client):
    client, _, _ = app_client
    assert client.get("/state").json() == {"main_tf": "", "resources": []}


def test_state_reflects_registry(app_client):
    client, registry, _ = app_client
    registry.register("s3_test", service="s3", file_path="")
    resources = client.get("/state").json()["resources"]
    assert resources[0]["name"] == "s3_test"
    assert resources[0]["status"] == "draft"


def test_canvas_round_trip(app_client):
    client, _, _ = app_client
    graph = {"nodes": [{"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "b"}}], "edges": []}
    assert client.post("/canvas", json=graph).status_code == 200
    assert client.get("/canvas").json()["nodes"][0]["data"]["label"] == "b"


def test_deploy_applies_validated_resource(app_client):
    client, registry, tf_dir = app_client
    tf_dir.mkdir(parents=True, exist_ok=True)
    (tf_dir / "main.tf").write_text(VPC_HCL)
    registry.register("vpc_v", service="vpc", file_path="")
    registry.update_status("vpc_v", "validated")

    resp = client.post("/deploy/vpc_v")
    assert resp.status_code == 200
    assert resp.json()["status"] == "live"


def test_deploy_unknown_resource_404(app_client):
    client, _, _ = app_client
    assert client.post("/deploy/nope").status_code == 404
