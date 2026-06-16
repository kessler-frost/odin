from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from odin.server import create_app
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry


@pytest.fixture
def setup(tmp_path):
    odin_dir = tmp_path / ".odin"
    odin_dir.mkdir()
    infra_dir = odin_dir / "infra"
    infra_dir.mkdir()
    registry_path = odin_dir / "registry.json"
    registry_path.write_text('{"resources": {}}')
    canvas_path = odin_dir / "canvas.json"
    engine = MotoEngine()
    engine.start()
    registry = ResourceRegistry(registry_path)
    with patch("odin.server.CANVAS_PATH", canvas_path):
        yield engine, registry, tmp_path
    engine.stop()


def test_canvas_round_trip(setup):
    """Save and load canvas via API."""
    engine, registry, tmp_path = setup
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app, raise_server_exceptions=True)

    graph = {
        "nodes": [
            {"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "test-bucket"}},
        ],
        "edges": [],
    }
    resp = client.post("/canvas", json=graph)
    assert resp.status_code == 200

    resp = client.get("/canvas")
    assert resp.status_code == 200
    assert resp.json()["nodes"][0]["data"]["label"] == "test-bucket"


def test_state_endpoint_returns_new_statuses(setup):
    """State endpoint uses new status values (draft, validated, live)."""
    engine, registry, _ = setup
    registry.register("s3_test", service="s3", file_path=".odin/infra/s3_test.py")

    app = create_app(engine=engine, registry=registry)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/state")
    resources = resp.json()["resources"]
    assert resources[0]["status"] == "draft"


def test_health_endpoint(setup):
    """Health endpoint works after new wiring."""
    engine, registry, _ = setup
    app = create_app(engine=engine, registry=registry)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.integration
async def test_validate_and_deploy_flow():
    """Full flow: validate canvas -> check validated -> deploy -> check live.
    Requires ANTHROPIC_API_KEY and is skipped by default."""
    # This test calls the real Claude Agent SDK
    # Left as a template -- flesh out when running integration tests
    pass
