"""S3 — full walking-skeleton slice through the real stack (no browser).

create_app(embed=True) + real Colima + embedded MiniStack: apply a canvas with
an app node + an RDS node wired by ${{db.DATABASE_URL}}, watch both reach
healthy (real Postgres + real app container that received the injected URL),
kill the app -> auto-restart, destroy -> clean teardown.

Marked `integration`: needs Colima/Docker. Run with `-m integration`.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

# A tiny app: HTTP 200 iff it received DATABASE_URL (proves the ref injection).
APP_SCRIPT = (
    "import os,http.server\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    " def do_GET(s):\n"
    "  ok=bool(os.environ.get('DATABASE_URL'))\n"
    "  s.send_response(200 if ok else 503); s.end_headers(); s.wfile.write(b'ok')\n"
    " def log_message(s,*a): pass\n"
    "http.server.HTTPServer(('0.0.0.0',8000),H).serve_forever()\n"
)

CANVAS = {
    "nodes": [
        {"type": "rds", "data": {"label": "db"}},
        {"type": "service", "data": {
            "label": "api",
            "image": "python:3.12-slim",
            "port": 8000,
            "command": ["python", "-c", APP_SCRIPT],
            "env": {"DATABASE_URL": "${{db.DATABASE_URL}}"},
        }},
    ],
    "edges": [],
}


def _phases(client) -> dict:
    return {r["id"]: r["phase"] for r in client.get("/world").json()["resources"]}


def _wait(client, predicate, timeout=150.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(_phases(client)):
            return _phases(client)
        time.sleep(2)
    raise AssertionError(f"condition not met within {timeout}s (last={_phases(client)})")


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for cid in rt.list_allfather():
        rt._docker("rm", "-f", cid, check=False)


def test_skeleton_slice_end_to_end(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path), embed=True, complete=False)
    with TestClient(app) as client:
        assert client.post("/apply", json=CANVAS).json()["status"] == "applied"

        # both come up: real Postgres, then the app once DATABASE_URL resolves
        _wait(client, lambda p: p.get("db") == "healthy" and p.get("api") == "healthy")

        # the app really received the resolved DATABASE_URL (its 200 depends on it)
        world = {r["id"]: r for r in client.get("/world").json()["resources"]}
        assert world["db"]["facts"]["DATABASE_URL"].startswith("postgresql://")
        assert world["api"]["facts"]["endpoint"].startswith("http://127.0.0.1:")

        # kill the app container -> the Reconciler restarts it
        runtime.stop("api")
        _wait(client, lambda p: p.get("api") == "healthy")

        # destroy -> clean teardown
        client.post("/destroy")
        _wait(client, lambda p: "api" not in p and "db" not in p)
    assert runtime.list_allfather() == []
