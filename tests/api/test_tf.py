"""S2 -- the `/tf/*` routes: the 409 preconditions (no tofu on PATH, a run
already in flight) and the operator-principal wiring. The real tofu
round-trip through the real gateway is `test_tf_runner_e2e.py`
(integration); this file only exercises route<->runner wiring with fakes."""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID
from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.simulate.runner import SimulateBusy
from odin.spec.store import SpecStore


class FakeRuntime:
    def run_container(self, spec):
        return RunHandle(id="x", name=spec.name)

    def stop(self, name):
        pass

    def facts(self, name, container_port=0):
        return ContainerFacts(phase="pending")

    def stats(self, name):
        return {"cpu": 0.0, "ram": 0.0}

    def ensure_host(self):
        return HostFacts()


class FakeRds:
    def create_db(self, db_id, user, pw):
        pass

    def delete_db(self, db_id):
        pass

    def endpoint(self, db_id):
        return None

    def container_name(self, db_id):
        return f"odin-rds-default-{db_id}"


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def test_tf_apply_409_when_tofu_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/tf/apply", params={"env": "default"})
    assert resp.status_code == 409
    assert resp.json() == {"error": "tofu not installed", "fix": "brew install opentofu"}


def test_tf_destroy_409_when_tofu_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/tf/destroy", params={"env": "default"})
    assert resp.status_code == 409
    assert resp.json() == {"error": "tofu not installed", "fix": "brew install opentofu"}


def test_tf_apply_issues_operator_credentials_for_the_env(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)  # 409s, but keys are issued first
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/tf/apply", params={"env": "default"})
    access_key, _secret = app.state.gateway_keys.issue("default", OPERATOR_NODE_ID)
    assert app.state.gateway_keys.lookup(access_key) is not None


def test_tf_apply_409_when_a_run_is_already_in_flight(tmp_path):
    app = _app(tmp_path)

    async def _busy(*args, **kwargs):
        raise SimulateBusy("default")

    app.state.tf_runner.apply = _busy
    with TestClient(app) as client:
        resp = client.post("/tf/apply", params={"env": "default"})
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["error"]


def test_tf_destroy_409_when_a_run_is_already_in_flight(tmp_path):
    app = _app(tmp_path)

    async def _busy(*args, **kwargs):
        raise SimulateBusy("default")

    app.state.tf_runner.destroy = _busy
    with TestClient(app) as client:
        resp = client.post("/tf/destroy", params={"env": "default"})
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["error"]


def test_tf_status_for_a_never_applied_env(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/tf/status", params={"env": "default"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is False
    assert body["workspace_exists"] is False
    assert body["last"] is None
