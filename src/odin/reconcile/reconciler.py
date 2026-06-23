"""The control loop: observe reality, plan, execute, repeat.

Each tick: (1) observe — refresh the World from runtime facts + assertions,
advancing started resources to healthy/crashed; (2) plan(Stack, World) → Actions;
(3) execute — create/run/stop. The pure plan() decides intent; this executor
builds specs (resolving refs via the Fabric) and runs them. Skeleton scope:
service + rds, single host, no scheduler.
"""
from __future__ import annotations

import asyncio
import logging
import time

from odin.aws.embed import CONTAINER_HOST
from odin.aws.provision import PROVISIONED
from odin.fabric.localhost import LocalhostFabric, Unresolved
from odin.reconcile import assertions
from odin.reconcile.actions import (
    CreateMiniStackResource,
    NoOp,
    RunContainer,
    StopContainer,
)
from odin.reconcile.plan import plan
from odin.reconcile.probes import ProbeEngine
from odin.runtime.colima import ContainerSpec
from odin.spec.models import ResourceDesired, Stack, World, WorldDelta

log = logging.getLogger("odin.reconcile")


class Reconciler:
    def __init__(
        self,
        store,
        runtime,
        rds,
        aws=None,
        fabric: LocalhostFabric | None = None,
        ws=None,
        env: str = "default",
        scheduler=None,
        aws_env=None,
        http_ok=assertions.http_ok,
        pg_ready=assertions.pg_ready,
        tcp_ok=assertions.tcp_open,
        ref_timeout: float = 30.0,
        poll_interval: float = 2.0,
    ) -> None:
        self._store = store
        self._rt = runtime
        self._rds = rds
        self._aws = aws
        self._scheduler = scheduler
        self._aws_env = aws_env
        self._fabric = fabric or LocalhostFabric()
        self._ws = ws
        self._env = env
        self._http_ok = http_ok
        self._pg_ready = pg_ready
        self._probes = ProbeEngine(http_ok, tcp_ok)
        self._ref_timeout = ref_timeout
        self._poll = poll_interval
        self._blocked_since: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._stop = False

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
        stack = self._store.get_stack(self._env)
        await self._observe(stack)
        world = self._store.current_world(self._env)
        for action in plan(stack, world):
            await self._execute(action, stack)

    # ---- helpers ----
    def _res(self, stack: Stack, rid: str) -> ResourceDesired:
        return next(r for r in stack.resources if r.id == rid)

    def _creds(self, res: ResourceDesired) -> tuple[str, str]:
        user = res.fields["username"].value if "username" in res.fields else "app"
        pw = res.fields["password"].value if "password" in res.fields else "apppass123"
        return str(user), str(pw)

    def _port(self, res: ResourceDesired) -> int:
        return int(res.fields["port"].value) if "port" in res.fields else 8000

    async def _emit(self, rid, kind, phase, facts=None, verdict=None) -> None:
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
            elif res.kind in ("service", "dep", "llm") and observed.phase in ("starting", "healthy"):
                await self._observe_container(res)
            elif res.kind == "batch" and observed.phase == "running":
                await self._observe_batch(res)
            elif res.kind in PROVISIONED and observed.phase == "starting":
                if await asyncio.to_thread(self._aws.exists, res.kind, res.id):
                    await self._emit(res.id, res.kind, "healthy")

    async def _observe_rds(self, res: ResourceDesired) -> None:
        cname = self._rds.container_name(res.id)
        if self._rt.facts(cname).phase == "crashed":
            # Clear MiniStack's stale record so the recreate boots a fresh DB
            # (else create_db sees AlreadyExists and the DB never recovers).
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

    async def _observe_container(self, res: ResourceDesired) -> None:
        """service / dep / llm: healthy when the kind's probe passes (Assertion
        Engine). Publishes a referenceable endpoint as World facts."""
        facts = self._rt.facts(res.id, container_port=self._port(res))
        if facts.phase != "starting":
            await self._emit(res.id, res.kind, "crashed")  # exited / removed
            return
        if not facts.host_port:
            return
        if not await self._probes.healthy(res.kind, facts.host_port):
            return  # still booting
        published = {
            "host_port": facts.host_port, "cpu": facts.cpu, "ram": facts.ram,
            "endpoint": (f"http://127.0.0.1:{facts.host_port}/" if res.kind == "service"
                         else f"127.0.0.1:{facts.host_port}"),
        }
        if res.kind in ("dep", "llm"):  # referenceable HOST/PORT for consumers
            published.update({"HOST": "127.0.0.1", "PORT": facts.host_port})
        await self._emit(res.id, res.kind, "healthy", facts=published)

    async def _observe_batch(self, res: ResourceDesired) -> None:
        status = self._rt.status(res.id)
        if status == "running":
            return  # still executing
        code = self._rt.exit_code(res.id) if status in ("exited", "dead") else -1
        if code == 0:
            await self._emit(res.id, "batch", "done")
        else:
            await self._emit(res.id, "batch", "error", verdict=f"exit {code}")

    # ---- execute ----
    async def _execute(self, action, stack: Stack) -> None:
        if isinstance(action, CreateMiniStackResource):
            res = self._res(stack, action.id)
            if action.service == "rds":
                user, pw = self._creds(res)
                await asyncio.to_thread(self._rds.create_db, action.id, user, pw)
                await self._emit(action.id, "rds", "starting")
            else:  # control-plane AWS resource (s3/sqs/sns/dynamodb)
                await asyncio.to_thread(self._aws.provision, action.service, action.id)
                await self._emit(action.id, action.service, "starting")
        elif isinstance(action, RunContainer):
            await self._run_service(stack, action.id)
        elif isinstance(action, StopContainer):
            if action.kind == "rds":
                self._rds.delete_db(action.id)  # clear MiniStack so re-apply re-boots
                self._rt.stop(self._rds.container_name(action.id))
            elif action.kind in PROVISIONED:
                await asyncio.to_thread(self._aws.deprovision, action.kind, action.id)
            else:
                self._rt.stop(action.name)
            self._prune(action.id)
        elif isinstance(action, NoOp):
            await self._gate_blocked(stack, action.id)

    def _running_footprint(self, stack: Stack, exclude: str) -> float:
        world = self._store.current_world(self._env)
        by_id = {r.id: r for r in stack.resources}
        return sum(
            self._scheduler.footprint(by_id[obs.id])
            for obs in world.resources
            if obs.id != exclude and obs.id in by_id
            and obs.phase in ("starting", "healthy", "running")
        )

    def _evictable_llms(self, stack: Stack, exclude: str) -> list:
        world = self._store.current_world(self._env)
        by_id = {r.id: r for r in stack.resources}
        return [
            by_id[obs.id] for obs in world.resources
            if obs.id != exclude and obs.id in by_id
            and by_id[obs.id].kind == "llm" and obs.phase in ("starting", "healthy")
        ]

    async def _run_service(self, stack: Stack, rid: str) -> None:
        res = self._res(stack, rid)
        if self._scheduler is not None:
            running = self._running_footprint(stack, exclude=rid)
            if not self._scheduler.admits(res, running):
                # Make room by evicting idle LLMs (only for higher-priority non-llm work).
                candidates = self._evictable_llms(stack, exclude=rid) if res.kind != "llm" else []
                to_evict = self._scheduler.evict_for(res, candidates, running)
                if not to_evict:
                    await self._emit(rid, res.kind, "queued")
                    return
                for eid in to_evict:
                    self._rt.stop(eid)
                    await self._emit(eid, "llm", "evicted")
        world = self._store.current_world(self._env)
        env_vars: dict = dict(self._aws_env()) if self._aws_env is not None else {}
        if "env" in res.fields:
            env_vars.update(res.fields["env"].value)  # user env wins over injected AWS
        for ref in res.refs:
            env_vars[ref.var] = self._fabric.resolve(ref, world)
        command = tuple(res.fields["command"].value) if "command" in res.fields else ()
        spec = ContainerSpec(
            name=rid,
            image=str(res.fields["image"].value),
            env={k: str(v) for k, v in env_vars.items()},
            ports={self._port(res): 0},
            command=command,
        )
        self._rt.stop(rid)  # idempotent: clear any crashed remnant before re-run
        self._rt.run_container(spec)
        self._blocked_since.pop(rid, None)
        phase = "running" if res.kind == "batch" else "starting"
        await self._emit(rid, res.kind, phase)

    async def _gate_blocked(self, stack: Stack, rid: str) -> None:
        """A service NoOp means it is waiting on an unready ref — mark blocked,
        and fail loudly if it stays blocked past the timeout."""
        if not rid:
            return
        res = next((r for r in stack.resources if r.id == rid), None)
        if res is None or res.kind not in ("service", "batch", "dep", "llm") or not res.refs:
            return
        world = self._store.current_world(self._env)
        if (obs := world.get(rid)) and obs.phase == "healthy":
            return
        try:
            for ref in res.refs:
                self._fabric.resolve(ref, world)
            return  # all resolvable — not blocked
        except Unresolved:
            pass
        first = self._blocked_since.setdefault(rid, time.monotonic())
        if time.monotonic() - first > self._ref_timeout:
            await self._emit(rid, res.kind, "error", verdict="reference never resolved")
        else:
            await self._emit(rid, res.kind, "blocked")

    def _prune(self, rid: str) -> None:
        world = self._store.current_world(self._env)
        kept = World(env=world.env, resources=tuple(r for r in world.resources if r.id != rid))
        self._store.write_world(kept)
