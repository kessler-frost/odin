"""`Phase` and `StatusBadge.tsx` are two halves of ONE wire contract.

The reconciler broadcasts a phase; the canvas styles it. Nothing type-checks
across that boundary, so a disagreement is silent in both directions and both
are odin showing the user one thing while doing another:

  * a phase odin EMITS that the UI cannot style renders through
    `statusStyles.draft`, so a crashed node can read as an untouched one;
  * a phase the UI styles that `Phase` does not contain is a `ValidationError`
    waiting for the first caller who builds the delta properly.

The second one was live. `reconcile/reconciler.py::_prune` broadcast
`{"phase": "draft", ...}` on every prune, `StatusBadge.tsx` had styled `draft`
all along, and `Phase` did not contain it -- `WorldDelta(phase="draft")` would
have raised. It stayed invisible for exactly one reason: the prune path
hand-built a dict instead of a `WorldDelta`, so pydantic never saw the value.
Both halves are fixed (v0.8.18); this is what stops the next one.

Reads the REAL value from each side rather than restating either: `get_args`
off the actual Literal, and a regex over the actual `statusStyles` object,
because importing TS is not available here and a hand-copied list would just be
a third place to drift.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from odin.spec.models import Phase

BADGE = Path(__file__).resolve().parents[2] / "ui" / "src" / "components" / "nodes" / "StatusBadge.tsx"


def _styled_phases() -> set[str]:
    """The keys of `statusStyles` -- every phase the canvas can render."""
    body = BADGE.read_text().partition("const statusStyles")[2].partition("};")[0]
    assert body, f"could not find `statusStyles` in {BADGE}"
    return set(re.findall(r"^\s{2}([a-z]+):", body, re.MULTILINE))


def test_every_phase_odin_can_emit_has_a_style():
    unstyled = set(get_args(Phase)) - _styled_phases()
    assert unstyled == set(), (
        "these phases are in `Phase` but `StatusBadge.tsx` cannot style them, so "
        f"the canvas would render them as `draft`: {sorted(unstyled)}"
    )


def test_every_phase_the_canvas_styles_is_a_real_phase():
    unknown = _styled_phases() - set(get_args(Phase))
    assert unknown == set(), (
        "`StatusBadge.tsx` styles these but `Phase` does not contain them, so "
        "`WorldDelta(phase=...)` would raise for any of them -- which is exactly "
        f"how `draft` hid behind a hand-built delta dict: {sorted(unknown)}"
    )
