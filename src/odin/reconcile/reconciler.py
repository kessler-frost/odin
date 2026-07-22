"""The control loop: observe reality, plan, execute, repeat.

Each tick: (1) observe — refresh the World from runtime facts + assertions,
advancing started resources to healthy/crashed; (2) plan(Stack, World) → Actions;
(3) execute — provision/stop. The pure plan() decides intent; this executor
builds specs (resolving refs via the Fabric) and runs them. Scope: rds + the
AWS-shaped PROVISIONED resources, single host.

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

from odin.aws.backings import PROVISIONED
from odin.fabric.localhost import LocalhostFabric
from odin.gateway.policy import compile_policies
from odin.reconcile import assertions
from odin.reconcile.actions import NoOp, ProvisionResource, StopContainer
from odin.reconcile.plan import plan
from odin.runtime.colima import CONTAINER_HOST
from odin.spec.models import ResourceDesired, Stack, World, WorldDelta

log = logging.getLogger("odin.reconcile")


class Reconciler:
    def __init__(
        self,
        store,
        runtime,
        rds,
        aws=None,
        gateway=None,
        fabric: LocalhostFabric | None = None,
        ws=None,
        env: str = "default",
        pg_ready=assertions.pg_ready,
        poll_interval: float = 2.0,
    ) -> None:
        self._store = store
        self._rt = runtime
        self._rds = rds
        self._aws = aws
        self._gateway = gateway
        self._fabric = fabric or LocalhostFabric()
        self._ws = ws
        self._env = env
        self._pg_ready = pg_ready
        self._poll = poll_interval
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

    # ---- helpers ----
    def _res(self, stack: Stack, rid: str) -> ResourceDesired:
        return next(r for r in stack.resources if r.id == rid)

    def _kind_of(self, stack: Stack, rid: str) -> str | None:
        return next((r.kind for r in stack.resources if r.id == rid), None)

    def _creds(self, res: ResourceDesired) -> tuple[str, str]:
        user = res.fields["username"].value if "username" in res.fields else "app"
        pw = res.fields["password"].value if "password" in res.fields else "apppass123"
        return str(user), str(pw)

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

    # ---- observe ----
    async def _observe(self, stack: Stack) -> None:
        world = self._store.current_world(self._env)
        for res in stack.resources:
            observed = world.get(res.id)
            if observed is None:
                continue
            if res.kind == "rds" and observed.phase in ("starting", "healthy"):
                await self._observe_rds(res)
            elif res.kind in PROVISIONED and observed.phase in ("starting", "healthy"):
                await self._observe_provisioned(res, observed.phase)

    async def _observe_provisioned(self, res: ResourceDesired, phase: str) -> None:
        """s3/sqs/sns/dynamodb: healthy once the resource exists in its backing;
        a healthy one whose backing lost it demotes to crashed (plan's existing
        pending/crashed branch then re-provisions)."""
        ok = await asyncio.to_thread(self._aws.exists, res.kind, res.id)
        if phase == "starting" and ok:
            facts = await asyncio.to_thread(self._aws.facts, res.kind, res.id)
            await self._emit(res.id, res.kind, "healthy", facts=facts)
        if phase == "healthy" and not ok:
            await self._emit(res.id, res.kind, "crashed")

    async def _observe_rds(self, res: ResourceDesired) -> None:
        cname = self._rds.container_name(res.id)
        if self._rt.facts(cname).phase == "crashed":
            # Clear the dead container so the recreate boots a fresh Postgres.
            await asyncio.to_thread(self._rds.delete_db, res.id)
            await self._emit(res.id, "rds", "crashed")
            return
        endpoint = self._rds.endpoint(res.id)
        if endpoint is None:
            return  # still creating
        user, pw = self._creds(res)
        if await self._pg_ready(endpoint[0], endpoint[1], user, pw):  # host-side probe
            # Publish a CONTAINER-reachable address: a consumer gets this verbatim
            # as DATABASE_URL, and "localhost" inside a container is the container
            # itself, not the Mac. host.docker.internal is the host (same as AWS).
            addr = f"{CONTAINER_HOST}:{endpoint[1]}"
            url = f"postgresql://{user}:{pw}@{addr}/postgres"
            stats = self._rt.stats(cname)
            await self._emit(
                res.id, "rds", "healthy",
                facts={"DATABASE_URL": url, "endpoint": addr, **stats},
            )

    # ---- execute ----
    async def _execute(self, action, stack: Stack) -> None:
        if isinstance(action, ProvisionResource):
            res = self._res(stack, action.id)
            if action.service == "rds":
                user, pw = self._creds(res)
                await asyncio.to_thread(self._rds.create_db, action.id, user, pw)
                await self._emit(action.id, "rds", "starting")
            else:  # AWS-shaped resource in a shared backing (s3/sqs/sns/dynamodb)
                # An sns→sqs canvas edge is a subscription: fan the topic out to
                # those queues at provision time.
                subs = tuple(
                    e.dst for e in stack.edges
                    if e.src == action.id and self._kind_of(stack, e.dst) == "sqs"
                ) if action.service == "sns" else ()
                await asyncio.to_thread(self._aws.provision, action.service, action.id, subs)
                await self._emit(action.id, action.service, "starting")
        elif isinstance(action, StopContainer):
            if action.kind == "rds":
                self._rds.delete_db(action.id)  # stop the DB container so re-apply re-boots
                self._rt.stop(self._rds.container_name(action.id))
            elif action.kind in PROVISIONED:
                await asyncio.to_thread(self._aws.deprovision, action.kind, action.id)
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
