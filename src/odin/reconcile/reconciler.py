"""The control loop: observe reality, plan, execute, repeat.

Each tick: (1) observe — refresh the World from runtime facts + assertions,
advancing started resources to healthy/crashed; (2) plan(Stack, World) → Actions;
(3) execute — provision/stop; (4) project the gateway's TF-owned resources
(vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr/logs/rds) into World too (fix-wave
2b finding #1 -- see reconcile/tf_status.py), cross-checked against REALITY on
a bounded cadence (W2.2 -- see reconcile/drift.py: a VM/container/database
deleted out of band projects `crashed` with a verdict instead of a permanent
stale `healthy`).
The pure plan() decides intent for the AWS-shaped PROVISIONED resources only;
this executor builds specs (resolving refs via the Fabric) and runs them;
TF_OWNED_KINDS never go through plan/execute at all -- tofu is their sole
creator/destroyer, this loop only OBSERVES what tofu already did.

W2.7 retired the last non-TF path here: `rds` was provisioned and supervised
by this loop (its own `_observe_rds`, its own `pg_ready` seam, its own
crash-clear-and-recreate). All three moved -- creation to tofu's
CreateDBInstance (`gateway/models/rdsctl.py`), the `pg_ready` assertion into
that model's create waiter, and the crash/recover story into the reality
sweep + an Apply-driven `converge_db_instances`, the shape W2.2 already
established for ecs/lambda. What's left of rds in this file is the same thing
that's left of every TF-owned kind: projection and pruning.

Per-node credential injection (a workload container's env bound to
keystore-issued creds + the gateway endpoint, formerly `_run_service`) is
deferred with the app-workload layer (NORTHSTAR.md, tag app-layer-parked) —
it returns here when workload nodes do. What tick() does today: push
(compiled policies, backing ports) into GatewayState every pass so the
gateway enforces against whatever Stack is applied, even before anything
dials in through it.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from odin.aws.backings import ENSURE_KINDS, PROVISIONED
from odin.fabric.localhost import LocalhostFabric
from odin.gateway.policy import compile_policies
from odin.gateway.stores import SynthStores
from odin.reconcile.actions import NoOp, ProvisionResource, StopContainer
from odin.reconcile.drift import DriftSweeper
from odin.reconcile.plan import plan
from odin.reconcile.tf_status import TF_OWNED_KINDS, project as project_tf_owned
from odin.spec.models import ResourceDesired, Stack, World, WorldDelta

log = logging.getLogger("odin.reconcile")


class Reconciler:
    def __init__(
        self,
        store,
        runtime,
        aws=None,
        gateway=None,
        fabric: LocalhostFabric | None = None,
        ws=None,
        env: str = "default",
        poll_interval: float = 2.0,
        stores: SynthStores | None = None,
        drift: DriftSweeper | None = None,
    ) -> None:
        self._store = store
        self._rt = runtime
        self._aws = aws
        self._gateway = gateway
        self._fabric = fabric or LocalhostFabric()
        self._ws = ws
        self._env = env
        self._poll = poll_interval
        # The gateway's synth stores (tags/ec2net/iamctl/ecr/ec2compute/
        # lambdactl/ecsctl) -- read-only here, for the TF-owned-status
        # projection (see tick()'s trailing step + tf_status.py). None in
        # every test that doesn't care; server.py's real wiring always
        # passes the SAME SynthStores instance the gateway itself uses.
        self._stores = stores
        # W2.2's reality sweep (reconcile/drift.py) -- the same optional-seam
        # shape as `stores`/`aws`/`gateway`: None means "no reality check"
        # (every unit test that hand-seeds synth records and doesn't want a
        # real `limactl list`/`docker ps` leaves it out), and create_app's
        # real, runtime-backed wiring always passes one.
        self._drift = drift
        self._task: asyncio.Task | None = None
        self._stop = False
        # tick() is called by BOTH the background loop and the /apply//destroy
        # endpoints; it yields at every to_thread, so unserialized ticks plan
        # on the same pre-execute world and double-run containers.
        self._tick_lock = asyncio.Lock()

    # ---- lifecycle ----
    async def start(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop:
            try:
                await self.tick()
            except Exception:  # a control loop must survive a bad tick
                log.exception("reconciler tick failed")
            await asyncio.sleep(self._poll)

    @asynccontextmanager
    async def hold(self):
        """Block the background loop's ticks while an external author
        mutates the env (the /apply-full route: ensure backings + tofu +
        store commit). Without this, a tick against the not-yet-committed
        stack gc's the very backing containers the ensure phase is booting
        (fresh env: old stack is empty, so gc(set()) stops everything)."""
        async with self._tick_lock:
            yield

    async def tick(self) -> None:
        async with self._tick_lock:
            stack = self._store.get_stack(self._env)
            await self._observe(stack)
            world = self._store.current_world(self._env)
            for action in plan(stack, world):
                await self._execute(action, stack)
            if self._aws is not None:  # stop backings no active kind needs anymore
                await asyncio.to_thread(self._aws.gc, {r.kind for r in stack.resources})
            if self._gateway is not None:  # policies/ports always track the applied Stack
                ports = await asyncio.to_thread(self._aws.backing_ports) if self._aws is not None else {}
                self._gateway.update(self._env, compile_policies(stack), ports)
            if self._stores is not None:  # fix-wave 2b finding #1: project tofu's own creations into World
                await self._project_tf_owned()

    async def _project_tf_owned(self) -> None:
        """`tf_status.project()` is the whole snapshot; diff it against the
        current World and emit only what changed (`_emit`'s own dedupe) plus
        prune any label that dropped out (tofu destroyed it -- this loop
        never destroys a TF-owned resource itself).

        W2.2: the drift sweep runs FIRST, deliberately -- its ecs half writes
        reality back into the task records (reconcile/drift.py's module
        docstring), so a container removed outside odin surfaces on THIS
        tick's projection rather than the next one; its ec2/lambda half comes
        back as a `label -> verdict` overlay that turns a record still
        claiming to be up into an honest `crashed` + why. `_emit`'s existing
        crashed path is what then broadcasts both the WorldDelta and the
        `type:"log"` error line -- drift needs no pipeline of its own."""
        drifted = await self._drift_verdicts()
        projected = await asyncio.to_thread(project_tf_owned, self._stores, self._env)
        for label, (kind, phase, facts, verdict) in projected.items():
            phase, verdict = ("crashed", drifted[label]) if label in drifted else (phase, verdict)
            await self._emit(label, kind, phase, facts=facts, verdict=verdict)
        world = self._store.current_world(self._env)
        for observed in world.resources:
            if observed.kind in TF_OWNED_KINDS and observed.id not in projected:
                await self._prune(observed.id)

    async def _drift_verdicts(self) -> dict[str, str]:
        if self._drift is None:
            return {}
        return await asyncio.to_thread(self._drift.verdicts, self._stores, self._env)

    async def ensure_backings(self, stack: Stack) -> None:
        """Boot (but don't create any resource on) the backing containers
        `stack`'s AWS-shaped kinds need, and register their ports + this
        Stack's compiled policies with the gateway -- WITHOUT running
        plan/execute. `ensure_backing` only starts a container; it never
        touches the resources inside it, so this is safe to call ahead of an
        external creator racing this same Stack (S5's /apply-full: tofu
        authors the actual resources through the gateway after this call).
        Without it, a never-before-applied env has no registered
        `backing_port`, the gateway 503s every forward, and tofu's own
        AWS-provider retry/backoff turns that into a long, opaque hang
        instead of a request-scoped failure.

        Uses ENSURE_KINDS (not the narrower PROVISIONED): "ecr" needs its
        registry:2 CONTAINER booted here too (V2b), even though its actual
        resource CRUD never runs through this instance's client()-based
        provision/exists/deprovision -- CreateRepository's very first call
        (via tofu, through the gateway) needs the registry's live port
        already resolvable to build `repositoryUri`."""
        if self._aws is None:
            return
        kinds = {r.kind for r in stack.resources if r.kind in ENSURE_KINDS}
        await asyncio.gather(*(asyncio.to_thread(self._aws.ensure_backing, k) for k in kinds))
        if self._gateway is not None:
            ports = await asyncio.to_thread(self._aws.backing_ports)
            self._gateway.update(self._env, compile_policies(stack), ports)

    # ---- helpers ----
    def _kind_of(self, stack: Stack, rid: str) -> str | None:
        return next((r.kind for r in stack.resources if r.id == rid), None)

    def _desired_subs(self, stack: Stack, sns_id: str) -> tuple[str, ...]:
        """An sns→sqs canvas edge is a subscription: the queues this topic
        must fan out to."""
        return tuple(e.dst for e in stack.edges
                     if e.src == sns_id and self._kind_of(stack, e.dst) == "sqs")

    async def _emit(self, rid, kind, phase, facts=None, verdict=None) -> None:
        # Skip unchanged status: observe runs every tick and the cpu/ram facts
        # fluctuate, but only a phase/verdict CHANGE is worth a WorldDelta — else
        # the event log + WS + events.jsonl fill with identical "healthy" noise.
        prior = self._store.current_world(self._env).get(rid)
        if prior is not None and prior.phase == phase and prior.verdict == verdict:
            return
        delta = WorldDelta(
            env=self._env, resource_id=rid, kind=kind, phase=phase,
            facts=facts or {}, verdict=verdict,
        )
        self._store.apply_delta(delta)
        if self._ws is not None:
            await self._ws.broadcast(delta.model_dump())
            if phase == "crashed":
                # Observability v1: push the failure's own log tail the moment
                # it happens, matching the `type:"log"` shape the UI's Logs
                # tab already parses (BottomPanel.tsx) -- fetch-on-demand via
                # /logs covers everything else; this is the "don't make the
                # user go looking" half for the resource that just died.
                await self._ws.broadcast(self._log_message(rid, facts or {}, verdict))

    def _log_message(self, rid: str, facts: dict, verdict: str | None) -> dict:
        text = "\n".join(part for part in (verdict, facts.get("logtail")) if part) or f"{rid} crashed"
        return {"type": "log", "env": self._env, "text": text, "source": rid, "level": "error"}

    # ---- observe ----
    async def _observe(self, stack: Stack) -> None:
        world = self._store.current_world(self._env)
        for res in stack.resources:
            observed = world.get(res.id)
            if observed is None:
                continue
            if res.kind in PROVISIONED and observed.phase in ("starting", "healthy"):
                await self._observe_provisioned(stack, res, observed.phase)

    async def _observe_provisioned(self, stack: Stack, res: ResourceDesired, phase: str) -> None:
        """s3/sqs/sns/dynamodb: healthy once the resource exists in its backing;
        a healthy one whose backing lost it demotes to crashed (plan's existing
        pending/crashed branch then re-provisions).

        sns additionally re-diffs its subscriptions here: plan() NoOps any
        healthy resource, so a live canvas edit that adds an sns→sqs edge to
        an already-healthy topic can only take effect on this observe pass.
        provision()'s sns branch is idempotent (create_topic returns the
        existing ARN; duplicate subscribes are swallowed), so re-provisioning
        just the missing queues is safe. Skipped when the topic doesn't
        currently exist (`not ok`): plan's pending/crashed path owns that."""
        ok = await asyncio.to_thread(self._aws.exists, res.kind, res.id)
        if phase == "starting" and ok:
            facts = await asyncio.to_thread(self._aws.facts, res.kind, res.id)
            await self._emit(res.id, res.kind, "healthy", facts=facts)
        if phase == "healthy" and not ok:
            logtail = await asyncio.to_thread(self._backing_logtail, res.kind)
            await self._emit(
                res.id, res.kind, "crashed",
                facts={"logtail": logtail} if logtail else {},
                verdict=f"the {res.kind} backing is no longer reachable",
            )
        if res.kind != "sns" or not ok:
            return
        actual = await asyncio.to_thread(self._aws.subscriptions, res.id)
        missing = tuple(q for q in self._desired_subs(stack, res.id) if q not in actual)
        if missing:
            await asyncio.to_thread(self._aws.provision, "sns", res.id, missing)

    def _backing_logtail(self, kind: str) -> str:
        """A short tail off the real backing container for a crash verdict --
        `container_name` is only on the real `BackingAws` (test doubles that
        don't implement it just skip the tail, never crash the observe pass
        over an optional diagnostic extra)."""
        container_name = getattr(self._aws, "container_name", None)
        return self._rt.logs(container_name(kind)) if container_name is not None else ""

    # ---- execute ----
    async def _execute(self, action, stack: Stack) -> None:
        if isinstance(action, ProvisionResource):
            # Only ever an AWS-shaped resource in a shared backing
            # (s3/sqs/sns/dynamodb) -- plan() emits nothing else (W2.7).
            subs = self._desired_subs(stack, action.id) if action.service == "sns" else ()
            await asyncio.to_thread(self._aws.provision, action.service, action.id, subs)
            await self._emit(action.id, action.service, "starting")
        elif isinstance(action, StopContainer):
            if action.kind in PROVISIONED:
                await asyncio.to_thread(self._aws.deprovision, action.kind, action.id)
            elif action.kind in TF_OWNED_KINDS:
                # tofu (never this reconciler) owns create/destroy for a
                # TF-managed kind -- a StopContainer here only means a stale
                # World entry (the canvas node was removed but tofu hasn't
                # destroyed the real resource yet); `action.name` is a
                # label, not a real container name, so `self._rt.stop`
                # would be a no-op at best. Just let the prune below clear
                # the stale entry; the NEXT tick's projection re-adds it
                # (still accurately) if the real resource is still there.
                pass
            else:
                self._rt.stop(action.name)
            await self._prune(action.id)
        elif isinstance(action, NoOp):
            pass  # nothing left to gate on now that workload refs are gone

    async def _prune(self, rid: str) -> None:
        world = self._store.current_world(self._env)
        gone = world.get(rid)
        kept = World(env=world.env, resources=tuple(r for r in world.resources if r.id != rid))
        self._store.write_world(kept)
        # Tell the canvas the node is back to draft, else its tile stays stale-green
        # after a Destroy / node removal (the World emptied but the UI never heard).
        if self._ws is not None and gone is not None:
            await self._ws.broadcast({
                "type": "world_delta", "env": self._env, "resource_id": rid,
                "kind": gone.kind, "phase": "draft", "facts": {}, "verdict": None,
            })
