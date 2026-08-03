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
from odin.reconcile.actions import Action, NoOp, PruneResource, ProvisionResource
from odin.spec.models import Stack, World


def plan(stack: Stack, world: World) -> list[Action]:
    actions: list[Action] = []
    desired_ids = {r.id for r in stack.resources}

    # Prune: anything observed but no longer desired. `kind` decides what that
    # MEANS -- for a PROVISIONED kind the executor deletes the real bucket /
    # queue / table / topic, so this is a data-destroying action, not a stop.
    for observed in world.resources:
        if observed.id not in desired_ids:
            actions.append(
                PruneResource(id=observed.id, name=observed.id, kind=observed.kind)
            )

    for res in stack.resources:
        observed = world.get(res.id)
        phase = observed.phase if observed else "pending"

        if phase == "healthy":
            actions.append(NoOp(id=res.id))
            continue

        if res.kind in PROVISIONED:
            # An AWS-shaped resource inside a shared backing (bucket / queue /
            # topic / table). NOT "create once and then it just exists", which
            # is what this used to say and what the very next line contradicts:
            # `crashed` is in the tuple deliberately, so anything that removes
            # the real resource while the canvas still asks for it is
            # RE-CREATED, not merely reported.
            #
            # That is the loop's contract and also its sharpest edge, because a
            # FAILED destroy leaves the desired state committed on purpose (it
            # is what makes a retry possible), so a resource that destroy did
            # delete comes straight back. Measured on a real server: a bucket
            # deleted directly out of the running RustFS backing -- probing the
            # backing itself, never odin -- was back in 0.73s, one tick at the
            # production 1s poll. `server.py::_RECREATED_BY_THE_LOOP` is the
            # sentence that tells the user so, and `odin stop` is what actually
            # stops it. Nothing here can.
            if phase in ("pending", "crashed"):
                actions.append(ProvisionResource(id=res.id, service=res.kind))
            else:
                actions.append(NoOp(id=res.id))
        else:
            actions.append(NoOp(id=res.id))

    return actions
