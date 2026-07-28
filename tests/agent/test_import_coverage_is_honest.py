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

_ROOT = Path(__file__).resolve().parents[2]
README = (_ROOT / "README.md").read_text()
# The translation-coverage claim moved out of the README in v0.8.10, when the
# front page was cut from 803 lines to ~250 and the deep material went to docs/.
# This test follows the CLAIM, not the file it used to live in -- pinning prose
# to behaviour only works if it keeps pointing at the prose.
INTERNALS = (_ROOT / "docs" / "internals.md").read_text()

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
    # An IAM edge, deliberately. Until field test 7 this fixture had NO edges,
    # which made the round-trip claim below vacuous on exactly the thing it
    # mattered most for: a drawn grant never reaches the Terraform at all
    # (`hcl.TfProject.not_in_terraform`), so "odin's own project round-trips
    # with nothing unsupported" was true only for canvases that granted nothing.
    "edges": [
        {"id": "e1", "source": "c1", "target": "b1",
         "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}},
    ],
}


def _round_trip():
    tf = generate_tf(canvas_to_stack(CANVAS)).files["main.tf"]
    return parse_hcl_text(tf)


def test_odins_own_project_now_round_trips_with_nothing_unsupported():
    """v0.8.4 finished the import direction: every kind odin GENERATES it also
    reads back, so its own `main.tf` no longer loses anything.

    This replaced "aws_lambda_function must be listed unsupported", which had
    walked down from five kinds to one over four commits and was about to become
    unfalsifiable. The invariant it protected did not go away -- it moved to the
    test below, where it belongs, because the durable claim is about resources
    odin does not MODEL, not ones it merely could not read.
    """
    result = _round_trip()
    assert result.unsupported == [], [e.type for e in result.unsupported]
    assert {n["type"] for n in result.nodes} >= {
        "vpc", "subnet", "sg", "ec2", "ecs", "s3", "lambda",
    }


def test_a_resource_odin_does_not_model_at_all_is_listed_with_a_reason():
    """Northstar directive 5, the half that stays true forever: silence would mean
    importing a real project and losing part of it without being told. Checked
    against a resource odin has no model for, so full generate-side coverage can
    never make it vacuous."""
    result = parse_hcl_text('resource "aws_route53_zone" "main" {\n  name = "example.com"\n}\n')
    (entry,) = result.unsupported
    assert entry.type == "aws_route53_zone"
    assert entry.reason, "listed with no reason"
    assert result.nodes == []


def test_the_readme_describes_the_coverage_import_actually_has():
    """The doc-drift half, in the direction that actually rots: every kind this
    bullet names as a gap must still BE one.

    Five were named when this file was written. All five became importable in
    v0.8.4, and every commit that closed one had to come back and correct the
    bullet -- which is the entire point of pinning prose to behaviour. The bullet
    must also still separate COVERAGE from FIDELITY: equal kind coverage does not
    make a round trip lossless, and what it does cost is named in Known limits.
    """
    claim = re.search(r"- \*\*Translation\*\*.*?(?=\n- \*\*)", INTERNALS, re.S)
    assert claim, "the Translation bullet moved again -- re-point this test at it"
    text = claim.group(0)
    for kind in ("aws_security_group", "aws_ecr_repository", "aws_instance",
                 "aws_ecs_service", "aws_lambda_function"):
        assert kind not in text, f"the README still calls {kind} un-importable, but import_tf reads it"
    assert "not lossless" in text, (
        "the README claims equal coverage without separating it from FIDELITY -- "
        "an ECS service's wiring, a security group's egress, and a function's code "
        "read from HCL text alone all still cost something"
    )


def test_a_drawn_GRANT_is_emitted_as_real_terraform_and_survives_a_round_trip():
    """v0.8.11 replaced the story this test used to tell.

    It read "a drawn grant does not reach the terraform and says so", and it
    carried an instruction: if odin ever started emitting real IAM policy, the
    test should fail and the whole `not_in_terraform` story should be deleted
    rather than patched to survive. It did fail, and that is what happened — the
    claim "the permission is absent" is gone.

    That instruction applied a second time, and is being honoured the same way
    rather than patched around. v0.8.11's replacement claim was "the policy IS
    emitted, but its `Resource` is odin's node LABEL where Amazon expects an
    ARN"; the ARN work closes that too, so the pinned `(gap,)` assertion is
    DELETED rather than rewritten to match whichever form it currently takes.

    What is asserted instead is the invariant that must hold in every version:
    `not_in_terraform` may note a portability difference, but it may never again
    say the permission is ABSENT. The behaviour worth pinning is the round trip
    below, which is the part that was actually lost.
    """
    project = generate_tf(canvas_to_stack(CANVAS))
    main_tf = project.files["main.tf"]

    assert "aws_iam_role_policy" in main_tf, "the grant must be real Terraform now"
    assert "s3:GetObject" in main_tf, "and carry the action that was drawn"

    assert not any("NO policy is emitted" in gap for gap in project.not_in_terraform), (
        project.not_in_terraform
    )

    # And the round trip keeps it, which is the part that used to be lost.
    imported = parse_hcl_text(main_tf)
    assert imported.unsupported == [], [e.type for e in imported.unsupported]
    (edge,) = [e for e in imported.edges if (e.get("data") or {}).get("edgeType") == "iam"]
    assert edge["source"] == "web" and edge["target"] == "uploads"
    assert edge["data"]["permissions"] == ["s3:GetObject"]

    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf, "generate -> import -> generate must be stable"
