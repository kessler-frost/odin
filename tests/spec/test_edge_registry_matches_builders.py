"""The canvas's edge registry and the Terraform builders must agree on which
pairs odin actually builds.

They live on opposite sides of a TypeScript/Python boundary where nothing
type-checks either side, and they are edited by different people at different
times. A disagreement is silent in BOTH directions and both are the same bug --
odin showing the user one thing and doing another:

  * UI wider than the builder: the canvas labels the line "LB Target" and the
    generator reports it in `unsupported`. The user is promised something that
    never gets built.
  * Builder wider than the UI: the canvas labels the line "Not modelled" while
    `tofu` registers a live target. Worse, because nothing draws attention to it
    at all -- the user has infrastructure they were told odin ignores.

This is not hypothetical. It is exactly how the `iam_role -> workload` edge sat
inert for months: the registry had no entry, nothing read the edge, and no test
compared the two halves. It is also live right now as a MERGE HAZARD -- the ALB
work extending `_ALB_TARGET_KINDS` to ec2 and the one-word `albTargetTypes` edit
that must accompany it are being made by two different agents in two different
worktrees, and whichever lands alone produces one of the two failures above.

So this reads the REAL value out of each file rather than restating either.
Regex over source, deliberately: importing the TS is not available here, and a
hand-copied expectation would be a third place to drift.
"""
from __future__ import annotations

import re
from pathlib import Path

from odin.agent.hcl import (
    _ALB_TARGET_KINDS,
    _DNS_TARGET_KINDS,
    _EFS_MOUNT_KINDS,
    _VOLUME_HOST_KINDS,
)

REPO = Path(__file__).resolve().parents[2]
IAM_TS = (REPO / "ui" / "src" / "lib" / "iam.ts").read_text()


def _ts_set(name: str) -> set[str]:
    """The members of an exported `new Set([...])` in iam.ts."""
    match = re.search(rf"export const {name} = new Set\(\[([^\]]*)\]\)", IAM_TS)
    assert match is not None, f"could not find `export const {name} = new Set([...])` in iam.ts"
    return set(re.findall(r"'([a-z0-9_]+)'", match.group(1)))


def test_the_extraction_finds_something():
    """Guards the guard: a regex that silently matched nothing would make the
    comparison below pass over two empty sets and prove nothing at all."""
    assert _ts_set("computeTypes") == {"ec2", "lambda", "ecs"}
    assert _ALB_TARGET_KINDS, "hcl.py::_ALB_TARGET_KINDS is empty -- the import is wrong"
    assert _VOLUME_HOST_KINDS, "hcl.py::_VOLUME_HOST_KINDS is empty -- the import is wrong"
    assert _DNS_TARGET_KINDS, "hcl.py::_DNS_TARGET_KINDS is empty -- the import is wrong"
    assert _EFS_MOUNT_KINDS, "hcl.py::_EFS_MOUNT_KINDS is empty -- the import is wrong"


def test_alb_target_kinds_agree_across_the_language_boundary():
    ui, builder = _ts_set("albTargetTypes"), set(_ALB_TARGET_KINDS)
    assert ui == builder, (
        f"`ui/src/lib/iam.ts::albTargetTypes` is {sorted(ui)} but "
        f"`agent/hcl.py::_ALB_TARGET_KINDS` is {sorted(builder)}.\n"
        f"  only in the UI  (promised, never built): {sorted(ui - builder)}\n"
        f"  only in the builder (built, labelled 'Not modelled'): {sorted(builder - ui)}\n"
        "Fix by editing the ONE line that is behind -- `albTargetTypes` in iam.ts, or "
        "`_ALB_TARGET_KINDS` in hcl.py. They must land in the same merge."
    )


def test_every_alb_target_kind_is_a_real_canvas_kind():
    """A kind in either list that the canvas cannot draw would make the pair
    unreachable, so the agreement above would be vacuously true for it."""
    from odin.spec.translate import MODELLED_NODE_TYPES
    assert set(_ALB_TARGET_KINDS) <= MODELLED_NODE_TYPES


