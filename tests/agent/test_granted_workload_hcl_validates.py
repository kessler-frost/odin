"""A granted workload's generated Terraform must survive `tofu validate`.

v0.8.12 made a drawn permission take effect only after an apply: the permission
is emitted as a real `aws_iam_role_policy`, and each granted workload gets a
`depends_on` pointing at it, so a container cannot start before the policy that
authorizes it. tofu is otherwise free to order two resources that merely share a
role either way, and a workload that came up first would be denied a permission
that WAS applied — a failure indistinguishable from a wrong grant.

The risk that buys is specific and severe, and `hcl.py`'s own pass-2 comment
already names it from an earlier bug: an unresolvable reference fails
`tofu plan` for the WHOLE project, so ONE workload whose policy was reserved but
never emitted would stop every other resource on the canvas from applying. A
`depends_on` is exactly such a reference.

So this runs the real `tofu validate` over the real generated file for every
kind that can hold a grant. Nothing here fabricates the checker (honesty rule 1):
`validate_refinement` shells out to the same `tofu` binary an apply uses, and it
is the only check of the three that reads the provider schema. The companion
assertion is by CONTENT — the block each `depends_on` names is really in the
file — because `tofu validate` alone would also pass a file with no IAM in it.
"""
from __future__ import annotations

import re
import shutil

import pytest

from odin.iac.hcl import generate_tf
from odin.agent.translate import validate_refinement
from odin.spec.translate import canvas_to_stack

_NO_TOFU = shutil.which("tofu") is None

# One canvas per grantable kind. Each is the minimum that reaches the new code:
# a workload, something to grant on, and an IAM edge between them.
WORKLOADS = {
    "lambda": {"id": "w", "type": "lambda",
               "data": {"label": "resizer", "runtime": "python3.12",
                        "code": "def lambda_handler(event, context):\n    return event\n"}},
    "ecs": {"id": "w", "type": "ecs", "data": {"label": "api", "image": "nginx:alpine"}},
    "ec2": {"id": "w", "type": "ec2", "data": {"label": "box", "instanceType": "t3.micro"}},
}


# An `aws_instance` is only emitted for an ec2 node drawn inside a subnet inside
# a VPC (`hcl.py` opts out otherwise, and says so in `unsupported`), so the ec2
# canvas carries that containment. Found by this test's own first run: without
# it there was no instance at all, and the assertion below correctly reported
# that nothing depended on the policy.
NETWORK = [
    {"id": "vpc", "type": "vpc", "position": {"x": 0, "y": 0},
     "data": {"label": "net", "cidrBlock": "10.0.0.0/16"}},
    {"id": "sub", "type": "subnet", "position": {"x": 20, "y": 20},
     "data": {"label": "net-a", "cidrBlock": "10.0.1.0/24", "vpc": "net"}},
]


def _canvas(kind: str) -> dict:
    workload = dict(WORKLOADS[kind])
    extra = NETWORK if kind == "ec2" else []
    if kind == "ec2":
        workload["data"] = {**workload["data"], "subnet": "net-a"}
    return {
        "nodes": [
            workload,
            {"id": "b", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "uploads"}},
            *extra,
        ],
        "edges": [{"id": "e", "source": "w", "target": "b",
                   "data": {"edgeType": "iam", "permissions": ["s3:GetObject", "s3:ListBucket"]}}],
    }


def _main_tf(kind: str) -> str:
    return generate_tf(canvas_to_stack(_canvas(kind))).files["main.tf"]


