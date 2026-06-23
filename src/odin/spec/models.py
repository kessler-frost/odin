"""The Spec Store data model: Stack (desired) and World (observed).

The Stack is whole-canvas declarative desired state authored by the Canvas and
the Brain; the World is observed state authored only by drivers + the Assertion
Engine. They are kept as separate frozen documents per environment. A Stack
carries no `rev` field — the revision is the sha256 of its canonical JSON,
computed by the SpecStore (carrying it inside would be circular).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Provenance = Literal["user", "ai", "default"]

# A resource's observed lifecycle phase.
Phase = Literal[
    "pending",    # desired but nothing started
    "starting",   # container launched, not yet healthy
    "healthy",    # assertion passed
    "crashed",    # was healthy/started, now down unexpectedly
    "blocked",    # waiting on an unresolved reference / dependency
    "queued",     # batch job waiting for capacity
    "running",    # batch job executing
    "done",       # batch job finished
    "evicted",    # llm intentionally unloaded under memory pressure
    "error",      # terminal failure (e.g. ref never resolved within timeout)
]


class FieldValue(BaseModel):
    """A single resource field plus where its value came from."""

    model_config = {"frozen": True}
    value: Any
    provenance: Provenance = "user"


class Ref(BaseModel):
    """A `${{target_id.target_attr}}` reference carried by a node's field."""

    model_config = {"frozen": True}
    var: str          # the env var / field on the consumer, e.g. "DATABASE_URL"
    target_id: str    # the producer node id, e.g. "db"
    target_attr: str  # the attribute to read, e.g. "DATABASE_URL"


class Edge(BaseModel):
    model_config = {"frozen": True}
    src: str
    dst: str
    kind: str = "ref"            # "ref" | "iam" | "network"
    perms: tuple[str, ...] = ()


class ResourceDesired(BaseModel):
    model_config = {"frozen": True}
    id: str
    kind: str                              # "service" | "rds" | "batch" | "llm" | …
    fields: dict[str, FieldValue] = {}
    refs: tuple[Ref, ...] = ()


class Stack(BaseModel):
    model_config = {"frozen": True}
    env: str = "default"
    resources: tuple[ResourceDesired, ...] = ()
    edges: tuple[Edge, ...] = ()


class ResourceObserved(BaseModel):
    model_config = {"frozen": True}
    id: str
    kind: str
    phase: Phase = "pending"
    facts: dict[str, Any] = {}             # endpoint, host_port, cpu, ram, logtail…
    verdict: str | None = None
    restarts: int = 0


class World(BaseModel):
    model_config = {"frozen": True}
    env: str = "default"
    resources: tuple[ResourceObserved, ...] = ()

    def get(self, resource_id: str) -> ResourceObserved | None:
        return next((r for r in self.resources if r.id == resource_id), None)


class WorldDelta(BaseModel):
    """A single observed-state change broadcast to the canvas."""

    model_config = {"frozen": True}
    type: str = "world_delta"
    env: str
    resource_id: str
    kind: str
    phase: Phase
    facts: dict[str, Any] = {}
    verdict: str | None = None
