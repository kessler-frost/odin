"""S2 -- the `/tf/*` routes: the 409 preconditions (no tofu on PATH, a run
already in flight) and the operator-principal wiring. The real tofu
round-trip through the real gateway is `test_tf_runner_e2e.py`
(integration); this file only exercises route<->runner wiring with fakes."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID
from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.simulate.runner import SimulateBusy, TfResult
from odin.spec.models import FieldValue, ResourceDesired, Stack
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack


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


class FakeRds:
    async def create_db(self, db_id, user, pw):
        pass

    async def delete_db(self, db_id):
        pass

    async def endpoint(self, db_id):
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


# --- owner directive B1: pre-apply admission control -----------------------


class _LowMemRuntime(FakeRuntime):
    async def ensure_host(self):
        return HostFacts(total_mem_mib=1000.0)


def test_tf_apply_409_when_the_stack_exceeds_the_memory_budget(tmp_path, monkeypatch):
    # See the twin in test_apply_full.py: the VM pool reads real host RAM.
    monkeypatch.setenv("ODIN_VM_MEMORY_BUDGET_MIB", "1000")
    app = create_app(runtime=_LowMemRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    stack = Stack(env="default", resources=tuple(
        ResourceDesired(id=f"web{i}", kind="ec2", fields={"instanceType": FieldValue(value="t3.medium")})
        for i in range(50)
    ))
    app.state.store.apply(stack)
    with TestClient(app) as client:
        resp = client.post("/tf/apply", params={"env": "default"})
    assert resp.status_code == 409
    body = resp.json()
    assert "GiB" in body["error"] and "reduce instance sizes or apply fewer nodes" in body["error"]
    assert body["estimated_mib"] > body["budget_mib"]


# --- field test 3: /tf/plan, the safe drift check --------------------------


def _plan_returning(result: TfResult):
    async def _plan(*args, **kwargs):
        return result

    return _plan


def test_tf_plan_409_when_tofu_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/tf/plan", params={"env": "default"})
    assert resp.status_code == 409
    assert resp.json() == {"error": "tofu not installed", "fix": "brew install opentofu"}


def test_tf_plan_409_when_a_run_is_already_in_flight(tmp_path):
    app = _app(tmp_path)

    async def _busy(*args, **kwargs):
        raise SimulateBusy("default")

    app.state.tf_runner.plan = _busy
    with TestClient(app) as client:
        resp = client.post("/tf/plan", params={"env": "default"})
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["error"]


def test_tf_plan_reports_no_changes_on_exit_zero(tmp_path):
    app = _app(tmp_path)
    app.state.tf_runner.plan = _plan_returning(TfResult(ok=True, exit_code=0, tail=("No changes.",)))
    with TestClient(app) as client:
        resp = client.post("/tf/plan", params={"env": "default"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_changes"
    assert body["exit_code"] == 0
    assert body["tail"] == ["No changes."]


def test_tf_plan_reports_changes_on_exit_two(tmp_path):
    """`-detailed-exitcode`'s 2 is drift, not a failed command -- so it is a
    200 with `status: changes`, and the exit code rides through untouched."""
    app = _app(tmp_path)
    app.state.tf_runner.plan = _plan_returning(TfResult(ok=True, exit_code=2, tail=("Plan: 1 to add",)))
    with TestClient(app) as client:
        resp = client.post("/tf/plan", params={"env": "default"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "changes"
    assert body["exit_code"] == 2


def test_tf_plan_reports_failed_on_a_real_error(tmp_path):
    app = _app(tmp_path)
    app.state.tf_runner.plan = _plan_returning(TfResult(ok=False, exit_code=1, tail=("Error: boom",)))
    with TestClient(app) as client:
        resp = client.post("/tf/plan", params={"env": "default"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "failed"
    assert body["tail"] == ["Error: boom"]


def test_tf_plan_commits_no_stack_revision(tmp_path):
    """A drift check must not mutate the spec store: same HEAD before/after."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env="default", resources=(ResourceDesired(id="uploads", kind="s3"),)))
    app = create_app(runtime=FakeRuntime(), store=store, rds=FakeRds(), backings=False)
    app.state.tf_runner.plan = _plan_returning(TfResult(ok=True, exit_code=0))
    before = store.head("default")

    with TestClient(app) as client:
        assert client.post("/tf/plan", params={"env": "default"}).status_code == 200

    assert store.head("default") == before


