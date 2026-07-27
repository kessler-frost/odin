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

import pytest

from odin.aws.backings import BackingAws, BackingUnavailable
from odin.gateway.policy import compile_policies
from odin.gateway.stores import SynthStores
from odin.compute.tasks import container_name as task_container_name
from odin.reconcile import tf_status
from odin.reconcile.drift import DriftSweeper, _sweep_ticks
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import _STATUS_TO_PHASE, ContainerFacts, HostFacts, RunHandle
from odin.spec.models import Edge, FieldValue, ResourceDesired, Stack, WorldDelta
from odin.spec.store import SpecStore
from tests.reconcile.test_drift import FakeContainers, FakeVms

DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})


class FakeRuntime:
    def __init__(self):
        self.runs, self.stopped, self.specs = [], [], {}
        self._status, self._port, self._exit, self._logs = {}, {}, {}, {}

    async def run_container(self, spec):
        self.runs.append(spec.name)
        self.specs[spec.name] = spec
        self._status[spec.name] = "running"
        self._port[spec.name] = 18080
        return RunHandle(id="fake-" + spec.name, name=spec.name)

    async def stop(self, name):
        self.stopped.append(name)
        self._status[name] = "absent"

    async def status(self, name):
        return self._status.get(name, "absent")

    async def exit_code(self, name):
        return self._exit.get(name, 0)

    async def facts(self, name, container_port=0):
        phase = _STATUS_TO_PHASE.get(await self.status(name), "pending")
        port = self._port.get(name, 0) if await self.status(name) == "running" else 0
        return ContainerFacts(phase=phase, host_port=port, cpu=1.0, ram=10.0)

    async def stats(self, name):
        return {"cpu": 1.0, "ram": 10.0}

    async def logs(self, name, tail=20):
        return self._logs.get(name, "")

    async def ensure_host(self):
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

    async def ensure_backing(self, service):
        self.ensured.append(service)

    async def provision(self, service, name, subscriptions=()):
        self.provisioned.append((service, name, subscriptions))
        if service == "sns":  # a re-provision is observable as a subscription change
            self.subs[name] = self.subs.get(name, ()) + tuple(subscriptions)

    async def subscriptions(self, topic):
        return self.subs.get(topic, ())

    async def exists(self, service, name):
        return True

    async def deprovision(self, service, name):
        pass

    async def facts(self, service, name):
        return {"endpoint": "http://host.docker.internal:9000"}

    async def gc(self, active_kinds):
        self.gc_calls.append(active_kinds)

    async def backing_ports(self):
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


async def _gone(service, name):
    """`BackingAws.exists` after the backing lost the resource.

    A module-level `async def` rather than the `lambda` this used to be:
    `exists` is a coroutine method now, and `await` inside a lambda is a
    SyntaxError -- so a lambda stand-in silently hands the reconciler a plain
    `False` where it awaits, which is a TypeError at the call site rather than
    the behaviour under test."""
    return False


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

    aws.exists = _gone  # the backing lost the resource
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

    aws.exists = _gone  # the backing lost the resource
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


async def test_hold_suspends_gc_even_though_ticks_keep_running(tmp_path):
    # The /apply-full race (S5 e2e v8): the route's ensure phase boots
    # backings BEFORE the new stack is committed, while the background loop
    # keeps ticking against the OLD (empty) stack — whose gc(set()) stops
    # the very containers being ensured. hold() must suppress that gc.
    #
    # v0.7.3: it suppresses the ACTION, not the tick. Ticks run to completion
    # throughout the hold (that is what keeps /world live during an apply);
    # what they must not do is touch anything.
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    async with recon.hold():
        for _ in range(5):
            await recon.tick()
        assert aws.gc_calls == []  # suspended: no gc while held
    await recon.tick()
    assert aws.gc_calls == [set()]  # released: the tick acts again


