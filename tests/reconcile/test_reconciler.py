"""S2.3 — the Reconciler loop, driven against fakes (no Colima, no real backings).

W2.7 note: this loop no longer creates ANYTHING but the AWS-shaped PROVISIONED
kinds (s3/sqs/sns/dynamodb), so those are what the provision/observe/prune
tests below drive. `rds` used to be the driver here; it's TF-owned now
(gateway/models/rdsctl.py) and appears only in the projection tests and in
`test_rds_is_no_longer_provisioned_by_this_loop_at_all`.
"""
from __future__ import annotations

import asyncio
import time

from odin.gateway.policy import compile_policies
from odin.gateway.stores import SynthStores
from odin.reconcile.drift import DriftSweeper
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import _STATUS_TO_PHASE, ContainerFacts, HostFacts, RunHandle
from odin.spec.models import Edge, FieldValue, ResourceDesired, Stack
from odin.spec.store import SpecStore
from tests.reconcile.test_drift import FakeContainers, FakeVms

DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})


class FakeRuntime:
    def __init__(self):
        self.runs, self.stopped, self.specs = [], [], {}
        self._status, self._port, self._exit, self._logs = {}, {}, {}, {}

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

    def logs(self, name, tail=20):
        return self._logs.get(name, "")

    def ensure_host(self):
        return HostFacts(total_mem_mib=48000, cpu_count=8)

    def set(self, name, docker_status, exit_code=0, logs=""):
        self._status[name] = docker_status
        self._exit[name] = exit_code
        if logs:
            self._logs[name] = logs


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

    def container_name(self, service):
        return f"odin-aws-{service}-default"


class FakeGateway:
    def __init__(self):
        self.calls = []

    def update(self, env, statements_by_node, backing_ports):
        self.calls.append((env, statements_by_node, backing_ports))


BUCKET = ResourceDesired(id="uploads", kind="s3")


class FakeWS:
    def __init__(self):
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)


async def test_provisioned_resource_reaches_healthy(tmp_path):
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)

    await recon.tick()                       # provision
    assert ("s3", "uploads", ()) in aws.provisioned
    assert store.current_world().get("uploads").phase == "starting"

    await recon.tick()                       # observe exists() -> healthy
    observed = store.current_world().get("uploads")
    assert observed.phase == "healthy"
    assert observed.facts["endpoint"] == "http://host.docker.internal:9000"


async def test_destroy_then_reapply_recreates_a_provisioned_resource(tmp_path):
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    for _ in range(2):
        await recon.tick()                    # provision, then observe it healthy
    assert store.current_world().get("uploads").phase == "healthy"

    store.apply(Stack())                      # destroy: empty desired state
    await recon.tick()
    assert store.current_world().resources == ()        # pruned

    store.apply(Stack(resources=(BUCKET,)))             # re-apply
    await recon.tick()
    assert len([p for p in aws.provisioned if p[1] == "uploads"]) == 2


async def test_rds_is_no_longer_provisioned_by_this_loop_at_all(tmp_path):
    """W2.7: `rds` is TF-owned now (tofu's CreateDBInstance ->
    gateway/models/rdsctl.py). The reconciler must neither provision it nor run
    a container for it -- only project it (see the TF-owned projection tests
    below and tests/reconcile/test_tf_status.py)."""
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(DB,)))
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)

    for _ in range(3):
        await recon.tick()

    assert aws.provisioned == []
    assert rt.runs == []
    # Never entered into World by this loop either: only tf_status.project()
    # (fed by the gateway's own DB-instance record) can put it there.
    assert store.current_world().get("db") is None


async def test_unchanged_status_is_not_rebroadcast(tmp_path):
    rt, aws, ws = FakeRuntime(), FakeAws(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))

    recon = Reconciler(store, rt, aws=aws, ws=ws, poll_interval=0)
    for _ in range(5):
        await recon.tick()                    # goes healthy, then stays healthy
    healthy = [m for m in ws.sent if m.get("resource_id") == "uploads" and m.get("phase") == "healthy"]
    assert len(healthy) == 1                  # emitted once, not re-spammed every tick


