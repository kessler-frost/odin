"""Which `ecr:*` permissions odin can actually ANSWER, pinned rather than claimed.

`test_iam_vocabulary_is_enforceable.py` is the sibling of this file and stops one
rung short of it, deliberately and in writing: for TARGET-derived services it
says "ANY op a real SDK sends is emittable and only the SERVICE prefix can be
checked here". True for the classifier -- `_classify_ecr` builds `ecr:<op>`
straight from `x-amz-target`, so it will happily emit `ecr:BatchGetImage`.

But ECR is not really a free-op service. `gateway/models/ecr.py` owns the whole
control plane through a FIXED handler table, exactly the shape `_LAMBDA_ROUTES`
has and exactly the shape that file already checks for lambda. An op with no
entry gets `InvalidAction` 400, so a grant naming it can never bite.

`catalog.ts` claimed the opposite for a while -- that these were "the op names a
real SDK sends, which is what makes the grant bite rather than decorate" -- and
cited the sibling test as its evidence. The sibling test does not check it. A
claim that was reviewed, believed, and pinned by nothing is honesty rule 1, so
this file pins it, in both directions.

TWO directions matters. Asserting only "unanswerable ops are declared" would let
the declaration rot the moment someone implements a handler: the doc comment in
`catalog.ts` would go on calling a working permission decorative. So an op in
`PORTABLE_ONLY` that gains a handler fails this too, and the message says to
promote it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from odin.gateway.models.ecr import _HANDLERS

REPO = Path(__file__).resolve().parents[2]
CATALOG_TS = (REPO / "ui" / "src" / "lib" / "catalog.ts").read_text()
IAM_TS = (REPO / "ui" / "src" / "lib" / "iam.ts").read_text()

# Offered but NOT answerable, and deliberately kept: a drawn permission becomes a
# real `aws_iam_role_policy`, and the generated Terraform is meant to be portable
# -- taken to Amazon these are exactly the verbs an image pull needs. Locally
# they gate nothing, for two independent reasons (no handler, AND the image bytes
# never reach the gateway: a real `docker pull` dials the `registry:2`
# container's port directly, per ecr.py's own docstring).
PORTABLE_ONLY = {"BatchGetImage", "GetDownloadUrlForLayer", "BatchCheckLayerAvailability"}


def _offered_ecr_ops(text: str) -> set[str]:
    return {op for op in re.findall(r"'ecr:([A-Za-z*]+)'", text) if op != "*"}


def test_the_extraction_finds_something():
    """Guards the guard: a regex matching nothing would make every test pass."""
    offered = _offered_ecr_ops(CATALOG_TS)
    assert len(offered) >= 4, f"only found {sorted(offered)} -- the extraction is broken"
    assert "GetAuthorizationToken" in offered
    assert _HANDLERS, "ecr._HANDLERS is empty -- the import is wrong, not the vocabulary"


@pytest.mark.parametrize("op", sorted(_offered_ecr_ops(CATALOG_TS) | _offered_ecr_ops(IAM_TS)))
def test_every_offered_ecr_op_is_answerable_or_declared_portable_only(op: str):
    assert op in _HANDLERS or op in PORTABLE_ONLY, (
        f"the UI offers 'ecr:{op}', but `gateway/models/ecr.py::_HANDLERS` has no entry for it "
        f"(it answers InvalidAction 400) and it is not declared in PORTABLE_ONLY. "
        f"Either implement the handler, or add it to PORTABLE_ONLY and say in catalog.ts "
        f"that it is emitted for portability and gates nothing locally. "
        f"Answerable today: {sorted(_HANDLERS)}"
    )


@pytest.mark.parametrize("op", sorted(PORTABLE_ONLY))
def test_a_portable_only_op_that_gained_a_handler_is_promoted(op: str):
    """The other direction, so the declaration cannot rot into a lie.

    Without this, implementing `BatchGetImage` would leave `catalog.ts` calling a
    working permission decorative -- a caveat outliving its fix, which is the
    third honesty rule and has already happened twice in this repo.
    """
    assert op not in _HANDLERS, (
        f"'ecr:{op}' now HAS a handler, so it is no longer portable-only: remove it from "
        "PORTABLE_ONLY here and from the 'gates nothing locally' note in catalog.ts, and "
        "consider ticking it by default in iam.ts."
    )


def test_only_the_answerable_action_is_ticked_by_default():
    """A DEFAULT is what odin ticks FOR you, so it may not name something odin
    cannot enforce -- that is odin claiming a protection it has not got. The
    tick-LIST may be wider (portability); the defaults may not."""
    defaults = re.search(r"ecr: \[([^\]]*)\]", IAM_TS)
    assert defaults is not None, "could not find `defaultPermissions.ecr` in iam.ts"
    for op in re.findall(r"'ecr:([A-Za-z*]+)'", defaults.group(1)):
        assert op in _HANDLERS, (
            f"'ecr:{op}' is ticked by default but the gateway cannot answer it "
            f"(answerable: {sorted(_HANDLERS)})"
        )


def test_the_registry_data_plane_really_is_out_of_scope():
    """The second, independent reason those grants cannot bite, read from the
    source rather than restated: image bytes never reach the gateway at all, so
    no IAM decision is ever taken over them.

    Whitespace-collapsed because the sentence wraps mid-phrase in the real
    docstring, and a test that breaks on a reflow is a test people delete.
    """
    ecr_py = (REPO / "src" / "odin" / "gateway" / "models" / "ecr.py").read_text()
    assert "does NOT proxy the registry's v2 HTTP protocol" in " ".join(ecr_py.split()), (
        "ecr.py no longer says the registry's v2 protocol is unproxied. If the gateway now "
        "DOES sit in front of image pulls, the layer verbs in PORTABLE_ONLY may finally be "
        "enforceable -- revisit them and catalog.ts's note rather than leaving it claiming "
        "they gate nothing."
    )
