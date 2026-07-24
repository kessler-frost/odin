"""S2.3 — the Reconciler loop, driven against fakes (no Colima, no real backings)."""
from __future__ import annotations

import asyncio
import time

from odin.gateway.policy import compile_policies
from odin.gateway.stores import SynthStores
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import _STATUS_TO_PHASE, ContainerFacts, HostFacts, RunHandle
from odin.spec.models import Edge, FieldValue, ResourceDesired, Stack
from odin.spec.store import SpecStore

DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})


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
        return f"odin-rds-default-{db_id}"


class FakeAws:
    def __init__(self):
        self.provisioned, self.gc_calls, self.ensured = [], [], []
        self.subs: dict[str, tuple[str, ...]] = {}  # pre-seedable, like the real backing's state

    def ensure_backing(self, service):
        self.ensured.append(service)

    def provision(self, service, name, subscriptions=()):
        self.provisioned.append((service, name, subscriptions))
        if service == "sns":  # a re-provision is observable as a subscription change
            self.subs[name] = self.subs.get(name, ()) + tuple(subscriptions)

    def subscriptions(self, topic):
        return self.subs.get(topic, ())

    def exists(self, service, name):
        return True

    def deprovision(self, service, name):
        pass

    def facts(self, service, name):
        return {"endpoint": "http://host.docker.internal:9000"}

    def gc(self, active_kinds):
        self.gc_calls.append(active_kinds)

    def backing_ports(self):
        return {"s3": 9000}


class FakeGateway:
    def __init__(self):
        self.calls = []

    def update(self, env, statements_by_node, backing_ports):
        self.calls.append((env, statements_by_node, backing_ports))


async def _yes(*a, **k):
    return True


async def test_db_reaches_healthy(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB,)))
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0)

    await recon.tick()                       # create db
    assert "db" in rds.created
    assert store.current_world().get("db").phase == "starting"

    rds.available = True
    await recon.tick()                       # db -> healthy
    facts = store.current_world().get("db").facts
    assert facts["DATABASE_URL"].startswith("postgresql://")
    # Finding #5: a container-form (host.docker.internal) AND a VM-form
    # (host.lima.internal, reachable from an EC2 Lima VM) endpoint, same port.
    assert facts["endpoint"] == "host.docker.internal:15432"
    assert facts["DATABASE_URL"] == "postgresql://app:apppass123@host.docker.internal:15432/postgres"
    assert facts["endpoint_vm"] == "host.lima.internal:15432"
    assert facts["DATABASE_URL_VM"] == "postgresql://app:apppass123@host.lima.internal:15432/postgres"


async def test_destroy_then_reapply_recreates_db(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB,)))
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0)
    for _ in range(2):
        await recon.tick()                    # create db, then observe it healthy
    assert store.current_world().get("db").phase == "healthy"

    store.apply(Stack())                  # destroy: empty desired state
    await recon.tick()
    assert store.current_world().resources == ()        # pruned
    assert rds.available is False                        # delete_db was called

    store.apply(Stack(resources=(DB,)))                 # re-apply
    rds.available = True
    await recon.tick()
    assert rds.created.count("db") == 2                 # re-created, not skipped


async def test_unchanged_status_is_not_rebroadcast(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    rds.available = True
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB,)))
    sent = []

    class FakeWS:
        async def broadcast(self, msg):
            sent.append(msg)

    recon = Reconciler(store, rt, rds, ws=FakeWS(), pg_ready=_yes, poll_interval=0)
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

    recon = Reconciler(store, rt, rds, ws=FakeWS(), pg_ready=_yes, poll_interval=0)
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
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0)
    await recon.tick()                        # create db
    await recon.tick()                        # db healthy
    assert store.current_world().get("db").phase == "healthy"
    assert rds.created.count("db") == 1

    rt.set("odin-rds-default-db", "exited", exit_code=1)  # the DB container dies
    # one tick: observe sees crashed -> clears the stale record -> plan recreates
    await recon.tick()
    assert rds.available is False             # delete_db was called (the fix)
    assert rds.created.count("db") == 2       # recreated (AlreadyExists would block this without the delete)


async def test_gc_called_every_tick_with_active_kinds(tmp_path):
    rt, rds, aws = FakeRuntime(), FakeRds(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, rds, aws=aws, pg_ready=_yes, poll_interval=0)
    await recon.tick()
    await recon.tick()
    assert aws.gc_calls == [set(), set()]  # empty stack -> every backing is stoppable, every tick