async def test_a_cancelled_hold_does_not_leave_the_reconciler_suspended(tmp_path):
    """An /apply-full killed mid-hold — a shutdown, a connection dropped 40s
    into a tofu run — must not disable the env's reconciler forever. A
    suspension is a flag now rather than a held lock, so the unwind has to put
    it back; a leaked one is an env that silently never gc's or provisions
    again, and nothing else in the system would notice."""
    rt, aws = FakeRuntime(), FakeAws()
    store = SpecStore(tmp_path)
    store.apply(Stack())
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)

    async def held():
        async with recon.hold():
            await asyncio.sleep(30)  # the tofu run that never gets to finish

    task = asyncio.create_task(held())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    await recon.tick()
    assert aws.gc_calls == [set()], "the suspension outlived the cancelled apply"


async def test_hold_suspends_provisioning_and_the_gateway_push(tmp_path):
    # The other two halves of "actions", asserted directly: nothing may be
    # created in a backing while an external author owns the env, and the
    # gateway's compiled policies/ports must stay exactly as the route's own
    # `ensure_backings` staged them — a tick pushing the OLD stack's would
    # break IAM and routing mid-tofu.
    rt, aws, gw = FakeRuntime(), FakeAws(), FakeGateway()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="uploads", kind="s3"),)))
    recon = Reconciler(store, rt, aws=aws, gateway=gw, poll_interval=0)
    async with recon.hold():
        for _ in range(3):
            await recon.tick()
        assert aws.provisioned == []
        assert gw.calls == []
    await recon.tick()
    assert aws.provisioned == [("s3", "uploads", ())]
    assert len(gw.calls) == 1


