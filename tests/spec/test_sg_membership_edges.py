"""An SG-membership EDGE authors the same field the text box already feeds.

Which instances a security group gates is a RELATIONSHIP, not ownership. odin's
containment already supplies an SG's own `vpc_id` -- it belongs to exactly one
VPC, immutably, and geometry expresses that correctly. But membership is a
many-to-many fact between peers, and until now it could only be TYPED into an
ec2/rds node's `securityGroups` field, one label per line, which meant the
canvas could not show it at all.

The design that makes this safe rather than a second source of truth: the edge
ADDS to that field instead of replacing it. `iac/hcl.py` is untouched -- it
still reads one field (`_security_group_refs`, used by `_ec2` and `_rds`) and
cannot tell how a line got there. A hand-authored canvas keeps working
unchanged, which matters because `odin canvas set`, the README's documented JSON
schema, and the translation agent next are all first-class authors.
"""
from __future__ import annotations

from odin.spec.translate import canvas_to_stack


def _canvas(edges: list[dict], sg_field: str = "") -> dict:
    return {
        "nodes": [
            {"id": "vpc-1", "type": "vpc", "position": {"x": 0, "y": 0}, "data": {"label": "prod-vpc"}},
            {"id": "sg-1", "type": "sg", "position": {"x": 20, "y": 20}, "data": {"label": "api-sg"}},
            {"id": "sg-2", "type": "sg", "position": {"x": 40, "y": 20}, "data": {"label": "db-sg"}},
            {"id": "ec2-1", "type": "ec2", "position": {"x": 60, "y": 20},
             "data": {"label": "api-server", **({"securityGroups": sg_field} if sg_field else {})}},
            {"id": "rds-1", "type": "rds", "position": {"x": 80, "y": 20}, "data": {"label": "app-db"}},
            {"id": "s3-1", "type": "s3", "position": {"x": 100, "y": 20}, "data": {"label": "uploads"}},
        ],
        "edges": edges,
    }


def _groups(stack, resource_id: str) -> list[str]:
    resource = next(r for r in stack.resources if r.id == resource_id)
    raw = resource.fields.get("securityGroups")
    return [line.strip() for line in str(raw.value).splitlines() if line.strip()] if raw else []


def _edge(source: str, target: str, kind: str = "sg") -> dict:
    return {"id": f"{source}-{target}", "source": source, "target": target, "data": {"edgeType": kind}}


def test_an_sg_edge_makes_the_instance_a_member():
    stack = canvas_to_stack(_canvas([_edge("sg-1", "ec2-1")]))
    assert _groups(stack, "api-server") == ["api-sg"]


def test_direction_does_not_matter():
    """sg->instance and instance->sg are the same intent, exactly as an IAM edge
    is the same either way round. One of them silently doing nothing would be a
    trap, not a distinction."""
    forward = canvas_to_stack(_canvas([_edge("sg-1", "ec2-1")]))
    backward = canvas_to_stack(_canvas([_edge("ec2-1", "sg-1")]))
    assert _groups(forward, "api-server") == _groups(backward, "api-server") == ["api-sg"]


def test_an_edge_ADDS_to_a_typed_field_rather_than_replacing_it():
    """The hand-authored path must keep working: the field is what `hcl.py`
    reads, and a canvas written by hand or by an agent fills it directly."""
    stack = canvas_to_stack(_canvas([_edge("sg-2", "ec2-1")], sg_field="api-sg"))
    assert _groups(stack, "api-server") == ["api-sg", "db-sg"]


def test_an_edge_duplicating_a_typed_line_is_a_no_op():
    """Real AWS rejects a doubled security_groups entry, so drawing what is
    already typed must not produce one."""
    stack = canvas_to_stack(_canvas([_edge("sg-1", "ec2-1")], sg_field="api-sg"))
    assert _groups(stack, "api-server") == ["api-sg"]


def test_several_groups_accumulate_in_order():
    stack = canvas_to_stack(_canvas([_edge("sg-1", "ec2-1"), _edge("sg-2", "ec2-1")]))
    assert _groups(stack, "api-server") == ["api-sg", "db-sg"]


def test_rds_is_a_member_kind_too():
    """`_rds` reads the same field through the same builder, so a database
    drawn behind an SG must work exactly like an instance."""
    stack = canvas_to_stack(_canvas([_edge("sg-2", "rds-1")]))
    assert _groups(stack, "app-db") == ["db-sg"]


def test_an_sg_edge_to_a_kind_with_no_such_field_is_ignored():
    """s3 has no `securityGroups` in its HCL, so inventing one would put a field
    into the stack that nothing reads -- a silent no-op dressed as a setting."""
    stack = canvas_to_stack(_canvas([_edge("sg-1", "s3-1")]))
    assert _groups(stack, "uploads") == []


def test_a_non_membership_edge_changes_nothing():
    """An IAM or network edge between the same two nodes must not grant
    membership -- the KIND is what carries the meaning."""
    for kind in ("iam", "network"):
        stack = canvas_to_stack(_canvas([_edge("sg-1", "ec2-1", kind=kind)]))
        assert _groups(stack, "api-server") == [], kind


def test_an_edge_naming_a_node_that_is_not_on_the_canvas_is_ignored():
    stack = canvas_to_stack(_canvas([_edge("sg-ghost", "ec2-1")]))
    assert _groups(stack, "api-server") == []


def test_the_edges_themselves_survive_into_the_stack():
    """The merge must not consume the edge: `/world`, the IAM review and the UI
    all still read `stack.edges`."""
    stack = canvas_to_stack(_canvas([_edge("sg-1", "ec2-1")]))
    assert [(e.src, e.dst, e.kind) for e in stack.edges] == [("api-sg", "api-server", "sg")]


def test_a_canvas_with_no_sg_edges_is_untouched():
    """Byte-for-byte the old behaviour for every canvas that predates this."""
    before = canvas_to_stack(_canvas([], sg_field="api-sg"))
    assert _groups(before, "api-server") == ["api-sg"]
    assert next(r for r in before.resources if r.id == "app-db").fields.get("securityGroups") is None
