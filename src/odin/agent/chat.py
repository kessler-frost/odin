"""The chat surface: plain English -> a canvas EDIT the user reviews first.

## What this is, and what the owner said it must not become

> *"canvas and navigating things around IS the language of odin and not chatting
> with a bot to update things around - that we'll add later too but this is a
> separate thing."*

So this is an ADDITION to the canvas language, never a replacement for it, and it
is deliberately the last thing built (ROADMAP's intelligence-layer section). Every
design decision below follows from that one sentence.

## Why OPERATIONS, not a rewritten canvas

The obvious shape -- hand the model a canvas, take a canvas back -- is the wrong
one, and not for prompt-engineering reasons. A returned canvas is unauditable:
a node silently dropped, a `password` quietly rewritten and a label changed all
arrive as the same opaque blob, and diffing two canvases to find out cannot
recover INTENT ("was that node meant to go?"). The whole of `agent/hcl.py` and
`agent/import_tf.py` is deterministic precisely so nothing an LLM does can change
what gets applied; a canvas-in-canvas-out chat would hand that back.

An operation list is the opposite. Each op names exactly what it touches, so:
  * it can be VALIDATED one at a time, against the real catalog and the real
    canvas, before anything is applied (`validate`);
  * it can be SHOWN to the user as a sentence per change (`describe`);
  * anything the model did not ask for is impossible by construction -- an
    unmentioned field cannot move, because there is no op that says so.

`apply_ops` is a pure function over dicts. No SDK, no I/O, no network: the agent
half below produces ops and this half decides whether they are allowed. That
split is the same one `debugger.py` uses (`assemble_context` pure, the SDK call
separate) and for the same reason -- the part that must be RIGHT is the part that
can be tested without a model.

## The invariants, which are the owner's

- **"things like name and stuff remains as is."** A `set_field` op may not touch
  `label`: renaming a node in odin renames the real resource (the label IS the
  bucket/queue/table name), so it is a destroy-and-recreate wearing a rename's
  clothes. `rename` exists as its OWN op, so the intent is explicit and the diff
  says so out loud.
- **Nothing is applied by the agent.** `propose` returns a proposal. Applying it
  is a separate, human-initiated call. An agent that edits a canvas the user is
  looking at is the "reports success it did not achieve" shape aimed at their
  work rather than at a status line.
- **A field odin does not model is refused**, not stored hopefully. The canvas is
  permissive about `data.*` by design (`spec/translate.py`), so an invented field
  would round-trip through the store and Apply looking real, and do nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SdkMcpTool, create_sdk_mcp_server, tool
from pydantic import BaseModel, Field, ValidationError

from odin.agent import ai
from odin.spec.translate import _KIND, EDGE_KINDS

log = logging.getLogger("odin.chat")

# The fields a `set_field` op may write, per kind, taken from the CATALOG the UI
# offers rather than restated here -- a second list would drift the first time
# someone adds a field, and then the agent would be refused a field the user can
# type by hand. `label` is deliberately absent from every set: see `rename`.
_UNSETTABLE = frozenset({"label", "vpc", "subnet", "host", "status", "error", "resourceId", "groupId", "vpcId"})

# Containment stamps and status fields are DERIVED (`ui/src/lib/containment.ts`,
# the World projection). Letting an op write one would put a value in the canvas
# that the next drag -- or the next reconcile tick -- silently discards, which is
# a lie with a delay on it.


class AddNode(BaseModel):
    op: Literal["add_node"] = "add_node"
    kind: str = Field(description="Node kind, e.g. s3, sqs, rds, ec2, lambda, ecs.")
    label: str = Field(description="The node's name. For most kinds this IS the real resource name.")
    fields: dict[str, str] = Field(default_factory=dict, description="Initial config fields.")


class SetField(BaseModel):
    op: Literal["set_field"] = "set_field"
    label: str = Field(description="The node to change, by its current label.")
    field: str = Field(description="The config field to set. Never 'label' -- use rename_node.")
    value: str = Field(description="The new value.")


class RenameNode(BaseModel):
    op: Literal["rename_node"] = "rename_node"
    label: str = Field(description="The node to rename, by its current label.")
    new_label: str = Field(description="The new name. For most kinds this RENAMES the real resource.")


class DeleteNode(BaseModel):
    op: Literal["delete_node"] = "delete_node"
    label: str = Field(description="The node to remove, by its label.")


class AddEdge(BaseModel):
    op: Literal["add_edge"] = "add_edge"
    source: str = Field(description="Source node label.")
    target: str = Field(description="Target node label.")
    edge_type: str = Field(
        default="iam",
        description=(
            "What the edge MEANS: 'iam' (a permission grant), 'connection' (wire the "
            "workload's environment to the producer's endpoint -- rds gives it "
            "DATABASE_URL, elasticache gives it REDIS_URL; producer must be rds or "
            "elasticache and consumer must be ecs or lambda), 'sg' (this security group "
            "gates this resource), 'role' (this lambda assumes this IAM role), "
            "'subscription' (this SNS topic fans out to this SQS queue), 'target' (this "
            "load balancer fronts this ECS service), or 'unmodelled' when none of those "
            "fit -- odin then stores the line and acts on nothing. "
            "EXACTLY ONE of these, never a combination: the canvas panel lets a person "
            "tick both 'connection' and 'iam' on one rds/ecs line, but an op naming two "
            "is refused. Pick the meaning the user asked for."
        ),
    )
    actions: list[str] = Field(default_factory=list, description="For an iam edge: the permissions granted.")


class DeleteEdge(BaseModel):
    op: Literal["delete_edge"] = "delete_edge"
    source: str = Field(description="Source node label.")
    target: str = Field(description="Target node label.")


Op = AddNode | SetField | RenameNode | DeleteNode | AddEdge | DeleteEdge


class Refusal(BaseModel):
    """One op odin will not perform, and why -- in the user's words, not a code."""
    model_config = {"frozen": True}
    op: dict[str, Any]
    reason: str


