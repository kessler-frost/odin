"""S2.1 — the pure plan(Stack, World) -> [Action] across lifecycle states."""
from __future__ import annotations

from odin.reconcile.actions import NoOp, ProvisionResource, StopContainer
from odin.reconcile.plan import plan
from odin.spec.models import FieldValue, ResourceDesired, ResourceObserved, Stack, World

DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})
STACK = Stack(resources=(DB,))


def _world(*observed: ResourceObserved) -> World:
    return World(resources=observed)


def test_empty_world_creates_db():
    actions = plan(STACK, World())
    assert ProvisionResource(id="db", service="rds") in actions


def test_db_noop_once_healthy():
    world = _world(ResourceObserved(id="db", kind="rds", phase="healthy", facts={"DATABASE_URL": "x"}))
    assert plan(STACK, world) == [NoOp(id="db")]


def test_db_recreated_when_crashed():
    world = _world(ResourceObserved(id="db", kind="rds", phase="crashed"))
    assert ProvisionResource(id="db", service="rds") in plan(STACK, world)


def test_crash_looped_rds_gives_up():
    from odin.reconcile.plan import MAX_RESTARTS

    at_cap = _world(ResourceObserved(id="db", kind="rds", phase="crashed", restarts=MAX_RESTARTS))
    assert plan(STACK, at_cap) == [NoOp(id="db")]           # rds recreate churn stops too


def test_aws_resource_created_once_then_exists():
    bucket = ResourceDesired(id="uploads", kind="s3")
    stack = Stack(resources=(bucket,))
    assert ProvisionResource(id="uploads", service="s3") in plan(stack, World())
    world = _world(ResourceObserved(id="uploads", kind="s3", phase="healthy"))
    assert plan(stack, world) == [NoOp(id="uploads")]


def test_prune_extra():
    world = _world(ResourceObserved(id="ghost", kind="rds", phase="healthy"))
    actions = plan(STACK, world)
    assert StopContainer(id="ghost", name="ghost", kind="rds") in actions
