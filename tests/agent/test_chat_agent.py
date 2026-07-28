"""The chat surface's SDK half, and the failure modes that must all look alike.

`_FakeClient` drives the REAL `create_sdk_mcp_server`/`@tool` dispatch with
canned args — the same technique `tests/agent/test_translate.py` uses, and for
the same reason: it exercises the tool registration `chat.py` actually ships
rather than a reimplementation of it, so a broken tool name or schema fails here.

## The property most of this file is about

`propose` NEVER raises and never returns a half-state. AI switched off, SDK
error, timeout, no tool call, malformed ops — every one lands on the same shape:
the canvas unchanged, `changes` empty, and a `note` saying why. A caller that had
to tell "nothing to do" from "it broke" by catching an exception would eventually
get it wrong, and the wrong guess here silently discards a user's request.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp import types as mcp_types

from odin.agent import chat
from odin.agent.chat import Proposal, propose

CANVAS = {
    "nodes": [
        {"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0},
         "data": {"label": "uploads", "versioning": "true"}},
        {"id": "lambda-1", "type": "lambda", "position": {"x": 220, "y": 0},
         "data": {"label": "thumbnailer", "code": "print(1)"}},
    ],
    "edges": [],
}


class _FakeClient:
    """Stands in for `ClaudeSDKClient`, driving the real MCP tool handler."""

    canned: dict | None = None
    raises: Exception | None = None
    stalls: bool = False

    def __init__(self, options=None) -> None:
        self._options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.prompt = prompt
        if self.raises is not None:
            raise self.raises
        if self.stalls:
            await asyncio.sleep(30)

    async def receive_response(self):
        # `mcp_servers[name]` is a dict whose "instance" is the real lowlevel
        # server -- dispatching through its own `request_handlers` is what makes
        # this exercise the SHIPPED tool registration (name, schema, handler)
        # rather than a stand-in for it. Reaching for the dict value directly
        # silently found no handler and every canned call vanished.
        if self.canned is not None:
            server = self._options.mcp_servers["chat"]["instance"]
            handler = server.request_handlers[mcp_types.CallToolRequest]
            await handler(mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name="propose_edits", arguments=self.canned),
            ))
        return
        yield  # pragma: no cover -- keeps this an async generator


def _client_with(**attrs) -> type:
    return type("_Canned", (_FakeClient,), attrs)


@pytest.fixture(autouse=True)
def _ai_on(monkeypatch):
    monkeypatch.setenv("ODIN_AI", "1")


# --- the happy path ------------------------------------------------------------


async def test_a_proposed_edit_comes_back_as_changes_and_a_preview_canvas():
    client = _client_with(canned={
        "reply": "Added a queue.",
        "ops": [{"op": "add_node", "kind": "sqs", "label": "jobs", "fields": {}}],
    })
    result = await propose(CANVAS, "add a queue called jobs", client_cls=client, timeout=5)

    assert result.reply == "Added a queue."
    assert result.changes == ["add a sqs called 'jobs'"]
    assert {n["data"]["label"] for n in result.canvas["nodes"]} == {"uploads", "thumbnailer", "jobs"}
    assert result.note == ""


async def test_nothing_is_applied_to_the_caller_s_canvas():
    """The proposal is a PREVIEW. If `propose` mutated the input, the surface
    would have applied itself and `--apply` would be decoration."""
    client = _client_with(canned={"reply": "ok", "ops": [{"op": "delete_node", "label": "uploads"}]})
    await propose(CANVAS, "remove uploads", client_cls=client, timeout=5)
    assert {n["data"]["label"] for n in CANVAS["nodes"]} == {"uploads", "thumbnailer"}


async def test_a_question_with_no_ops_is_answered_and_changes_nothing():
    client = _client_with(canned={"reply": "You have an S3 bucket and a Lambda.", "ops": []})
    result = await propose(CANVAS, "what is on this canvas?", client_cls=client, timeout=5)
    assert result.reply.startswith("You have")
    assert result.changes == []
    assert result.canvas["nodes"] == CANVAS["nodes"]


# --- the guardrail, reached through the agent ----------------------------------


async def test_an_op_the_guardrail_refuses_is_reported_not_applied():
    """The pure half decides; this proves the agent cannot go around it."""
    client = _client_with(canned={
        "reply": "Renamed it.",
        "ops": [{"op": "set_field", "label": "uploads", "field": "label", "value": "archives"}],
    })
    result = await propose(CANVAS, "rename uploads", client_cls=client, timeout=5)
    assert result.changes == []
    (refusal,) = result.refused
    assert "rename" in refusal.reason
    assert {n["data"]["label"] for n in result.canvas["nodes"]} == {"uploads", "thumbnailer"}


async def test_an_op_odin_does_not_model_names_ITSELF_and_the_alternatives():
    client = _client_with(canned={"reply": "done", "ops": [{"op": "reticulate_splines", "label": "uploads"}]})
    result = await propose(CANVAS, "reticulate", client_cls=client, timeout=5)
    (refusal,) = result.refused
    assert "'reticulate_splines'" in refusal.reason
    assert "add_edge" in refusal.reason, "the refusal should say what odin CAN do"
    assert result.changes == []


async def test_a_modelled_op_with_wrong_ARGUMENTS_says_which_field_was_missing():
    """A different failure from the one above, and it was reported identically at
    first. MEASURED against the real agent: asked to rename a bucket it sent
    `{"op": "rename_node", "node": "uploads", "new_label": "archives"}` -- the
    right operation, with `node` where the schema says `label`. Saying "odin does
    not model that operation" was flatly untrue and hid the only actionable
    detail."""
    client = _client_with(canned={"reply": "done", "ops": [
        {"op": "rename_node", "new_label": "archives"},
    ]})
    result = await propose(CANVAS, "rename it", client_cls=client, timeout=5)
    (refusal,) = result.refused
    assert "'rename_node'" in refusal.reason
    assert "label" in refusal.reason
    assert "does not model" not in refusal.reason


async def test_the_one_measured_field_alias_is_accepted():
    """`node` for `label`, observed from the real agent. Only variants actually
    seen are accepted -- guessing at more would be a treadmill, and the specific
    error above is the durable half."""
    client = _client_with(canned={"reply": "done", "ops": [
        {"op": "rename_node", "node": "uploads", "new_label": "archives"},
    ]})
    result = await propose(CANVAS, "rename uploads", client_cls=client, timeout=5)
    assert result.refused == []
    assert "rename 'uploads' to 'archives'" in result.changes[0]
    assert "DESTROYS and recreates" in result.changes[0]


async def test_a_malformed_op_costs_only_itself():
    """`add_node` with no label cannot be validated into existence, but the
    well-formed op beside it must still land."""
    client = _client_with(canned={"reply": "done", "ops": [
        {"op": "add_node", "kind": "sqs"},
        {"op": "add_node", "kind": "sqs", "label": "jobs"},
    ]})
    result = await propose(CANVAS, "add queues", client_cls=client, timeout=5)
    assert result.changes == ["add a sqs called 'jobs'"]
    assert len(result.refused) == 1


# --- every failure looks the same ----------------------------------------------


async def test_ai_switched_off_says_so_and_changes_nothing(monkeypatch):
    monkeypatch.setenv("ODIN_AI", "0")
    result = await propose(CANVAS, "add a queue", client_cls=_client_with(canned={"reply": "x", "ops": []}))
    assert result.canvas == CANVAS
    assert result.changes == []
    assert "agent unavailable" in result.note
    assert "ODIN_AI" in result.note


async def test_an_sdk_error_is_a_note_not_an_exception():
    client = _client_with(raises=RuntimeError("claude CLI not found"))
    result = await propose(CANVAS, "add a queue", client_cls=client, timeout=5)
    assert isinstance(result, Proposal)
    assert result.canvas == CANVAS
    assert "could not be reached" in result.note


async def test_a_timeout_is_bounded_and_reported():
    """The bound has to apply to the RUN, not to a finished value -- awaiting the
    coroutine before `wait_for` would let a stalled agent hang the request
    forever (translate.py and debugger.py both carry this note)."""
    client = _client_with(stalls=True)
    result = await propose(CANVAS, "add a queue", client_cls=client, timeout=0.2)
    assert result.canvas == CANVAS
    assert "could not be reached" in result.note


async def test_an_agent_that_calls_nothing_says_so():
    result = await propose(CANVAS, "add a queue", client_cls=_client_with(canned=None), timeout=5)
    assert result.changes == []
    assert "proposed nothing" in result.note


# --- what the model is allowed to see ------------------------------------------


def test_the_summary_carries_field_NAMES_and_never_their_values():
    """A canvas holds real secrets -- an rds `password`, a secret's value, an ssm
    parameter. The cheapest way not to leak one is never to assemble it
    (`debugger.assemble_context`'s rule). The cost is that the agent cannot
    answer "what is the password", which is the correct trade."""
    canvas = {
        "nodes": [{"id": "rds-1", "type": "rds", "position": {"x": 0, "y": 0},
                   "data": {"label": "app-db", "password": "hunter2SuperSecret"}}],
        "edges": [],
    }
    summary = chat.canvas_summary(canvas)
    assert summary == [{"kind": "rds", "label": "app-db", "fields_set": ["password"], "edges": []}]
    assert "hunter2SuperSecret" not in str(summary)


async def test_the_prompt_itself_carries_no_field_values():
    """The end-to-end version of the test above: whatever the summary does, the
    STRING handed to the model is what actually leaves the process."""
    canvas = {
        "nodes": [{"id": "rds-1", "type": "rds", "position": {"x": 0, "y": 0},
                   "data": {"label": "app-db", "password": "hunter2SuperSecret"}}],
        "edges": [],
    }
    client = _client_with(canned={"reply": "ok", "ops": []})
    captured: list[str] = []

    class _Capturing(client):  # type: ignore[misc, valid-type]
        async def query(self, prompt: str, session_id: str = "default") -> None:
            captured.append(prompt)
            await super().query(prompt, session_id)

    await propose(canvas, "what is the db password?", client_cls=_Capturing, timeout=5)
    assert captured, "the agent was never prompted"
    assert "hunter2SuperSecret" not in captured[0]
    assert "app-db" in captured[0]


def test_the_timeout_knob_is_read_per_call(monkeypatch):
    monkeypatch.delenv("ODIN_CHAT_TIMEOUT", raising=False)
    assert chat.default_timeout() == 60.0
    monkeypatch.setenv("ODIN_CHAT_TIMEOUT", "12")
    assert chat.default_timeout() == 12.0


# --- the shape `ops` actually arrives in ---------------------------------------


async def test_ops_sent_as_a_JSON_STRING_are_read(monkeypatch):
    """MEASURED, and the bug that made the whole surface look broken.

    The real SDK handed `ops` back as a JSON **string**, not a list:

        "ops": "[{\\"op\\": \\"add_edge\\", \\"source\\": \\"thumbnailer\\", ...}]"

    The first version iterated it CHARACTER BY CHARACTER looking for dicts, found
    none, and reported "the agent proposed nothing" — while the agent had
    proposed exactly the right edge. Nothing raised, nothing logged; a correct
    request simply evaporated.
    """
    client = _client_with(canned={
        "reply": "Granted access.",
        "ops": '[{"op": "add_edge", "source": "thumbnailer", "target": "uploads", '
               '"edge_type": "iam", "actions": ["s3:GetObject"]}]',
    })
    result = await propose(CANVAS, "grant read access", client_cls=client, timeout=5)

    assert result.refused == []
    assert result.changes == [
        "draw a iam edge from 'thumbnailer' to 'uploads' granting s3:GetObject",
    ]
    (edge,) = result.canvas["edges"]
    assert edge["data"]["actions"] == ["s3:GetObject"]


async def test_ops_as_a_string_that_is_not_JSON_is_reported_not_swallowed():
    """"no ops" and "your ops could not be read" must never look the same."""
    client = _client_with(canned={"reply": "done", "ops": "add an edge please"})
    result = await propose(CANVAS, "grant access", client_cls=client, timeout=5)
    assert result.changes == []
    (refusal,) = result.refused
    assert "not readable JSON" in refusal.reason


async def test_ops_as_a_json_object_rather_than_a_list_is_reported():
    client = _client_with(canned={"reply": "done", "ops": '{"op": "add_node"}'})
    result = await propose(CANVAS, "add something", client_cls=client, timeout=5)
    (refusal,) = result.refused
    assert "not a list" in refusal.reason


async def test_an_empty_answer_says_it_is_empty():
    """The agent called the tool with nothing in it. Reporting silence as success
    is the shape honesty rule 2 exists for -- measured: odin printed a blank line
    and exited 0."""
    client = _client_with(canned={"reply": "", "ops": []})
    result = await propose(CANVAS, "do something", client_cls=client, timeout=5)
    assert result.changes == []
    assert "nothing at all" in result.note