async def test_world_is_projected_while_an_apply_holds_the_reconciler(tmp_path):
    """FIELD TEST 3, the whole point of the split. `/apply-full` holds the
    reconciler for the entire tofu run (~60s on a real stack), and `/world`
    reads the last committed snapshot — so a service whose deployment died at
    t≈4s still read `healthy` at t=64.0s, measured with a 2s sampler.

    Observation is not an action: projecting the gateway's synth stores into
    World creates nothing and destroys nothing, so it keeps running."""
    rt, ws = FakeRuntime(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-1", {"instance_id": "i-1", "state_name": "running"})
    stores.tags.set("default", "ec2:i-1", {"odin:node": "server"})
    recon = Reconciler(store, rt, ws=ws, poll_interval=0, stores=stores)
    await recon.tick()
    assert store.current_world().get("server").phase == "healthy"

    async with recon.hold():  # an apply is now in flight
        stores.ec2compute.set("default", "instance:i-1", {
            "instance_id": "i-1", "state_name": "stopped",
            "state_reason": {"code": "Server.InternalError", "message": "it died"},
        })
        await recon.tick()

        observed = store.current_world().get("server")
        assert observed.phase == "crashed", "the world froze for the whole apply again"
        assert observed.verdict == "Server.InternalError: it died"
        assert [m["phase"] for m in ws.sent if m.get("resource_id") == "server"] == ["healthy", "crashed"]


async def test_hold_never_prunes_a_label_that_left_the_projection(tmp_path):
    """The ONE World mutation the observing tick still must not make. tofu
    replaces a resource by destroying then re-creating it, so a prune landing
    in that window emits `draft` for a label that returns seconds later —
    exactly the flap v0.7.1 killed. The tick after the hold is what prunes
    whatever genuinely went away."""
    rt, ws = FakeRuntime(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    stores = SynthStores(tmp_path)
    stores.ec2net.set("default", "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set("default", "ec2:vpc-1", {"odin:node": "net"})
    recon = Reconciler(store, rt, ws=ws, poll_interval=0, stores=stores)
    await recon.tick()

    async with recon.hold():
        stores.ec2net.delete("default", "vpc:vpc-1")  # mid-replace: gone, for now
        for _ in range(3):
            await recon.tick()
        assert store.current_world().get("net") is not None, "pruned mid-apply"
        assert not [m for m in ws.sent if m.get("phase") == "draft"]

    await recon.tick()
    assert store.current_world().get("net") is None  # ...and the truth lands right after


async def test_hold_reports_known_drift_without_sweeping_for_more(tmp_path):
    """A sweep CORRECTS records off a `docker ps`/`limactl list` sample, and
    taking that sample while tofu has the daemon pinned is the busy-daemon
    hazard drift.py's confirm-before-correcting note names. So the observing
    tick reads the last sweep's cache instead — a drift already reported stays
    reported, and no new one is written mid-apply."""
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    stores = _ec2_stores(tmp_path)
    sweeper = _drift(vm_names=["veronica"])  # the instance's own VM is GONE
    recon = Reconciler(store, rt, poll_interval=0, stores=stores, drift=sweeper)

    async with recon.hold():
        await recon.tick()
        assert stores.ec2compute.get("default", "instance:i-1")["state_name"] == "running", (
            "the record was corrected by a sweep taken mid-apply"
        )
    await recon.tick()
    assert store.current_world().get("server").phase == "crashed"  # the real sweep still runs after


class FakeTaskContainers:
    """A `TaskRuntime` stand-in for the ECS half of the projection, keyed by
    task id. `absent` is what `ColimaRuntime.status` answers for a container
    that no longer exists."""

    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses

    async def status(self, env, task_id, container_name):
        return self._statuses.get(task_id, "running")

    async def exit_code(self, env, task_id, container_name):
        return 0

    async def logs(self, env, task_id, container_name, tail=20):
        return ""


def _ecs_service_stores(tmp_path, task_ids):
    stores = SynthStores(tmp_path)
    taskdef_arn = "arn:aws:ecs:us-east-1:000000000000:task-definition/app:1"
    stores.ecsctl.set("default", "service:odin:app", {
        "cluster_name": "odin", "service_name": "app", "desired_count": len(task_ids),
        "status": "ACTIVE", "task_definition_arn": taskdef_arn,
    })
    for task_id in task_ids:
        stores.ecsctl.set("default", f"task:odin:{task_id}", {
            "cluster_name": "odin", "service_name": "app", "task_id": task_id, "container_name": "app",
            "last_status": "RUNNING", "stopped_reason": None, "exit_code": None, "stopped_at": None,
            "task_definition_arn": taskdef_arn,
        })
    return stores


async def test_a_task_container_removed_mid_apply_is_seen_within_one_tick(tmp_path, monkeypatch):
    """FIELD TEST 4, P4-4, re-measured after the `absent` fix.

    The drift SWEEP is cache-only during an apply (the test above), but the ECS
    half of observation never went through it: `tf_status.project` runs
    `ecsctl.sweep_tasks` LIVE on every observing tick, hold or no hold. That
    sweep used to recognise only `exited`/`dead`/`removing`, so a container
    that was GONE matched nothing and the task kept reading RUNNING for the
    rest of the apply — 57 seconds stale in the field test, 14 of them after
    the apply had already returned. Recognising `absent` closes it on the
    passive path: no `docker ps`, no record correction by the sweeper, just the
    per-task `status` call the projection was already making."""
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="app", kind="ecs"),)))
    stores = _ecs_service_stores(tmp_path, ["t1", "t2"])
    monkeypatch.setattr(
        tf_status, "TaskRuntime", lambda: FakeTaskContainers({"t1": "running", "t2": "running"}),
    )
    # The drift sweeper sees BOTH containers, so it is not what reports this.
    recon = Reconciler(
        store, rt, poll_interval=0, stores=stores,
        drift=_drift(container_names=[task_container_name("default", t, "app") for t in ("t1", "t2")]),
    )
    await recon.tick()
    assert store.current_world().get("app").phase == "healthy"

    async with recon.hold():  # an apply is now in flight, actions suspended
        monkeypatch.setattr(
            tf_status, "TaskRuntime", lambda: FakeTaskContainers({"t1": "running", "t2": "absent"}),
        )
        await recon.tick()

        observed = store.current_world().get("app")
        assert observed.phase == "crashed", "an out-of-band removal was invisible for the whole apply"
        assert stores.ecsctl.get("default", "task:odin:t2")["last_status"] == "STOPPED"