async def test_destroy_broadcasts_draft_reset_so_canvas_clears(tmp_path):
    rt, aws, ws = FakeRuntime(), FakeAws(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))

    recon = Reconciler(store, rt, aws=aws, ws=ws, poll_interval=0)
    await recon.tick()                        # -> starting
    store.apply(Stack())                      # destroy
    await recon.tick()                        # prune
    resets = [m for m in ws.sent if m.get("resource_id") == "uploads" and m.get("phase") == "draft"]
    assert resets, "prune must tell the canvas the node is draft again (else stale-green tile)"


async def test_crash_pushes_a_type_log_message_matching_the_uis_bottompanel_shape(tmp_path):
    # BottomPanel.tsx's parseWebSocketMessage already understands
    # {type:"log", text, source, level} -- this is the "feed the dead Logs
    # tab" half of observability v1, no UI-side shape change needed.
    rt, aws, ws = FakeRuntime(), FakeAws(), FakeWS()
    rt.set("odin-aws-s3-default", "running", logs="disk full")
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))

    recon = Reconciler(store, rt, aws=aws, ws=ws, poll_interval=0)
    await recon.tick()
    await recon.tick()  # healthy

    aws.exists = lambda service, name: False  # the backing lost the resource
    await recon.tick()

    log_msgs = [m for m in ws.sent if m.get("type") == "log"]
    assert len(log_msgs) == 1
    msg = log_msgs[0]
    assert msg["source"] == "uploads"
    assert msg["level"] == "error"
    assert "the s3 backing is no longer reachable" in msg["text"]
    assert "disk full" in msg["text"]
    assert msg["env"] == "default"


async def test_healthy_never_pushes_a_log_message(tmp_path):
    rt, aws, ws = FakeRuntime(), FakeAws(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))

    recon = Reconciler(store, rt, aws=aws, ws=ws, poll_interval=0)
    await recon.tick()
    await recon.tick()  # healthy, never crashed

    assert not [m for m in ws.sent if m.get("type") == "log"]


async def test_provisioned_crash_carries_a_verdict_and_logtail(tmp_path):
    rt, aws = FakeRuntime(), FakeAws()
    rt.set("odin-aws-s3-default", "running", logs="panic: disk full")
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="uploads", kind="s3"),)))
    sent = []

    class FakeWS:
        async def broadcast(self, msg):
            sent.append(msg)

    recon = Reconciler(store, rt, aws=aws, ws=FakeWS(), poll_interval=0)
    await recon.tick()  # provision -> starting
    await recon.tick()  # observe exists() == True -> healthy
    assert store.current_world().get("uploads").phase == "healthy"

    aws.exists = lambda service, name: False  # the backing lost the resource
    await recon.tick()

    crashed = next(m for m in sent if m.get("resource_id") == "uploads" and m.get("phase") == "crashed")
    assert crashed["verdict"] == "the s3 backing is no longer reachable"
    assert crashed["facts"]["logtail"] == "panic: disk full"


async def test_gc_called_every_tick_with_active_kinds(tmp_path):
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    await recon.tick()
    await recon.tick()
    assert aws.gc_calls == [set(), set()]  # empty stack -> every backing is stoppable, every tick


async def test_hold_blocks_ticks_until_released(tmp_path):
    # The /apply-full race (S5 e2e v8): the route's ensure phase boots
    # backings BEFORE the new stack is committed, while the background loop
    # keeps ticking against the OLD (empty) stack — whose gc(set()) stops
    # the very containers being ensured. hold() must make ticks wait.
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    async with recon.hold():
        tick_task = asyncio.create_task(recon.tick())
        await asyncio.sleep(0.05)  # give the tick every chance to run
        assert aws.gc_calls == []  # blocked: no gc while held
    await tick_task
    assert aws.gc_calls == [set()]  # released: the tick proceeded


