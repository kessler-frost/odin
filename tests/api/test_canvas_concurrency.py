"""Two tabs must CONVERGE on the one global canvas, not clobber each other.

Keeping the canvas global is the design (one architecture, many environments,
and every tab showing the same thing). What happened instead was divergence:
each page held its own copy plus a debounced save, so whichever re-rendered
last silently overwrote the rest. Measured repeatedly while recording the
v0.7.7 GIFs -- once replacing three applied resources with a single node from a
tab left open in another window, with nothing said and no way back.

Two mechanisms are pinned here because they cover different failures:

  If-Match          a client that says which revision it edited gets a 409
                    instead of overwriting a newer canvas
  canvas_updated    other tabs are told, so they reload instead of sitting on
                    a stale copy until their next render overwrites this one

The precondition is OPTIONAL on purpose -- `odin canvas set`, curl and every
existing test keep working without it -- so these tests also pin that a
no-`If-Match` save still succeeds. A guard nobody can omit would have been
simpler; one that can be omitted has to prove it still protects the client
that opts in.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from odin.api.canvas import canvas_revision
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime

ONE = {"nodes": [{"id": "s1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "one"}}], "edges": []}
TWO = {"nodes": [{"id": "s2", "type": "s3", "position": {"x": 20, "y": 0}, "data": {"label": "two"}}], "edges": []}


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def test_get_returns_a_revision_in_the_etag_header(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        first = client.get("/canvas")
        assert first.headers.get("ETag"), "no ETag — a client has nothing to send back"
        # The body must stay byte-for-byte what is on disk; the revision rides
        # in the header precisely so it cannot leak into `odin canvas get`.
        assert set(first.json()) <= {"nodes", "edges"}


def test_the_revision_changes_when_the_canvas_does_and_not_otherwise(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        empty = client.get("/canvas").headers["ETag"]
        assert client.get("/canvas").headers["ETag"] == empty, "a read must not change the revision"
        client.post("/canvas", json=ONE)
        after = client.get("/canvas").headers["ETag"]
        assert after != empty
        client.post("/canvas", json=ONE)
        assert client.get("/canvas").headers["ETag"] == after, "an identical save is the same content"


def test_a_stale_writer_is_refused_instead_of_overwriting(tmp_path):
    """The bug, exactly: tab A loads, tab B saves, tab A saves its old copy."""
    with TestClient(_app(tmp_path)) as client:
        tab_a_saw = client.get("/canvas").headers["ETag"]
        client.post("/canvas", json=TWO)                      # tab B gets there first
        stale = client.post("/canvas", json=ONE, headers={"If-Match": tab_a_saw})
        assert stale.status_code == 409, stale.text
        assert "another tab" in stale.json()["detail"]
        # and tab B's work is still there
        assert client.get("/canvas").json()["nodes"][0]["data"]["label"] == "two"


def test_a_current_writer_is_accepted(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        current = client.get("/canvas").headers["ETag"]
        ok = client.post("/canvas", json=ONE, headers={"If-Match": current})
        assert ok.status_code == 200
        assert ok.json()["rev"] == canvas_revision(SpecStore(tmp_path).root / "canvas.json")


def test_omitting_the_precondition_still_saves(tmp_path):
    """`odin canvas set`, curl, and every test that predates this."""
    with TestClient(_app(tmp_path)) as client:
        client.post("/canvas", json=TWO)
        assert client.post("/canvas", json=ONE).status_code == 200
        assert client.get("/canvas").json()["nodes"][0]["data"]["label"] == "one"


class RecordingWs:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast(self, message: dict) -> None:
        self.messages.append(message)


def test_a_save_broadcasts_so_other_tabs_can_converge(tmp_path):
    """The half that makes tabs AGREE rather than merely refusing to clobber.

    Driven through a RECORDING FAKE rather than a live websocket, because the
    live version could not fail — only hang. Written first as
    `websocket_connect(...)` then `receive_json()` in a loop, it blocked
    forever the moment the broadcast was removed, so the mutation test that was
    supposed to prove the assertion works instead wedged for ten minutes and
    left the mutated source in the tree. A test that hangs on the failure it
    exists to detect is worse than no test: a hang is indistinguishable from a
    slow machine, and it stops the run rather than reporting it.
    """
    from odin.api.canvas import create_canvas_router

    canvas = tmp_path / "canvas.json"
    ws = RecordingWs()
    app = FastAPI()
    app.include_router(create_canvas_router(canvas, ws=ws))
    with TestClient(app) as client:
        client.post("/canvas", json=ONE)
        updates = [m for m in ws.messages if m.get("type") == "canvas_updated"]
        assert updates, f"no canvas_updated broadcast; saw {ws.messages}"
        assert updates[-1]["rev"] == canvas_revision(canvas)


def test_the_real_app_wires_the_broadcast_up(tmp_path, monkeypatch):
    """The fake above proves the router broadcasts; this proves the app hands
    it something to broadcast WITH. Without this, deleting `ws=ws_manager` in
    server.py would leave every test above green and every tab still stale."""
    import odin.server as server_mod

    seen: dict = {}
    real = server_mod.create_canvas_router

    def spy(path, ws=None):
        seen["ws"] = ws
        return real(path, ws=ws)

    monkeypatch.setattr(server_mod, "create_canvas_router", spy)
    with TestClient(_app(tmp_path)):
        pass
    assert seen.get("ws") is not None, "create_app built the canvas router with no ws — tabs cannot converge"
