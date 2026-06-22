"""S1.1 — Stack/World models round-trip and preserve field provenance."""
from __future__ import annotations

from odin.spec.models import (
    FieldValue,
    Ref,
    ResourceDesired,
    Stack,
    World,
    WorldDelta,
)


def _sample_stack() -> Stack:
    return Stack(
        env="default",
        resources=(
            ResourceDesired(
                id="db",
                kind="rds",
                fields={
                    "engine": FieldValue(value="postgres", provenance="user"),
                    "port": FieldValue(value=5432, provenance="ai"),
                },
            ),
            ResourceDesired(
                id="api",
                kind="service",
                fields={"image": FieldValue(value="myapp:latest")},
                refs=(Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL"),),
            ),
        ),
    )


def test_stack_round_trips_with_provenance():
    stack = _sample_stack()
    again = Stack.model_validate_json(stack.model_dump_json())
    assert again == stack
    db = next(r for r in again.resources if r.id == "db")
    assert db.fields["engine"].provenance == "user"
    assert db.fields["port"].provenance == "ai"


def test_ref_is_carried():
    stack = _sample_stack()
    api = next(r for r in stack.resources if r.id == "api")
    assert api.refs[0].target_id == "db"
    assert api.refs[0].var == "DATABASE_URL"


def test_world_get_and_delta_type():
    world = World(env="default")
    assert world.get("db") is None
    delta = WorldDelta(env="default", resource_id="db", kind="rds", phase="healthy")
    assert delta.type == "world_delta"
