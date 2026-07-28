"""The chat surface's PURE half: what an agent may and may not do to a canvas.

This is the part that has to be right, and the part a model cannot influence.
`agent/chat.py` takes OPERATIONS rather than a rewritten canvas precisely so it
can be tested like this -- against the real catalog and the real canvas, one op
at a time, with no SDK anywhere near it.

The invariants under test are the owner's own, from the intelligence-layer
section: *"things like name and stuff remains as is"*, and chat as an ADDITION to
the canvas language rather than a replacement for it. Concretely that means an
agent may not rename a node as if it were a field edit (in odin the label IS the
real resource name), may not write a value the canvas derives from geometry, and
may not apply anything at all -- `apply_ops` produces a proposal, and a human
applies it.
"""
from __future__ import annotations

from odin.agent.chat import (
    AddEdge,
    AddNode,
    DeleteEdge,
    DeleteNode,
    RenameNode,
    SetField,
    apply_ops,
    validate,
)

CANVAS = {
    "nodes": [
        {"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "uploads"}},
        {"id": "lambda-1", "type": "lambda", "position": {"x": 220, "y": 0},
         "data": {"label": "thumbnailer", "runtime": "python3.12", "code": "print(1)"}},
    ],
    "edges": [],
}


def _labels(canvas: dict) -> set[str]:
    return {n["data"]["label"] for n in canvas["nodes"]}


# --- adding -------------------------------------------------------------------


def test_a_node_is_added_with_its_fields():
    canvas, changes, refused = apply_ops(CANVAS, [AddNode(kind="sqs", label="jobs", fields={"delay": "5"})])
    assert refused == []
    assert _labels(canvas) == {"uploads", "thumbnailer", "jobs"}
    node = next(n for n in canvas["nodes"] if n["data"]["label"] == "jobs")
    assert node["type"] == "sqs"
    assert node["data"]["delay"] == "5"
    assert changes == ["add a sqs called 'jobs' (delay=5)"]


def test_a_kind_odin_does_not_model_is_refused_by_name():
    _canvas, _changes, (refusal,) = apply_ops(CANVAS, [AddNode(kind="kinesis", label="stream")])
    assert "no 'kinesis' node" in refusal.reason
    assert "Nothing was added" in refusal.reason


def test_a_duplicate_label_is_refused():
    """Labels are identities here -- two `uploads` buckets is not a canvas odin
    can build, and picking one silently would be the agent choosing."""
    _canvas, _changes, (refusal,) = apply_ops(CANVAS, [AddNode(kind="s3", label="uploads")])
    assert "already a node called 'uploads'" in refusal.reason


def test_a_new_node_lands_to_the_RIGHT_and_never_inside_a_container():
    """Geometry compiles to infrastructure here. Dropping a node into a VPC box
    because it looked like a gap would author a `vpc_id` nobody asked for."""
    canvas, _changes, _refused = apply_ops(CANVAS, [AddNode(kind="sqs", label="jobs")])
    node = next(n for n in canvas["nodes"] if n["data"]["label"] == "jobs")
    assert node["position"]["x"] == 440
    assert "vpc" not in node["data"] and "subnet" not in node["data"]


# --- the owner's invariant: names and derived values ---------------------------


def test_set_field_may_not_touch_the_label():
    """THE invariant. In odin the label IS the bucket/queue/table name, so a
    rename disguised as a field edit is a destroy-and-recreate nobody agreed to."""
    reason = validate(SetField(label="uploads", field="label", value="archives"), CANVAS)
    assert reason is not None and "rename" in reason


def test_renaming_is_its_own_op_and_says_what_it_costs():
    canvas, changes, refused = apply_ops(CANVAS, [RenameNode(label="uploads", new_label="archives")])
    assert refused == []
    assert _labels(canvas) == {"archives", "thumbnailer"}
    assert "DESTROYS and recreates" in changes[0]


def test_a_derived_field_is_refused():
    """`vpc`/`subnet`/`host` come from where a box is DRAWN, and `status` from the
    live world. Writing one would put a value in the canvas that the next drag or
    the next reconcile tick discards -- a lie with a delay on it."""
    for field in ("vpc", "subnet", "host", "status"):
        reason = validate(SetField(label="uploads", field=field, value="x"), CANVAS)
        assert reason is not None, field
        assert "derives" in reason or "identity" in reason, (field, reason)


def test_an_ordinary_field_is_set_and_nothing_else_moves():
    canvas, _changes, refused = apply_ops(CANVAS, [SetField(label="thumbnailer", field="runtime", value="python3.13")])
    assert refused == []
    node = next(n for n in canvas["nodes"] if n["data"]["label"] == "thumbnailer")
    assert node["data"]["runtime"] == "python3.13"
    assert node["data"]["code"] == "print(1)", "an unmentioned field must survive untouched"


def test_the_input_canvas_is_never_mutated():
    """`apply_ops` is pure: the caller still holds the canvas the user is looking
    at, and a proposal that edited it in place would have already 'applied'."""
    before = {n["data"]["label"] for n in CANVAS["nodes"]}
    apply_ops(CANVAS, [DeleteNode(label="uploads"), AddNode(kind="sqs", label="jobs")])
    assert {n["data"]["label"] for n in CANVAS["nodes"]} == before
    assert CANVAS["edges"] == []


# --- deleting and edges --------------------------------------------------------


def test_deleting_a_node_takes_its_edges_with_it():
    """An edge to a node that no longer exists is a dangling reference the UI
    draws into nowhere and `canvas_to_stack` silently skips."""
    wired, _changes, _refused = apply_ops(CANVAS, [
        AddEdge(source="thumbnailer", target="uploads", actions=["s3:GetObject"]),
    ])
    assert len(wired["edges"]) == 1
    pruned, changes, refused = apply_ops(wired, [DeleteNode(label="uploads")])
    assert refused == []
    assert pruned["edges"] == []
    assert "destroys the real resource" in changes[0]


def test_an_iam_edge_carries_its_actions():
    canvas, changes, refused = apply_ops(CANVAS, [
        AddEdge(source="thumbnailer", target="uploads", actions=["s3:GetObject", "s3:PutObject"]),
    ])
    assert refused == []
    (edge,) = canvas["edges"]
    assert edge["data"] == {"edgeType": "iam", "actions": ["s3:GetObject", "s3:PutObject"]}
    assert "granting s3:GetObject, s3:PutObject" in changes[0]


def test_an_edge_to_a_node_that_is_not_there_is_refused_by_name():
    _canvas, _changes, (refusal,) = apply_ops(CANVAS, [AddEdge(source="thumbnailer", target="ghost")])
    assert "'ghost'" in refusal.reason


def test_an_edge_can_be_removed_by_the_labels_at_its_ends():
    wired, _c, _r = apply_ops(CANVAS, [AddEdge(source="thumbnailer", target="uploads")])
    pruned, changes, refused = apply_ops(wired, [DeleteEdge(source="thumbnailer", target="uploads")])
    assert refused == []
    assert pruned["edges"] == []
    assert changes == ["remove the edge from 'thumbnailer' to 'uploads'"]


# --- partial failure -----------------------------------------------------------


def test_one_bad_op_does_not_cost_the_user_the_good_ones():
    """A model that gets one op wrong should not lose the four it got right --
    provided every skip is reported, which is what makes it safe."""
    canvas, changes, refused = apply_ops(CANVAS, [
        AddNode(kind="sqs", label="jobs"),
        AddNode(kind="nonsense", label="whatever"),
        SetField(label="thumbnailer", field="handler", value="main.handler"),
    ])
    assert len(changes) == 2
    assert len(refused) == 1
    assert refused[0].op["kind"] == "nonsense"
    assert "jobs" in _labels(canvas)


def test_an_op_against_a_node_an_earlier_op_deleted_is_refused_not_crashed():
    """Ops are applied in sequence against the RUNNING canvas, so the second one
    here is genuinely invalid by the time it is reached."""
    _canvas, changes, (refusal,) = apply_ops(CANVAS, [
        DeleteNode(label="uploads"),
        SetField(label="uploads", field="versioning", value="true"),
    ])
    assert len(changes) == 1
    assert "no node called 'uploads'" in refusal.reason


def test_no_ops_is_a_clean_no_op():
    canvas, changes, refused = apply_ops(CANVAS, [])
    assert changes == [] and refused == []
    assert canvas["nodes"] == CANVAS["nodes"] and canvas["edges"] == CANVAS["edges"]
