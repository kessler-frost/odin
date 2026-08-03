"""Localhost fabric: resolve `${{node.VAR}}` references from observed World facts.

**`resolve()` HAS NO PRODUCTION CALLER.** The Reconciler stores a `LocalhostFabric`
in `self._fabric` and never calls it; the live ref-resolution path is
`gateway/wiring.py::_resolve`, which raises `UnresolvedRef` before tofu runs
rather than at reconcile time. `spec/models.py`'s `REFERENCEABLE_KINDS` comment
already said so — the correction landed one file away from the file it is about,
which is how this docstring survived.

What it used to claim, and why the claim is worth naming rather than deleting:
that an `Unresolved` raised here "the Reconciler turns into a deterministic
`blocked` phase". Nothing does that. `blocked` is in the `Phase` literal and
reachable from nowhere in live code, so a reader chasing this sentence finds a
mechanism, a phase and a test file that all exist and never run together.

What is still true: a producer publishes its address as a World fact (e.g.
`facts["DATABASE_URL"]`) when it goes healthy, and `resolve()` reads it, gating
on health. `fabric/nebula.py`'s `NebulaFabric` is a drop-in for this same
interface for the multi-Mac (M7) path. Kept, with its tests, because the M7
work is live and this is the seam it plugs into — but it is a SEAM today, not a
path.
"""
from __future__ import annotations

from odin.spec.models import Ref, World


class Unresolved(Exception):
    pass


class LocalhostFabric:
    def resolve(self, ref: Ref, world: World) -> str:
        target = world.get(ref.target_id)
        if target is None or target.phase != "healthy":
            raise Unresolved(f"{ref.target_id} is not healthy")
        value = target.facts.get(ref.target_attr)
        if value is None:
            raise Unresolved(f"{ref.target_id} exposes no {ref.target_attr}")
        return str(value)
