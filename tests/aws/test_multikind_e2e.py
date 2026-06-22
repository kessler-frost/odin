"""M2/M3 — a richer real scenario: app + Postgres + Redis dep + batch job.

Validates all the new workload kinds end-to-end with real containers:
- rds  -> real Postgres (MiniStack control plane + allfather's runner)
- dep  -> real Redis, publishes its endpoint
- service -> app gated on BOTH db and cache, reading the injected URLs
- batch -> run-to-completion (exits 0 -> done)

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

APP_SCRIPT = (
    "import os,http.server\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    " def do_GET(s):\n"
    "  ok=bool(os.environ.get('DATABASE_URL')) and bool(os.environ.get('REDIS'))\n"
    "  s.send_response(200 if ok else 503); s.end_headers(); s.wfile.write(b'ok')\n"
    " def log_message(s,*a): pass\n"
    "http.server.HTTPServer(('0.0.0.0',8000),H).serve_forever()\n"
)

CANVAS = {
    "nodes": [
        {"type": "rds", "data": {"label": "db"}},
        {"type": "dep", "data": {"label": "cache", "image": "redis:7-alpine", "port": 6379}},
        {"type": "batch", "data": {"label": "migrate", "image": "busybox:latest",
                                   "command": ["true"]}},
        {"type": "service", "data": {
            "label": "api", "image": "python:3.12-slim", "port": 8000,
            "command": ["python", "-c", APP_SCRIPT],
            "env": {"DATABASE_URL": "${{db.DATABASE_URL}}", "REDIS": "${{cache.endpoint}}"}}},
    ],
    "edges": [],
}


def _phases(client):
    return {r["id"]: r["phase"] for r in client.get("/world").json()["resources"]}


def _wait(client, predicate, timeout=180.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(_phases(client)):
            return _phases(client)
        time.sleep(2)
    raise AssertionError(f"not met within {timeout}s (last={_phases(client)})")


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for cid in rt.list_allfather():
        rt._docker("rm", "-f", cid, check=False)


def test_multikind_slice(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path), embed=True, complete=False)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        _wait(client, lambda p: (
            p.get("db") == "healthy" and p.get("cache") == "healthy"
            and p.get("api") == "healthy" and p.get("migrate") == "done"
        ))
        client.post("/destroy")
        _wait(client, lambda p: not p)
    assert runtime.list_allfather() == []