async def test_hold_blocks_ticks_until_released(tmp_path):
    # The /apply-full race (S5 e2e v8): the route's ensure phase boots
    # backings BEFORE the new stack is committed, while the background loop
    # keeps ticking against the OLD (empty) stack — whose gc(set()) stops
    # the very containers being ensured. hold() must make ticks wait.
    rt, rds, aws = FakeRuntime(), FakeRds(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, rds, aws=aws, pg_ready=_yes, poll_interval=0)
    async with recon.hold():
        tick_task = asyncio.create_task(recon.tick())
        await asyncio.sleep(0.05)  # give the tick every chance to run
        assert aws.gc_calls == []  # blocked: no gc while held
    await tick_task
    assert aws.gc_calls == [set()]  # released: the tick proceeded


async def test_gateway_updated_every_tick_with_compiled_policies_and_backing_ports(tmp_path):
    rt, rds, aws, gw = FakeRuntime(), FakeRds(), FakeAws(), FakeGateway()
    store = SpecStore(tmp_path)
    stack = Stack(
        resources=(ResourceDesired(id="uploads", kind="s3"),),
        edges=(Edge(src="app", dst="uploads", kind="iam", perms=("s3:GetObject",)),),
    )
    store.apply(stack)
    recon = Reconciler(store, rt, rds, aws=aws, gateway=gw, pg_ready=_yes, poll_interval=0)

    await recon.tick()
    assert len(gw.calls) == 1
    env, statements_by_node, backing_ports = gw.calls[0]
    assert env == "default"
    assert statements_by_node == compile_policies(stack)
    assert backing_ports == {"s3": 9000}

    await recon.tick()                        # cheap + idempotent: pushed every pass
    assert len(gw.calls) == 2


async def test_ensure_backings_boots_containers_and_routes_the_gateway_without_provisioning(tmp_path):
    # S5's /apply-full pre-flight: get the gateway routable for tofu WITHOUT
    # racing tofu's own resource creation (ensure_backing only starts the
    # container -- provision(), which actually creates the resource, is
    # never called here).
    rt, rds, aws, gw = FakeRuntime(), FakeRds(), FakeAws(), FakeGateway()
    store = SpecStore(tmp_path)
    stack = Stack(resources=(
        ResourceDesired(id="uploads", kind="s3"),
        ResourceDesired(id="jobs", kind="sqs"),
        DB,  # rds is not PROVISIONED -- must be excluded from ensure_backing calls
    ))
    store.apply(stack)
    recon = Reconciler(store, rt, rds, aws=aws, gateway=gw, pg_ready=_yes, poll_interval=0)

    await recon.ensure_backings(stack)

    assert set(aws.ensured) == {"s3", "sqs"}
    assert aws.provisioned == []  # no resource created -- that's tofu's job
    assert gw.calls == [("default", compile_policies(stack), {"s3": 9000})]


async def test_ensure_backings_is_a_noop_without_an_aws_seam(tmp_path):
    rt, rds, gw = FakeRuntime(), FakeRds(), FakeGateway()
    store = SpecStore(tmp_path)
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
    store.apply(stack)
    recon = Reconciler(store, rt, rds, gateway=gw, pg_ready=_yes, poll_interval=0)

    await recon.ensure_backings(stack)  # must not raise

    assert gw.calls == []  # nothing to route without an aws seam


async def test_gateway_update_uses_empty_ports_without_an_aws_seam(tmp_path):
    rt, rds, gw = FakeRuntime(), FakeRds(), FakeGateway()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, rds, gateway=gw, pg_ready=_yes, poll_interval=0)
    await recon.tick()
    assert gw.calls == [("default", {}, {})]


async def test_no_gateway_configured_does_not_crash_tick(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0)
    await recon.tick()  # gateway=None is the default; must not raise


class SlowAws(FakeAws):
    """provision takes real time (as a container boot does), widening the
    window in which a second tick can plan on the not-yet-updated world."""

    def provision(self, service, name, subscriptions=()):
        time.sleep(0.05)
        super().provision(service, name, subscriptions)


async def test_concurrent_ticks_do_not_double_provision(tmp_path):
    # /apply's synchronous tick overlaps the background loop's tick (tick
    # yields at to_thread) — unserialized, both plan on the same pre-provision
    # world and double-provision (live: two `docker run` on one backing name).
    rt, rds, aws = FakeRuntime(), FakeRds(), SlowAws()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="uploads", kind="s3"),)))
    recon = Reconciler(store, rt, rds, aws=aws, pg_ready=_yes, poll_interval=0)
    await asyncio.gather(recon.tick(), recon.tick())
    assert aws.provisioned == [("s3", "uploads", ())]