class Proposal(BaseModel):
    """What the agent wants to do, what odin will not do, and the canvas that
    results from the allowed part. NOTHING here has been applied."""
    model_config = {"frozen": True}
    reply: str = ""
    changes: list[str] = []
    refused: list[Refusal] = []
    canvas: dict[str, Any] = {}
    note: str = ""


def _labels(canvas: dict) -> dict[str, dict]:
    return {node.get("data", {}).get("label", node.get("id", "")): node for node in canvas.get("nodes") or []}


def _edge_pair(edge: dict, by_id: dict[str, str]) -> tuple[str, str]:
    return by_id.get(edge.get("source", ""), ""), by_id.get(edge.get("target", ""), "")


def validate(op: Op, canvas: dict) -> str | None:
    """Why odin will not perform `op`, or None. Pure, and the ONLY gate.

    Every branch here is a rule the user would otherwise discover as a silently
    wrong canvas rather than as a sentence.
    """
    known = _labels(canvas)
    if isinstance(op, AddNode):
        if op.kind not in _KIND:
            return (f"odin has no {op.kind!r} node -- it models "
                    f"{', '.join(sorted(_KIND))}. Nothing was added.")
        if op.label in known:
            return f"there is already a node called {op.label!r}. Nothing was added."
        return None
    if isinstance(op, SetField):
        if op.label not in known:
            return f"there is no node called {op.label!r} on this canvas."
        if op.field in _UNSETTABLE:
            return (f"{op.field!r} is not a field odin lets an agent set: it is either the node's "
                    "identity (use a rename, which says so out loud) or a value odin derives from "
                    "the canvas geometry or the live world, which the next change would discard.")
        return None
    if isinstance(op, RenameNode):
        if op.label not in known:
            return f"there is no node called {op.label!r} on this canvas."
        if op.new_label in known:
            return f"there is already a node called {op.new_label!r}."
        return None
    if isinstance(op, DeleteNode):
        return None if op.label in known else f"there is no node called {op.label!r} on this canvas."
    if isinstance(op, AddEdge):
        missing = [label for label in (op.source, op.target) if label not in known]
        if missing:
            return f"no node called {' or '.join(repr(m) for m in missing)} on this canvas."
        # `edge_type` was an unvalidated free string, and `Edge.kind` is a free
        # `str` all the way down to the store, so an invented kind ('access',
        # 'connects', 'permission') round-tripped through a revision and through
        # Apply looking exactly like a real edge -- and did nothing, for ever.
        # The same shape as the invented FIELD this module already refuses, one
        # level up: the canvas is permissive by design, so the gate has to be
        # here rather than in the schema underneath.
        #
        # NOTE what this does NOT close, because it must not be read as having
        # closed it: `agent/hcl.py`'s subscription and ALB passes match on the
        # two NODE kinds and never read the kind at all, so a perfectly valid
        # 'iam' edge between an sns node and an sqs node still emits a real
        # `aws_sns_topic_subscription`. Kind-blindness is the primary defect and
        # it survives this fix entirely.
        if op.edge_type not in EDGE_KINDS:
            return (f"odin has no {op.edge_type!r} edge -- it models "
                    f"{', '.join(sorted(EDGE_KINDS))}. Nothing was drawn.")
        return None
    if isinstance(op, DeleteEdge):
        missing = [label for label in (op.source, op.target) if label not in known]
        return f"no node called {' or '.join(repr(m) for m in missing)} on this canvas." if missing else None
    return "odin does not recognise that operation."


