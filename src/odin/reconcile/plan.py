"""The pure control-loop core: plan(Stack, World) -> [Action].

TOTAL + IDEMPOTENT: re-running on unchanged (desired, observed) yields only
NoOps, which makes the loop crash-safe (restart re-derives from the Spec Store)
and fixture-testable.

Scope (W2.7): the AWS-shaped PROVISIONED kinds (s3/sqs/sns/dynamodb) in shared
backings, single host -- and NOTHING else. `rds` used to live here too (a
direct Postgres container this plan created and re-created); it moved onto
Terraform, so it now falls through to the same NoOp every other TF_OWNED kind
gets: tofu is its sole creator/destroyer (`gateway/models/rdsctl.py`) and the
reconciler only OBSERVES it, via the World projection in tf_status.py. The
prune branch below still applies to it -- a removed canvas node's stale World
entry is cleared here, while the real resource is destroyed by tofu.
"""
from __future__ import annotations

from odin.aws.backings import PROVISIONED
from odin.reconcile.actions import Action, NoOp, ProvisionResource, StopContainer
from odin.spec.models import Stack, World


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

        if res.kind in PROVISIONED:
            # AWS-shaped resource in a shared backing: create once, then it just exists.
            if phase in ("pending", "crashed"):
                actions.append(ProvisionResource(id=res.id, service=res.kind))
            else:
                actions.append(NoOp(id=res.id))
        else:
            actions.append(NoOp(id=res.id))

    return actions
