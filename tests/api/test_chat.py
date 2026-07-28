"""`POST /chat` — the route the `odin chat` command and the UI panel both go through.

The property this file exists for: **the route applies nothing.** Its `canvas` is
a preview, and saving it is a separate call a human makes. That is the owner's
rule for the whole surface — the canvas is odin's language and chat is an
addition to it — so an agent that edited the canvas someone was looking at would
be taking the language away from them.

The agent itself is stubbed here. What is under test is the WIRING: that the
route reads the env's saved canvas, hands it to `chat.propose`, returns the
proposal verbatim, and leaves the file on disk exactly as it found it.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from odin.agent.chat import Proposal
from odin.runtime.driver import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.spec.store import SpecStore


class FakeRuntime:
    """Same shape as tests/api/test_import_tf.py's -- these tests never touch a
    container, they only need `create_app` to build."""

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

CANVAS = {
    "nodes": [{"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "uploads"}}],
    "edges": [],
}


def _app(tmp_path, monkeypatch, proposal: Proposal):
    async def fake_propose(canvas, message, **kwargs):
        fake_propose.seen = (canvas, message)
        return proposal

    monkeypatch.setattr("odin.agent.chat.propose", fake_propose)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)
    return app, fake_propose


def _save_canvas(client, canvas: dict) -> None:
    assert client.post("/canvas", params={"env": "default"}, json=canvas).status_code in (200, 201)


def test_the_route_hands_the_saved_canvas_to_the_agent(tmp_path, monkeypatch):
    proposal = Proposal(reply="ok", changes=["add a sqs called 'jobs'"], canvas=CANVAS)
    app, fake = _app(tmp_path, monkeypatch, proposal)
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        resp = client.post("/chat", params={"env": "default"}, json={"message": "add a queue"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "ok"
    assert body["changes"] == ["add a sqs called 'jobs'"]
    seen_canvas, seen_message = fake.seen
    assert seen_message == "add a queue"
    assert [n["data"]["label"] for n in seen_canvas["nodes"]] == ["uploads"]


def test_the_route_SAVES_the_edit(tmp_path, monkeypatch):
    """Owner decision, 2026-07-28: the agent edits the canvas directly.

    This test asserted the opposite until then -- "the route applies nothing" --
    and it was the right contract for a surface whose review step was a
    confirmation. It is not, now: the CANVAS is the review surface. The change
    appears where you are already looking, the UI's undo stack picks it up
    (Canvas.tsx records history from a `[nodes, edges]` effect, so the
    WebSocket-driven `setNodes` lands on it exactly like a drag), and Cmd-Z
    reverses it.

    What the route still must NOT do is provision: see
    `test_the_route_never_triggers_an_apply` below. That is the line that
    matters -- an agent may rearrange the drawing, never build from it.
    """
    proposed = {
        "nodes": [
            *CANVAS["nodes"],
            {"id": "sqs-1", "type": "sqs", "position": {"x": 220, "y": 0}, "data": {"label": "jobs"}},
        ],
        "edges": [],
    }
    app, _fake = _app(tmp_path, monkeypatch, Proposal(reply="ok", changes=["add"], canvas=proposed))
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        resp = client.post("/chat", params={"env": "default"}, json={"message": "add a queue"})
        body = resp.json()
        on_disk = client.get("/canvas", params={"env": "default"}).json()

    assert body["status"] == "saved" and body["rev"]
    assert sorted(n["data"]["label"] for n in on_disk["nodes"]) == ["jobs", "uploads"]


def test_dry_run_still_writes_nothing(tmp_path, monkeypatch):
    """`odin chat --dry-run` keeps the look-first behaviour for anyone who wants
    it, and it must genuinely not touch the file."""
    proposed = {
        "nodes": [
            *CANVAS["nodes"],
            {"id": "sqs-1", "type": "sqs", "position": {"x": 220, "y": 0}, "data": {"label": "jobs"}},
        ],
        "edges": [],
    }
    app, _fake = _app(tmp_path, monkeypatch, Proposal(reply="ok", changes=["add"], canvas=proposed))
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        body = client.post("/chat", params={"env": "default"},
                           json={"message": "add a queue", "dry_run": True}).json()
        on_disk = client.get("/canvas", params={"env": "default"}).json()

    assert len(body["canvas"]["nodes"]) == 2, "the preview should still describe the change"
    assert "status" not in body, "a dry run must not report a save"
    assert [n["data"]["label"] for n in on_disk["nodes"]] == ["uploads"]


def test_a_proposal_with_no_changes_writes_nothing(tmp_path, monkeypatch):
    """A question ("what is on this canvas?") must not rewrite the file with an
    identical copy -- that would bump the revision and make every other tab
    reload for nothing."""
    app, _fake = _app(tmp_path, monkeypatch, Proposal(reply="You have one bucket.", canvas=CANVAS))
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        before = client.get("/canvas", params={"env": "default"}).headers.get("etag")
        body = client.post("/chat", params={"env": "default"}, json={"message": "what is here?"}).json()
        after = client.get("/canvas", params={"env": "default"}).headers.get("etag")

    assert body["reply"] == "You have one bucket."
    assert "status" not in body
    assert before == after, "an answer with no edits must not touch the canvas revision"


def test_an_unavailable_agent_is_a_200_with_a_note(tmp_path, monkeypatch):
    """A client must not have to distinguish "nothing to do" from "it broke" by
    status code -- `note` carries the reason and the canvas is unchanged."""
    app, _fake = _app(tmp_path, monkeypatch, Proposal(canvas=CANVAS, note="agent unavailable: ODIN_AI=0"))
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        resp = client.post("/chat", params={"env": "default"}, json={"message": "add a queue"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["changes"] == []
    assert "agent unavailable" in body["note"]


def test_refusals_survive_the_wire(tmp_path, monkeypatch):
    """The refusal list is the most useful part of the answer -- it is the
    difference between "it did what I asked" and "it did some of it"."""
    proposal = Proposal(
        reply="partly done", changes=["add a sqs called 'jobs'"],
        refused=[{"op": {"op": "add_node", "kind": "kinesis"}, "reason": "odin has no 'kinesis' node"}],
        canvas=CANVAS,
    )
    app, _fake = _app(tmp_path, monkeypatch, proposal)
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        body = client.post("/chat", params={"env": "default"}, json={"message": "add streams"}).json()

    (refusal,) = body["refused"]
    assert refusal["reason"] == "odin has no 'kinesis' node"


def test_chat_is_per_env_like_every_other_canvas_read(tmp_path, monkeypatch):
    """`/chat` reads `canvas_for(env)`; asking in one env must not show another's
    work (the per-env canvas invariant from v0.7.9)."""
    other = {"nodes": [{"id": "sqs-1", "type": "sqs", "position": {"x": 0, "y": 0},
                        "data": {"label": "staging-queue"}}], "edges": []}
    app, fake = _app(tmp_path, monkeypatch, Proposal(canvas=CANVAS))
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        assert client.post("/canvas", params={"env": "staging"}, json=other).status_code in (200, 201)
        client.post("/chat", params={"env": "staging"}, json={"message": "what is here?"})

    seen_canvas, _message = fake.seen
    assert [n["data"]["label"] for n in seen_canvas["nodes"]] == ["staging-queue"]


def test_an_env_with_no_canvas_yet_is_answerable(tmp_path, monkeypatch):
    """Asking "add a bucket" before drawing anything is the FIRST thing someone
    will do, and it must not 500 on a missing file."""
    app, fake = _app(tmp_path, monkeypatch, Proposal(canvas={"nodes": [], "edges": []}))
    with TestClient(app) as client:
        resp = client.post("/chat", params={"env": "brand-new"}, json={"message": "add a bucket"})

    assert resp.status_code == 200, resp.text
    seen_canvas, _message = fake.seen
    assert seen_canvas.get("nodes") == []


def test_the_proposed_canvas_is_valid_input_to_the_canvas_route(tmp_path, monkeypatch):
    """`--apply` posts the proposal straight to `POST /canvas`, so the shape the
    agent half produces has to pass the SAME validation a hand-drawn canvas does.
    There is no privileged path for an agent-authored canvas."""
    proposed = {
        "nodes": [
            *CANVAS["nodes"],
            {"id": "sqs-1", "type": "sqs", "position": {"x": 220, "y": 0}, "data": {"label": "jobs"}},
        ],
        "edges": [],
    }
    app, _fake = _app(tmp_path, monkeypatch, Proposal(reply="ok", changes=["add"], canvas=proposed))
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        body = client.post("/chat", params={"env": "default"}, json={"message": "add a queue"}).json()
        saved = client.post("/canvas", params={"env": "default"}, json=body["canvas"])
        assert saved.status_code in (200, 201), saved.text
        on_disk = client.get("/canvas", params={"env": "default"}).json()

    assert sorted(n["data"]["label"] for n in on_disk["nodes"]) == ["jobs", "uploads"]
    assert json.dumps(on_disk)  # round-trips as JSON, i.e. nothing exotic leaked in


# --- the line the agent may not cross -----------------------------------------


def test_the_route_never_triggers_an_apply(tmp_path, monkeypatch):
    """The agent edits the DRAWING; only the user builds from it.

    Owner: "only the user should press the apply button". So `/chat` may write
    the canvas and must never reach `/apply-full` — a canvas edit is reversible
    with Cmd-Z, whereas an apply creates real containers and, for rds, can
    destroy real data.
    """
    applied: list[str] = []

    async def boom(*args, **kwargs):
        applied.append("apply")
        raise AssertionError("chat must never provision")

    monkeypatch.setattr("odin.simulate.runner.TfRunner.apply", boom, raising=False)
    proposed = {
        "nodes": [
            *CANVAS["nodes"],
            {"id": "sqs-1", "type": "sqs", "position": {"x": 220, "y": 0}, "data": {"label": "jobs"}},
        ],
        "edges": [],
    }
    app, _fake = _app(tmp_path, monkeypatch, Proposal(reply="ok", changes=["add"], canvas=proposed))
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        assert client.post("/chat", params={"env": "default"},
                           json={"message": "add a queue"}).status_code == 200

    assert applied == []


# --- the session --------------------------------------------------------------


def test_the_conversation_is_remembered_across_turns(tmp_path, monkeypatch):
    """"no, make it two" is the most common second message, and it is meaningless
    without the first."""
    seen: list[list] = []

    async def fake_propose(canvas, message, **kwargs):
        seen.append(list(kwargs.get("history") or []))
        return Proposal(reply=f"did {message}", canvas=canvas)

    monkeypatch.setattr("odin.agent.chat.propose", fake_propose)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        client.post("/chat", params={"env": "default"}, json={"message": "add a queue"})
        client.post("/chat", params={"env": "default"}, json={"message": "make it two"})

    assert seen[0] == [], "the first turn has no history"
    assert seen[1] == [("add a queue", "did add a queue")], "the second turn must see the first"


def test_each_env_has_its_own_conversation(tmp_path, monkeypatch):
    """Envs are isolated everywhere else in odin; a conversation about staging
    must not leak into prod's context."""
    seen: dict[str, list] = {}

    async def fake_propose(canvas, message, **kwargs):
        seen[message] = list(kwargs.get("history") or [])
        return Proposal(reply="ok", canvas=canvas)

    monkeypatch.setattr("odin.agent.chat.propose", fake_propose)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        client.post("/chat", params={"env": "default"}, json={"message": "first in default"})
        client.post("/chat", params={"env": "staging"}, json={"message": "first in staging"})

    assert seen["first in staging"] == [], "staging must not inherit default's conversation"


