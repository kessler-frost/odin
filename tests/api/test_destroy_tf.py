"""Release finding #5 -- `/destroy` parity: the route must run the tofu
half too (whatever tofu created -- vpc/subnet/sg have NO reconciler-driven
teardown path at all) before pruning the reconciler's own half, not just
empty the Stack and walk away leaving tofu's state to lie forever.

Unit-level only (fakes for runner.destroy, no real tofu/workspace
materialization) -- the existing integration suites
(test_apply_full_e2e.py, test_gateway_e2e.py, test_backings_e2e.py) already
prove the real tofu round-trip for the sibling /apply-full and /tf/* routes,
so this file asserts the *wiring*: destroy calls runner.destroy exactly
when a workspace exists, honors the same 409/tofu-unavailable semantics
/tf/destroy already has, and the reconciler half always still runs after.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate.runner import SimulateBusy, TfResult, TofuNotInstalled
from odin.spec.store import SpecStore
from tests.api.test_apply import CANVAS, FakeRds, FakeRuntime


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def _make_workspace(tmp_path, env: str = "default") -> None:
    (tmp_path / env / "tf").mkdir(parents=True, exist_ok=True)


def test_destroy_skips_tofu_when_no_workspace_ever_existed(tmp_path):
    app = _app(tmp_path)
    calls = []

    async def _destroy(*args, **kwargs):
        calls.append(args)
        return TfResult(ok=True, exit_code=0)

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] is None
    assert calls == []  # never called -- nothing was ever applied through tofu


def test_destroy_runs_tofu_destroy_when_a_workspace_exists(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)
    calls = []
    principals = []

    async def _destroy(env, gateway_port, access_key, secret_key, **kwargs):
        calls.append((env, gateway_port, access_key, secret_key))
        # Captured DURING the call -- /destroy revokes the env's keys
        # (including this operator credential) right after, so the
        # principal must be resolved here, not after the request returns.
        principals.append(app.state.gateway_keys.lookup(access_key))
        return TfResult(ok=True, exit_code=0)

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] == {"status": "ok", "exit_code": 0}
    assert len(calls) == 1
    env, _gateway_port, _access_key, _secret_key = calls[0]
    assert env == "default"
    # operator credentials, the same wiring /tf/destroy uses
    assert principals[0].node_id == OPERATOR_NODE_ID


def test_destroy_reports_tofu_failure_but_still_prunes_the_reconciler_half(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)

    async def _destroy(*args, **kwargs):
        return TfResult(ok=False, exit_code=1, tail=("boom",))

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] == {"status": "failed", "exit_code": 1, "tail": ["boom"]}
    world = client.get("/world").json()
    assert world["resources"] == []  # the reconciler half still pruned


def test_destroy_tofu_unavailable_proceeds_with_reconciler_half_only(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)

    async def _destroy(*args, **kwargs):
        raise TofuNotInstalled()

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        access_key, _secret = app.state.gateway_keys.issue("default", "db")
        resp = client.post("/destroy")
    assert resp.status_code == 200  # not a request-level error
    body = resp.json()
    assert body["tf"]["status"] == "unavailable"
    assert client.get("/world").json()["resources"] == []  # reconciler half still ran
    assert app.state.gateway_keys.lookup(access_key) is None  # keys still revoked


def test_destroy_409_when_a_tofu_run_is_already_in_progress(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)
    app.state.tf_runner.status = lambda env: {"env": env, "running": True, "workspace_exists": True, "last": None}

    async def _destroy(*args, **kwargs):  # must never be called -- the guard fires first
        raise AssertionError("runner.destroy should not run while busy")

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["error"]
    # nothing was pruned -- the busy guard runs before any mutation
    assert client.get("/world").json()["resources"] != []


def test_destroy_409_when_a_run_races_in_after_the_guard(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)

    async def _busy(*args, **kwargs):
        raise SimulateBusy("default")

    app.state.tf_runner.destroy = _busy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["error"]
