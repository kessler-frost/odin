"""The typed Action union the Reconciler's `plan()` emits.

Actions are INTENT keyed by resource id; the executor (reconciler.py) turns each
into concrete runtime / backing calls, building specs from the Stack + Fabric.
Skeleton scope: provision an AWS-shaped resource, run an app container, stop a
pruned one.
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
class StopContainer:
    id: str
    name: str
    kind: str = "service"


@dataclass(frozen=True)
class NoOp:
    id: str = ""


Action = Union[ProvisionResource, RunContainer, StopContainer, NoOp]