def describe(op: Op) -> str:
    """One sentence per change, for a human reading the proposal before it runs.

    Deliberately spells out the COST where there is one. A rename is not a label
    edit: for most kinds the label IS the real resource name, so applying it
    destroys and recreates.
    """
    if isinstance(op, AddNode):
        detail = f" ({', '.join(f'{k}={v}' for k, v in sorted(op.fields.items()))})" if op.fields else ""
        return f"add a {op.kind} called {op.label!r}{detail}"
    if isinstance(op, SetField):
        return f"set {op.label!r}'s {op.field} to {op.value!r}"
    if isinstance(op, RenameNode):
        return (f"rename {op.label!r} to {op.new_label!r} -- for most kinds the label IS the real "
                "resource name, so applying this DESTROYS and recreates it")
    if isinstance(op, DeleteNode):
        return f"remove {op.label!r} -- applying this destroys the real resource"
    if isinstance(op, AddEdge):
        grant = f" granting {', '.join(op.actions)}" if op.actions else ""
        return f"draw a {op.edge_type} edge from {op.source!r} to {op.target!r}{grant}"
    if isinstance(op, DeleteEdge):
        return f"remove the edge from {op.source!r} to {op.target!r}"
    return "an operation odin does not recognise"


def _next_id(canvas: dict, kind: str) -> str:
    used = {node.get("id") for node in canvas.get("nodes") or []}
    index = 1
    while f"{kind}-{index}" in used:
        index += 1
    return f"{kind}-{index}"


def _placement(canvas: dict) -> dict[str, int]:
    """Where a new node goes: to the right of everything, on the 20px grid.

    Not clever, and deliberately so -- geometry means something in odin
    (containment compiles to infrastructure), so dropping a new node INSIDE an
    existing VPC box because it looked like a nice gap would be the agent
    authoring a `vpc_id` nobody asked for.
    """
    nodes = canvas.get("nodes") or []
    right = max((int(n.get("position", {}).get("x", 0)) for n in nodes), default=-220)
    return {"x": right + 220, "y": 0}


