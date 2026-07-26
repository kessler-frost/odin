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

A tick has two halves, and v0.7.3 made the difference load-bearing: it
ACTS (plan/execute, gc, the gateway push, the World prune) and it OBSERVES
(projecting the TF-owned kinds and broadcasting WorldDeltas). `hold()`
suspends only the first, so `/world` and the canvas stay live for the whole
of an apply instead of freezing at their last pre-apply reading -- see
`hold()` and `_watch()`, which carry the argument and the field-test
measurement behind it.

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

from odin.aws.backings import BackingUnavailable, ENSURE_KINDS, PROVISIONED
from odin.fabric.localhost import LocalhostFabric
from odin.gateway.policy import compile_policies
from odin.gateway.stores import SynthStores
from odin.reconcile.actions import NoOp, ProvisionResource, StopContainer
from odin.reconcile.drift import DriftSweeper
from odin.reconcile.plan import plan
from odin.reconcile.tf_status import TF_OWNED_KINDS, project as project_tf_owned
from odin.spec.models import ResourceDesired, Stack, World, WorldDelta

log = logging.getLogger("odin.reconcile")


# Facts that describe WHAT A RESOURCE IS (endpoint, address, arn) rather than
# how it happens to be doing right now. Only these take part in change
# detection -- see `_emit`. `logtail` is the lone exclusion today; add here
# rather than widening the comparison if a genuinely volatile fact ever
# returns.
_VOLATILE_FACTS = ("logtail",)


def _identity_facts(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if k not in _VOLATILE_FACTS}