async def test_a_deleted_ec2_vm_is_NOT_seen_until_the_apply_releases(tmp_path):
    """The other half of P4-4, and the part that is still true: ec2/lambda/rds
    are checked ONLY by the drift sweep, which an in-flight apply reads from
    cache. Recorded as a test (and in ROADMAP's v1 limits) rather than fixed —
    a sweep CORRECTS records off a sample taken while tofu has the daemon
    pinned, which is the busy-daemon hazard `drift.py` exists to avoid."""
    rt = FakeRuntime()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="server", kind="ec2"),)))
    stores = _ec2_stores(tmp_path)
    recon = Reconciler(store, rt, poll_interval=0, stores=stores, drift=_drift(vm_names=["odin-ec2-default-i-1"]))
    await recon.tick()
    assert store.current_world().get("server").phase == "healthy"

    async with recon.hold():
        recon._drift._vms = FakeVms(names=[])  # the VM is deleted out of band, mid-apply
        for _ in range(30):  # 30 ticks — a whole apply's worth
            await recon.tick()
        assert store.current_world().get("server").phase == "healthy", "the sweep ran mid-apply"

    # ...and the tail the field test measured: the trailing tick both routes run
    # after the hold does NOT force a sweep either — the cadence counter never
    # advanced while suspended, so the truth lands on the next SWEEP, up to
    # `ODIN_DRIFT_SWEEP_TICKS` (10, ~10s) ticks later.
    await recon.tick()
    assert store.current_world().get("server").phase == "healthy"
    for _ in range(_sweep_ticks()):
        await recon.tick()
    assert store.current_world().get("server").phase == "crashed"


async def test_an_ordinary_apply_does_not_burst_deltas(tmp_path):
    """v0.7.1's regression guard, re-run against the observing tick: 60 watch
    ticks over a steady projection (an apply where nothing about this resource
    changes) must emit nothing at all — `_emit`'s dedupe is what keeps the
    freeze fix from turning into a badge storm."""
    rt, ws = FakeRuntime(), FakeWS()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(ResourceDesired(id="net", kind="vpc"),)))
    recon = Reconciler(store, rt, ws=ws, poll_interval=0, stores=_synthesized_sg_stores(tmp_path))
    await recon.tick()
    baseline = len(ws.sent)

    async with recon.hold():  # a ~60s apply at the production 1s poll interval
        for _ in range(60):
            await recon.tick()

    assert len(ws.sent) == baseline, [m for m in ws.sent[baseline:]]


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

    async def provision(self, service, name, subscriptions=()):
        # `await`, not `time.sleep`: a blocking sleep would pin the ONE event
        # loop and the two ticks below could never overlap -- the test would
        # pass without ever reproducing the race it exists for.
        await asyncio.sleep(0.05)
        await super().provision(service, name, subscriptions)