@pytest.mark.parametrize("kind", sorted(WORKLOADS))
def test_every_depends_on_names_a_block_that_is_really_in_the_file(kind):
    """The content check. A reserved-but-unemitted policy leaves a `depends_on`
    pointing at nothing, and that one dangling reference fails the plan for
    every resource on the canvas, not just this one."""
    main_tf = _main_tf(kind)
    referenced = set(re.findall(r"depends_on = \[([^\]]*)\]", main_tf))
    addresses = {a.strip() for group in referenced for a in group.split(",") if a.strip()}
    assert addresses, f"{kind}: nothing depends on anything — the grant ordering is missing"

    declared = {f"{t}.{n}" for t, n in re.findall(r'resource "(\w+)" "(\w+)"', main_tf)}
    dangling = addresses - declared
    assert dangling == set(), (
        f"{kind}: these depends_on addresses are not declared anywhere in the file: "
        f"{sorted(dangling)} — `tofu plan` fails for the WHOLE project on an "
        f"unresolvable reference, so this stops every other resource applying too"
    )
    assert any(a.startswith("aws_iam_role_policy.") for a in addresses), (
        f"{kind}: the workload does not depend on its grants policy, so tofu may "
        "start it first and it would be denied a permission that WAS applied"
    )


# A lambda's HCL hashes its zip with `filebase64sha256("<fn>.zip")`, and the
# archive is materialized by the apply path, not by `validate_refinement` — so
# validating one here fails on the missing FILE rather than on anything about
# the IAM. Lambda's grant is proven end-to-end instead, and harder: the
# re-entrancy test in tests/simulate/test_lambda_tf_e2e.py really applies a
# granted lambda with tofu and has its handler call S3/DynamoDB back through
# the gateway with its own credentials. Mutation-tested — emptying
# `compile_policies_from_iam` fails that test.
@pytest.mark.parametrize("kind", sorted(set(WORKLOADS) - {"lambda"}))
@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_the_generated_file_passes_the_real_tofu_validate(kind):
    """The real checker, over the real file. This is the one that reads the
    provider schema, so it also catches an argument that does not exist on
    `aws_iam_role_policy` / `aws_iam_instance_profile` — neither pure-Python
    check would."""
    files = {"main.tf": _main_tf(kind)}
    reason, formatted = await validate_refinement(files, files)

    assert reason is None, f"{kind}: generated Terraform does not validate — {reason}"
    assert formatted is not None


# v0.8.18: the VOLUME ATTACHMENT companion, validated here for the same reason
# the grants are. `aws_volume_attachment` is a resource type nothing else in this
# suite hands to tofu, and a wrong argument NAME on it (`device` for
# `device_name`, `instance` for `instance_id`) is invisible to every pure-Python
# assertion in tests/agent — only the provider schema knows. Two volumes on one
# instance, so the device-name assignment is exercised too, not just the refs.
VOLUMES_CANVAS = {
    "nodes": [
        *NETWORK,
        {"id": "i", "type": "ec2", "position": {"x": 40, "y": 40},
         "data": {"label": "box", "instanceType": "t3.micro", "subnet": "net-a"}},
        {"id": "d1", "type": "ebs", "position": {"x": 0, "y": 0},
         "data": {"label": "alpha", "az": "us-east-1a", "size": "40"}},
        {"id": "d2", "type": "ebs", "position": {"x": 0, "y": 0},
         "data": {"label": "beta", "az": "us-east-1a", "size": "20"}},
    ],
    "edges": [
        {"id": "e1", "source": "d1", "target": "i", "data": {"edgeType": "volume"}},
        # Drawn the other way round, and carrying the pre-registry type name that
        # every canvas saved before v0.8.15 actually has.
        {"id": "e2", "source": "i", "target": "d2", "data": {"edgeType": "network"}},
    ],
}


@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_the_generated_volume_attachments_pass_the_real_tofu_validate():
    project = generate_tf(canvas_to_stack(VOLUMES_CANVAS))
    assert project.unsupported == [], project.unsupported
    main_tf = project.files["main.tf"]
    # The content check first: `tofu validate` also passes a file with no
    # attachment in it at all, so this is what keeps the assertion below honest.
    assert main_tf.count('resource "aws_volume_attachment"') == 2
    assert main_tf.count('resource "aws_ebs_volume"') == 2

    files = {"main.tf": main_tf}
    reason, formatted = await validate_refinement(files, files)
    assert reason is None, f"generated Terraform does not validate — {reason}"
    assert formatted is not None