def _assert_string_facts(rid: str, facts: dict) -> None:
    """WORLD FACT VALUES ARE STRINGS. Not a style rule -- an invariant the
    change detection in `_emit` depends on, load-bearing since v0.7.4 brought
    facts INTO that comparison, and invisible until field test 5's audit named
    it.

    Why it bites. `prior.facts` is not held in memory: `current_world` re-reads
    and re-parses `.odin/<env>/world.json` on every single `_emit`. So a fact
    value only compares equal to itself if it survives a JSON round-trip
    UNCHANGED -- and most containers don't. `facts={"ports": (80, 443)}` is
    written as `[80, 443]` and read back as a `list`, which never equals the
    tuple the next tick builds. Every tick then sees a change and emits a
    delta: one per resource per poll interval, forever. That is exactly the
    43%-of-all-events flap v0.7.1 already killed once, and it would arrive
    with a credential in tow -- `DATABASE_URL` embeds the RDS password
    (SECURITY.md), so each spurious delta writes another cleartext copy into
    `world.json`, `events.jsonl` and the WebSocket broadcast.

    `str` is the one JSON type with no such gap, and every fact odin publishes
    today is one -- `cachectl.facts` spells `str(port)` out precisely for this.
    This guard is what keeps that true as new builders appear. It sits at the
    EMIT boundary, the single funnel every fact passes through, rather than in
    each producer -- trusting every future builder is what let the invariant go
    unwritten in the first place. And it RAISES rather than coercing: a silent
    `str(v)` would hide the bug, and the loop's own `except` in `_run` turns a
    raise into a logged traceback naming the resource and the offending key.
    """
    bad = sorted(f"{key}={value!r} ({type(value).__name__})"
                 for key, value in facts.items() if not isinstance(value, str))
    if bad:
        raise TypeError(
            f"{rid}: World fact values must be str (they round-trip through "
            f"world.json on every emit) — got {', '.join(bad)}"
        )


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
        # How many `hold()`s are currently open (see hold(): actions off, eyes
        # on). Read and written ONLY under `_tick_lock` -- that is the whole
        # safety argument, so never touch it from anywhere else.
        self._suspended = 0

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
        """Suspend this loop's ACTIONS while an external author mutates the
        env (the /apply-full route: ensure backings + tofu + store commit; the
        /destroy route: ensure + tofu destroy + the empty-Stack commit).
        Ticks keep running -- they just stop touching anything.

        v0.7.3 split two things this used to conflate, because only ONE of
        them ever needed to pause:

        * ACTIONS -- gc, provision/deprovision, the gateway policy/port push,
          and the World prune -- genuinely must. gc's whole job is "stop the
          backings no active kind needs", judged against the OLD, not-yet-
          committed stack (empty on a fresh env, so `gc(set())` stops
          EVERYTHING), so a tick landing between `ensure_backings` and tofu
          finishing kills the very container tofu is mid-conversation with:
          the S5 "rustfs never became ready, empty logs" failure and field
          test 2's B6 8m26s destroy hang. The gateway push is the same shape
          from the other side -- `ensure_backings` has already registered THIS
          apply's compiled policies and backing ports, and a tick would
          overwrite them with the old stack's mid-tofu.
        * OBSERVATION -- projecting the TF-owned kinds into World and
          broadcasting the WorldDeltas -- never did. It creates nothing,
          destroys nothing, and touches no backing; it only reads odin's own
          synth stores and reports. Pausing it is what froze `/world` for the
          entire ~60s of a real apply. Field test 3 measured it with a 2s
          sampler: `/world` still read `healthy` at t=64.0s for a service
          whose deployment had died at t≈4s. That stale reading has now
          concealed both a total outage (pre-v0.7.2) and a rollout that never
          stopped serving (post-v0.7.2) -- opposite lies from the same blind
          spot, at the one moment an operator is actually watching.

        THE SAFETY ARGUMENT, and it is the whole reason this is only a flag:
        `_suspended` is flipped UNDER `_tick_lock`, and a tick's entire body
        runs under that same lock. So every tick is atomically either wholly
        BEFORE this suspension (its gc ran while the caller had not yet
        ensured anything) or wholly AFTER it (its gc is suppressed). There is
        no interleaving in which a tick reads "not suspended" and then acts --
        which is exactly the race the original whole-lock hold was introduced
        to close. Nesting is safe (a depth, not a boolean).

        The asymmetry is deliberate: TAKING the suspension needs the lock
        (nothing may act after the caller has started mutating), RELEASING it
        does not. "Actions may resume" carries no ordering requirement --
        resuming one tick later is harmless, and both routes kick an explicit
        `tick()` straight after the hold anyway -- so the release is a bare
        decrement with no await in it. That matters on the unwind path: a
        `finally` that awaits a lock is one a cancelled request (a shutdown,
        a connection dropped 40s into a tofu run) can stall in, and a
        suspension that never lifts is an env whose reconciler silently stops
        acting."""
        async with self._tick_lock:
            self._suspended += 1
        try:
            yield
        finally:
            self._suspended -= 1

    async def tick(self) -> None:
        async with self._tick_lock:
            step = self._watch if self._suspended else self._converge
            await step()

    async def _converge(self) -> None:
        """The whole tick: observe, plan, execute, gc, push, project."""
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

    async def _watch(self) -> None:
        """A tick with its hands tied: the projection, and nothing else --
        what an in-flight apply (`hold()`) leaves running.

        Three things `_converge` does are deliberately absent, each because it
        would ACT rather than look:

        * plan/execute + gc + the gateway push -- the actions hold() exists
          for, argued in hold()'s own docstring.
        * `_observe` of the PROVISIONED kinds (s3/sqs/sns/dynamodb). It is not
          read-only (sns re-subscribes missing queues through `provision`), and
          its existence check runs against the very backing tofu is creating
          and deleting inside right now -- a bucket mid-replacement would read
          `crashed`, which is a manufactured lie, not an observation. Nothing
          is lost: no PROVISIONED kind has a runtime failure mode that only
          shows up DURING an apply, and every kind field test 3 was watching
          (ecs/ec2/lambda/rds/alb) is projected, not observed.
        * the prune half of `_project_tf_owned` (see `act`). Pruning is the one
          World-MUTATING removal, and it is driven by a snapshot tofu is
          rewriting: any replace is a delete-then-create, so a prune landing in
          between emits `draft` for a label that returns seconds later --
          precisely the flap v0.7.1 killed (field test 2 finding #3). The
          trailing `tick()` both routes already run right after the hold prunes
          whatever genuinely went away. The freeze this fix removes was about
          watching a resource go BAD, never about seeing one disappear a few
          seconds sooner."""
        if self._stores is not None:
            await self._project_tf_owned(act=False)

    async def _project_tf_owned(self, act: bool = True) -> None:
        """`tf_status.project()` is the whole snapshot; diff it against the
        current World and emit only what changed (`_emit`'s own dedupe) plus
        prune any label that dropped out (tofu destroyed it -- this loop
        never destroys a TF-owned resource itself).

        This prune is the SOLE authority on a TF-owned label leaving World
        (see `_execute`'s StopContainer branch): "the desired Stack no longer
        wants it" is not the same question as "it no longer exists", and
        answering the second one with the first is what made every
        odin-synthesized resource flap (field test 2 finding #3).

        W2.2: the drift sweep runs FIRST, deliberately -- its ecs half writes
        reality back into the task records (reconcile/drift.py's module
        docstring), so a container removed outside odin surfaces on THIS
        tick's projection rather than the next one; its ec2/lambda half comes
        back as a `label -> verdict` overlay that turns a record still
        claiming to be up into an honest `crashed` + why. `_emit`'s existing
        crashed path is what then broadcasts both the WorldDelta and the
        `type:"log"` error line -- drift needs no pipeline of its own.

        `act=False` is the observe-only form an in-flight apply runs under
        (`_watch`): report everything, prune nothing, and read the drift
        sweep's CACHE instead of taking a fresh sweep. Both halves are about
        not ACTING while an external author holds the env -- the prune for the
        reason `_watch` gives, the sweep because it is not a passive listing
        either: it CORRECTS records (`mark_task_stopped`,
        `mark_instance_terminated`, `rdsctl.mark_instance_failed`) off a
        `docker ps`/`limactl list`/`pg_ready` sample taken while tofu is
        pulling images and booting VMs -- the exact busy-daemon load
        drift.py's own confirm-before-correcting note names as the hazard.
        The cache still carries any drift reported BEFORE the apply, so
        nothing already known goes quiet mid-apply.

        WHAT THAT COSTS, and where it is written down (field test 4, P4-4):
        the cached half covers ec2, lambda and rds ONLY. ECS is not affected --
        `project_tf_owned` runs `ecsctl.sweep_tasks` live on every one of these
        ticks, and that sweep recognises a VANISHED container (`absent`), so a
        task container removed out of band mid-apply is caught on the next
        tick, not after the apply. For the other three the staleness lasts the
        rest of the apply plus up to one sweep cadence (default 10 ticks, ~10s)
        after it returns, since the cadence counter never advanced while
        suspended. That is a REAL LIMIT a user can hit, so it lives in
        ROADMAP's "v1 limits, recorded rather than hidden" list -- not only
        here. Field test 4 hit it as 57s of a stale task count, and the two
        `tests/reconcile/test_reconciler.py` tests named in that entry pin both
        halves."""
        drifted = await self._drift_verdicts(act)
        projected = await asyncio.to_thread(project_tf_owned, self._stores, self._env)
        for label, (kind, phase, facts, verdict) in projected.items():
            phase, verdict = ("crashed", drifted[label]) if label in drifted else (phase, verdict)
            await self._emit(label, kind, phase, facts=facts, verdict=verdict)
        if not act:
            return
        world = self._store.current_world(self._env)
        for observed in world.resources:
            if observed.kind in TF_OWNED_KINDS and observed.id not in projected:
                await self._prune(observed.id)

    async def _drift_verdicts(self, sweep: bool = True) -> dict[str, str]:
        if self._drift is None:
            return {}
        return await asyncio.to_thread(self._drift.verdicts, self._stores, self._env, sweep)

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
        # Skip unchanged status: observe runs every tick, so re-emitting an
        # identical reading would fill the event log + WS + events.jsonl with
        # "healthy" noise (43% of all events before v0.7.1 fixed exactly that).
        #
        # But FACTS count as a change too. They were left out when the facts of
        # the day were the parked workload layer's fluctuating cpu/ram; today's
        # are identity — endpoints, IPs, ARNs — and they can arrive AFTER the
        # phase settles. Field test 4: an rds node reaches `healthy` before
        # `rdsctl._join_mesh` records its overlay_ip, so with facts excluded
        # `DATABASE_URL_MESH` could never enter World on a first apply, and no
        # later phase change ever came to carry it. That made README's central
        # security advice (point VM consumers at the SG-gated `_MESH` ref)
        # impossible to follow, silently downgrading them to the ungated `_VM`,
        # and left `mesh_health.gate` dead code because the keys it withholds
        # never arrived. Generally: any fact that changed while a resource
        # stayed healthy was stale in World forever.
        #
        # `logtail` is excluded because it is diagnostic, not identity: it can
        # differ between reads of the same dead container, and re-emitting for
        # that is the noise this guard exists to stop.
        #
        # The comparison below is what makes fact VALUES have to be strings --
        # `prior` is re-parsed from world.json every time, so anything that
        # doesn't round-trip through JSON unchanged compares unequal forever.
        # `_assert_string_facts` states and enforces that, here at the one
        # boundary every fact crosses.
        _assert_string_facts(rid, facts or {})
        prior = self._store.current_world(self._env).get(rid)
        if (
            prior is not None
            and prior.phase == phase
            and prior.verdict == verdict
            and _identity_facts(prior.facts) == _identity_facts(facts or {})
        ):
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
            try:
                facts = await asyncio.to_thread(self._aws.facts, res.kind, res.id)
            except BackingUnavailable as exc:
                # Honesty rule 1. `facts()` raises rather than inventing an
                # endpoint when the backing's published port can't be read
                # (aws/backings.py::_published_port), and a resource whose
                # endpoint odin cannot NAME is not healthy -- publishing
                # `healthy` + `http://host.docker.internal:0` was the field
                # test 5 hazard, and it was permanent because these facts are
                # written once on this very transition and never refreshed.
                # Stay `starting`, carry the real reason, retry next tick.
                # `_emit`'s dedupe makes a persistent failure exactly ONE
                # delta, not one per tick.
                await self._emit(res.id, res.kind, "starting", verdict=str(exc))
            else:
                await self._emit(res.id, res.kind, "healthy", facts=facts)
        if phase == "healthy" and not ok:
            logtail = await asyncio.to_thread(self._backing_logtail, res.kind)
            await self._emit(
                res.id, res.kind, "crashed",
                facts=self._crash_facts(res.id, logtail),
                verdict=f"the {res.kind} backing is no longer reachable",
            )
        if res.kind != "sns" or not ok:
            return
        actual = await asyncio.to_thread(self._aws.subscriptions, res.id)
        missing = tuple(q for q in self._desired_subs(stack, res.id) if q not in actual)
        if missing:
            await asyncio.to_thread(self._aws.provision, "sns", res.id, missing)

    def _crash_facts(self, rid: str, logtail: str) -> dict:
        """A crashed resource's facts: the identity it still HAS, plus the
        diagnostic tail of why it died.

        The crash branch used to replace facts WHOLESALE with
        `{"logtail": …}`, which destroyed a crashed bucket's `BUCKET` and
        `endpoint` in World (field test 5's facts audit). That is wrong on its
        own terms -- a crashed s3 node still IS that bucket; its name did not
        stop being its name -- and it broke resolution: anything referencing
        `${{bucket.BUCKET}}` through the Fabric saw the value VANISH the moment
        the backing hiccuped, and only a full starting->healthy round trip put
        it back.

        This is NOT the stale-green shape `_cache_clusters`/`_db_instances`
        gate against, and the line between them is worth stating: those two
        withhold a REACHABILITY claim (dial this and you get a database) for
        something that is not up. What survives here is IDENTITY (this node is
        the bucket named `uploads`), and it ships with `phase="crashed"` plus a
        verdict saying the backing is gone -- so nothing reads it as green.

        Costs no extra deltas: `logtail` is excluded from change detection
        (`_VOLATILE_FACTS`) and the identity half is by construction equal to
        what World already holds, so a resource sitting crashed emits once and
        then stays silent however many ticks pass over it."""
        prior = self._store.current_world(self._env).get(rid)
        identity = _identity_facts(prior.facts) if prior is not None else {}
        return {**identity, **({"logtail": logtail} if logtail else {})}

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
            elif action.kind in TF_OWNED_KINDS and self._stores is not None:
                # tofu (never this reconciler) owns create/destroy for a
                # TF-managed kind, and `_project_tf_owned` -- which prunes any
                # projected-kind label that has dropped out of the snapshot --
                # is the ONLY authority on whether the real resource is still
                # there. So this branch does nothing at all, prune included.
                #
                # Pruning here instead was field test 2 finding #3: a resource
                # odin SYNTHESIZED (a VPC's auto-created `default` security
                # group, a Lambda's auto-generated execution role) is real,
                # projected every tick, and in no canvas -- so plan() called it
                # "observed but not desired" and pruned it, the projection
                # re-added it, and the pair flapped `draft`/`healthy` forever:
                # ~1.8 events/second into the WebSocket and the append-only
                # events.jsonl, burying real crash events. `action.name` is a
                # label rather than a container name anyway, so there was never
                # anything for `self._rt.stop` to do here either.
                return
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
