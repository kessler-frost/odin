import json
import stat

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


def test_post_canvas_creates_missing_parent_dir(tmp_path):
    # A fresh start (no .odin yet): saving the canvas must create the dir, not 500.
    path = tmp_path / "fresh" / "canvas.json"
    app = FastAPI()
    app.include_router(create_canvas_router(path))
    with TestClient(app) as fresh_client:
        resp = fresh_client.post("/canvas", json={"nodes": [{"id": "a", "type": "service",
                                  "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
        assert resp.status_code == 200
        assert path.exists()


def test_post_canvas_persists_and_overwrites(client, canvas_path):
    client.post("/canvas", json={"nodes": [{"id": "a", "type": "rds", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
    assert canvas_path.exists()
    client.post("/canvas", json={"nodes": [{"id": "b", "type": "s3", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
    nodes = client.get("/canvas").json()["nodes"]
    assert len(nodes) == 1 and nodes[0]["id"] == "b"
    assert len(json.loads(canvas_path.read_text())["nodes"]) == 1


def test_post_canvas_refuses_a_structurally_broken_canvas_and_stores_nothing(client, canvas_path):
    """Field test 4, P4-5: `canvas set` used to accept anything that was valid
    JSON, so a canvas that could never be applied sat on disk until the next
    translate or apply tripped over it with a 500."""
    resp = client.post("/canvas", json={
        "nodes": [{"id": "s3", "type": "s3", "data": {"label": ["s3", "p2-assets"]}}],
        "edges": [{"source": "s3", "target": "s3", "data": {"edgeType": "iam"}}],
    })
    assert resp.status_code == 422
    assert "data.label" in json.dumps(resp.json())
    assert not canvas_path.exists()


def test_post_canvas_still_accepts_a_kind_odin_cannot_build(client):
    """The boundary: an unsupported KIND is well-formed. It is stored, applied
    and reported as skipped -- refusing it here would break that."""
    canvas = {"nodes": [{"id": "k1", "type": "kinesis", "position": {"x": 0, "y": 0},
                         "data": {"label": "stream"}}], "edges": []}
    assert client.post("/canvas", json=canvas).status_code == 200
    assert client.get("/canvas").json() == canvas


def test_get_canvas_returns_a_hand_broken_file_verbatim(client, canvas_path):
    """`odin canvas get` is the command you REPAIR a bad canvas with, so it
    must be able to read one back. (POST is where the shape is enforced.)"""
    broken = {"nodes": [{"id": "s3", "type": "s3", "data": {"label": ["s3", "p2-assets"]}}], "edges": []}
    canvas_path.write_text(json.dumps(broken))
    resp = client.get("/canvas")
    assert resp.status_code == 200 and resp.json() == broken


def test_post_canvas_writes_the_file_0600(client, canvas_path):
    # Security finding #3a: a node's fields can carry a cleartext secret
    # (an rds `password`) -- 0600 is the only thing stopping another local
    # account from reading it.
    client.post("/canvas", json={"nodes": [{"id": "a", "type": "rds", "position": {"x": 0, "y": 0},
                                             "data": {"password": "s3cr3t"}}], "edges": []})
    assert stat.S_IMODE(canvas_path.stat().st_mode) == 0o600
