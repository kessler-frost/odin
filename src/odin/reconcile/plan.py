"""The pure control-loop core: plan(Stack, World) -> [Action].

TOTAL + IDEMPOTENT: re-running on unchanged (desired, observed) yields only
NoOps, which makes the loop crash-safe (restart re-derives from the Spec Store)
and fixture-testable. Skeleton scope: service + rds kinds, single host, no
scheduler / batch / llm branches.
"""
from __future__ import annotations

from odin.aws.provision import PROVISIONED
from odin.reconcile.actions import (
    Action,
    CreateMiniStackResource,
    NoOp,
    RunContainer,
    StopContainer,
)
from odin.spec.models import ResourceDesired, ResourceObserved, Stack, World

MAX_RESTARTS = 5  # consecutive crashes before plan gives up (don't respawn a bad image forever)


def _refs_ready(res: ResourceDesired, world: World) -> bool:
    """All of a resource's reference targets are healthy in the World."""
    for ref in res.refs:
        target = world.get(ref.target_id)
        if target is None or target.phase != "healthy":
            return False
    return True


def _crash_looped(observed: ResourceObserved | None) -> bool:
    """Given up: crashed MAX_RESTARTS times in a row without going healthy."""
    return observed is not None and observed.restarts >= MAX_RESTARTS


def plan(stack: Stack, world: World) -> list[Action]:
    actions: list[Action] = []
    desired_ids = {r.id for r in stack.resources}

    # Prune: anything observed but no longer desired.
    for observed in world.resources:
        if observed.id not in desired_ids:
            actions.append(
                StopContainer(id=observed.id, name=observed.id, kind=observed.kind)
            )

    for res in stack.resources:
        observed = world.get(res.id)
        phase = observed.phase if observed else "pending"

        if phase == "healthy":
            actions.append(NoOp(id=res.id))
            continue

        if res.kind == "rds":
            # (re)create when nothing is up; otherwise wait for it to go healthy.
            if phase in ("pending", "crashed") and not _crash_looped(observed):
                actions.append(CreateMiniStackResource(id=res.id, service="rds"))
            else:
                actions.append(NoOp(id=res.id))
        elif res.kind in ("service", "dep", "llm"):
            if not _refs_ready(res, world) or _crash_looped(observed):
                actions.append(NoOp(id=res.id))  # blocked, or given up after crash-looping
            elif phase in ("pending", "crashed", "blocked", "error", "queued", "evicted"):
                # "error" recovers when a ref heals; "queued"/"evicted" retry when
                # memory frees (the scheduler re-queues if it's still tight).
                actions.append(RunContainer(id=res.id))
            else:
                actions.append(NoOp(id=res.id))
        elif res.kind == "batch":
            # run-to-completion: run once when ready; never restart on exit.
            if phase in ("pending", "blocked", "queued") and _refs_ready(res, world):
                actions.append(RunContainer(id=res.id))
            else:
                actions.append(NoOp(id=res.id))
        elif res.kind in PROVISIONED:
            # control-plane AWS resource: create once, then it just exists.
            if phase in ("pending", "crashed"):
                actions.append(CreateMiniStackResource(id=res.id, service=res.kind))
            else:
                actions.append(NoOp(id=res.id))
        else:
            actions.append(NoOp(id=res.id))

    return actions
