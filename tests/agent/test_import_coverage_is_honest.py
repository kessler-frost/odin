"""What `import_tf` cannot take, it must NAME -- and the README must agree.

Canvas -> Terraform covers every kind odin builds. Terraform -> canvas does not,
and the README described the pair as "deterministic in both directions", which
reads as equal coverage. Determinism and completeness are different claims.

The behaviour itself was already honest (northstar directive 5: unsupported
types are listed, never dropped). What was missing was any check that the
README's summary kept matching it -- the doc-drift this repo keeps auditing for.
"""
from __future__ import annotations

import re
from pathlib import Path

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

README = (Path(__file__).resolve().parents[2] / "README.md").read_text()

CANVAS = {
    "nodes": [
        {"id": "v1", "type": "vpc", "position": {"x": 0, "y": 0}, "data": {"label": "prod-vpc"}},
        {"id": "s1", "type": "subnet", "position": {"x": 0, "y": 0},
         "data": {"label": "app-subnet", "vpc": "prod-vpc"}},
        {"id": "g1", "type": "sg", "position": {"x": 0, "y": 0},
         "data": {"label": "api-sg", "vpc": "prod-vpc"}},
        {"id": "e1", "type": "ec2", "position": {"x": 0, "y": 0},
         "data": {"label": "api-server", "subnet": "app-subnet", "securityGroups": "api-sg"}},
        {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0},
         "data": {"label": "web", "image": "nginx:alpine", "count": "1", "port": "80"}},
        {"id": "b1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "uploads"}},
        # v0.8.4: as `sg`/`ec2`/`ecs` became importable this canvas ran out of
        # un-importable kinds to check, and the assertion below would have passed
        # vacuously against a canvas that produced no `aws_lambda_function` at
        # all. The last remaining gap has to actually be IN the fixture.
        {"id": "f1", "type": "lambda", "position": {"x": 0, "y": 0},
         "data": {"label": "thumbnailer", "code": "def lambda_handler(event, context):\n    return event\n"}},
    ],
    "edges": [],
}


def _round_trip():
    tf = generate_tf(canvas_to_stack(CANVAS)).files["main.tf"]
    return parse_hcl_text(tf)


def test_what_cannot_be_imported_is_listed_rather_than_dropped():
    """Northstar directive 5. Silence here would mean a user re-imports their
    own generated project and loses half of it without being told."""
    result = _round_trip()
    listed = {entry.type for entry in result.unsupported}
    # `aws_security_group` left this list in v0.8.4 by BECOMING importable --
    # which is what this file is for: the ratchet failed, and the README was
    # corrected in the same change rather than drifting.
    for kind in ("aws_lambda_function",):
        assert kind in listed, f"{kind} vanished silently; listed={sorted(listed)}"
    for entry in result.unsupported:
        assert entry.reason, f"{entry.type} was listed with no reason"


def test_what_can_be_imported_actually_comes_back():
    result = _round_trip()
    assert {n["type"] for n in result.nodes} >= {"vpc", "subnet", "s3"}


def test_the_readme_does_not_claim_import_covers_everything():
    """The doc-drift half. `import_tf` gaining `aws_instance` support should
    make this fail, so the README is updated in the same change."""
    claim = re.search(r"- \*\*Translation\*\*.*?(?=\n- \*\*)", README, re.S)
    assert claim, "the Translation bullet moved -- re-point this test"
    text = claim.group(0)
    assert "do not cover the same ground" in text, (
        "the README summarises translation without saying the import direction "
        "covers less -- determinism and completeness are different claims"
    )
    for kind in ("aws_lambda_function",):
        assert kind in text, f"the README does not name {kind} as un-importable"
    # And the inverse, which is the half that actually rots: a kind that BECAME
    # importable must stop being advertised as a gap.
    for kind in ("aws_security_group", "aws_ecr_repository", "aws_instance", "aws_ecs_service"):
        assert kind not in text, f"the README still calls {kind} un-importable, but import_tf reads it"
