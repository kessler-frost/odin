"""S3b -- the `/translate` route. The real SDK pass is never exercised here
(unit-level, no `integration` mark): an empty stack makes `translate()`
short-circuit before touching the SDK at all (see tests/agent/test_translate.py
for the SDK-pass tests via a fake client), and the wiring test monkeypatches
`translate_mod.translate` directly, mirroring how test_tf.py verifies
/tf/* route<->runner wiring with fakes."""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.agent.translate import TranslateResult
from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.spec.models import ResourceDesired, Stack
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
        return f"allfather-rds-default-{db_id}"


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def test_translate_empty_stack_returns_skeleton_shape_without_an_sdk_call(tmp_path):
    # No /apply was ever called for this env -> store.get_stack returns an
    # empty Stack -> translate() short-circuits before constructing any SDK
    # client, so this is safe to run without the `integration` mark.
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/translate", params={"env": "default"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["refined"] is False
    assert body["notes"] == []
    assert body["unsupported"] == []
    assert "main.tf" in body["files"]


def test_translate_route_uses_the_env_stack_and_returns_the_result_verbatim(tmp_path, monkeypatch):
    app = _app(tmp_path)
    store: SpecStore = app.state.store
    stack = Stack(env="default", resources=(ResourceDesired(id="uploads", kind="s3"),))
    store.apply(stack)

    seen_stacks = []

    async def fake_translate(passed_stack, **kwargs):
        seen_stacks.append(passed_stack)
        return TranslateResult(files={"main.tf": "fake"}, notes=["hi"], unsupported=[], refined=True)

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    with TestClient(app) as client:
        resp = client.post("/translate", params={"env": "default"})
    assert resp.status_code == 200
    assert resp.json() == {
        "files": {"main.tf": "fake"}, "notes": ["hi"], "unsupported": [], "refined": True, "binary_files": {},
    }
    assert seen_stacks == [stack]


def test_translate_with_a_graph_body_previews_the_unsaved_canvas_not_the_stored_stack(tmp_path, monkeypatch):
    # Finding #1 (release sweep, fw2-ui): the stored Stack (last /apply) can
    # lag behind what's actually drawn on the canvas. A CanvasGraph body lets
    # the caller preview the CURRENT canvas -- same shape /apply-full takes --
    # instead of whatever was last applied.
    app = _app(tmp_path)
    store: SpecStore = app.state.store
    store.apply(Stack(env="default", resources=(ResourceDesired(id="uploads", kind="s3"),)))

    seen_stacks = []

    async def fake_translate(passed_stack, **kwargs):
        seen_stacks.append(passed_stack)
        return TranslateResult(files={"main.tf": "fake"}, notes=[], unsupported=[], refined=False)

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    graph = {
        "nodes": [{"id": "n1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "unsaved-bucket"}}],
        "edges": [],
    }
    with TestClient(app) as client:
        resp = client.post("/translate", params={"env": "default"}, json=graph)
    assert resp.status_code == 200
    assert len(seen_stacks) == 1
    assert [r.id for r in seen_stacks[0].resources] == ["unsaved-bucket"]