def apply_ops(canvas: dict, ops: list[Op]) -> tuple[dict, list[str], list[Refusal]]:
    """(new canvas, descriptions of what changed, refusals). PURE.

    Refused ops are skipped, never partially applied, and the rest still go
    through: a model that gets one op wrong should not cost the user the four it
    got right, as long as every skip is reported.
    """
    result = {
        "nodes": [dict(node) for node in canvas.get("nodes") or []],
        "edges": [dict(edge) for edge in canvas.get("edges") or []],
    }
    changes: list[str] = []
    refused: list[Refusal] = []

    for op in ops:
        reason = validate(op, result)
        if reason is not None:
            refused.append(Refusal(op=op.model_dump(), reason=reason))
            continue

        by_label = _labels(result)
        by_id = {node.get("id", ""): node.get("data", {}).get("label", "") for node in result["nodes"]}

        if isinstance(op, AddNode):
            result["nodes"].append({
                "id": _next_id(result, op.kind), "type": op.kind,
                "position": _placement(result),
                "data": {"label": op.label, **op.fields},
            })
        elif isinstance(op, SetField):
            by_label[op.label]["data"] = {**by_label[op.label].get("data", {}), op.field: op.value}
        elif isinstance(op, RenameNode):
            by_label[op.label]["data"] = {**by_label[op.label].get("data", {}), "label": op.new_label}
        elif isinstance(op, DeleteNode):
            doomed = by_label[op.label].get("id")
            result["nodes"] = [n for n in result["nodes"] if n.get("id") != doomed]
            result["edges"] = [
                e for e in result["edges"]
                if e.get("source") != doomed and e.get("target") != doomed
            ]
        elif isinstance(op, AddEdge):
            source, target = by_label[op.source].get("id"), by_label[op.target].get("id")
            # `permissions`, NOT `actions`. `spec/translate.py::_edge` reads
            # `data["permissions"]`, and the UI's own `edgeDataForConnection`
            # writes that key -- an edge stored under any other name compiles to
            # `perms=()`, i.e. a grant that allows NOTHING.
            #
            # Shipped that way in v0.8.5 and caught by field test 7 against a
            # live env: `odin chat "give resizer read access to uploads"` drew
            # the edge, applied clean, reported "Granted resizer read access
            # (s3:GetObject, s3:ListBucket)" -- and the gateway compiled
            # `Statement(actions=(), resources=('uploads',))`. A DECORATIVE
            # permission, the exact class `tests/gateway/
            # test_iam_vocabulary_is_enforceable.py` exists to prevent.
            #
            # The tests missed it for the reason that keeps recurring here: they
            # asserted on the canvas key this code writes, so both ends agreed
            # with each other and neither was checked against the thing between
            # them. `test_chat_grants_are_enforceable.py` asserts the COMPILED
            # POLICY instead.
            #
            # The agent-facing schema keeps saying `actions`: that is the word an
            # LLM produces for IAM and the word AWS uses. The mapping lives here,
            # in one line, rather than in the model's head.
            data: dict[str, Any] = {"edgeType": op.edge_type}
            if op.actions:
                data["permissions"] = op.actions
            result["edges"].append({"id": f"{source}-{target}", "source": source, "target": target, "data": data})
        elif isinstance(op, DeleteEdge):
            result["edges"] = [
                edge for edge in result["edges"]
                if _edge_pair(edge, by_id) != (op.source, op.target)
            ]
        changes.append(describe(op))
    return result, changes, refused


# --- the SDK half -------------------------------------------------------------
#
# Everything above is pure and decides what is ALLOWED. This half decides what is
# ASKED FOR, and it is the only part a model touches. Same split, same reasons and
# largely the same shape as `agent/translate.py`'s refine pass and
# `agent/debugger.py`'s diagnosis: one typed MCP tool as the sole effect channel,
# and a bounded `wait_for` around the un-awaited coroutine (awaiting first would
# complete the pass unbounded and leave the timeout measuring a finished value --
# the exact hang the bound exists to stop).
#
# ONE DIFFERENCE, stated because this comment used to claim the opposite:
# chat does NOT call `ai.refuse_if_off()`. Its gate is `propose`'s own
# `disabled_reason()` -- which IS `ai.off_reason()` -- checked before anything
# is constructed, and that is sufficient today for one reason only: `_run_agent`
# below has exactly ONE caller. `refuse_if_off` exists because translate has an
# uncached path that reaches the SDK with no per-feature check; chat has no such
# path yet. Give `_run_agent` a second caller and it needs the boundary check,
# and note that `propose`'s blanket `except Exception` would relabel an
# `AiDisabled` raised in there as "the agent could not be reached" -- so add the
# call above `create_sdk_mcp_server`, where the other two put it, not inside.
# `tests/agent/test_ai_switch.py` pins the property that matters either way: no
# client is CONSTRUCTED while the switch is off.


