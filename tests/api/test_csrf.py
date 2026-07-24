"""Security finding #1c: CSRF defense-in-depth. odin has no authentication of
its own, so a cross-origin browser POST is the attack this middleware blocks
-- a browser always sends `Origin` on a cross-site state-changing request;
curl/the CLI/an agent's own HTTP client send neither, so only a browser
acting on a page odin didn't serve is ever rejected."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from odin.server import _is_loopback_origin, create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime


@pytest.fixture
def client(tmp_path):
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as c:
        yield c


CANVAS = {"nodes": [], "edges": []}


def test_cross_origin_post_is_rejected(client):
    resp = client.post("/canvas", json=CANVAS, headers={"origin": "http://evil.example"})
    assert resp.status_code == 403


def test_no_origin_post_is_allowed(client):
    resp = client.post("/canvas", json=CANVAS)
    assert resp.status_code == 200


def test_loopback_origin_post_is_allowed(client):
    resp = client.post("/canvas", json=CANVAS, headers={"origin": "http://127.0.0.1:4200"})
    assert resp.status_code == 200


def test_localhost_origin_post_is_allowed(client):
    resp = client.post("/canvas", json=CANVAS, headers={"origin": "http://localhost:4200"})
    assert resp.status_code == 200


def test_cross_origin_referer_without_origin_is_also_rejected(client):
    resp = client.post("/canvas", json=CANVAS, headers={"referer": "http://evil.example/attack.html"})
    assert resp.status_code == 403


def test_loopback_referer_without_origin_is_allowed(client):
    resp = client.post("/canvas", json=CANVAS, headers={"referer": "http://127.0.0.1:4200/"})
    assert resp.status_code == 200


def test_get_requests_are_never_blocked_regardless_of_origin(client):
    resp = client.get("/canvas", headers={"origin": "http://evil.example"})
    assert resp.status_code == 200


def test_cross_origin_apply_full_style_post_is_rejected(client):
    resp = client.post("/apply", json=CANVAS, headers={"origin": "http://attacker.example:1234"})
    assert resp.status_code == 403


@pytest.mark.parametrize("origin,expected", [
    ("http://127.0.0.1:4200", True),
    ("http://localhost:4200", True),
    ("https://localhost", True),
    ("http://[::1]:4200", True),
    ("http://192.168.1.5:4200", False),
    ("http://evil.example", False),
    ("http://127.0.0.1.evil.example", False),
])
def test_is_loopback_origin_classifies_hosts_correctly(origin, expected):
    assert _is_loopback_origin(origin) is expected
