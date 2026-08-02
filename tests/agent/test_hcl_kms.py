"""The `aws_kms_key` builder, and the one canvas label that used to create a key
nothing could ever address.

W2.9 made `kms` a real kind. Its whole identity rests on a single unusual fact:
real `CreateKey` takes NO name argument, so `gateway/models/kmsctl.py` keys a key
by the `odin:node` tag `agent/hcl.py::_tags_block` stamps, and the canvas label
IS the key id. Everything downstream -- a secret's `kms_key_id`, an IAM grant's
Resource ARN, the World projection -- resolves through that one string.

THE DEFECT THIS FILE PINS was found by driving the real handlers rather than by
reading them. `_create_key` keys its record by the RAW tag value, while every
other op keys by `bare_key_id(KeyId)`; the two agree only while `bare_key_id` is
the identity. Measured, with a canvas label of `alias/prod-key`:

    CreateKey   -> 200, KeyId 'alias/prod-key'
    DescribeKey -> 400 NotFoundException "Key 'prod-key' does not exist"
    Encrypt     -> 400 NotFoundException "Key 'prod-key' does not exist"

A green create for a key that is dead on arrival, and then an `EncryptionFailure`
on the secret quoting a key id the user never typed. `alias/` is not an exotic
thing to type -- AWS users think in aliases.
"""
from __future__ import annotations

import pytest

from odin.agent.hcl import generate_tf
from odin.gateway.models.kmsctl import bare_key_id
from odin.spec.translate import canvas_to_stack


def _canvas(label: str, **data) -> dict:
    return {
        "nodes": [{"id": "k", "type": "kms", "position": {"x": 0, "y": 0},
                   "data": {"label": label, **data}}],
        "edges": [],
    }


def _key_block(main_tf: str) -> str:
    return main_tf.split('resource "aws_kms_key"')[1].split("\nresource ")[0]


def test_a_key_emits_no_name_because_the_aws_resource_has_none():
    """`aws_kms_key` has no `name` argument at all, which is exactly why the
    label has to ride on the tag."""
    block = _key_block(generate_tf(canvas_to_stack(_canvas("app-key"))).files["main.tf"])
    assert '"odin:node" = "app-key"' in block
    assert "name" not in block


def test_the_deletion_window_is_portability_only_and_says_so():
    """odin's ScheduleKeyDeletion is IMMEDIATE, so 7 is not a promise odin
    keeps. 0 would be the honest number and AWS's provider rejects it
    client-side, which is why the comment beside it has to carry the truth."""
    block = _key_block(generate_tf(canvas_to_stack(_canvas("app-key"))).files["main.tf"])
    assert "deletion_window_in_days = 7" in block


def test_rotation_is_emitted_only_when_the_canvas_asks():
    """No field = no argument, the `_logs` retention rule: emitting a made-up
    default would claim a setting the user never chose."""
    off = _key_block(generate_tf(canvas_to_stack(_canvas("app-key"))).files["main.tf"])
    assert "enable_key_rotation" not in off
    on = _key_block(generate_tf(canvas_to_stack(_canvas("app-key", rotate="true"))).files["main.tf"])
    assert "enable_key_rotation     = true" in on


def test_a_bad_rotate_value_is_declined_rather_than_dropped():
    project = generate_tf(canvas_to_stack(_canvas("app-key", rotate="yes")))
    (declined,) = project.unsupported
    assert declined.startswith("app-key (kms):") and "rotate" in declined
    assert "aws_kms_key" not in project.files["main.tf"]


# --- the label that creates an unaddressable key ------------------------------


@pytest.mark.parametrize("label", ["alias/prod-key", "arn:aws:kms:us-east-1:0:key/other"])
def test_a_label_the_gateway_would_rewrite_is_DECLINED(label: str):
    """Not merely renamed behind the user's back: renaming would break the
    `${{...}}` ref path and the IAM grant's ARN at the same time, which is the
    reasoning `_rds` already spells out for its own identifier."""
    project = generate_tf(canvas_to_stack(_canvas(label)))
    (declined,) = project.unsupported
    assert declined.startswith(f"{label} (kms):")
    assert "data.label" in declined
    assert "aws_kms_key" not in project.files["main.tf"]


@pytest.mark.parametrize("label", ["app-key", "my-alias/key", "keys/app", "a:b:c"])
def test_a_label_the_gateway_leaves_ALONE_still_builds(label: str):
    """The other half, and the reason the check mirrors `bare_key_id` exactly
    instead of substring-testing for `alias/`. `my-alias/key` CONTAINS `alias/`
    and `bare_key_id` does not touch it -- a looser guard would decline a name
    that works, which is its own wrong answer."""
    project = generate_tf(canvas_to_stack(_canvas(label)))
    assert project.unsupported == []
    assert f'"odin:node" = "{label}"' in _key_block(project.files["main.tf"])


def test_the_guard_is_in_LOCK_STEP_with_the_gateways_own_reducer():
    """The guard duplicates `bare_key_id` because hcl.py stays independent of
    the gateway (the `_SSM_TYPES` precedent). Prose lock-step is what goes
    stale, so this compares the two functions over both families of input --
    the ones the real reducer rewrites and the ones it leaves alone.

    Mutation target: relax the builder's check to a substring test and
    `my-alias/key` starts being declined here; drop it entirely and the two
    `alias/` rows above stop being declined.
    """
    from odin.agent.hcl import _bare_key_id
    for label in ("app-key", "my-alias/key", "keys/app", "a:b:c", "alias/prod-key",
                  "arn:aws:kms:us-east-1:0:key/other", "alias/", "x:key/y"):
        assert _bare_key_id(label) == bare_key_id(label), label
        declined = generate_tf(canvas_to_stack(_canvas(label))).unsupported
        assert bool(declined) is (bare_key_id(label) != label), label
