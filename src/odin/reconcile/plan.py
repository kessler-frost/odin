"""The pure control-loop core: plan(Stack, World) -> [Action].

TOTAL + IDEMPOTENT: re-running on unchanged (desired, observed) yields only
NoOps, which makes the loop crash-safe (restart re-derives from the Spec Store)
and fixture-testable. Scope: rds (a direct Postgres container) + the
AWS-shaped PROVISIONED kinds (s3/sqs/sns/dynamodb) in shared backings, single
host.
"""
from __future__ import annotations

from odin.aws.backings import PROVISIONED
from odin.reconcile.actions import Action, NoOp, ProvisionResource, StopContainer
from odin.spec.models import ResourceObserved, Stack, World

MAX_RESTARTS = 5  # consecutive crashes before plan gives up (don't respawn a bad image forever)


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
                actions.append(ProvisionResource(id=res.id, service="rds"))
            else:
                actions.append(NoOp(id=res.id))
        elif res.kind in PROVISIONED:
            # AWS-shaped resource in a shared backing: create once, then it just exists.
            if phase in ("pending", "crashed"):
                actions.append(ProvisionResource(id=res.id, service=res.kind))
            else:
                actions.append(NoOp(id=res.id))
        else:
            actions.append(NoOp(id=res.id))

    return actions
