"""Release finding #4 -- the startup EC2-VM reaper is wired into
create_app's lifespan, but must default to OFF whenever a caller brings its
own SpecStore (every test in this suite does): the reaper cross-references
REAL, machine-global `limactl` VMs against the app's store, and a test's
`tmp_path` store has no way to know about a VM that legitimately belongs to
a different process/store on the same machine. Only the real production
app (no `store=` override) may default to reaping.

`ec2compute.reap_orphaned_vms` itself is monkeypatched here so this test
never shells out to a real `limactl`."""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime


def test_reap_is_not_invoked_by_default_when_a_custom_store_is_given(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("odin.server.ec2compute.reap_orphaned_vms", lambda *a, **k: calls.append((a, k)) or [])
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app):
        pass
    assert calls == []


def test_reap_runs_when_explicitly_enabled(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("odin.server.ec2compute.reap_orphaned_vms", lambda *a, **k: calls.append((a, k)) or [])
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False, reap_ec2_vms=True,
    )
    with TestClient(app):
        pass
    assert len(calls) == 1
    (root, envs), _kwargs = calls[0]
    assert root == tmp_path
    assert envs == ["default"]  # list_envs() on a fresh store


def test_reap_failure_never_blocks_startup(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("limactl not installed")

    monkeypatch.setattr("odin.server.ec2compute.reap_orphaned_vms", boom)
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False, reap_ec2_vms=True,
    )
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True
