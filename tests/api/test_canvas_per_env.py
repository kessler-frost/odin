"""The canvas is PER-ENVIRONMENT (owner decision, 2026-07-27).

It used to be one global `.odin/canvas.json` shared by every environment, and
`?env=` was silently IGNORED on this route while `/world`, `/apply` and
`/destroy` all honoured it. That made `/canvas` the only route where the
parameter was a lie, and it meant two environments could never hold different
architectures -- only the same one applied twice.

The migration matters as much as the split: a user upgrading has real content in
the global file, and moving the read without moving the file would make their
architecture appear to VANISH. That is precisely the silently-empty-canvas
failure v0.7.7 was spent fixing, so it is pinned here.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from odin.server import CANVAS_NAME, _migrate_global_canvas, create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime

ONE = {"nodes": [{"id": "s1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "one"}}], "edges": []}
TWO = {"nodes": [{"id": "s2", "type": "sqs", "position": {"x": 20, "y": 0}, "data": {"label": "two"}}], "edges": []}


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def test_two_envs_hold_different_canvases(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assert client.post("/canvas", params={"env": "staging"}, json=ONE).status_code == 200
        assert client.post("/canvas", params={"env": "prod"}, json=TWO).status_code == 200

        staging = client.get("/canvas", params={"env": "staging"}).json()
        prod = client.get("/canvas", params={"env": "prod"}).json()
        assert staging["nodes"][0]["data"]["label"] == "one"
        assert prod["nodes"][0]["data"]["label"] == "two"


def test_no_env_means_default_like_every_other_route(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        client.post("/canvas", json=ONE)
        assert client.get("/canvas", params={"env": "default"}).json() == client.get("/canvas").json()
        # ...and a different env is untouched by it
        assert client.get("/canvas", params={"env": "other"}).json() == {"nodes": [], "edges": []}


def test_a_fresh_env_starts_empty_rather_than_inheriting(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        client.post("/canvas", json=ONE)
        assert client.get("/canvas", params={"env": "brand-new"}).json() == {"nodes": [], "edges": []}


def test_the_revision_is_per_env(tmp_path):
    """Otherwise one env's save would 409 another env's writer."""
    with TestClient(_app(tmp_path)) as client:
        client.post("/canvas", params={"env": "a"}, json=ONE)
        rev_a = client.get("/canvas", params={"env": "a"}).headers["ETag"]
        rev_b = client.get("/canvas", params={"env": "b"}).headers["ETag"]
        assert rev_a != rev_b

        # A writer holding env b's revision is not blocked by env a's content.
        assert client.post("/canvas", params={"env": "b"}, json=TWO,
                           headers={"If-Match": rev_b}).status_code == 200


def test_the_broadcast_names_the_env_it_changed(tmp_path):
    """A tab showing `prod` must not reload because `staging` was saved."""
    from odin.api.canvas import create_canvas_router
    from fastapi import FastAPI

    class RecordingWs:
        def __init__(self):
            self.messages = []

        async def broadcast(self, message):
            self.messages.append(message)

    ws = RecordingWs()
    app = FastAPI()
    app.include_router(create_canvas_router(lambda env: tmp_path / env / CANVAS_NAME, ws=ws))
    with TestClient(app) as client:
        client.post("/canvas", params={"env": "staging"}, json=ONE)

    (update,) = [m for m in ws.messages if m.get("type") == "canvas_updated"]
    assert update["env"] == "staging", update


# --- migration: an existing global canvas must not appear to vanish ----------


def test_the_old_global_canvas_seeds_every_existing_env(tmp_path):
    root = tmp_path
    (root / "prod").mkdir(parents=True)
    (root / "staging").mkdir(parents=True)
    (root / CANVAS_NAME).write_text(json.dumps(ONE))

    seeded = _migrate_global_canvas(root)

    # Every env was SHOWING that canvas before, so every env keeps showing it.
    assert sorted(seeded) == ["default", "prod", "staging"]
    for env in ("default", "prod", "staging"):
        assert json.loads((root / env / CANVAS_NAME).read_text()) == ONE
    # The original is renamed, not deleted -- recoverable if this guessed wrong.
    assert not (root / CANVAS_NAME).exists()
    assert (root / f"{CANVAS_NAME}.pre-per-env").exists()


def test_migration_never_overwrites_a_canvas_an_env_already_has(tmp_path):
    root = tmp_path
    (root / "prod").mkdir(parents=True)
    (root / "prod" / CANVAS_NAME).write_text(json.dumps(TWO))
    (root / CANVAS_NAME).write_text(json.dumps(ONE))

    seeded = _migrate_global_canvas(root)

    assert "prod" not in seeded
    assert json.loads((root / "prod" / CANVAS_NAME).read_text()) == TWO


def test_migration_is_idempotent_and_a_no_op_without_a_legacy_file(tmp_path):
    assert _migrate_global_canvas(tmp_path) == []
    (tmp_path / CANVAS_NAME).write_text(json.dumps(ONE))
    assert _migrate_global_canvas(tmp_path) == ["default"]
    # The rename is what makes the second run a no-op: no marker file needed.
    assert _migrate_global_canvas(tmp_path) == []
