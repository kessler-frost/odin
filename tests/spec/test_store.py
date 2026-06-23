"""S1.2 — SpecStore: append-only content-addressed revisions + World."""
from __future__ import annotations

from odin.spec.models import FieldValue, ResourceDesired, Stack, WorldDelta
from odin.spec.store import SpecStore, rev_of


def _stack(image: str) -> Stack:
    return Stack(
        resources=(
            ResourceDesired(
                id="api", kind="service", fields={"image": FieldValue(value=image)}
            ),
        )
    )


def test_apply_creates_revision_and_moves_head(tmp_path):
    store = SpecStore(tmp_path)
    rev1 = store.apply(_stack("v1"))
    rev2 = store.apply(_stack("v2"))

    assert rev1 != rev2
    assert store.head() == rev2
    # old revision is still retrievable (append-only, not overwritten)
    assert store.get_stack(rev=rev1).resources[0].fields["image"].value == "v1"
    assert store.get_stack().resources[0].fields["image"].value == "v2"


def test_rev_is_deterministic():
    assert rev_of(_stack("x")) == rev_of(_stack("x"))


def test_world_delta_upserts_and_persists(tmp_path):
    store = SpecStore(tmp_path)
    store.apply_delta(WorldDelta(env="default", resource_id="db", kind="rds", phase="starting"))
    world = store.apply_delta(
        WorldDelta(env="default", resource_id="db", kind="rds", phase="healthy",
                   facts={"endpoint": "postgres://localhost:15432"})
    )
    assert len(world.resources) == 1
    db = world.get("db")
    assert db.phase == "healthy"
    assert db.facts["endpoint"] == "postgres://localhost:15432"
    # persisted
    assert SpecStore(tmp_path).current_world().get("db").phase == "healthy"


def test_apply_delta_counts_consecutive_crashes(tmp_path):
    store = SpecStore(tmp_path)

    def push(phase):
        return store.apply_delta(WorldDelta(env="default", resource_id="api", kind="service", phase=phase))

    push("starting")
    assert push("healthy").get("api").restarts == 0
    assert push("crashed").get("api").restarts == 1       # fresh crash
    assert push("crashed").get("api").restarts == 1       # still crashed -> not double-counted
    assert push("starting").get("api").restarts == 1      # preserved across restart
    assert push("crashed").get("api").restarts == 2       # next crash
    assert push("healthy").get("api").restarts == 0       # recovery resets the streak
