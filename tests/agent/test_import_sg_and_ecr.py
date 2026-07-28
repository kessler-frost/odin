"""Terraform -> canvas for `aws_security_group` and `aws_ecr_repository`.

## Why these two, and why the security group is the interesting one

odin generates 18 kinds and, before v0.8.4, read back 13 -- so feeding odin its
own `main.tf` lost a third of it. A security group is the worst of the five to
lose, because what goes missing is not a label: its `ingress` rules are what
`fabric/nebula.py::sg_rules_to_firewall` compiles into the real Nebula firewall,
so an import that drops them drops the security posture and the canvas shows no
sign a rule ever existed. ECR is the opposite case -- one argument, reported
unsupported only because nobody had written the line.

## The two directions an import can lie, and which one is worse

A regenerated group can allow MORE than the source (a restriction lost) or LESS
(a rule lost). Both are reported here, and neither is silent:

  * `ingress` blocks that cannot become one `protocol:port:source` line -- a port
    RANGE, or a source odin cannot resolve -- are named, with the count, because
    the regenerated group allows LESS and traffic that used to work stops.
  * `egress` is worse in principle: odin re-emits its own wide-open default and
    has no field for outbound rules at all, so a source that restricted egress
    comes back UNRESTRICTED. That is a posture change in the dangerous
    direction, and v0.8.4 is the first release where it can happen, so it warns
    from the start rather than being found later.

## The identity rule

`security_groups = [aws_security_group.web.id]` is the "only the web tier may
reach me" form, and the only source form that gates by IDENTITY rather than
address (nebula matches the peer's certificate groups; overlay addresses are not
VPC addresses, so a CIDR rule could never gate mesh traffic). It has to come
back as the referenced group's LABEL, because a canvas contains no ids -- which
is also why it needs a post-pass: a rule may name a group defined later in the
file.
"""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

CANVAS = {
    "nodes": [
        {"id": "v1", "type": "vpc", "position": {"x": 0, "y": 0},
         "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "g1", "type": "sg", "position": {"x": 0, "y": 0},
         "data": {"label": "web-sg", "vpc": "prod-vpc", "ingressRules": "tcp:443:0.0.0.0/0"}},
        {"id": "g2", "type": "sg", "position": {"x": 0, "y": 0},
         "data": {"label": "db-sg", "vpc": "prod-vpc", "ingressRules": "tcp:5432:web-sg"}},
        {"id": "r1", "type": "ecr", "position": {"x": 0, "y": 0}, "data": {"label": "app-images"}},
    ],
    "edges": [],
}


def _round_trip(canvas: dict):
    tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    return parse_hcl_text(tf), tf


def _node(result, label: str) -> dict:
    return next(n for n in result.nodes if n["id"] == label)


def test_both_kinds_come_back_as_nodes_instead_of_being_listed_unsupported():
    result, _tf = _round_trip(CANVAS)
    assert {n["type"] for n in result.nodes} == {"vpc", "sg", "ecr"}
    listed = {entry.type for entry in result.unsupported}
    assert "aws_security_group" not in listed, listed
    assert "aws_ecr_repository" not in listed, listed


def test_a_cidr_rule_round_trips_as_its_own_text():
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "web-sg")["data"]["ingressRules"] == "tcp:443:0.0.0.0/0"


def test_a_group_to_group_rule_comes_back_as_the_LABEL_not_an_id():
    """The identity rule. A canvas has no `sg-` ids in it, so reading the
    reference back as anything but the label would break the round trip."""
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "db-sg")["data"]["ingressRules"] == "tcp:5432:web-sg"


def test_the_vpc_containment_stamp_is_rebuilt_from_vpc_id():
    """`hcl.py::_sg` REFUSES to build a group that is not inside a VPC, so
    without this stamp the imported node is one Apply silently skips."""
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "web-sg")["data"]["vpc"] == "prod-vpc"
    assert _node(result, "db-sg")["data"]["vpc"] == "prod-vpc"


def test_the_whole_thing_regenerates_byte_for_byte():
    """THE round-trip proof: generate -> import -> generate reproduces the same
    Terraform, so nothing was quietly added, dropped or renamed in the middle."""
    _result, first = _round_trip(CANVAS)
    imported, _ = _round_trip(CANVAS)
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == first


def test_odins_own_output_produces_no_warnings():
    """A round trip of odin's own generation must be QUIET. If odin's default
    egress warned about itself, the warning would be noise on every import and
    would train people to ignore the one that matters."""
    result, _tf = _round_trip(CANVAS)
    assert result.warnings == [], result.warnings


# --- the two directions an imported group can differ from its source ----------

_RESTRICTED_EGRESS = """
resource "aws_vpc" "net" {
  cidr_block = "10.0.0.0/16"
  tags = { "odin:node" = "prod-vpc" }
}

resource "aws_security_group" "locked" {
  name   = "locked-sg"
  vpc_id = aws_vpc.net.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
}
"""


def test_a_restricted_egress_is_now_CARRIED_rather_than_reported_as_lost():
    """v0.8.14 closed this. Until then odin had no canvas field for outbound
    rules, so the best this import could do was warn that a restricted group came
    back UNRESTRICTED -- which is what this test used to assert.

    `hcl-generate` added an `egressRules` field and real `egress` emission, so the
    rules survive and the warning would now be a caveat outliving its fix
    (honesty rule 3). The direction of the assertion flips: the rule must be on
    the node, and nothing may claim it was lost. See
    `tests/agent/test_import_wiring.py` for the round trip and for the case that
    still IS lossy (a rule odin cannot express, which empties the field and
    therefore does come back wide open)."""
    result = parse_hcl_text(_RESTRICTED_EGRESS)
    data = _node(result, "locked-sg")["data"]
    assert data["ingressRules"] == "tcp:443:0.0.0.0/0"
    assert data["egressRules"] == "tcp:443:10.0.0.0/16"
    assert [w for w in result.warnings if "egress" in w] == []


_PORT_RANGE = """
resource "aws_vpc" "net" {
  cidr_block = "10.0.0.0/16"
  tags = { "odin:node" = "prod-vpc" }
}

resource "aws_security_group" "ranged" {
  name   = "ranged-sg"
  vpc_id = aws_vpc.net.id

  ingress {
    from_port   = 8000
    to_port     = 8999
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
}
"""


def test_a_port_range_is_named_rather_than_narrowed_to_one_port():
    """odin's rule is a SINGLE port. Importing 8000-8999 as `tcp:8000:...` would
    close 999 ports without a word; the rule is left out and counted instead."""
    result = parse_hcl_text(_PORT_RANGE)
    assert _node(result, "ranged-sg")["data"]["ingressRules"] == "tcp:22:10.0.0.0/16"
    (warning,) = [w for w in result.warnings if "ingress rule" in w]
    assert "1 of 2" in warning, warning
    assert "allows LESS" in warning, warning


def test_a_group_outside_any_imported_vpc_is_reported_not_silently_applied():
    tf = 'resource "aws_security_group" "orphan" {\n  name = "orphan-sg"\n  vpc_id = "vpc-123"\n}\n'
    result = parse_hcl_text(tf)
    (warning,) = [w for w in result.warnings if "containment" in w]
    assert "Apply will skip it" in warning, warning
