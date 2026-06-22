import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odin.api.canvas import create_canvas_router


@pytest.fixture
def canvas_path(tmp_path):
    return tmp_path / "canvas.json"


@pytest.fixture
def client(canvas_path):
    app = FastAPI()
    app.include_router(create_canvas_router(canvas_path))
    return TestClient(app)


def test_get_canvas_empty(client):
    resp = client.get("/canvas")
    assert resp.status_code == 200
    assert resp.json() == {"nodes": [], "edges": []}


def test_post_and_get_canvas(client):
    canvas = {
        "nodes": [{"id": "n1", "type": "service", "position": {"x": 100, "y": 80},
                   "data": {"label": "api"}}],
        "edges": [{"id": "e1", "source": "api", "target": "db"}],
    }
    assert client.post("/canvas", json=canvas).status_code == 200
    data = client.get("/canvas").json()
    assert len(data["nodes"]) == 1 and data["nodes"][0]["id"] == "n1"
    assert len(data["edges"]) == 1


def test_post_canvas_persists_and_overwrites(client, canvas_path):
    client.post("/canvas", json={"nodes": [{"id": "a", "type": "rds", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
    assert canvas_path.exists()
    client.post("/canvas", json={"nodes": [{"id": "b", "type": "s3", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
    nodes = client.get("/canvas").json()["nodes"]
    assert len(nodes) == 1 and nodes[0]["id"] == "b"
    assert len(json.loads(canvas_path.read_text())["nodes"]) == 1