async def test_concurrent_ticks_do_not_double_provision(tmp_path):
    # /apply's tick overlaps the background loop's tick — unserialized, both
    # plan on the same pre-provision world and double-provision (live: two
    # `docker run` on one backing name). Two TASKS now rather than two threads;
    # `SlowAws.provision`'s await is the suspension point that lets the second
    # in, so the race is real and `_tick_lock` is what closes it.
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
    # The projection is revision-aware (field test 3), so both records carry
    # the task-definition arn a real CreateService/`_launch_task` pair writes.
    taskdef_arn = "arn:aws:ecs:us-east-1:000000000000:task-definition/app:1"
    stores.ecsctl.set("default", "service:odin:app", {
        "cluster_name": "odin", "service_name": "app", "desired_count": 1, "status": "ACTIVE",
        "task_definition_arn": taskdef_arn,
    })
    stores.ecsctl.set("default", "task:odin:t1", {
        "cluster_name": "odin", "service_name": "app", "task_id": "t1", "container_name": "app",
        "last_status": "RUNNING", "stopped_reason": None, "exit_code": None, "stopped_at": None,
        "task_definition_arn": taskdef_arn,
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


async def test_a_fact_arriving_after_the_phase_settles_still_reaches_world(tmp_path):
    """Field test 4: an rds node reaches `healthy` BEFORE its mesh join records
    an overlay_ip, so with facts outside change detection `DATABASE_URL_MESH`
    could never enter World on a first apply -- no later phase change ever came
    to carry it. That made README's central security advice (point VM consumers
    at the SG-gated `_MESH` ref) impossible to follow, silently leaving them on
    the ungated `_VM` path."""
    r = Reconciler(store=SpecStore(tmp_path), runtime=FakeRuntime(), env="e")

    await r._emit("db", "rds", "healthy", facts={"DATABASE_URL": "postgresql://x"})
    await r._emit("db", "rds", "healthy",
                  facts={"DATABASE_URL": "postgresql://x", "DATABASE_URL_MESH": "postgresql://10.42.1.3"})

    assert r._store.current_world("e").get("db").facts["DATABASE_URL_MESH"] == "postgresql://10.42.1.3"


async def test_an_unchanged_reading_still_emits_nothing(tmp_path):
    """The guard this fix loosened is what killed a 43%-of-all-events flap.
    Identical facts must stay silent, or that bug returns."""
    r = Reconciler(store=SpecStore(tmp_path), runtime=FakeRuntime(), env="e")
    facts = {"DATABASE_URL": "postgresql://x", "endpoint": "h:5432"}

    await r._emit("db", "rds", "healthy", facts=facts)
    assert r._store.current_world("e").get("db") is not None
    emitted = []
    r._store.apply_delta = lambda d: emitted.append(d)
    for _ in range(20):
        await r._emit("db", "rds", "healthy", facts=dict(facts))

    assert emitted == [], "20 identical readings must produce no deltas"


async def test_a_changing_logtail_alone_does_not_re_emit(tmp_path):
    """logtail is diagnostic, not identity: two reads of the same dead
    container can differ, and re-emitting for that is the noise the guard
    exists to stop."""
    r = Reconciler(store=SpecStore(tmp_path), runtime=FakeRuntime(), env="e")
    await r._emit("q", "sqs", "crashed", facts={"logtail": "line one"}, verdict="gone")
    emitted = []
    r._store.apply_delta = lambda d: emitted.append(d)
    await r._emit("q", "sqs", "crashed", facts={"logtail": "line one\nline two"}, verdict="gone")
    assert emitted == []


# --- field test 5 facts audit -------------------------------------------------


def _unreadable(reason: str = "docker cannot read odin-aws-rustfs-default's published ports: no daemon"):
    async def facts(service, name):
        raise BackingUnavailable(reason)
    return facts


async def test_a_backing_whose_port_cannot_be_read_is_not_published_healthy(tmp_path):
    """Fix 1, at the layer that DURABLY records the lie. `BackingAws.facts`
    raises rather than naming an endpoint it could not read, and a resource
    whose endpoint odin cannot name is not healthy. Publishing `healthy` +
    `http://host.docker.internal:0` was permanent corruption: these facts are
    written once, on this very transition, and never refreshed."""
    rt, aws, ws = FakeRuntime(), FakeAws(), FakeWS()
    aws.facts = _unreadable()
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))

    recon = Reconciler(store, rt, aws=aws, ws=ws, poll_interval=0)
    await recon.tick()   # provision -> starting
    await recon.tick()   # exists() is True, but the endpoint is unreadable

    observed = store.current_world().get("uploads")
    assert observed.phase == "starting", "an unreadable endpoint must not read healthy"
    assert observed.facts == {}
    assert "published ports" in observed.verdict   # the real reason, not a shrug
    assert ":0" not in str(ws.sent)                # nowhere, in any delta


async def test_a_persistent_unreadable_port_costs_exactly_one_delta(tmp_path):
    """The failure path must not become its own delta storm: `_emit`'s dedupe
    covers the verdict too, so twenty failing ticks are one event."""
    rt, aws = FakeRuntime(), FakeAws()
    aws.facts = _unreadable("unreadable")
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    await recon.tick()
    await recon.tick()

    emitted = []
    store.apply_delta = lambda d: emitted.append(d)
    for _ in range(20):
        await recon.tick()
    assert emitted == []


async def test_the_endpoint_is_published_once_the_port_becomes_readable_again(tmp_path):
    rt, aws = FakeRuntime(), FakeAws()
    aws.facts = _unreadable("unreadable")
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    await recon.tick()
    await recon.tick()

    async def readable(service, name):
        return {"BUCKET": name, "endpoint": "http://host.docker.internal:51001"}

    aws.facts = readable
    await recon.tick()

    observed = store.current_world().get("uploads")
    assert observed.phase == "healthy"
    assert observed.facts["endpoint"] == "http://host.docker.internal:51001"
    assert observed.verdict is None


# --- the fact-value invariant: strings, enforced at the emit boundary ---------