class ProposeEditsInput(BaseModel):
    """The ONE thing the agent may hand back. Deliberately NOT a canvas.

    `reply` is what the user reads; `ops` is what odin might do. Both are
    required, because an edit with no explanation is unreviewable and an
    explanation with no ops is a chatbot.
    """
    reply: str = Field(description="A short plain-English answer to the user, in the second person.")
    ops: list[dict] = Field(
        default_factory=list,
        description=(
            "The canvas edits you propose, in order. Each is an object with an `op` key: "
            "add_node{kind,label,fields}, set_field{label,field,value}, "
            "rename_node{label,new_label}, delete_node{label}, "
            "add_edge{source,target,edge_type,actions}, delete_edge{source,target}. "
            "Empty when the user asked a question rather than for a change."
        ),
    )


_MODEL = "claude-sonnet-5"
_TIMEOUT_ENV = "ODIN_CHAT_TIMEOUT"
_DEFAULT_TIMEOUT = 60.0

_SYSTEM = (
    "You edit an odin canvas: a diagram of AWS-shaped infrastructure that odin builds for real. "
    "You never apply anything -- you PROPOSE edits a human reviews first.\n\n"
    "Rules, in order of importance:\n"
    "1. Change only what was asked for. Never 'tidy up' a field nobody mentioned.\n"
    "2. Never rename a node unless renaming is the request. A node's label IS the real "
    "resource name, so a rename destroys and recreates it.\n"
    "3. Never set vpc/subnet/host/status: odin derives those from where boxes are DRAWN and "
    "from the live world, and your value would be discarded.\n"
    "4. If the request is ambiguous, propose nothing and ask for the missing detail in `reply`.\n"
    "5. If the request is a question about the canvas, answer it in `reply` with no ops.\n\n"
    "When the user asks for a CHANGE you must put it in `ops` -- describing it in `reply` "
    "changes nothing, and odin will report that nothing happened. Worked example:\n"
    "  user: give the worker lambda read access to the uploads bucket\n"
    "  ops:  [{\"op\": \"add_edge\", \"source\": \"worker\", \"target\": \"uploads\", "
    "\"edge_type\": \"iam\", \"actions\": [\"s3:GetObject\", \"s3:ListBucket\"]}]\n"
    "  reply: Granted worker read access to uploads.\n\n"
    "Always call propose_edits exactly once."
)


def default_timeout() -> float:
    """Read per call so it can be raised for one slow request without a restart
    (`rdsctl.available_timeout`'s shape)."""
    return float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT))


def disabled_reason() -> str | None:
    """Why chat is unavailable, in the user's words, or None. Mirrors
    `debugger.disabled_reason` -- the panel/CLI says `agent unavailable` and the
    REASON rather than failing with a stack trace."""
    return ai.off_reason()


def make_propose_tool(collector: list[dict]) -> SdkMcpTool:
    @tool("propose_edits", "Propose canvas edits for the user to review, plus a short reply.", ProposeEditsInput)
    async def propose_edits(args: ProposeEditsInput) -> dict:
        collector.append(dict(args))
        return {"content": [{"type": "text", "text": "recorded"}]}

    return propose_edits


