import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odin.api.canvas import (
    create_canvas_router,
    hcl_name,
    node_reg_name,
    node_tf_address,
)


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
        "nodes": [
            {
                "id": "vpc-1", "type": "vpc",
                "position": {"x": 100, "y": 80},
                "size": {"width": 560, "height": 380},
                "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"},
            }
        ],
        "edges": [{"id": "e1", "source": "ec2-1", "target": "s3-1"}],
    }
    assert client.post("/canvas", json=canvas).status_code == 200
    data = client.get("/canvas").json()
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["id"] == "vpc-1"
    assert len(data["edges"]) == 1


def test_post_canvas_persists_to_file(client, canvas_path):
    client.post(
        "/canvas",
        json={"nodes": [{"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "test"}}], "edges": []},
    )
    assert canvas_path.exists()
    assert len(json.loads(canvas_path.read_text())["nodes"]) == 1


def test_post_canvas_overwrites(client):
    client.post("/canvas", json={"nodes": [{"id": "a", "type": "ec2", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
    client.post("/canvas", json={"nodes": [{"id": "b", "type": "s3", "position": {"x": 0, "y": 0}, "data": {}}], "edges": []})
    nodes = client.get("/canvas").json()["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["id"] == "b"


# --- canvas node helpers ---

def test_hcl_name_sanitizes():
    assert hcl_name("prod-vpc") == "prod_vpc"
    assert hcl_name("Web Server 1") == "web_server_1"
    assert hcl_name("123bucket").startswith("r_")  # must not start with a digit


def test_node_reg_name():
    node = {"id": "vpc-1", "type": "vpc", "data": {"label": "prod-vpc"}}
    assert node_reg_name(node) == ("prod-vpc", "vpc_prod-vpc")


def test_node_tf_address():
    node = {"id": "ec2-1", "type": "ec2", "data": {"label": "web server"}}
    assert node_tf_address(node) == "aws_instance.web_server"


def test_node_tf_address_unknown_type():
    assert node_tf_address({"id": "x", "type": "mystery", "data": {"label": "y"}}) is None
