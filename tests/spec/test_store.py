"""S1.2 — SpecStore: append-only content-addressed revisions + World."""
from __future__ import annotations

import stat

import pytest

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


def test_stack_revision_is_written_0600(tmp_path):
    # Security finding #3a: a Stack revision carries every field's raw value
    # (rds `password`, etc.) in cleartext, immutably -- 0600 is the only
    # defense available for this file.
    store = SpecStore(tmp_path)
    rev = store.apply(_stack("v1"))
    path = tmp_path / "default" / "stacks" / f"{rev}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_world_json_is_written_0600(tmp_path):
    # Security finding #3a: `facts` can carry a live credential in cleartext
    # (rds's DATABASE_URL embeds user:password) -- not redacted (the Fabric
    # resolves refs out of these same facts functionally), so 0600 is the
    # only defense available.
    store = SpecStore(tmp_path)
    store.apply_delta(WorldDelta(env="default", resource_id="db", kind="rds", phase="healthy",
                                  facts={"DATABASE_URL": "postgresql://app:s3cr3t@host/postgres"}))
    path = tmp_path / "default" / "world.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


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


def test_write_world_crash_leaves_prior_world_intact(tmp_path, monkeypatch):
    # Release finding #2: write_world is atomic -- a crash mid-write must
    # not corrupt or drop the previously-persisted World.
    store = SpecStore(tmp_path)
    store.apply_delta(WorldDelta(env="default", resource_id="db", kind="rds", phase="healthy"))

    def boom(*a, **k):
        raise OSError("simulated crash")

    monkeypatch.setattr("odin.util.os.replace", boom)
    with pytest.raises(OSError):
        store.apply_delta(WorldDelta(env="default", resource_id="api", kind="service", phase="starting"))

    reloaded = SpecStore(tmp_path).current_world()
    assert reloaded.get("db").phase == "healthy"
    assert reloaded.get("api") is None


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