def canvas_summary(canvas: dict) -> list[dict[str, Any]]:
    """What the model is allowed to SEE. One entry per node: kind, label, the
    fields it carries and its edges.

    VALUES ARE NOT INCLUDED. Only field NAMES, because a canvas holds real
    secrets -- an rds node's `password`, a secret's `secretString`, an ssm
    parameter's value -- and `debugger.assemble_context` already learned that
    the cheapest way not to leak one is never to assemble it. The cost is that
    the agent cannot answer "what is the password"; that is the correct trade,
    and it can still change a field it cannot read.
    """
    by_id = {n.get("id", ""): n.get("data", {}).get("label", "") for n in canvas.get("nodes") or []}
    edges = [
        {"from": by_id.get(e.get("source", ""), ""), "to": by_id.get(e.get("target", ""), ""),
         "type": (e.get("data") or {}).get("edgeType", "iam")}
        for e in canvas.get("edges") or []
    ]
    return [
        {
            "kind": node.get("type", ""),
            "label": node.get("data", {}).get("label", node.get("id", "")),
            "fields_set": sorted(k for k in (node.get("data") or {}) if k != "label"),
            "edges": [e for e in edges if e["from"] == node.get("data", {}).get("label")],
        }
        for node in canvas.get("nodes") or []
    ]


def _prompt(canvas: dict, message: str, history: list[tuple[str, str]] = ()) -> str:
    """The one prompt, with the conversation so far in front of it.

    History is (user, reply) pairs, replayed as text rather than resumed through
    an SDK session id. Deliberate: odin already re-sends the CANVAS every turn
    (it is the ground truth and the user may have changed it by hand between
    turns), so the only thing a resumed session would add is the model's own
    prior wording -- and replaying it here keeps the whole input visible,
    testable, and clearable by dropping a list. An SDK-side session would put
    the conversation somewhere odin cannot inspect or reset.
    """
    past = "".join(
        f"\nEarlier — you: {said}\nEarlier — your answer: {answered}\n"
        for said, answered in history
    )
    return (
        f"The canvas right now (field VALUES are withheld; you get field names only):\n"
        f"{json.dumps(canvas_summary(canvas), indent=2)}\n\n"
        f"Kinds odin can build: {', '.join(sorted(_KIND))}\n"
        f"{past}\n"
        f"The user says: {message}\n\n"
        "Call propose_edits once."
    )


async def _run_agent(prompt: str, options: ClaudeAgentOptions, client_cls: type) -> None:
    async with client_cls(options=options) as client:
        await client.query(prompt)
        async for _ in client.receive_response():
            pass


def _raw_ops(value: Any) -> tuple[list[Any], str | None]:
    """The op list out of the tool payload, whatever shape it arrived in.

    MEASURED against the real agent, and the reason this function exists: the
    SDK handed `ops` back as a JSON **string** --

        "ops": "[{\"op\": \"add_edge\", \"source\": \"thumbnailer\", ...}]"

    -- not as a list. The first version did `for raw in payload.get("ops") or []`
    and iterated that string CHARACTER BY CHARACTER, found no dicts, and reported
    "the agent proposed nothing". The agent had proposed exactly the right edge.
    Nothing failed, nothing logged; the request simply evaporated, which is this
    repo's honesty rule 1 in a new costume -- a reader wired to a shape the
    signal does not arrive in.

    So both shapes are accepted. A string that is not JSON, or JSON that is not a
    list, is REPORTED rather than silently treated as empty: "no ops" and "your
    ops could not be read" must never look the same to a user.
    """
    if isinstance(value, list):
        return value, None
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [], "the agent's edit list was not readable JSON, so nothing was changed."
        if isinstance(decoded, list):
            return decoded, None
        return [], "the agent's edit list was not a list, so nothing was changed."
    return [], None


# The same concept under a different word. MEASURED, not anticipated: asked to
# rename a bucket, the real agent sent
# `{"op": "rename_node", "node": "uploads", "new_label": "archives"}` -- the right
# operation, with `node` where the schema says `label`. Only variants actually
# observed go in here; guessing at more would be a treadmill, and the specific
# error message below is the durable half of the fix.
_FIELD_ALIASES = {"node": "label", "name": "label"}


def _normalise(raw: dict) -> dict:
    return {_FIELD_ALIASES.get(key, key): value for key, value in raw.items()}