def test_sg_member_kinds_agree_across_the_language_boundary():
    """The same comparison for the other pair-limited edge type. `_SG_MEMBERS` is
    private to translate.py and `sgMemberTypes` mirrors it by hand, which is the
    identical drift risk one file over."""
    from odin.spec.translate import _SG_MEMBERS
    ui = _ts_set("sgMemberTypes")
    assert ui == set(_SG_MEMBERS), (
        f"`iam.ts::sgMemberTypes` is {sorted(ui)} but `translate.py::_SG_MEMBERS` is "
        f"{sorted(_SG_MEMBERS)} -- an sg edge the canvas offers but the merge ignores, "
        "or one it merges without ever offering."
    )


def test_encryption_target_kinds_agree_across_the_language_boundary():
    """W2.9's edge, and the one where drift is not merely cosmetic.

    UI wider than the merge: the canvas draws a teal "Encrypted With" line to a
    kind whose builder reads no key field, so the value stays sealed under the
    env's DEFAULT key while the screen names a different one -- odin showing one
    key and using another.

    Merge wider than the UI: `_merge_encryption_edges` writes `kmsKeyId` on a
    kind the picker never offers `encryption` for, so a line the canvas labels
    "Not modelled" silently changes which key a secret is sealed under. Deleting
    that key then destroys the value (`ScheduleKeyDeletion` is immediate in
    odin), which is the most expensive of the four drift outcomes on this page.
    """
    from odin.spec.translate import _ENCRYPTION_TARGETS
    ui = _ts_set("encryptionTargetTypes")
    assert ui == set(_ENCRYPTION_TARGETS), (
        f"`iam.ts::encryptionTargetTypes` is {sorted(ui)} but "
        f"`translate.py::_ENCRYPTION_TARGETS` is {sorted(_ENCRYPTION_TARGETS)} -- an encryption "
        "edge the canvas offers and nothing folds into a key field, or one folded without ever "
        "being offered."
    )


def test_every_encryption_target_kind_has_a_key_field_a_builder_reads():
    """The half the set-comparison above cannot see. Both sides could agree on a
    kind whose HCL reads no key field at all, and the edge would still do
    nothing -- the drawn-line-that-does-nothing bug wearing an agreement. So the
    field each kind maps to is checked against the builder that consumes it."""
    from odin.agent.hcl import _BUILDERS
    from odin.spec.translate import _ENCRYPTION_FIELDS
    hcl_source = (REPO / "src" / "odin" / "agent" / "hcl.py").read_text()
    for kind, field in _ENCRYPTION_FIELDS.items():
        assert kind in _BUILDERS, f"{kind} has no Terraform builder, so its key field reaches nothing"
        assert f'"{field}"' in hcl_source, (
            f"`translate.py` folds an encryption edge into {field!r} on a {kind} node, but "
            f"`agent/hcl.py` never reads that field name -- the edge would author a field "
            f"nothing consumes"
        )
def test_volume_host_kinds_agree_across_the_language_boundary():
    """v0.8.18's pair, and the one with the worst failure mode of the four.

    UI wider than the builder is the usual "promised, never built". Builder wider
    than the UI is the usual "built, labelled Not modelled". But an ebs edge
    LOSING its meaning also means the generated file stops carrying the
    attachment, and `tofu apply` then DETACHES a disk that has data on it -- so
    this pair is worth pinning even though the pass keys on node kinds and not on
    the edge type name."""
    ui, builder = _ts_set("volumeHostTypes"), set(_VOLUME_HOST_KINDS)
    assert ui == builder, (
        f"`ui/src/lib/iam.ts::volumeHostTypes` is {sorted(ui)} but "
        f"`agent/hcl.py::_VOLUME_HOST_KINDS` is {sorted(builder)}.\n"
        f"  only in the UI  (promised, never built): {sorted(ui - builder)}\n"
        f"  only in the builder (built, labelled 'Not modelled'): {sorted(builder - ui)}\n"
        "They must land in the same merge."
    )


