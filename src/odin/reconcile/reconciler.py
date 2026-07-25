"""The control loop: observe reality, plan, execute, repeat.

Each tick: (1) observe — refresh the World from runtime facts + assertions,
advancing started resources to healthy/crashed; (2) plan(Stack, World) → Actions;
(3) execute — provision/stop; (4) project the gateway's TF-owned resources
(vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr) into World too (fix-wave 2b
finding #1 -- see reconcile/tf_status.py). The pure plan() decides intent for
rds + the AWS-shaped PROVISIONED resources; this executor builds specs
(resolving refs via the Fabric) and runs them; TF_OWNED_KINDS never go
through plan/execute at all -- tofu is their sole creator/destroyer, this
loop only OBSERVES what tofu already did.

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
from odin.fabric.models import FirewallRules
from odin.fabric.nebula import sg_firewall_by_name
from odin.gateway.policy import compile_policies
from odin.gateway.stores import SynthStores
from odin.reconcile import assertions
from odin.reconcile.actions import NoOp, ProvisionResource, StopContainer
from odin.reconcile.plan import plan
from odin.reconcile.tf_status import TF_OWNED_KINDS, project as project_tf_owned
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST
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
        stores: SynthStores | None = None,
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
        # The gateway's synth stores (tags/ec2net/iamctl/ecr/ec2compute/
        # lambdactl/ecsctl) -- read-only here, for the TF-owned-status
        # projection (see tick()'s trailing step + tf_status.py). None in
        # every test that doesn't care; server.py's real wiring always
        # passes the SAME SynthStores instance the gateway itself uses.
        self._stores = stores
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
        never destroys a TF-owned resource itself)."""
        projected = await asyncio.to_thread(project_tf_owned, self._stores, self._env)
        for label, (kind, phase, facts, verdict) in projected.items():
            await self._emit(label, kind, phase, facts=facts, verdict=verdict)
        world = self._store.current_world(self._env)
        for observed in world.resources:
            if observed.kind in TF_OWNED_KINDS and observed.id not in projected:
                await self._prune(observed.id)

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
    def _res(self, stack: Stack, rid: str) -> ResourceDesired:
        return next(r for r in stack.resources if r.id == rid)

    def _kind_of(self, stack: Stack, rid: str) -> str | None:
        return next((r.kind for r in stack.resources if r.id == rid), None)

    def _desired_subs(self, stack: Stack, sns_id: str) -> tuple[str, ...]:
        """An sns→sqs canvas edge is a subscription: the queues this topic
        must fan out to."""
        return tuple(e.dst for e in stack.edges
                     if e.src == sns_id and self._kind_of(stack, e.dst) == "sqs")

    def _sg_names(self, res: ResourceDesired) -> list[str]:
        """The security-group LABELS a canvas node names in its
        `securityGroups` field (one per line -- the same convention an ec2
        node already uses, `agent/hcl.py::_security_group_refs`)."""
        field = res.fields.get("securityGroups")
        raw = str(field.value) if field is not None else ""
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _sg_firewall(self, res: ResourceDesired) -> FirewallRules | None:
        """W2.6 piece 3: the drawn SG's ALREADY-compiled nebula firewall, for
        a resource this reconciler owns (an rds node -- tofu owns SGs but not
        the database). Read through fabric's `ec2net.json` boundary, so the
        firewall the DB gets is byte-identical to what an EC2 VM in the same
        group gets. None (no field, or the group isn't created yet) means
        "not gated" -- the backing still joins the mesh, with nebula's
        allow-all default, exactly as it behaved before an SG was drawn."""
        return sg_firewall_by_name(self._store.root, self._env, self._sg_names(res))

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
            if res.kind == "rds" and observed.phase in ("starting", "healthy"):
                await self._observe_rds(res)
            elif res.kind in PROVISIONED and observed.phase in ("starting", "healthy"):
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

    async def _observe_rds(self, res: ResourceDesired) -> None:
        cname = self._rds.container_name(res.id)
        if self._rt.facts(cname).phase == "crashed":
            # Clear the dead container so the recreate boots a fresh Postgres.
            exit_code = self._rt.exit_code(cname)
            logtail = self._rt.logs(cname)
            await asyncio.to_thread(self._rds.delete_db, res.id)
            await self._emit(
                res.id, "rds", "crashed",
                facts={"logtail": logtail} if logtail else {},
                verdict=f"container exited (code {exit_code})",
            )
            return
        endpoint = self._rds.endpoint(res.id)
        if endpoint is None:
            return  # still creating
        # W2.6: (re)join the env's mesh behind this node's drawn SG. Here, not
        # only at create time, so a live SG edit takes effect on the next tick
        # (the same reason sns re-diffs its subscriptions in this pass) and a
        # dead sidecar heals. Idempotent and cheap: unchanged firewall +
        # running sidecar is two file reads and one container-status call.
        await asyncio.to_thread(self._rds.join_mesh, res.id, self._sg_firewall(res))
        user, pw = self._creds(res)
        result = await self._pg_ready(endpoint[0], endpoint[1], user, pw)  # host-side probe
        if not result.ok:
            # Not necessarily a crash (Postgres may simply still be booting)
            # -- phase stays "starting", but WHY the health check keeps
            # failing must not vanish silently (observability v1). _emit's
            # own dedupe on (phase, verdict) means this only broadcasts once
            # per distinct error, never spams every tick.
            if result.error:
                await self._emit(res.id, "rds", "starting", verdict=f"not ready: {result.error}")
            return
        # Publish a CONTAINER-reachable address: a consumer gets this verbatim
        # as DATABASE_URL, and "localhost" inside a container is the container
        # itself, not the Mac. host.docker.internal is the host (same as AWS).
        addr = f"{CONTAINER_HOST}:{endpoint[1]}"
        url = f"postgresql://{user}:{pw}@{addr}/postgres"
        # A container consumer resolves host.docker.internal; an EC2 Lima VM
        # does NOT (finding #5) -- it resolves host.lima.internal. Publish a
        # SECOND, VM-reachable form pointing at the SAME Postgres, so an ec2
        # node consumes ${{db.DATABASE_URL_VM}} while containers keep
        # ${{db.DATABASE_URL}} (per-consumer-type ref routing is deferred --
        # a distinct fact is the honest, smaller fix).
        vm_addr = f"{LIMA_HOST}:{endpoint[1]}"
        vm_url = f"postgresql://{user}:{pw}@{vm_addr}/postgres"
        stats = self._rt.stats(cname)
        # W2.6: a THIRD form of the same database -- its overlay address,
        # the one a drawn SG actually gates. Published only when this env has
        # a mesh, and alongside (never instead of) the host-reachable pair
        # above, which the gateway/probes/tests all still ride.
        overlay = self._rds.overlay_endpoint(res.id)
        mesh_facts = {
            "DATABASE_URL_MESH": f"postgresql://{user}:{pw}@{overlay[0]}:{overlay[1]}/postgres",
            "endpoint_mesh": f"{overlay[0]}:{overlay[1]}",
        } if overlay else {}
        await self._emit(
            res.id, "rds", "healthy",
            facts={
                "DATABASE_URL": url, "endpoint": addr,
                "DATABASE_URL_VM": vm_url, "endpoint_vm": vm_addr,
                **mesh_facts,
                **stats,
            },
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
                subs = self._desired_subs(stack, action.id) if action.service == "sns" else ()
                await asyncio.to_thread(self._aws.provision, action.service, action.id, subs)
                await self._emit(action.id, action.service, "starting")
        elif isinstance(action, StopContainer):
            if action.kind == "rds":
                self._rds.delete_db(action.id)  # stop the DB container so re-apply re-boots
                self._rt.stop(self._rds.container_name(action.id))
            elif action.kind in PROVISIONED:
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