# v0.8.19: the EFS MOUNTS, here for the same reason as everything above it and
# with one addition of its own -- an efs mount is emitted as a NESTED BLOCK on
# the consumer (`file_system_config` on the function, `volume` +
# `efs_volume_configuration` on the task definition), and the provider schema is
# the only thing in this repo that knows those block names and their arguments.
# A pure-Python assertion cannot tell `local_mount_path` from `local_path`, or
# `volume` from `vol`; `tofu validate` rejects all four (measured, each one
# exit 1 with "Missing required argument"/"Unsupported block type").
#
# The LAMBDA is included, unlike the grants cases above, and it costs one extra
# thing to get right: a lambda's HCL calls `filebase64sha256("worker.zip")`,
# which tofu evaluates at validate time, so the zip `generate_tf` produced has to
# reach the scratch dir or validation fails on a missing FILE and proves nothing
# about the mount. `validate_refinement` takes `binary_files` for exactly that,
# so the caveat at the top of this file ("a lambda canvas cannot go through
# validate_refinement") is out of date rather than a limit to work around --
# MEASURED here, this case validates green with the real zip on disk, and the
# alternative would have left `file_system_config` proven by nothing but string
# assertions on the one resource type whose argument names only tofu knows.
MOUNTS_CANVAS = {
    "nodes": [
        {"id": "fs", "type": "efs", "position": {"x": 0, "y": 0},
         "data": {"label": "shared-data", "path": "/mnt/efs"}},
        {"id": "svc", "type": "ecs", "position": {"x": 20, "y": 0},
         "data": {"label": "api", "image": "nginx:alpine", "port": "8080"}},
        {"id": "fn", "type": "lambda", "position": {"x": 40, "y": 0},
         "data": {"label": "worker", "runtime": "python3.12",
                  "code": "def lambda_handler(event, context):\n    return event\n"}},
    ],
    "edges": [
        {"id": "e1", "source": "fs", "target": "svc", "data": {"edgeType": "mount"}},
        # Drawn the other way round AND carrying the pre-registry catch-all type,
        # which is not hypothetical for efs: `ac796d6` (2026-06-20) shipped the
        # tile draggable with NO `(placeholder)` marker, `1b158fe` (2026-07-26)
        # added the marker and `41d214b` (2026-07-27) hid placeholders from the
        # palette only -- so for five weeks it looked like an ordinary Storage
        # tile, and a canvas saved then can hold an efs node whose edges are typed
        # `network`. Gate the mount pass on `edge.kind` and this file loses the
        # lambda's `file_system_config` -- while still validating perfectly.
        {"id": "e2", "source": "fn", "target": "fs", "data": {"edgeType": "network"}},
    ],
}


@pytest.mark.skipif(_NO_TOFU, reason="tofu not on PATH")
async def test_the_generated_efs_mounts_pass_the_real_tofu_validate():
    project = generate_tf(canvas_to_stack(MOUNTS_CANVAS))
    assert project.unsupported == [], project.unsupported
    main_tf = project.files["main.tf"]
    # Content first, because `tofu validate` is just as green over a file with no
    # mount in it at all -- which is precisely the failure being guarded against.
    assert main_tf.count('resource "aws_efs_file_system"') == 1
    assert main_tf.count('resource "aws_efs_access_point"') == 1
    assert main_tf.count("efs_volume_configuration {") == 1
    assert main_tf.count("file_system_config {") == 1
    assert '"mountPoints\\": [{\\"sourceVolume\\": \\"shared-data\\"' in main_tf

    files = {"main.tf": main_tf}
    reason, formatted = await validate_refinement(files, files, project.binary_files)
    assert reason is None, f"generated Terraform does not validate — {reason}"
    assert formatted is not None
