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


def test_the_route_does_NOT_save_anything(tmp_path, monkeypatch):
    """THE property. The proposal names a canvas with a second node; the file on
    disk must still hold one."""
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
        assert len(resp.json()["canvas"]["nodes"]) == 2
        on_disk = client.get("/canvas", params={"env": "default"}).json()

    assert [n["data"]["label"] for n in on_disk["nodes"]] == ["uploads"], "the route applied the proposal"


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
