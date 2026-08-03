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

from odin.agent import hcl, import_tf
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
        # v0.8.18: an ebs node plus the edge below, so the round-trip claim
        # covers the `aws_volume_attachment` COMPANION. A companion that does not
        # come back is not a cosmetic loss here -- the regenerated project omits
        # the attachment, and the next apply detaches a disk with data on it.
        {"id": "d1", "type": "ebs", "position": {"x": 0, "y": 0},
         "data": {"label": "scratch", "az": "us-east-1a", "size": "40"}},
        # v0.8.19: a route53 zone plus the edge below, for the ebs reason one
        # service over -- the round-trip claim has to cover the
        # `aws_route53_record` COMPANION. A record that does not come back means
        # the regenerated project writes no hosts entry, so a name that resolved
        # stops resolving on the next apply.
        {"id": "z1", "type": "route53", "position": {"x": 0, "y": 0},
         "data": {"label": "example.com"}},
        # v0.8.19: an efs node plus BOTH mount edges below, so the round-trip
        # claim covers the `aws_efs_access_point` companion and the two mount
        # shapes at once.
        #
        # `/mnt/shared` IS NOT ARBITRARY -- do not tidy it to `/mnt/efs`. A
        # round trip over DEFAULT values proves the defaults agree, not that
        # the data survives: `/mnt/efs` regenerates byte-identically even with
        # the path dropped entirely, because `hcl.py::_DEFAULT_EFS_PATH` refills
        # it. Measured over an efs+ecs canvas -- `/mnt/efs` vs path ABSENT are
        # IDENTICAL, `/mnt/scratch` vs ABSENT DIFFER. Same reasoning, at length,
        # in `test_import_efs.py::test_the_round_trip_keeps_a_non_default_mount_path`.
        {"id": "fs1", "type": "efs", "position": {"x": 0, "y": 0},
         "data": {"label": "shared", "path": "/mnt/shared"}},
        # v0.8.21: a load balancer, plus the target edge and the ec2 GRANT below.
        # This is the widening the defect asked for, and the defect is the
        # argument for it: `aws_iam_instance_profile` and
        # `aws_lb_target_group_attachment` both came back `unsupported` from a
        # project odin itself had just written, and the assertion below did not
        # notice because THIS CANVAS CONTAINED NEITHER SHAPE. The guard was real;
        # its fixture was narrower than the generator. Both resources exist only
        # for a canvas with an `alb -> ec2` target and a granted `ec2`, so both
        # have to be here or the gap reopens in silence.
        #
        # `listenerPort`/`port`/`healthCheckPath` ARE NOT ARBITRARY -- do not
        # tidy them to the defaults. 80/80/"/" regenerate byte-identically even
        # if import drops all three, because `hcl.py` refills them; 8080/9000/
        # "/healthz" cannot be reproduced by a default. Same reasoning, at
        # length, in `test_import_alb_targets.py`.
        {"id": "lb1", "type": "alb", "position": {"x": 0, "y": 0},
         "data": {"label": "front", "vpc": "prod-vpc", "subnet": "app-subnet",
                  "listenerPort": "8080", "port": "9000", "healthCheckPath": "/healthz"}},
    ],
    # An IAM edge, deliberately. Until field test 7 this fixture had NO edges,
    # which made the round-trip claim below vacuous on exactly the thing it
    # mattered most for: a drawn grant never reaches the Terraform at all
    # (`hcl.TfProject.not_in_terraform`), so "odin's own project round-trips
    # with nothing unsupported" was true only for canvases that granted nothing.
    "edges": [
        {"id": "e1", "source": "c1", "target": "b1",
         "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}},
        {"id": "e2", "source": "d1", "target": "e1", "data": {"edgeType": "volume"}},
        {"id": "e3", "source": "z1", "target": "e1", "data": {"edgeType": "dns"}},
        # ONE file system, TWO consumers, which is the whole difference between
        # `mount` and `volume`: a gp3 volume attaches to exactly one instance,
        # an EFS file system is shared. The second edge is also drawn the OTHER
        # WAY ROUND, because `hcl.py`'s mount pass keys on the two node kinds
        # and must read either direction the same.
        {"id": "e3", "source": "fs1", "target": "c1", "data": {"edgeType": "mount"}},
        {"id": "e4", "source": "f1", "target": "fs1", "data": {"edgeType": "mount"}},
        # v0.8.21, the two edges the alb node above exists for. The TARGET edge
        # makes odin emit an `aws_lb_target_group_attachment` (an ec2 is
        # registered by tofu; an ecs service registers its own tasks with a
        # `load_balancer` block, which is a second shape of the same canvas edge
        # and is covered by `e6`), and the GRANT makes it emit an
        # `aws_iam_instance_profile` -- odin emits one for a granted instance and
        # for no other instance, so nothing short of a real grant produces it.
        {"id": "e5", "source": "lb1", "target": "e1", "data": {"edgeType": "target"}},
        {"id": "e6", "source": "lb1", "target": "c1", "data": {"edgeType": "target"}},
        {"id": "e7", "source": "e1", "target": "b1",
         "data": {"edgeType": "iam", "permissions": ["s3:PutObject"]}},
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
        "vpc", "subnet", "sg", "ec2", "ecs", "s3", "lambda", "ebs", "route53",
        "vpc", "subnet", "sg", "ec2", "ecs", "s3", "lambda", "ebs", "efs",
        "alb",
    }


def test_a_resource_odin_does_not_model_at_all_is_listed_with_a_reason():
    """Northstar directive 5, the half that stays true forever: silence would mean
    importing a real project and losing part of it without being told. Checked
    against a resource odin has no model for, so full generate-side coverage can
    never make it vacuous.

    `aws_kinesis_stream`, not `aws_route53_zone`: route53 became importable in
    v0.8.19, which is the THIRD time this example has had to move (lambda ->
    route53 -> here). It stops moving now, because the choice is no longer "a
    kind nobody has got to yet" -- ROADMAP.md records `kinesis` as DROPPED, with
    the reason "has no substrate at all", so it cannot quietly become modelled
    the way the other two did.
    """
    result = parse_hcl_text('resource "aws_kinesis_stream" "events" {\n  name = "events"\n}\n')
    (entry,) = result.unsupported
    assert entry.type == "aws_kinesis_stream"
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


def test_the_coverage_numbers_in_that_bullet_are_measured_not_written():
    """The bullet quotes three counts, and until v0.8.18 nothing read them.

    They had gone stale exactly the way `docs/limits.md`'s edge-pair counts
    did before `edge-types.test.ts` started recomputing them -- the bullet
    said "builds 18 … across 24 resource types" while the real companion
    count made 24 arithmetically impossible for 18 kinds. Nobody was
    careless; the numbers simply lived in a file no build reads, which is the
    definition of prose that cannot fail. So they are derived here from the
    real registries instead, and a kind or companion added on either side
    fails this test until the sentence is corrected.

    Mutation-test: change either number in `docs/internals.md` and this
    fails."""
    builds = len(hcl._BUILDERS)
    reads = len(set(import_tf._KIND.values()))
    # Every `aws_*` type `parse_hcl` recognises: the primaries, plus every
    # companion folded onto a node or turned into an edge rather than
    # becoming one. `aws_key_pair` is handled by a literal branch in
    # `parse_hcl` and belongs to no registry, so it is named here.
    companions = (
        set(import_tf._ECS_COMPANION_TYPES)
        | set(import_tf._ALB_COMPANION_TYPES)
        | set(import_tf._CARRIED_COMPANION_ATTRS)
        | {import_tf._IAM_POLICY_TYPE, "aws_key_pair"}
    )
    types = len(set(import_tf._KIND) | companions)

    claim = re.search(r"- \*\*Translation\*\*.*?(?=\n- \*\*)", INTERNALS, re.S).group(0)
    assert f"builds {builds}" in claim, f"internals.md is behind: odin builds {builds} kinds"
    assert f"reads all {reads} back across {types} resource types" in claim, (
        f"internals.md is behind: import reads {reads} kinds across {types} resource types"
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
    # BOTH grants, by (source, target), not `(edge,) = ...`. The canvas gained an
    # ec2 grant in v0.8.21 (it is what makes odin emit the
    # `aws_iam_instance_profile` this round trip has to survive), and a one-tuple
    # unpack would have made that widening look like a regression here.
    assert {
        (e["source"], e["target"]): e["data"]["permissions"] for e in imported.edges
        if (e.get("data") or {}).get("edgeType") == "iam"
    } == {("web", "uploads"): ["s3:GetObject"], ("api-server", "uploads"): ["s3:PutObject"]}

    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf, "generate -> import -> generate must be stable"
