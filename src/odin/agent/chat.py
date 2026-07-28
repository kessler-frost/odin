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

from typing import Any, Literal

from pydantic import BaseModel, Field

from odin.spec.translate import _KIND

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
    edge_type: str = Field(default="iam", description="iam, network or sg.")
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
            data: dict[str, Any] = {"edgeType": op.edge_type}
            if op.actions:
                data["actions"] = op.actions
            result["edges"].append({"id": f"{source}-{target}", "source": source, "target": target, "data": data})
        elif isinstance(op, DeleteEdge):
            result["edges"] = [
                edge for edge in result["edges"]
                if _edge_pair(edge, by_id) != (op.source, op.target)
            ]
        changes.append(describe(op))
    return result, changes, refused