async def test_gateway_updated_every_tick_with_compiled_policies_and_backing_ports(tmp_path):
    rt, aws, gw = FakeRuntime(), FakeAws(), FakeGateway()
    store = SpecStore(tmp_path)
    stack = Stack(
        resources=(ResourceDesired(id="uploads", kind="s3"),),
        edges=(Edge(src="app", dst="uploads", kind="iam", perms=("s3:GetObject",)),),
    )
    store.apply(stack)
    recon = Reconciler(store, rt, aws=aws, gateway=gw, poll_interval=0)

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
    rt, aws, gw = FakeRuntime(), FakeAws(), FakeGateway()
    store = SpecStore(tmp_path)
    stack = Stack(resources=(
        ResourceDesired(id="uploads", kind="s3"),
        ResourceDesired(id="jobs", kind="sqs"),
        DB,  # rds is not PROVISIONED -- must be excluded from ensure_backing calls
    ))
    store.apply(stack)
    recon = Reconciler(store, rt, aws=aws, gateway=gw, poll_interval=0)

    await recon.ensure_backings(stack)

    assert set(aws.ensured) == {"s3", "sqs"}
    assert aws.provisioned == []  # no resource created -- that's tofu's job
    assert gw.calls == [("default", compile_policies(stack), {"s3": 9000})]


async def test_ensure_backings_is_a_noop_without_an_aws_seam(tmp_path):
    rt, gw = FakeRuntime(), FakeGateway()
    store = SpecStore(tmp_path)
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
    store.apply(stack)
    recon = Reconciler(store, rt, gateway=gw, poll_interval=0)

    await recon.ensure_backings(stack)  # must not raise

    assert gw.calls == []  # nothing to route without an aws seam


async def test_gateway_update_uses_empty_ports_without_an_aws_seam(tmp_path):
    rt, gw = FakeRuntime(), FakeGateway()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, gateway=gw, poll_interval=0)
    await recon.tick()
    assert gw.calls == [("default", {}, {})]


async def test_no_gateway_configured_does_not_crash_tick(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, poll_interval=0)
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
    rt, aws = FakeRuntime(), SlowAws()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="uploads", kind="s3"),)))
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    await asyncio.gather(recon.tick(), recon.tick())
    assert aws.provisioned == [("s3", "uploads", ())]


# --- TF-owned status projection (fix-wave 2b finding #1): vpc/subnet/sg/
# ec2/ecs/lambda/iam_role/ecr are created ONLY by tofu (never by this
# reconciler's own plan/execute), but a Reconciler wired with `stores=` still
# projects the gateway's synth stores into World every tick -- else these
# kinds show a permanently stale DRAFT badge on the canvas even once tofu
# has a real VM/role/etc. up. ------------------------------------------------


async def test_tf_owned_resource_becomes_healthy_via_projection(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, poll_interval=0, stores=stores)

    await recon.tick()

    observed = store.current_world().get("net")
    assert observed.kind == "vpc"
    assert observed.phase == "healthy"


async def test_tf_owned_resource_pruned_when_tofu_destroys_it(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, poll_interval=0, stores=stores)
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
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-1", {"instance_id": "i-1", "state_name": "running"})
    stores.tags.set("default", "ec2:i-1", {"odin:node": "server"})
    recon = Reconciler(store, rt, poll_interval=0, stores=stores)
    await recon.tick()
    assert store.current_world().get("server") is not None

    stores.ec2compute.set("default", "instance:i-1", {"instance_id": "i-1", "state_name": "terminated"})
    await recon.tick()

    assert store.current_world().get("server") is None


async def test_ec2_instance_phase_reflects_real_state_name(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-1", {"instance_id": "i-1", "state_name": "running"})
    stores.tags.set("default", "ec2:i-1", {"odin:node": "server"})
    recon = Reconciler(store, rt, poll_interval=0, stores=stores)

    await recon.tick()

    observed = store.current_world().get("server")
    assert observed.kind == "ec2"
    assert observed.phase == "healthy"


async def test_stale_tf_owned_world_entry_never_calls_runtime_stop(tmp_path):
    # A canvas node removed WITHOUT a tofu destroy having run yet (e.g. a
    # canvas edit ahead of the next Apply) leaves plan() seeing "observed but
    # no longer desired" -> a StopContainer action -- tofu, never
    # `self._rt.stop`, owns tearing down a TF-managed resource.
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, poll_interval=0, stores=stores)
    await recon.tick()  # "net" enters World, healthy

    store.apply(Stack())  # the canvas node is gone; the synth store record still exists
    await recon.tick()

    assert rt.stopped == []


# --- field test 2 finding #3: a resource odin SYNTHESIZED (a VPC's `default`
# security group, a Lambda's auto-generated execution role) exists in observed
# reality with no canvas node behind it. plan() pruned it every tick (observed
# but not desired) and the projection re-added it every tick -- two deltas a
# tick, forever: 537 of 1263 events in one 8-minute env, ~840 KiB/hour into an
# append-only log, and a real ECS crash event buried under it. ---------------


