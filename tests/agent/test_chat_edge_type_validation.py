"""`odin chat` may not invent an edge KIND.

`AddEdge.edge_type` was a plain `str` with a description and no enum, and
`Edge.kind` is a free `str` all the way down to the store, so an invented kind
('access', 'connects', 'permission') round-tripped through a revision and
through Apply looking exactly like a real edge -- and did nothing, for ever.
It is the same shape as the invented FIELD `chat.py` already refuses one level
up: the canvas is permissive about `data.*` BY DESIGN, so the gate has to sit
where the op is authored.

WHAT THIS DOES NOT CLOSE, stated here so the file cannot be read as having
closed it. `iac/hcl.py`'s subscription and ALB passes match on the two NODE
kinds and never read `edge.kind` at all, so a perfectly valid `iam` edge between
an sns node and an sqs node still emits a real `aws_sns_topic_subscription`.
Kind-blindness is the PRIMARY defect and it survives this fix entirely --
`tests/spec/test_edge_types.py` pins that behaviour deliberately, because
requiring the kind without a migration would make `tofu` destroy the live
subscription of every canvas saved before edge types were named.
"""
from __future__ import annotations

import pytest

from odin.agent.chat import AddEdge, apply_ops, validate
from odin.spec.translate import EDGE_KINDS

CANVAS = {
    "nodes": [
        {"id": "sns-1", "type": "sns", "position": {"x": 0, "y": 0}, "data": {"label": "events"}},
        {"id": "sqs-1", "type": "sqs", "position": {"x": 20, "y": 0}, "data": {"label": "jobs"}},
    ],
    "edges": [],
}


@pytest.mark.parametrize("kind", sorted(EDGE_KINDS))
def test_every_kind_odin_models_is_accepted(kind: str):
    assert validate(AddEdge(source="events", target="jobs", edge_type=kind), CANVAS) is None


@pytest.mark.parametrize("invented", ["access", "connects", "permission", "IAM", "", "network "])
def test_an_invented_kind_is_refused(invented: str):
    reason = validate(AddEdge(source="events", target="jobs", edge_type=invented), CANVAS)
    assert reason is not None
    assert repr(invented) in reason
    # The refusal must NAME the valid types -- an agent (or a user reading the
    # proposal) cannot correct a vocabulary it is never shown.
    for kind in EDGE_KINDS:
        assert kind in reason


def test_a_refused_edge_is_not_drawn():
    """`apply_ops` skips a refused op and reports it; nothing may reach the
    canvas. Checked through `apply_ops` rather than `validate` alone because the
    gate only matters if the caller honours it."""
    updated, changes, refused = apply_ops(
        CANVAS, [AddEdge(source="events", target="jobs", edge_type="connects")],
    )
    assert updated["edges"] == []
    assert changes == []
    assert len(refused) == 1 and "connects" in refused[0].reason


def test_a_valid_edge_still_gets_drawn():
    """Guards the guard: a validation that refused everything would pass every
    assertion above while breaking the feature."""
    updated, changes, refused = apply_ops(
        CANVAS, [AddEdge(source="events", target="jobs", edge_type="subscription")],
    )
    assert refused == [] and len(changes) == 1
    (edge,) = updated["edges"]
    assert edge["data"]["edgeType"] == "subscription"


def test_a_missing_node_is_still_reported_before_the_kind():
    """The endpoint check runs first: told an edge names a node that does not
    exist AND carries a nonsense kind, the missing node is the actionable half."""
    reason = validate(AddEdge(source="ghost", target="jobs", edge_type="connects"), CANVAS)
    assert reason is not None and "ghost" in reason


def test_the_legacy_catch_all_is_still_accepted():
    """Every canvas on disk carries `network` on its unmodelled edges. Refusing
    it would make `odin chat` unable to reproduce an edge the user already has."""
    assert validate(AddEdge(source="events", target="jobs", edge_type="network"), CANVAS) is None
