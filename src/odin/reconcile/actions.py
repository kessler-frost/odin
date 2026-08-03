"""The typed Action union the Reconciler's `plan()` emits.

Actions are INTENT keyed by resource id; the executor (reconciler.py) turns each
into concrete runtime / backing calls, building specs from the Stack + Fabric.

Scope today: provision an AWS-shaped resource, prune one that is no longer
desired, or do nothing. THREE members, and `_execute` handles all three -- the
docstring used to advertise a fourth, "run an app container", for a
`RunContainer` action that was deleted at tag `app-layer-parked` in everything
but name: `plan()` never emitted one and `_execute` never handled one, so an
emitted `RunContainer` would have been dropped in silence. That is the unmapped
outcome honesty rule 2 forbids, which is why `_execute` now ends in an `else`
that raises rather than falling off the end.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class ProvisionResource:
    id: str
    service: str  # "rds" or a PROVISIONED kind (s3/sqs/sns/dynamodb)


@dataclass(frozen=True)
class PruneResource:
    """"Observed but no longer desired" -- the ONE prune action `plan()` emits,
    for every kind.

    Named `StopContainer` until v0.8.18, which was true for exactly one kind:
    `service`, removed at tag `app-layer-parked`. What it really does depends
    on the kind, and only the last of the three stops a container
    (`reconciler.py::_execute`):

      * a PROVISIONED kind -> `BackingAws.deprovision` -> a real
        `delete_bucket` / `delete_queue` / `delete_table` / `delete_topic`.
        **This DELETES the user's data**, which is precisely what the old name
        told a reader could not be happening.
      * a TF_OWNED kind    -> nothing at all, prune included: tofu owns
        create/destroy and `_project_tf_owned` is the sole authority on the
        label leaving World.
      * anything else      -> `RuntimeDriver.stop(name)`.

    `kind` carries no default on purpose: `plan()` reads it off the observed
    resource every time, and a default is how the wrong branch gets taken in
    silence.
    """

    id: str
    name: str
    kind: str


@dataclass(frozen=True)
class NoOp:
    id: str = ""


Action = Union[ProvisionResource, PruneResource, NoOp]
