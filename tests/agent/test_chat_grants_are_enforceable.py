"""A permission the chat agent grants must actually GRANT something.

## The bug this exists for

`odin chat "give resizer read access to the uploads bucket"` drew the edge,
applied clean, and reported

    Granted resizer read access (s3:GetObject, s3:ListBucket) to the uploads bucket.

while the gateway compiled `Statement(actions=(), resources=('uploads',))` — a
grant that allows NOTHING. Measured against a live environment in field test 7.

The cause was one word: `apply_ops` wrote the permissions under `data["actions"]`
and `spec/translate.py::_edge` reads `data["permissions"]`.

## Why the existing tests missed it, and what this file does differently

They asserted `edge["data"]["actions"] == [...]` — the key the code under test
had just written. Both ends agreed with each other, and neither was ever checked
against the thing between them, so the suite was green while the feature was
decorative.

So these assert the END of the chain: the `Statement` the GATEWAY compiles, and
`policy.evaluate` returning True for the granted call. Nothing about the canvas
key appears here, which means renaming it again can only fail loudly.

This is the same guarantee `tests/gateway/test_iam_vocabulary_is_enforceable.py`
gives the UI's vocabulary, applied to the agent's output.
"""
from __future__ import annotations

from odin.agent.chat import AddEdge, AddNode, apply_ops
from odin.gateway.policy import compile_policies, evaluate
from odin.spec.translate import canvas_to_stack

CANVAS = {
    "nodes": [
        {"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "uploads"}},
        {"id": "lambda-1", "type": "lambda", "position": {"x": 220, "y": 0},
         "data": {"label": "resizer", "code": "print(1)"}},
    ],
    "edges": [],
}


def _policies(canvas: dict):
    return compile_policies(canvas_to_stack(canvas))


def test_a_granted_action_is_actually_allowed():
    """THE test. A grant the gateway would deny is not a grant."""
    canvas, _changes, refused = apply_ops(CANVAS, [
        AddEdge(source="resizer", target="uploads", actions=["s3:GetObject", "s3:ListBucket"]),
    ])
    assert refused == []

    statements = _policies(canvas)["resizer"]
    assert evaluate(statements, "s3:GetObject", "uploads") is True
    assert evaluate(statements, "s3:ListBucket", "uploads") is True


def test_an_ungranted_action_is_still_denied():
    """A policy that allows everything would pass the test above and be worse
    than the bug it replaced."""
    canvas, _changes, _refused = apply_ops(CANVAS, [
        AddEdge(source="resizer", target="uploads", actions=["s3:GetObject"]),
    ])
    statements = _policies(canvas)["resizer"]
    assert evaluate(statements, "s3:PutObject", "uploads") is False
    assert evaluate(statements, "s3:GetObject", "some-other-bucket") is False


def test_the_compiled_statement_is_not_empty():
    """The precise shape of the bug: `Statement(actions=(), ...)` — an edge that
    exists, applies, and permits nothing."""
    canvas, _changes, _refused = apply_ops(CANVAS, [
        AddEdge(source="resizer", target="uploads", actions=["s3:GetObject"]),
    ])
    (statement,) = _policies(canvas)["resizer"]
    assert statement.actions == ("s3:GetObject",), "an empty actions tuple is a decorative grant"
    assert statement.resources == ("uploads",)


def test_an_edge_to_a_node_the_agent_just_added_is_enforceable_too():
    """The common two-op request ("add a queue and let the worker write to it")
    must not produce a grant against a node that only half exists."""
    canvas, _changes, refused = apply_ops(CANVAS, [
        AddNode(kind="sqs", label="jobs"),
        AddEdge(source="resizer", target="jobs", actions=["sqs:SendMessage"]),
    ])
    assert refused == []
    assert evaluate(_policies(canvas)["resizer"], "sqs:SendMessage", "jobs") is True


def test_a_network_edge_grants_nothing_and_should_not():
    """Only `iam` edges compile to policy. A network edge granting access would
    be the inverse bug — a permission nobody drew."""
    canvas, _changes, _refused = apply_ops(CANVAS, [
        AddEdge(source="resizer", target="uploads", edge_type="network"),
    ])
    assert _policies(canvas) == {}