def test_clear_forgets_the_conversation_and_leaves_the_canvas_alone(tmp_path, monkeypatch):
    """Clear resets the AGENT, not your work. Rolling the canvas back would throw
    away edits made by hand since, which is a far worse surprise than a stale
    conversation."""
    seen: list[list] = []

    async def fake_propose(canvas, message, **kwargs):
        seen.append(list(kwargs.get("history") or []))
        return Proposal(reply="ok", canvas=canvas)

    monkeypatch.setattr("odin.agent.chat.propose", fake_propose)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        client.post("/chat", params={"env": "default"}, json={"message": "first"})
        cleared = client.post("/chat/clear", params={"env": "default"}).json()
        client.post("/chat", params={"env": "default"}, json={"message": "second"})
        on_disk = client.get("/canvas", params={"env": "default"}).json()

    assert cleared == {"status": "cleared", "env": "default", "turns_forgotten": 1}
    assert seen[-1] == [], "the turn after a clear must start fresh"
    assert [n["data"]["label"] for n in on_disk["nodes"]] == ["uploads"], "clear touched the canvas"


def test_clearing_one_env_does_not_clear_another(tmp_path, monkeypatch):
    seen: dict[str, list] = {}

    async def fake_propose(canvas, message, **kwargs):
        seen[message] = list(kwargs.get("history") or [])
        return Proposal(reply="ok", canvas=canvas)

    monkeypatch.setattr("odin.agent.chat.propose", fake_propose)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), backings=False)
    with TestClient(app) as client:
        _save_canvas(client, CANVAS)
        client.post("/chat", params={"env": "default"}, json={"message": "keep me"})
        client.post("/chat/clear", params={"env": "staging"})
        client.post("/chat", params={"env": "default"}, json={"message": "still there?"})

    assert seen["still there?"] == [("keep me", "ok")]


def test_clearing_an_env_that_never_chatted_is_a_clean_no_op(tmp_path, monkeypatch):
    app, _fake = _app(tmp_path, monkeypatch, Proposal(canvas=CANVAS))
    with TestClient(app) as client:
        body = client.post("/chat/clear", params={"env": "never-used"}).json()
    assert body == {"status": "cleared", "env": "never-used", "turns_forgotten": 0}
