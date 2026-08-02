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

from odin.agent.hcl import _ALB_TARGET_KINDS, _VOLUME_HOST_KINDS

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
