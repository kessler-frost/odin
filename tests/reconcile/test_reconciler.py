"""S2.3 — the Reconciler loop, driven against fakes (no Colima / no MiniStack)."""
from __future__ import annotations


from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import _STATUS_TO_PHASE, ContainerFacts, HostFacts, RunHandle
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
        self.runs, self.stopped, self.specs = [], [], {}
        self._status, self._port, self._exit = {}, {}, {}

    def run_container(self, spec):
        self.runs.append(spec.name)
        self.specs[spec.name] = spec
        self._status[spec.name] = "running"
        self._port[spec.name] = 18080
        return RunHandle(id="fake-" + spec.name, name=spec.name)

    def stop(self, name):
        self.stopped.append(name)
        self._status[name] = "absent"

    def status(self, name):
        return self._status.get(name, "absent")

    def exit_code(self, name):
        return self._exit.get(name, 0)

    def facts(self, name, container_port=0):
        phase = _STATUS_TO_PHASE.get(self.status(name), "pending")
        port = self._port.get(name, 0) if self.status(name) == "running" else 0
        return ContainerFacts(phase=phase, host_port=port, cpu=1.0, ram=10.0)

    def stats(self, name):
        return {"cpu": 1.0, "ram": 10.0}

    def ensure_host(self):
        return HostFacts(total_mem_mib=48000, cpu_count=8)

    def set(self, name, docker_status, exit_code=0):
        self._status[name] = docker_status
        self._exit[name] = exit_code


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

    rt.set("api", "exited", exit_code=1)     # the container dies
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


async def test_scheduler_queues_over_budget_then_runs_when_freed(tmp_path):
    from odin.reconcile.scheduler import Scheduler

    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    a = ResourceDesired(id="a", kind="service",
                        fields={"image": FieldValue(value="x"), "memory_mib": FieldValue(value=200)})
    b = ResourceDesired(id="b", kind="service",
                        fields={"image": FieldValue(value="x"), "memory_mib": FieldValue(value=200)})
    store.apply(Stack(resources=(a, b)))
    recon = Reconciler(store, rt, rds, scheduler=Scheduler(budget_mib=300),
                       http_ok=_yes, pg_ready=_yes, poll_interval=0)

    await recon.tick()                       # only one 200-MiB service fits in 300
    phases = sorted([store.current_world().get("a").phase,
                     store.current_world().get("b").phase])
    assert phases == ["queued", "starting"]  # one ran, one queued

    store.apply(Stack(resources=(b,)))       # drop a -> frees memory
    await recon.tick()                       # b now fits and runs
    assert "b" in rt.runs


async def test_llm_evicted_to_make_room_for_service(tmp_path):
    from odin.reconcile.scheduler import Scheduler

    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    llm = ResourceDesired(id="model", kind="llm",
                          fields={"image": FieldValue(value="m"), "port": FieldValue(value=1234),
                                  "memory_mib": FieldValue(value=200)})
    svc = ResourceDesired(id="svc", kind="service",
                          fields={"image": FieldValue(value="s"), "memory_mib": FieldValue(value=200)})
    store.apply(Stack(resources=(llm,)))
    recon = Reconciler(store, rt, rds, scheduler=Scheduler(budget_mib=300),
                       http_ok=_yes, pg_ready=_yes, poll_interval=0)

    await recon.tick()                       # the model loads...
    await recon.tick()                       # ...and goes healthy
    assert store.current_world().get("model").phase == "healthy"

    store.apply(Stack(resources=(llm, svc)))  # a service now needs memory
    await recon.tick()                        # evict the model to fit the service
    assert store.current_world().get("model").phase == "evicted"
    assert "svc" in rt.runs

    store.apply(Stack(resources=(llm,)))      # pressure clears: drop the service
    await recon.tick()                        # the evicted model must come back
    assert store.current_world().get("model").phase == "starting"
    assert rt.runs.count("model") == 2        # re-admitted, not stranded


