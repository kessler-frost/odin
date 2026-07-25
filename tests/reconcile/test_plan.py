"""S2.1 — the pure plan(Stack, World) -> [Action] across lifecycle states.

W2.7: `rds` left this module's scope (it's TF-owned now -- tofu's
CreateDBInstance through gateway/models/rdsctl.py), so plan() must NoOp it like
every other TF-owned kind while still PRUNING a stale World entry for one. The
AWS-shaped PROVISIONED kinds are all that's left to create here.
"""
from __future__ import annotations

from odin.reconcile.actions import NoOp, ProvisionResource, StopContainer
from odin.reconcile.plan import plan
from odin.spec.models import FieldValue, ResourceDesired, ResourceObserved, Stack, World

DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})
STACK = Stack(resources=(DB,))


def _world(*observed: ResourceObserved) -> World:
    return World(resources=observed)


def test_rds_is_never_provisioned_only_noopd():
    """tofu is an rds node's sole creator. plan() emitting a ProvisionResource
    for one would race `tofu apply`'s own CreateDBInstance -- the exact class of
    bug /apply-full's deferred store commit exists to prevent."""
    for world in (
        World(),
        _world(ResourceObserved(id="db", kind="rds", phase="crashed")),
        _world(ResourceObserved(id="db", kind="rds", phase="starting")),
    ):
        assert plan(STACK, world) == [NoOp(id="db")]


def test_rds_noop_once_healthy():
    world = _world(ResourceObserved(id="db", kind="rds", phase="healthy", facts={"DATABASE_URL": "x"}))
    assert plan(STACK, world) == [NoOp(id="db")]


def test_aws_resource_created_once_then_exists():
    bucket = ResourceDesired(id="uploads", kind="s3")
    stack = Stack(resources=(bucket,))
    assert ProvisionResource(id="uploads", service="s3") in plan(stack, World())
    world = _world(ResourceObserved(id="uploads", kind="s3", phase="healthy"))
    assert plan(stack, world) == [NoOp(id="uploads")]


def test_aws_resource_recreated_when_crashed():
    bucket = ResourceDesired(id="uploads", kind="s3")
    stack = Stack(resources=(bucket,))
    world = _world(ResourceObserved(id="uploads", kind="s3", phase="crashed"))
    assert ProvisionResource(id="uploads", service="s3") in plan(stack, world)


def test_prune_extra():
    world = _world(ResourceObserved(id="ghost", kind="rds", phase="healthy"))
    actions = plan(STACK, world)
    assert StopContainer(id="ghost", name="ghost", kind="rds") in actions
