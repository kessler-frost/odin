"""`GET/POST /ai` — the switch behind the top bar's AI toggle.

Model calls are OFF until someone turns them on (owner decision, 2026-07-28).
The env var stays the ops override: a CI job running `ODIN_AI=0` must not be
quietly overruled by a preference file, and `ODIN_AI=1` must still force calls on
for anyone driving odin headlessly.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.agent import ai
from odin.runtime.driver import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.spec.store import SpecStore


class FakeRuntime:
    async def run_container(self, spec):
        return RunHandle(id="x", name=spec.name)

    async def stop(self, name):
        pass

    async def facts(self, name, container_port=0):
        return ContainerFacts(phase="pending")

    async def stats(self, name):
        return {"cpu": 0.0, "ram": 0.0}

    async def ensure_host(self):
        return HostFacts()


def _app(tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_AI", raising=False)
    monkeypatch.setattr(ai, "STATE_FILE", tmp_path / "ai.json")
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)


def test_it_starts_off(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        body = client.get("/ai").json()
    assert body["enabled"] is False
    assert body["source"] == "switch"
    assert "switch is off" in body["reason"]


def test_turning_it_on_and_back_off(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        on = client.post("/ai", json={"enabled": True}).json()
        assert on["enabled"] is True and on["reason"] is None
        assert client.get("/ai").json()["enabled"] is True, "and it persists across reads"

        off = client.post("/ai", json={"enabled": False}).json()
        assert off["enabled"] is False
        assert "switch is off" in off["reason"]


def test_an_env_var_wins_and_says_so(tmp_path, monkeypatch):
    """The UI must not offer a control that silently does nothing: `source`
    tells it to render the switch disabled with the reason."""
    monkeypatch.setattr(ai, "STATE_FILE", tmp_path / "ai.json")
    ai.set_runtime_enabled(True)
    monkeypatch.setenv("ODIN_AI", "0")
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)

    with TestClient(app) as client:
        body = client.get("/ai").json()
        # ...and a POST cannot talk it round.
        after = client.post("/ai", json={"enabled": True}).json()

    assert body["source"] == "env"
    assert body["enabled"] is False
    assert after["enabled"] is False, "ODIN_AI=0 must beat the switch"


def test_the_switch_survives_a_new_app(tmp_path, monkeypatch):
    """A server restart must not silently re-disable a user's choice."""
    monkeypatch.delenv("ODIN_AI", raising=False)
    monkeypatch.setattr(ai, "STATE_FILE", tmp_path / "ai.json")
    first = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)
    with TestClient(first) as client:
        client.post("/ai", json={"enabled": True})

    second = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)
    with TestClient(second) as client:
        assert client.get("/ai").json()["enabled"] is True