def _synthesized_sg_stores(tmp_path):
    """The auto-created `default` security group: a REAL record in the gateway's
    store, resolved to a label by its own GroupName, with no `odin:node` tag
    because no canvas node ever drew it."""
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    stores.ec2net.set("default", "sg:sg-1", {"group_id": "sg-1", "group_name": "default"})
    return stores


async def test_a_synthesized_resource_emits_one_delta_across_many_ticks(tmp_path):
    rt, ws = FakeRuntime(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    recon = Reconciler(store, rt, ws=ws, poll_interval=0, stores=_synthesized_sg_stores(tmp_path))

    for _ in range(10):
        await recon.tick()

    deltas = [m for m in ws.sent if m.get("resource_id") == "default"]
    assert [m["phase"] for m in deltas] == ["healthy"], "nothing about it changed: one delta, ever"


async def test_a_synthesized_resource_stays_in_world_instead_of_flapping_to_draft(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = _synthesized_sg_stores(tmp_path)
    recon = Reconciler(store, rt, poll_interval=0, stores=stores)

    for _ in range(3):
        await recon.tick()

    observed = store.current_world().get("default")
    assert observed is not None and observed.phase == "healthy"  # it really does exist

    # ...and the projection is still the authority on when it's gone: tofu
    # destroying it (the record disappears) prunes it exactly once.
    stores.ec2net.delete("default", "sg:sg-1")
    await recon.tick()
    assert store.current_world().get("default") is None


async def test_a_tf_owned_node_removed_from_the_canvas_is_pruned_once_tofu_destroys_it(tmp_path):
    # The other side of the same coin: the World entry for a REAL resource must
    # not be pruned while the resource still exists (that was the flap), but it
    # must still be pruned -- with the `draft` reset the canvas needs -- as soon
    # as the resource is genuinely gone.
    rt, ws = FakeRuntime(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, ws=ws, poll_interval=0, stores=stores)
    await recon.tick()

    store.apply(Stack())  # the canvas node is gone; tofu hasn't destroyed it yet
    for _ in range(4):
        await recon.tick()
    assert not [m for m in ws.sent if m.get("resource_id") == "net" and m.get("phase") == "draft"]

    stores.ec2net.delete("default", "vpc:vpc-1")  # tofu destroy ran
    await recon.tick()
    resets = [m for m in ws.sent if m.get("resource_id") == "net" and m.get("phase") == "draft"]
    assert len(resets) == 1
    assert store.current_world().get("net") is None


async def test_without_stores_a_stale_tf_owned_entry_is_still_pruned(tmp_path):
    # No projection wired means nothing else can ever clear the entry, so
    # plan()'s prune stays in charge -- there is no flap to cause either.
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    Reconciler(store, rt, poll_interval=0, stores=stores)
    await Reconciler(store, rt, poll_interval=0, stores=stores).tick()
    assert store.current_world().get("net") is not None

    store.apply(Stack())
    await Reconciler(store, rt, poll_interval=0).tick()  # stores=None

    assert store.current_world().get("net") is None


async def test_no_stores_configured_does_not_crash_tick(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    recon = Reconciler(store, rt, poll_interval=0)  # stores=None is the default
    await recon.tick()  # must not raise
    assert store.current_world().get("net") is None  # nothing to project without stores


# --- live-edit sns subscriptions: plan() NoOps a healthy resource, so a new
# sns→sqs edge on an ALREADY-healthy topic can only take effect on the
# reconciler's per-tick observe pass (desired vs actual subscription diff). ----


async def test_new_sns_edge_on_a_healthy_topic_provisions_the_missing_subscription(tmp_path):
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    alerts = ResourceDesired(id="alerts", kind="sns")
    jobs = ResourceDesired(id="jobs", kind="sqs")
    store.apply(Stack(resources=(alerts, jobs)))  # no edge yet
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
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


# --- W2.2 drift detection: the TF-owned projection is cross-checked against
# REALITY, so a VM/container deleted out of band stops reading `healthy`
# forever. tests/reconcile/test_drift.py covers the sweep itself (cadence,
# exemptions, boundedness); these cover the RECONCILER half -- the WorldDelta
# and the `type:"log"` error line a drift TRANSITION emits. ------------------


def _drift(vm_names=(), container_names=()):
    return DriftSweeper(
        vms=FakeVms(names=list(vm_names)), containers=FakeContainers(names=list(container_names)),
    )


def _ec2_stores(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-1", {
        "instance_id": "i-1", "state_name": "running", "state_reason": None,
    })
    stores.tags.set("default", "ec2:i-1", {"odin:node": "server"})
    return stores


async def test_drifted_ec2_projects_crashed_with_a_verdict_and_an_error_log(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    sent = []

    class FakeWS:
        async def broadcast(self, msg):
            sent.append(msg)

    recon = Reconciler(
        store, rt, ws=FakeWS(), poll_interval=0, stores=_ec2_stores(tmp_path),
        drift=_drift(vm_names=["veronica"]),  # the instance's own VM is GONE
    )

    await recon.tick()

    observed = store.current_world().get("server")
    assert observed.phase == "crashed", "a record whose real VM is gone must not read healthy"
    assert observed.verdict == "VM odin-ec2-default-i-1 deleted outside odin — re-Apply to recreate"
    # The WS half: the world_delta the canvas badge projects...
    deltas = [m for m in sent if m.get("resource_id") == "server" and m.get("phase") == "crashed"]
    assert deltas and deltas[0]["kind"] == "ec2"
    # ...and the wave-1-shaped log line the UI's Logs tab parses.
    logs = [m for m in sent if m.get("type") == "log"]
    assert logs == [{
        "type": "log", "env": "default", "level": "error", "source": "server",
        "text": "VM odin-ec2-default-i-1 deleted outside odin — re-Apply to recreate",
    }]


async def test_live_vm_keeps_the_ec2_node_healthy(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    recon = Reconciler(
        store, rt, poll_interval=0, stores=_ec2_stores(tmp_path),
        drift=_drift(vm_names=["odin-ec2-default-i-1"]),
    )

    await recon.tick()

    assert store.current_world().get("server").phase == "healthy"


async def test_drift_verdict_is_not_rebroadcast_every_tick(tmp_path):
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    sent = []

    class FakeWS:
        async def broadcast(self, msg):
            sent.append(msg)

    recon = Reconciler(
        store, rt, ws=FakeWS(), poll_interval=0, stores=_ec2_stores(tmp_path),
        drift=_drift(),
    )
    for _ in range(4):
        await recon.tick()

    assert len([m for m in sent if m.get("type") == "log"]) == 1  # the TRANSITION, not every tick


async def test_drifted_ecs_task_makes_the_service_crash_with_a_drift_verdict(tmp_path):
    # ecs reports through its OWN task record (drift.py's module docstring):
    # the sweep marks the task STOPPED, and tf_status's existing service
    # projection turns that into crashed + the real reason. The sweep running
    # BEFORE the projection is also what keeps this test docker-free --
    # `sweep_tasks` only inspects tasks still claiming RUNNING.
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="app", kind="ecs"),)))
    stores = SynthStores(tmp_path)
    stores.ecsctl.set("default", "service:odin:app", {
        "cluster_name": "odin", "service_name": "app", "desired_count": 1, "status": "ACTIVE",
    })
    stores.ecsctl.set("default", "task:odin:t1", {
        "cluster_name": "odin", "service_name": "app", "task_id": "t1", "container_name": "app",
        "last_status": "RUNNING", "stopped_reason": None, "exit_code": None, "stopped_at": None,
    })
    recon = Reconciler(
        store, rt, poll_interval=0, stores=stores, drift=_drift(),
    )

    await recon.tick()

    observed = store.current_world().get("app")
    assert observed.phase == "crashed"
    assert "removed outside odin" in observed.verdict


async def test_drift_is_off_when_no_sweeper_is_wired(tmp_path):
    # The `stores=`-style optional seam: a Reconciler with no sweeper never
    # touches limactl/docker, so hand-seeded records stay exactly as projected.
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    recon = Reconciler(store, rt, poll_interval=0, stores=_ec2_stores(tmp_path))

    await recon.tick()

    assert store.current_world().get("server").phase == "healthy"