async def test_unchanged_status_is_not_rebroadcast(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB,)))
    sent = []

    class FakeWS:
        async def broadcast(self, msg):
            sent.append(msg)

    recon = Reconciler(store, rt, rds, ws=FakeWS(), http_ok=_yes, pg_ready=_yes, poll_interval=0)
    for _ in range(5):
        await recon.tick()                    # db goes healthy, then stays healthy
    healthy = [m for m in sent if m.get("resource_id") == "db" and m.get("phase") == "healthy"]
    assert len(healthy) == 1                  # emitted once, not re-spammed every tick


async def test_destroy_broadcasts_draft_reset_so_canvas_clears(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB,)))
    sent = []

    class FakeWS:
        async def broadcast(self, msg):
            sent.append(msg)

    recon = Reconciler(store, rt, rds, ws=FakeWS(), http_ok=_yes, pg_ready=_yes, poll_interval=0)
    await recon.tick()                        # db -> starting
    store.apply(Stack())                      # destroy
    await recon.tick()                        # prune db
    resets = [m for m in sent if m.get("resource_id") == "db" and m.get("phase") == "draft"]
    assert resets, "prune must tell the canvas the node is draft again (else stale-green tile)"


async def test_rds_crash_clears_record_and_recreates(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB,)))
    recon = Reconciler(store, rt, rds, http_ok=_yes, pg_ready=_yes, poll_interval=0)
    await recon.tick()                        # create db
    await recon.tick()                        # db healthy
    assert store.current_world().get("db").phase == "healthy"
    assert rds.created.count("db") == 1

    rt.set("ministack-rds-db", "exited", exit_code=1)  # the DB container dies
    # one tick: observe sees crashed -> clears the stale record -> plan recreates
    await recon.tick()
    assert rds.available is False             # delete_db was called (the fix)
    assert rds.created.count("db") == 2       # recreated (AlreadyExists would block this without the delete)


async def test_aws_env_injected_into_app_containers(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB, API)))
    recon = Reconciler(store, rt, rds, http_ok=_yes, pg_ready=_yes, poll_interval=0,
                       aws_env=lambda: {"AWS_ENDPOINT_URL": "http://host.docker.internal:4566"})
    for _ in range(3):
        await recon.tick()
    spec = rt.specs["api"]
    assert spec.env["AWS_ENDPOINT_URL"] == "http://host.docker.internal:4566"
    assert spec.env["DATABASE_URL"].startswith("postgresql://")  # ref still wins/coexists


async def test_dep_healthy_publishes_endpoint(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    redis = ResourceDesired(id="redis", kind="dep",
                            fields={"image": FieldValue(value="redis:7"),
                                    "port": FieldValue(value=6379)})
    store.apply(Stack(resources=(redis,)))
    recon = Reconciler(store, rt, rds, http_ok=_yes, pg_ready=_yes, tcp_ok=_yes, poll_interval=0)

    await recon.tick()                       # run redis -> starting
    await recon.tick()                       # observe running -> healthy
    obs = store.current_world().get("redis")
    assert obs.phase == "healthy"
    assert obs.facts["endpoint"].startswith("127.0.0.1:")  # referenceable by other nodes
    assert obs.facts["PORT"] == 18080


async def test_batch_runs_to_completion_no_restart(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    job = ResourceDesired(id="job", kind="batch",
                          fields={"image": FieldValue(value="busybox")})
    store.apply(Stack(resources=(job,)))
    recon = Reconciler(store, rt, rds, http_ok=_yes, pg_ready=_yes, poll_interval=0)

    await recon.tick()                       # run the job
    assert "job" in rt.runs
    assert store.current_world().get("job").phase == "running"

    rt.set("job", "exited", exit_code=0)     # the job finishes successfully
    await recon.tick()                       # observe -> done
    assert store.current_world().get("job").phase == "done"

    await recon.tick()                       # terminal: never restarts
    assert rt.runs.count("job") == 1


async def test_blocked_ref_times_out_to_error(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()       # rds never becomes available
    store, recon = _recon(tmp_path, rt, rds, ref_timeout=0.0)
    await recon.tick()                       # api blocked
    await recon.tick()                       # past timeout -> error
    assert store.current_world().get("api").phase == "error"
    assert "api" not in rt.runs