def test_every_volume_host_kind_is_a_real_canvas_kind():
    """The companion check: a kind the canvas cannot draw makes the agreement
    above vacuously true for it."""
    from odin.spec.translate import MODELLED_NODE_TYPES
    assert set(_VOLUME_HOST_KINDS) <= MODELLED_NODE_TYPES


def test_dns_target_kinds_agree_across_the_language_boundary():
    """v0.8.19's pair, and the one where the UI-wider direction is the sharper
    lie of the two.

    UI wider than the builder: the canvas draws an indigo "DNS Record" line to a
    kind whose address is not something a hosts entry can express, so the user is
    promised a NAME and gets nothing -- `hcl.py` declines the target by name and
    emits no record. Worse than the usual "promised, never built", because the
    thing promised is reachability: an app configured against that name fails to
    resolve at runtime, far from the canvas that promised it.

    Builder wider than the UI: `hcl.py` writes a real `--add-host` for a pair the
    canvas labels "Not modelled", so a name is resolving inside every container
    in the env and nothing on screen says where it came from.
    """
    ui, builder = _ts_set("dnsTargetTypes"), set(_DNS_TARGET_KINDS)
    assert ui == builder, (
        f"`ui/src/lib/iam.ts::dnsTargetTypes` is {sorted(ui)} but "
        f"`agent/hcl.py::_DNS_TARGET_KINDS` is {sorted(builder)}.\n"
        f"  only in the UI  (promised, never built): {sorted(ui - builder)}\n"
        f"  only in the builder (built, labelled 'Not modelled'): {sorted(builder - ui)}\n"
        "They must land in the same merge."
    )


def test_efs_mount_kinds_agree_across_the_language_boundary():
    """v0.8.19's pair. Both drift directions are the usual ones, but the second is
    worse than usual here because a mount is INVISIBLE when it is missing: a
    service whose task definition carries no `volume` starts perfectly, serves
    traffic, and writes to its own container filesystem instead of the shared one
    -- so the data is silently not shared and nothing fails until someone looks
    for a file that is not there."""
    ui, builder = _ts_set("efsMountTypes"), set(_EFS_MOUNT_KINDS)
    assert ui == builder, (
        f"`ui/src/lib/iam.ts::efsMountTypes` is {sorted(ui)} but "
        f"`agent/hcl.py::_EFS_MOUNT_KINDS` is {sorted(builder)}.\n"
        f"  only in the UI  (promised, never built): {sorted(ui - builder)}\n"
        f"  only in the builder (built, labelled 'Not modelled'): {sorted(builder - ui)}\n"
        "They must land in the same merge."
    )

def test_every_dns_target_kind_is_a_real_canvas_kind():
    """The companion check: a kind the canvas cannot draw makes the agreement
    above vacuously true for it."""
    from odin.spec.translate import MODELLED_NODE_TYPES
    assert set(_DNS_TARGET_KINDS) <= MODELLED_NODE_TYPES

def test_every_efs_mount_kind_is_a_real_canvas_kind():
    """The companion check: a kind the canvas cannot draw makes the agreement
    above vacuously true for it."""
    from odin.spec.translate import MODELLED_NODE_TYPES
    assert set(_EFS_MOUNT_KINDS) <= MODELLED_NODE_TYPES


def test_role_holder_kinds_agree_across_the_language_boundary():
    """And for the role edge added in the same change as this file -- the one
    whose absence from the registry was the original inert-edge bug."""
    from odin.spec.translate import _ROLE_HOLDERS
    ui = _ts_set("roleHolderTypes")
    assert ui == set(_ROLE_HOLDERS), (
        f"`iam.ts::roleHolderTypes` is {sorted(ui)} but `translate.py::_ROLE_HOLDERS` is "
        f"{sorted(_ROLE_HOLDERS)} -- a role edge the canvas offers and nothing folds into "
        "the `role` field, or one folded without ever being offered."
    )
