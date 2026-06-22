"""S2.3 — the Reconciler loop, driven against fakes (no Colima / no MiniStack)."""
from __future__ import annotations

import pytest

from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.spec.models import FieldValue, Ref, ResourceDesired, Stack
from odin.spec.store import SpecStore

DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})
API = ResourceDesired(
    id="api", kind="service",
    fields={"image": FieldValue(value="app:latest"), "port": FieldValue(value=8000)},
    refs=(Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL"),),
)


class FakeRuntime:
    def __init__(self):
        self.runs, self.stopped = [], []
        self._phase, self._port = {}, {}

    def run_container(self, spec):
        self.runs.append(spec.name)
        self._phase[spec.name] = "starting"  # i.e. running
        self._port[spec.name] = 18080
        return RunHandle(id="fake-" + spec.name, name=spec.name)

    def stop(self, name):
        self.stopped.append(name)
        self._phase[name] = "pending"

    def facts(self, name, container_port=0):
        return ContainerFacts(phase=self._phase.get(name, "pending"),
                              host_port=self._port.get(name, 0), cpu=1.0, ram=10.0)

    def stats(self, name):
        return {"cpu": 1.0, "ram": 10.0}

    def ensure_host(self):
        return HostFacts(total_mem_mib=48000, cpu_count=8)

    def set(self, name, phase):
        self._phase[name] = phase


class FakeRds:
    def __init__(self):
        self.created, self.available = [], False

    def create_db(self, db_id, user, pw):
        self.created.append(db_id)

    def delete_db(self, db_id):
        self.available = False

    def endpoint(self, db_id):
        return ("127.0.0.1", 15432) if self.available else None

    def container_name(self, db_id):
        return f"ministack-rds-{db_id}"


async def _yes(*a, **k):
    return True


def _recon(tmp_path, rt, rds, **kw):
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB, API)))
    return store, Reconciler(store, rt, rds, http_ok=_yes, pg_ready=_yes,
                             poll_interval=0, **kw)


async def test_brings_app_up_after_db(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store, recon = _recon(tmp_path, rt, rds)

    await recon.tick()                       # create db; api blocked
    assert "db" in rds.created
    assert store.current_world().get("db").phase == "starting"
    assert store.current_world().get("api").phase == "blocked"
    assert "api" not in rt.runs              # gated on db

    rds.available = True
    await recon.tick()                       # db -> healthy; api runs
    assert store.current_world().get("db").phase == "healthy"
    assert store.current_world().get("db").facts["DATABASE_URL"].startswith("postgresql://")
    assert "api" in rt.runs

    await recon.tick()                       # api -> healthy
    assert store.current_world().get("api").phase == "healthy"


async def test_restarts_crashed_service(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store, recon = _recon(tmp_path, rt, rds)
    for _ in range(3):
        await recon.tick()
    assert store.current_world().get("api").phase == "healthy"

    rt.set("api", "crashed")                 # the container dies
    await recon.tick()                       # observe crashed -> plan restart
    assert rt.runs.count("api") == 2         # restarted
    assert store.current_world().get("api").phase == "starting"


async def test_destroy_then_reapply_recreates_db(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store, recon = _recon(tmp_path, rt, rds)
    for _ in range(3):
        await recon.tick()
    assert store.current_world().get("api").phase == "healthy"

    store.apply(Stack())                  # destroy: empty desired state
    await recon.tick()
    assert store.current_world().resources == ()        # pruned
    assert rds.available is False                        # delete_db was called

    store.apply(Stack(resources=(DB, API)))             # re-apply
    rds.available = True
    await recon.tick()
    assert rds.created.count("db") == 2                 # re-created, not skipped


async def test_blocked_ref_times_out_to_error(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()       # rds never becomes available
    store, recon = _recon(tmp_path, rt, rds, ref_timeout=0.0)
    await recon.tick()                       # api blocked
    await recon.tick()                       # past timeout -> error
    assert store.current_world().get("api").phase == "error"
    assert "api" not in rt.runs