# --- TF-owned status projection (fix-wave 2b finding #1): vpc/subnet/sg/
# ec2/ecs/lambda/iam_role/ecr are created ONLY by tofu (never by this
# reconciler's own plan/execute), but a Reconciler wired with `stores=` still
# projects the gateway's synth stores into World every tick -- else these
# kinds show a permanently stale DRAFT badge on the canvas even once tofu
# has a real VM/role/etc. up. ------------------------------------------------


async def test_tf_owned_resource_becomes_healthy_via_projection(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0, stores=stores)

    await recon.tick()

    observed = store.current_world().get("net")
    assert observed.kind == "vpc"
    assert observed.phase == "healthy"


async def test_tf_owned_resource_pruned_when_tofu_destroys_it(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0, stores=stores)
    await recon.tick()
    assert store.current_world().get("net") is not None

    stores.ec2net.delete("default", "vpc:vpc-1")  # tofu destroy already ran
    await recon.tick()

    assert store.current_world().get("net") is None


async def test_terminated_ec2_is_pruned_from_world_after_teardown(tmp_path):
    # Release sweep finding #2: an ec2 that reaches `terminated` (tofu destroy /
    # empty-canvas Apply deleted its Lima VM) must leave World -- not linger as
    # a phantom `crashed` node forever. The synth record survives (ec2compute's
    # lazy sweep is Describe-driven and never fires here), so the projection
    # excluding `terminated` is the ONLY thing that lets the reconciler prune it.
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-1", {"instance_id": "i-1", "state_name": "running"})
    stores.tags.set("default", "ec2:i-1", {"odin:node": "server"})
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0, stores=stores)
    await recon.tick()
    assert store.current_world().get("server") is not None

    stores.ec2compute.set("default", "instance:i-1", {"instance_id": "i-1", "state_name": "terminated"})
    await recon.tick()

    assert store.current_world().get("server") is None


async def test_ec2_instance_phase_reflects_real_state_name(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-1", {"instance_id": "i-1", "state_name": "running"})
    stores.tags.set("default", "ec2:i-1", {"odin:node": "server"})
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0, stores=stores)

    await recon.tick()

    observed = store.current_world().get("server")
    assert observed.kind == "ec2"
    assert observed.phase == "healthy"


async def test_stale_tf_owned_world_entry_never_calls_runtime_stop(tmp_path):
    # A canvas node removed WITHOUT a tofu destroy having run yet (e.g. a
    # canvas edit ahead of the next Apply) leaves plan() seeing "observed but
    # no longer desired" -> a StopContainer action -- tofu, never
    # `self._rt.stop`, owns tearing down a TF-managed resource.
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0, stores=stores)
    await recon.tick()  # "net" enters World, healthy

    store.apply(Stack())  # the canvas node is gone; the synth store record still exists
    await recon.tick()

    assert rt.stopped == []


async def test_no_stores_configured_does_not_crash_tick(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    recon = Reconciler(store, rt, rds, pg_ready=_yes, poll_interval=0)  # stores=None is the default
    await recon.tick()  # must not raise
    assert store.current_world().get("net") is None  # nothing to project without stores


# --- live-edit sns subscriptions: plan() NoOps a healthy resource, so a new
# sns→sqs edge on an ALREADY-healthy topic can only take effect on the
# reconciler's per-tick observe pass (desired vs actual subscription diff). ----


async def test_new_sns_edge_on_a_healthy_topic_provisions_the_missing_subscription(tmp_path):
    rt, rds, aws = FakeRuntime(), FakeRds(), FakeAws()
    store = SpecStore(tmp_path)
    alerts = ResourceDesired(id="alerts", kind="sns")
    jobs = ResourceDesired(id="jobs", kind="sqs")
    store.apply(Stack(resources=(alerts, jobs)))  # no edge yet
    recon = Reconciler(store, rt, rds, aws=aws, pg_ready=_yes, poll_interval=0)
    await recon.tick()                            # provision both -> starting
    await recon.tick()                            # observe -> healthy
    assert store.current_world().get("alerts").phase == "healthy"
    assert aws.subs.get("alerts", ()) == ()       # applied without edges: zero subscriptions

    # The live edit: same nodes plus a new sns→sqs edge, applied while alerts
    # is healthy in World (so plan() emits only a NoOp for it).
    store.apply(Stack(resources=(alerts, jobs), edges=(Edge(src="alerts", dst="jobs"),)))
    await recon.tick()

    assert ("sns", "alerts", ("jobs",)) in aws.provisioned  # observe re-provisioned the gap
    assert aws.subs["alerts"] == ("jobs",)
    assert store.current_world().get("alerts").phase == "healthy"  # never left healthy

    await recon.tick()                            # already subscribed: no re-provision spam
    assert aws.provisioned.count(("sns", "alerts", ("jobs",))) == 1