async def test_a_non_string_fact_value_is_refused_loudly(tmp_path):
    """The invariant was real and load-bearing but unwritten and unenforced.
    `prior.facts` is re-parsed from world.json on every `_emit`, so a value
    JSON reshapes never compares equal to itself again -- one delta per
    resource per tick, forever. Coercing silently would hide it; the whole
    point is that it cannot be introduced quietly."""
    r = Reconciler(store=SpecStore(tmp_path), runtime=FakeRuntime(), env="e")
    with pytest.raises(TypeError, match=r"ports=\[80, 443\] \(list\)"):
        await r._emit("lb", "alb", "healthy", facts={"ports": [80, 443]})
    for value in ((80, 443), 8080, None, {"a": "b"}, True):
        with pytest.raises(TypeError, match="must be str"):
            await r._emit("lb", "alb", "healthy", facts={"x": value})
    assert r._store.current_world("e").get("lb") is None, "nothing durable was written"


async def test_the_guard_names_the_resource_and_every_offending_key(tmp_path):
    r = Reconciler(store=SpecStore(tmp_path), runtime=FakeRuntime(), env="e")
    with pytest.raises(TypeError) as raised:
        await r._emit("db", "rds", "healthy", facts={"PORT": 5432, "HOST": "h", "TAGS": ("a",)})
    message = str(raised.value)
    assert message.startswith("db: World fact values must be str")
    assert "PORT=5432 (int)" in message and "TAGS=('a',) (tuple)" in message
    assert "HOST" not in message  # the good one isn't blamed


async def test_a_tuple_valued_fact_would_have_stormed_one_delta_per_tick(tmp_path):
    """The measurement behind the guard, run against the real store: JSON turns
    a tuple into a list, so the round-tripped `prior` never matches what the
    next tick builds. Written as the demonstration it is -- bypassing `_emit`'s
    guard to show what it prevents."""
    r = Reconciler(store=SpecStore(tmp_path), runtime=FakeRuntime(), env="e")
    r._store.apply_delta(WorldDelta(env="e", resource_id="lb", kind="alb",
                                    phase="healthy", facts={"ports": ("80", "443")}))
    prior = r._store.current_world("e").get("lb")
    assert prior.facts["ports"] == ["80", "443"], "a tuple round-trips as a list"
    assert prior.facts != {"ports": ("80", "443")}, "…so it never equals itself again"
    # …and the guard is what stops that shape from ever reaching the store.
    with pytest.raises(TypeError):
        await r._emit("lb", "alb", "healthy", facts={"ports": ("80", "443")})


# --- a crash must not destroy the resource's identity -------------------------


class _PortOnlyRuntime:
    """Just enough runtime for the REAL `BackingAws.facts` to run, so the
    identity facts under test are the ones production publishes rather than a
    hand-written approximation of them."""

    async def host_port(self, name, container_port):
        return 51001


async def _real_backing_facts(tmp_path, service, name):
    return await BackingAws(_PortOnlyRuntime(), env="default", root=tmp_path).facts(service, name)


async def test_a_crashed_resource_keeps_its_identity_and_gains_a_logtail(tmp_path):
    """Field test 5's facts audit, hazard 4. The crash branch replaced facts
    WHOLESALE with `{"logtail": …}`, so a crashed bucket's `BUCKET` and
    `endpoint` were destroyed in World -- and anything resolving
    `${{uploads.BUCKET}}` saw the value vanish the moment the backing
    hiccuped, restored only by a full starting->healthy round trip. A crashed
    resource still IS that bucket."""
    rt, aws, ws = FakeRuntime(), FakeAws(), FakeWS()
    rt.set("odin-aws-s3-default", "running", logs="panic: disk full")
    async def facts(service, name):
        return await _real_backing_facts(tmp_path, service, name)
    aws.facts = facts
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))

    recon = Reconciler(store, rt, aws=aws, ws=ws, poll_interval=0)
    await recon.tick()   # provision -> starting
    await recon.tick()   # -> healthy, with the real identity facts
    healthy = store.current_world().get("uploads").facts
    assert healthy["BUCKET"] == "uploads" and healthy["endpoint"].endswith(":51001")

    aws.exists = _gone   # the backing lost the resource
    await recon.tick()   # observe demotes to crashed, then plan re-provisions

    crashed = next(m for m in ws.sent if m.get("resource_id") == "uploads"
                   and m.get("phase") == "crashed")
    assert crashed["verdict"] == "the s3 backing is no longer reachable"
    assert crashed["facts"]["logtail"] == "panic: disk full"    # the diagnosis arrives
    assert crashed["facts"]["BUCKET"] == healthy["BUCKET"]      # …and identity survives it
    assert crashed["facts"]["endpoint"] == healthy["endpoint"]


