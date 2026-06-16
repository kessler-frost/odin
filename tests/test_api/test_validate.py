from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odin.api.canvas import create_validate_router


@pytest.fixture
def mock_agent():
    agent = AsyncMock()

    async def fake_validate(graph):
        return
        yield  # makes this an async generator that yields nothing

    agent.validate = fake_validate
    agent.is_running = True
    return agent


@pytest.fixture
def client(mock_agent):
    app = FastAPI()
    app.include_router(create_validate_router(mock_agent))
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
