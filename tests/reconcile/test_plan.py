"""S2.1 — the pure plan(Stack, World) -> [Action] across lifecycle states."""
from __future__ import annotations

from odin.reconcile.actions import (
    CreateMiniStackResource,
    NoOp,
    RunContainer,
    StopContainer,
)
from odin.reconcile.plan import plan
from odin.spec.models import (
    FieldValue,
    Ref,
    ResourceDesired,
    ResourceObserved,
    Stack,
    World,
)

DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})
API = ResourceDesired(
    id="api", kind="service", fields={"image": FieldValue(value="app:latest")},
    refs=(Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL"),),
)
STACK = Stack(resources=(DB, API))


def _world(*observed: ResourceObserved) -> World:
    return World(resources=observed)


def test_empty_world_creates_db_and_gates_app():
    actions = plan(STACK, World())
    assert CreateMiniStackResource(id="db", service="rds") in actions
    # api gated on db (not healthy yet) -> NoOp, no RunContainer
    assert RunContainer(id="api") not in actions
    assert NoOp(id="api") in actions


def test_app_runs_once_db_healthy():
    world = _world(
        ResourceObserved(id="db", kind="rds", phase="healthy",
                         facts={"DATABASE_URL": "postgres://x"}),
    )
    actions = plan(STACK, world)
    assert RunContainer(id="api") in actions
    assert NoOp(id="db") in actions  # db healthy -> no-op


def test_idempotent_when_all_healthy():
    world = _world(
        ResourceObserved(id="db", kind="rds", phase="healthy", facts={"DATABASE_URL": "x"}),
        ResourceObserved(id="api", kind="service", phase="healthy"),
    )
    assert plan(STACK, world) == [NoOp(id="db"), NoOp(id="api")]


def test_restart_crashed_service():
    world = _world(
        ResourceObserved(id="db", kind="rds", phase="healthy", facts={"DATABASE_URL": "x"}),
        ResourceObserved(id="api", kind="service", phase="crashed"),
    )
    assert RunContainer(id="api") in plan(STACK, world)


def test_errored_service_recovers_when_refs_ready():
    world = _world(
        ResourceObserved(id="db", kind="rds", phase="healthy", facts={"DATABASE_URL": "x"}),
        ResourceObserved(id="api", kind="service", phase="error"),
    )
    assert RunContainer(id="api") in plan(STACK, world)


def test_two_services_share_one_db():
    api2 = ResourceDesired(
        id="api2", kind="service", fields={"image": FieldValue(value="app:latest")},
        refs=(Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL"),),
    )
    stack = Stack(resources=(DB, API, api2))
    world = _world(
        ResourceObserved(id="db", kind="rds", phase="healthy", facts={"DATABASE_URL": "x"}),
    )
    actions = plan(stack, world)
    assert RunContainer(id="api") in actions and RunContainer(id="api2") in actions


def test_batch_runs_once_then_terminal():
    job = ResourceDesired(id="job", kind="batch", fields={"image": FieldValue(value="busybox")})
    stack = Stack(resources=(job,))
    assert RunContainer(id="job") in plan(stack, World())          # pending -> run
    for terminal in ("running", "done", "error"):
        world = _world(ResourceObserved(id="job", kind="batch", phase=terminal))
        assert plan(stack, world) == [NoOp(id="job")]             # never re-run


def test_aws_resource_created_once_then_exists():
    bucket = ResourceDesired(id="uploads", kind="s3")
    stack = Stack(resources=(bucket,))
    assert CreateMiniStackResource(id="uploads", service="s3") in plan(stack, World())
    world = _world(ResourceObserved(id="uploads", kind="s3", phase="healthy"))
    assert plan(stack, world) == [NoOp(id="uploads")]


def test_prune_extra():
    world = _world(ResourceObserved(id="ghost", kind="service", phase="healthy"))
    actions = plan(STACK, world)
    assert StopContainer(id="ghost", name="ghost", kind="service") in actions
