import json

import pytest
from fastapi.testclient import TestClient

from odin.api.canvas import create_canvas_router


@pytest.fixture
def canvas_path(tmp_path):
    return tmp_path / "canvas.json"


@pytest.fixture
def client(canvas_path):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_canvas_router(canvas_path))
    return TestClient(app)


def test_get_canvas_empty(client, canvas_path):
    """GET /canvas returns empty graph when no file exists."""
    resp = client.get("/canvas")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"nodes": [], "edges": []}


def test_post_and_get_canvas(client, canvas_path):
    """POST /canvas saves, GET /canvas retrieves."""
    canvas = {
        "nodes": [
            {
                "id": "vpc-1",
                "type": "vpc",
                "position": {"x": 100, "y": 80},
                "size": {"width": 560, "height": 380},
                "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"},
            }
        ],
        "edges": [{"id": "e1", "source": "ec2-1", "target": "s3-1"}],
    }
    resp = client.post("/canvas", json=canvas)
    assert resp.status_code == 200

    resp = client.get("/canvas")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["id"] == "vpc-1"
    assert len(data["edges"]) == 1


def test_post_canvas_persists_to_file(client, canvas_path):
    """Canvas data is written to disk."""
    canvas = {"nodes": [{"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "test"}}], "edges": []}
    client.post("/canvas", json=canvas)

    assert canvas_path.exists()
    on_disk = json.loads(canvas_path.read_text())
    assert len(on_disk["nodes"]) == 1


def test_post_canvas_overwrites(client, canvas_path):
    """Subsequent POSTs overwrite previous canvas."""
    client.post("/canvas", json={"nodes": [{"id": "a", "type": "ec2", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
    client.post("/canvas", json={"nodes": [{"id": "b", "type": "s3", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})

    resp = client.get("/canvas")
    assert len(resp.json()["nodes"]) == 1
    assert resp.json()["nodes"][0]["id"] == "b"