async def test_preserving_identity_across_a_crash_costs_no_extra_deltas(tmp_path):
    """The churn check the fix has to pass. `logtail` is excluded from change
    detection (`_VOLATILE_FACTS`) and the identity half is by construction
    equal to what World already holds, so a resource re-read as crashed emits
    once and then stays silent however many reads follow -- even as the log
    tail keeps changing under it."""
    r = Reconciler(store=SpecStore(tmp_path), runtime=FakeRuntime(), env="e")
    facts = await _real_backing_facts(tmp_path, "s3", "uploads")
    await r._emit("uploads", "s3", "healthy", facts=facts)
    await r._emit("uploads", "s3", "crashed", verdict="gone",
                  facts=r._crash_facts("uploads", "panic: disk full"))
    assert r._store.current_world("e").get("uploads").facts["BUCKET"] == "uploads"

    emitted = []
    r._store.apply_delta = lambda d: emitted.append(d)
    for read in range(30):
        await r._emit("uploads", "s3", "crashed", verdict="gone",
                      facts=r._crash_facts("uploads", f"panic: disk full\nretry {read}"))
    assert emitted == [], "30 reads of a settled crash must emit nothing"


async def test_many_ticks_over_a_steady_healthy_env_emit_nothing(tmp_path):
    """The storm re-confirmation for the whole set of fixes: a settled env
    stays silent. Field test 5 measured a 3-resource apply at exactly the
    6-event minimum and 90s idle at zero new bytes -- none of these changes
    may cost that."""
    rt, aws = FakeRuntime(), FakeAws()
    async def facts(service, name):
        return await _real_backing_facts(tmp_path, service, name)
    aws.facts = facts
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(
        ResourceDesired(id="uploads", kind="s3"),
        ResourceDesired(id="jobs", kind="sqs"),
        ResourceDesired(id="alerts", kind="sns"),
    )))
    recon = Reconciler(store, rt, aws=aws, poll_interval=0)
    await recon.tick()   # 3 x starting
    await recon.tick()   # 3 x healthy

    emitted = []
    store.apply_delta = lambda d: emitted.append(d)
    for _ in range(45):  # 45 ticks ~= 90s at the real 2s poll interval
        await recon.tick()
    assert emitted == []


# --- loop liveness: a reconciler that stopped converging must not look
# --- healthy (the CancelledError-is-a-BaseException hole) -------------------


def _fresh(tmp_path, **kwargs) -> Reconciler:
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))
    return Reconciler(store, FakeRuntime(), aws=FakeAws(), **kwargs)