def test_tf_status_for_a_never_applied_env(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/tf/status", params={"env": "default"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is False
    assert body["workspace_exists"] is False
    assert body["last"] is None


# --- v0.7.4: what "not covered by this plan" actually describes ------------
#
# v0.7.3 had `odin tf plan` GET /canvas and derive `skipped` from the canvas
# drawn NOW, then union it into `not_covered` beside an `unsupported` derived
# from the LAST-APPLIED Stack -- two different things in one array, with
# nothing saying so. Both halves are computed here now (one source of truth,
# and `curl /tf/plan` gets them), and when they no longer describe the same
# thing the payload says which is which.

TYPO_CANVAS = {
    "nodes": [
        {"id": "n0", "type": "s3", "data": {"label": "uploads"}},
        {"id": "n1", "type": "kinesis", "data": {"label": "stream"}},
    ],
    "edges": [],
}


def _plan_app(tmp_path, result: TfResult, canvas: dict | None = None):
    """An app whose /tf/plan returns `result`, with `canvas` already saved --
    at the STORE's own canvas path, so this reads the test's canvas and never
    the checkout's real one.

    The canvas is PER-ENV now (`.odin/<env>/canvas.json`), which is also what
    makes `canvas_drift` meaningful: /tf/plan compares this env's canvas against
    this env's stack, where it used to compare one global file against every
    env's stack."""
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    app.state.tf_runner.plan = _plan_returning(result)
    if canvas is not None:
        target = tmp_path / "default" / "canvas.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(canvas))
    return app


def test_tf_plan_publishes_the_gate_field_the_cli_used_to_compute(tmp_path):
    app = _plan_app(tmp_path, TfResult(ok=True, exit_code=0), TYPO_CANVAS)
    with TestClient(app) as client:
        body = client.post("/tf/plan", params={"env": "default"}).json()
    assert body["skipped"] == ["kinesis"]
    assert body["not_covered"] == ["kinesis"]


def test_tf_plan_says_so_when_the_canvas_is_not_what_this_env_applied(tmp_path):
    """THE correctness fix. Nothing has been applied to this env, so the plan
    covers an EMPTY Stack while the saved canvas has two nodes -- the skipped
    list describes something the plan never saw. The payload names the
    mismatch instead of quietly presenting them as one thing."""
    app = _plan_app(tmp_path, TfResult(ok=True, exit_code=0), TYPO_CANVAS)
    with TestClient(app) as client:
        body = client.post("/tf/plan", params={"env": "default"}).json()
    assert body["canvas_drift"] is True
    assert "not what env 'default' last applied" in body["note"]
    assert "`skipped` describes the saved canvas" in body["note"]


def test_tf_plan_reports_no_drift_when_the_canvas_is_exactly_what_was_applied(tmp_path):
    """...and no false alarm: apply the canvas first, and the two halves DO
    describe the same thing, so there is no note to print."""
    store = SpecStore(tmp_path)
    store.apply(canvas_to_stack(TYPO_CANVAS, env="default"))  # what an Apply commits
    app = _plan_app(tmp_path, TfResult(ok=True, exit_code=0), TYPO_CANVAS)
    with TestClient(app) as client:
        body = client.post("/tf/plan", params={"env": "default"}).json()
    assert body["canvas_drift"] is False
    assert "note" not in body
    assert body["not_covered"] == ["kinesis"]  # still reported -- it IS outside the plan


def test_tf_plan_survives_a_store_with_no_saved_canvas_at_all(tmp_path):
    app = _plan_app(tmp_path, TfResult(ok=True, exit_code=0))  # nothing drawn yet
    with TestClient(app) as client:
        body = client.post("/tf/plan", params={"env": "default"}).json()
    assert body["skipped"] == []
    assert body["not_covered"] == []
    assert body["canvas_drift"] is False


def test_tf_apply_publishes_not_covered_too(tmp_path, monkeypatch):
    """One gate shape across every route: this one applies the STORED Stack, so
    the union is `unsupported` and there is no canvas half to confuse it with."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env="default", resources=(
        ResourceDesired(id="cache1", kind="elasticache",
                        fields={"engine": FieldValue(value="memcached", provenance="user")}),
    )))
    app = create_app(runtime=FakeRuntime(), store=store, rds=FakeRds(), backings=False)
    app.state.tf_runner.apply = _plan_returning(TfResult(ok=True, exit_code=0))
    with TestClient(app) as client:
        body = client.post("/tf/apply", params={"env": "default"}).json()
    assert body["not_covered"] == body["unsupported"]
