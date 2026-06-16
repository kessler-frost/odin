import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odin.api.canvas import create_validate_router


class FakeOrchestrator:
    async def validate(self, graph):
        return
        yield  # async generator that yields nothing


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(create_validate_router(FakeOrchestrator()))
    return TestClient(app)


def test_validate_returns_200(client):
    graph = {
        "nodes": [{"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "test"}}],
        "edges": [],
    }
    resp = client.post("/validate", json=graph)
    assert resp.status_code == 200
    assert "status" in resp.json()


def test_validate_empty_graph(client):
    resp = client.post("/validate", json={"nodes": [], "edges": []})
    assert resp.status_code == 200
