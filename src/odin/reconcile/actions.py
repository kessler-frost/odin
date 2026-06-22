"""The typed Action union the Reconciler's `plan()` emits.

Actions are INTENT keyed by resource id; the executor (reconciler.py) turns each
into concrete runtime / MiniStack calls, building specs from the Stack + Fabric.
Skeleton scope: create an AWS resource, run an app container, stop a pruned one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class CreateMiniStackResource:
    id: str
    service: str  # "rds" for the skeleton


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


Action = Union[CreateMiniStackResource, RunContainer, StopContainer, NoOp]
