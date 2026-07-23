"""S1 -- stores.py: the per-env JSON sidecar stores synth.py's
control-plane persists tags/attributes/delete-markers to."""
from __future__ import annotations

import stat
import threading
from pathlib import Path

from odin.gateway.stores import NO_CHANGE, JsonStore, SynthStores


def test_get_returns_default_when_absent(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    assert store.get("default", "missing", "fallback") == "fallback"


def test_get_returns_none_by_default_when_absent(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    assert store.get("default", "missing") is None


def test_set_then_get_roundtrips(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    store.set("default", "jobs", {"env": "prod"})
    assert store.get("default", "jobs") == {"env": "prod"}


def test_set_persists_to_disk(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    store.set("default", "jobs", {"env": "prod"})
    path = tmp_path / "default" / "gateway" / "widgets.json"
    assert path.exists()
    assert "jobs" in path.read_text()


def test_reload_from_disk_preserves_value(tmp_path: Path):
    first = JsonStore(tmp_path, "widgets")
    first.set("default", "jobs", {"env": "prod"})

    second = JsonStore(tmp_path, "widgets")
    assert second.get("default", "jobs") == {"env": "prod"}


def test_reload_lookup_without_prior_set_call(tmp_path: Path):
    """A fresh store (e.g. after a server restart) must resolve a value
    persisted by a prior instance even if get() is the very first call."""
    first = JsonStore(tmp_path, "widgets")
    first.set("default", "jobs", {"env": "prod"})

    second = JsonStore(tmp_path, "widgets")
    assert second.get("default", "jobs") == {"env": "prod"}


def test_per_env_isolation(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    store.set("a", "jobs", "a-value")
    store.set("b", "jobs", "b-value")
    assert store.get("a", "jobs") == "a-value"
    assert store.get("b", "jobs") == "b-value"


def test_set_overwrites_existing_key(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    store.set("default", "jobs", "first")
    store.set("default", "jobs", "second")
    assert store.get("default", "jobs") == "second"


def test_persisted_file_is_0600(tmp_path: Path):
    # Release finding #2: this sidecar can carry another env's IAM/EC2
    # state -- never briefly world-readable, and never left world-readable.
    store = JsonStore(tmp_path, "widgets")
    store.set("default", "jobs", {"env": "prod"})
    path = tmp_path / "default" / "gateway" / "widgets.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_concurrent_set_calls_do_not_raise_during_persist(tmp_path: Path):
    """Release finding #2: `_persist` serializes a SNAPSHOT of the env dict,
    not the live dict itself -- many threads calling `set()` concurrently
    must never raise "dictionary changed size during iteration" out of the
    `json.dumps` inside `_persist`."""
    store = JsonStore(tmp_path, "widgets")
    errors: list[Exception] = []

    def hammer(i: int) -> None:
        try:
            for j in range(50):
                store.set("default", f"key-{i}", {"n": j})
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


def test_update_mutates_existing_value_atomically(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    store.set("default", "counter", {"n": 1})
    result = store.update("default", "counter", lambda v: {"n": v["n"] + 1})
    assert result == {"n": 2}
    assert store.get("default", "counter") == {"n": 2}


def test_update_passes_none_for_absent_key(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    seen = []

    def mutator(current):
        seen.append(current)
        return {"created": True}

    store.update("default", "missing", mutator)
    assert seen == [None]
    assert store.get("default", "missing") == {"created": True}


def test_update_no_change_sentinel_skips_the_write(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    result = store.update("default", "missing", lambda v: NO_CHANGE)
    assert result is NO_CHANGE
    assert store.get("default", "missing") is None
    path = tmp_path / "default" / "gateway" / "widgets.json"
    assert not path.exists()  # NO_CHANGE never even triggers a persist


def test_update_no_change_leaves_existing_value_untouched(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    store.set("default", "jobs", {"env": "prod"})
    store.update("default", "jobs", lambda v: NO_CHANGE)
    assert store.get("default", "jobs") == {"env": "prod"}


def test_concurrent_updates_to_the_same_key_are_serialized_and_lossless(tmp_path: Path):
    """Many threads incrementing the SAME counter via `update()` must never
    lose an increment -- the read-modify-write is atomic under the env lock,
    unlike a bare get()-then-set() pair racing across threads."""
    store = JsonStore(tmp_path, "widgets")
    store.set("default", "counter", {"n": 0})

    def increment():
        for _ in range(50):
            store.update("default", "counter", lambda v: {"n": v["n"] + 1})

    threads = [threading.Thread(target=increment) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert store.get("default", "counter") == {"n": 400}


def test_different_envs_do_not_contend_on_the_same_lock(tmp_path: Path):
    store = JsonStore(tmp_path, "widgets")
    store.set("a", "jobs", "a-value")
    store.set("b", "jobs", "b-value")
    assert store._lock_for("a") is not store._lock_for("b")


def test_synth_stores_are_independently_namespaced(tmp_path: Path):
    """The four SynthStores share a root but never a JSON file -- a queue
    and a tag entry that happen to share a lookup key never collide."""
    stores = SynthStores(tmp_path)
    stores.tags.set("default", "jobs", {"env": "prod"})
    stores.sqs_queues.set("default", "jobs", {"attributes": {}, "deleted_at": None})

    assert stores.tags.get("default", "jobs") == {"env": "prod"}
    assert stores.sqs_queues.get("default", "jobs") == {"attributes": {}, "deleted_at": None}
    assert (tmp_path / "default" / "gateway" / "tags.json").exists()
    assert (tmp_path / "default" / "gateway" / "sqs_queues.json").exists()
