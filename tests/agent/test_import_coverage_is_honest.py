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
    claim = re.search(r"- \*\*Translation\*\*.*?(?=\n- \*\*)", README, re.S)
    assert claim, "the Translation bullet moved -- re-point this test"
    text = claim.group(0)
    for kind in ("aws_security_group", "aws_ecr_repository", "aws_instance",
                 "aws_ecs_service", "aws_lambda_function"):
        assert kind not in text, f"the README still calls {kind} un-importable, but import_tf reads it"
    assert "not lossless" in text, (
        "the README claims equal coverage without separating it from FIDELITY -- "
        "an ECS service's wiring, a security group's egress, and a function's code "
        "read from HCL text alone all still cost something"
    )


def test_a_drawn_GRANT_does_not_reach_the_terraform_and_says_so():
    """The round trip is complete for NODES and lossy for EDGES, and the honest
    version of that sentence is the point of this test.

    A drawn IAM edge is enforced -- `gateway/policy.py::compile_policies` builds
    it from the Stack and the gateway denies anything without a matching grant --
    but enforcement happens in odin's gateway, not through Terraform, so nothing
    about it is written into `main.tf`. Measured in field test 7: a canvas
    granting a lambda `s3:GetObject` produced five resources and ZERO mentions of
    the permission, and importing that file back returned the nodes with no edges
    and no warning of any kind.

    So the loss is REPORTED at the point it happens (translate), because import
    cannot warn about something that was never in the file it reads.
    """
    project = generate_tf(canvas_to_stack(CANVAS))
    assert "s3:GetObject" not in project.files["main.tf"], (
        "if odin starts emitting real IAM policy for drawn edges, this test should "
        "fail and the whole not_in_terraform story should be deleted, not updated"
    )
    (gap,) = project.not_in_terraform
    assert "web -> uploads" in gap
    assert "s3:GetObject" in gap
    assert "grants it nothing" in gap

    # ...and the round trip really does drop it, which is what the warning is for.
    assert parse_hcl_text(project.files["main.tf"]).edges == []
