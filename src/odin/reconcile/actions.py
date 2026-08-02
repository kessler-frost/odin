"""The typed Action union the Reconciler's `plan()` emits.

Actions are INTENT keyed by resource id; the executor (reconciler.py) turns each
into concrete runtime / backing calls, building specs from the Stack + Fabric.
Skeleton scope: provision an AWS-shaped resource, run an app container, prune
one that is no longer desired.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class ProvisionResource:
    id: str
    service: str  # "rds" or a PROVISIONED kind (s3/sqs/sns/dynamodb)


@dataclass(frozen=True)
class RunContainer:
    id: str


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


Action = Union[ProvisionResource, RunContainer, PruneResource, NoOp]