def _parse_op(raw: dict) -> tuple[Op | None, str | None]:
    """(typed op, why not) for one raw op dict. Never raises.

    The two failure modes are DIFFERENT and were reported identically at first:
    an op odin has no concept of, and an op odin models perfectly whose arguments
    were wrong. "odin does not model that operation" was flatly untrue for the
    second, and it hid the one detail that makes the failure actionable -- which
    field was missing. A single malformed op still costs only itself
    (`apply_ops`' partial-failure rule).
    """
    kinds: dict[str, type[BaseModel]] = {
        "add_node": AddNode, "set_field": SetField, "rename_node": RenameNode,
        "delete_node": DeleteNode, "add_edge": AddEdge, "delete_edge": DeleteEdge,
    }
    name = str(raw.get("op", ""))
    model = kinds.get(name)
    if model is None:
        return None, (
            f"odin has no {name or 'unnamed'!r} operation -- it models "
            f"{', '.join(sorted(kinds))}. Nothing was changed by it."
        )
    try:
        return model.model_validate(_normalise(raw)), None  # type: ignore[return-value]
    except ValidationError as invalid:
        missing = ", ".join(
            str(error["loc"][0]) for error in invalid.errors() if error.get("loc")
        )
        return None, (
            f"the {name!r} operation arrived without {missing or 'the fields it needs'}, "
            "so odin could not carry it out."
        )


async def propose(
    canvas: dict, message: str, client_cls: type[Any] | None = None, timeout: float | None = None,
    history: list[tuple[str, str]] = (),
) -> Proposal:
    """Ask the agent what to do, then decide what odin will actually do.

    Returns a Proposal and applies NOTHING -- the canvas in it is a preview the
    caller may choose to save. Every failure mode (AI off, SDK error, timeout,
    no tool call) lands on the SAME shape: the canvas unchanged, no changes, and
    a `note` saying why, so a caller never has to distinguish "nothing to do"
    from "it broke" by inspecting an exception.
    """
    off = disabled_reason()
    if off is not None:
        return Proposal(canvas=canvas, note=f"agent unavailable: {off}")

    collected: list[dict] = []
    os.environ.pop("CLAUDECODE", None)  # nested-Claude-Code confusion, as in translate.py
    server = create_sdk_mcp_server(name="chat", tools=[make_propose_tool(collected)])
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM, model=_MODEL,
        mcp_servers={"chat": server}, allowed_tools=["mcp__chat__propose_edits"],
    )
    try:
        await asyncio.wait_for(
            _run_agent(_prompt(canvas, message, history), options, client_cls or ClaudeSDKClient),
            timeout=timeout if timeout is not None else default_timeout(),
        )
    except Exception:
        log.exception("chat agent SDK pass failed")
        return Proposal(canvas=canvas, note="the agent could not be reached — nothing was changed")

    if not collected:
        return Proposal(canvas=canvas, note="the agent proposed nothing — nothing was changed")

    payload = collected[-1]
    raw_ops, ops_error = _raw_ops(payload.get("ops"))
    parsed = [(raw, *_parse_op(raw)) for raw in raw_ops if isinstance(raw, dict)]
    unparsed = [Refusal(op=raw, reason=why or "odin could not read that operation.")
                for raw, op, why in parsed if op is None]
    if ops_error is not None:
        unparsed.append(Refusal(op={}, reason=ops_error))
    ops = [op for _raw, op, _why in parsed if op is not None]

    updated, changes, refused = apply_ops(canvas, ops)
    reply = str(payload.get("reply", ""))
    # An answer with no changes AND no refusals AND no words is not an answer.
    # Measured against the real agent: one run called the tool with an empty
    # reply and no ops, and odin reported literally nothing -- the CLI printed
    # a blank line and exited 0, which is indistinguishable from success. The
    # note is what stops silence from reading as "done".
    note = "" if (changes or refused or reply.strip()) else (
        "the agent answered with nothing at all — nothing was changed"
    )
    return Proposal(
        reply=reply, changes=changes, refused=[*unparsed, *refused],
        canvas=updated, note=note,
    )