async def _await_a_tick(recon: Reconciler, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while (await recon.health()).ticks == 0:
        assert time.monotonic() < deadline, "the loop never completed a tick"
        await asyncio.sleep(0.01)


async def test_a_live_loop_reports_ticking_with_a_real_completed_tick_behind_it(tmp_path):
    recon = _fresh(tmp_path, poll_interval=0.01)
    await recon.start()
    try:
        await _await_a_tick(recon)
        health = await recon.health()
        assert health.ticking is True
        assert health.verdict is None
        assert health.consecutive_failures == 0
        assert health.last_error is None
        assert health.last_tick_seconds is not None  # a measured duration, not a guess
        assert health.last_tick_seconds_ago is not None
    finally:
        await recon.stop()


async def test_a_cancelled_loop_is_reported_dead_instead_of_silently_stopping(tmp_path):
    """THE bug this landed for. `asyncio.CancelledError` inherits from
    BaseException, so it walked straight through `_run`'s `except Exception`
    and out of the coroutine with no log line, and nothing ever inspected the
    task afterwards -- `/world` kept serving its last snapshot and `odin
    status` said "running" while nothing converged. A REAL cancel of the REAL
    task is the signal here; nothing is fabricated."""
    recon = _fresh(tmp_path, poll_interval=0.01)
    await recon.start()
    await _await_a_tick(recon)
    task = recon._task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    health = await recon.health()
    assert health.ticking is False
    assert "CANCELLED" in health.verdict
    assert "is NOT converging" in health.verdict
    assert "odin stop && odin start" in health.verdict  # the remedy, named
    assert health.ticks >= 1  # it did work before it died -- say so, don't erase it


async def test_a_loop_whose_every_tick_raises_stops_reading_as_converging(tmp_path):
    """The narrower live-but-useless case: a tick that fails every time was
    logged and swallowed forever, which looked identical to a healthy loop.
    The failure here is REAL -- a corrupt `world.json`, which is what a
    truncated write or a hand-edit leaves behind -- so `current_world()`
    genuinely raises on every pass."""
    recon = _fresh(tmp_path, poll_interval=0.01, stall_after=0.05)
    (tmp_path / "default" / "world.json").write_text("{not json")
    await recon.start()
    try:
        deadline = time.monotonic() + 2.0
        while (await recon.health()).consecutive_failures < 2:
            assert time.monotonic() < deadline, "the loop never recorded a failing tick"
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.06)  # past `stall_after`, with the loop still alive

        health = await recon.health()
        assert health.ticking is False
        assert health.ticks == 0
        assert health.consecutive_failures >= 2
        assert "have all raised" in health.verdict
        assert "ValidationError" in health.verdict or "JSONDecodeError" in health.verdict
        # Field test 6's advice sweep: the remedy follows the REASON. This
        # verdict used to end "Restart odin (`odin stop && odin start`) to bring
        # it back" -- which is right for a cancelled or dead task and wrong for
        # this one: a fresh process re-reads the same corrupt file and raises on
        # tick 1 forever (measured on a real server -- 20+ consecutive failures
        # with the file unchanged, because every writer reads before it writes).
        assert "odin stop && odin start" not in health.verdict
        assert "a restart re-reads the same inputs and will raise again" in health.verdict
        assert "world.json" in health.verdict, "the reason has to name the file to be actionable"
        assert "odin events --env default" in health.verdict
    finally:
        recon._task.cancel()
        await asyncio.gather(recon._task, return_exceptions=True)


async def test_a_hung_tick_is_reported_stalled_while_the_task_is_still_alive(tmp_path):
    recon = _fresh(tmp_path, poll_interval=0.01, stall_after=0.05)
    entered = asyncio.Event()

    async def _never_returns(_stack):
        entered.set()
        await asyncio.Event().wait()

    recon._observe = _never_returns
    await recon.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        await asyncio.sleep(0.06)
        health = await recon.health()
        assert health.ticking is False
        assert recon._task.done() is False  # alive, and still not converging
        assert "a tick is hung" in health.verdict
    finally:
        recon._task.cancel()
        await asyncio.gather(recon._task, return_exceptions=True)


async def test_a_slow_but_completing_tick_is_not_called_stalled(tmp_path):
    """The false-positive direction: `stall_after` must measure COMPLETED
    ticks, not tick duration -- a loop doing slow real work (a live drift
    sweep shelling out to limactl/docker) is converging."""
    recon = _fresh(tmp_path, poll_interval=0.01, stall_after=0.5)
    await recon.start()
    try:
        await _await_a_tick(recon)
        for _ in range(5):
            await asyncio.sleep(0.05)
            assert (await recon.health()).ticking is True
    finally:
        await recon.stop()


async def test_a_stopped_loop_says_so_rather_than_claiming_to_tick(tmp_path):
    recon = _fresh(tmp_path, poll_interval=0.01)
    await recon.start()
    await _await_a_tick(recon)
    await recon.stop()
    health = await recon.health()
    assert health.ticking is False
    assert "it has been stopped" in health.verdict


async def test_a_never_started_loop_is_not_reported_as_ticking(tmp_path):
    health = await _fresh(tmp_path).health()
    assert health.ticking is False
    assert "it was never started" in health.verdict
    assert health.last_tick_seconds_ago is None
