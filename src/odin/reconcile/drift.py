"""W2.2 -- the reality sweep for TF-owned COMPUTE: does the VM/container a
synth record CLAIMS to be running actually still exist?

`reconcile/tf_status.py` projects the TF-owned kinds out of odin's OWN synth
stores. That makes the canvas honest about what tofu created, but not about
what's still THERE: `limactl delete odin-ec2-...` a VM or `docker rm -f` an
ECS task container out of band and the record still reads `running`, so
/world reported `healthy` forever (the only reality cross-check was the
startup-only EC2 reaper). This module closes that with BULK calls -- one
`limactl list`, one or two `docker ps` -- plus, for the two kinds whose exact
STATE matters (`_live_states`), one `inspect` per resource that still exists.
Never a listing per resource, and nothing at all for a kind this env has none of.

TWO HALVES, and the split is the point (field test 5). `live_verdicts` /
`sweep_compute` are LIVE: no cadence, no cache, called by anything that has to
be sure right now (`tf_status.project()` and /apply-full). `DriftSweeper` is the
background half: the same reality check on a cadence (default every 10
reconciler ticks, ~10s at the production 1s poll; `ODIN_DRIFT_SWEEP_TICKS`
overrides) with every other tick answering from the last sweep's cached result,
plus the two checks only it can afford -- `limactl list` for ec2 and a real
`pg_ready` per database.

REPORT, DON'T AUTO-HEAL (the wave-2 plan is explicit): tofu owns these
kinds, so re-creating a VM behind its back would fight its state. A drifted
resource projects `crashed` with a verdict that NAMES the drift and tells
the user the fix ("re-Apply to recreate"); the reconciler's existing pipeline
turns that into a WorldDelta + an error log line. This sweep never calls
tofu, and never re-creates anything -- the user still drives the recovery.

...BUT TELL TOFU THE TRUTH TOO (the honesty fix W2.2 shipped without): the
sweep also CORRECTS THE RECORD of a resource it just confirmed gone, because
that record is what odin's own gateway answers tofu's next refresh with.
Reporting "re-Apply to recreate" while leaving an ec2 record claiming
`running` made the advice a LIE: DescribeInstances kept answering `running`
for a VM that no longer existed, tofu planned nothing, and the resource never
came back -- exactly the "never promise a recovery that doesn't happen" rule
in NORTHSTAR directive 5. Correcting the store is not auto-healing; it is the
minimum honesty that makes the user's Apply actually work. Per kind:
 - ec2: `ec2compute.mark_instance_terminated` -> the record reads
   `terminated` with a real `StateReason` naming the out-of-band deletion,
   which is what real AWS reports for an instance that's gone AND what makes
   terraform-provider-aws's own Read drop it from state -> the next `tofu
   apply` plans a create and the VM genuinely returns. The record still
   projects (`crashed` + that reason, tf_status.py's `drifted` exception), so
   the honest report and the working recovery are the same fact.
 - lambda: `lambdactl.mark_function_failed` -> `State: Failed` with the same
   reason. The record is deliberately NOT deleted: a function's RIE container
   is its EXECUTION ENVIRONMENT, not a TF resource, and real AWS never deletes
   a function because a sandbox died (it starts a new one). tofu therefore
   can't be the fixer here either -- the provider's `aws_lambda_function`
   schema has no state/status attribute to diff on (verified against the
   v5.100.0 provider schema), so its plan stays empty -- which is why the
   recovery is `lambdactl.converge_functions`, an Apply-driven re-`ensure` of
   the container, exactly parallel to ecs below.
 - ecs: `ecsctl.mark_task_stopped` -> see the next paragraph.

 - rds (W2.7): `rdsctl.mark_instance_failed` -> `failed` with the same kind of
   reason, and `rdsctl.converge_db_instances` is the Apply-driven recovery, for
   lambda's exact reason (the provider exposes `status` as read-only Computed,
   so an `aws_db_instance` whose container died has an empty plan forever).
   Its CHECK is different from every other kind here, deliberately -- see the
   next paragraph.

WHICH KINDS: only the four with a real runtime footprint -- ec2 (a real Lima
VM), ecs (a real task container), lambda (a real RIE container), rds (a real
Postgres container). vpc/subnet/sg/iam_role have NO runtime footprint at all
(a VPC is a Nebula network config + a JSON record, an SG is a compiled
firewall, an IAM role is a policy document -- there is nothing to `docker ps`
or `limactl list` for them, so a reality sweep could neither confirm nor deny
them). `ecr` is out too: an ECR *repository* is a control-plane record
(gateway/models/ecr.py), and the registry:2 CONTAINER its image bytes live in
is a per-env BACKING, already supervised by `aws/backings.py`'s own ensure/gc
lifecycle -- not this projection's business.

RDS IS CHECKED TWICE, BY TWO DIFFERENT THINGS, because no single check covers
it. `ColimaRuntime.container_names` lists exited containers too, so a
`docker kill`ed Postgres -- the canonical way a database dies -- looks
perfectly present in a NAME listing; and a container that is up while Postgres
inside it is wedged is ALSO down as far as any consumer of the DATABASE_URL
fact is concerned, which only a real connection can tell. So:
  - `sweep_compute` (the LIVE half below) reads the container's real STATE via
    `_live_states`: gone, `exited`, `paused`, `dead` -- each of them a database
    that is not serving -- and corrects the record to `failed` so the next
    Apply's converge genuinely brings it back. Field test 5 is why this is
    state-based and cadence-free rather than a name listing on a timer.
  - then each still-`available` instance gets one `pg_ready_sync` against its
    stored port (no subprocess at all), which catches the one thing docker's
    own state cannot: a running container whose Postgres has stopped
    answering. That is reported as a verdict but the RECORD IS LEFT ALONE, so a
    transient probe failure self-heals on the next sweep instead of needing a
    human Apply. (This mirrors the old reconciler exactly: it only cleared and
    recreated the container on a real exit, and otherwise just surfaced the
    connection error.)

MID-BOOT IS NOT DRIFT (the sharpest edge here): a record is only swept when
it CLAIMS to be up -- ec2 `running`, lambda `Active` and not mid-redeploy,
ecs task `RUNNING`. Every transitional state is exempt, because odin's real
substrates all boot on background threads (`ec2compute._finish_boot`,
`lambdactl`'s v0.5.4 create/update thread, `ecsctl._launch_task`) and a VM or
container that limactl/docker hasn't registered YET is not gone -- it's
starting. Same in reverse for `shutting-down`/`stopping` (mid-delete) and
`stopped`/`Failed`/`STOPPED` (already crashed, with their own real reasons).

THE ECS SHAPE -- why that kind's correction is a task record and not a TF
resource state: an ECS *task* is not a TF resource at all (real terraform
never manages one; real ECS's own service scheduler replaces a lost task). So
for ecs the honest record of "this task's container is gone" is the task
record itself, marked STOPPED exactly as `ecsctl.sweep_tasks` already marks a
container that exited on its own -- which (a) gives the crashed projection +
verdict through the machinery wave 1 already built, and (b) is what makes
re-Apply actually converge: `ecsctl.converge_services` relaunches the missing
task, while an `aws_ecs_service` whose config never changed would give tofu an
empty plan forever.
"""
from __future__ import annotations

import logging
import asyncio
import os
from functools import partial
from typing import NamedTuple

from odin.aws.rds import container_name as db_container_name
from odin.compute.functions import container_name as function_container_name
from odin.compute.instances import InstanceVm, vm_name
from odin.compute.tasks import container_name as task_container_name
from odin.gateway.models import rdsctl
from odin.gateway.models.ec2compute import mark_instance_terminated
from odin.gateway.models.ecsctl import container_gone_reason, mark_task_stopped
from odin.gateway.models.lambdactl import mark_function_failed
from odin.gateway.stores import SynthStores
from odin.reconcile.assertions import pg_ready
from odin.runtime.colima import ColimaRuntime

log = logging.getLogger("odin.reconcile.drift")

_DEFAULT_SWEEP_TICKS = 10

# How long the rds half waits before re-asking "is it really down?" (see
# `_sweep_databases`). Short enough that a genuinely dead database is still
# reported on this same sweep, long enough to outlast a busy-daemon blip. Only
# ever paid on the failure path. (v0.7.7: the sweep used to run off the event
# loop via asyncio.to_thread; it is now awaited on the loop itself, so this
# delay yields rather than blocking.)
_CONFIRM_DELAY = 1.0

# What a failed probe that carries no error text says. `PgReady.error` is None
# on the `ok=False`-without-an-exception path (`_pg_connect` returning False),
# and interpolating that None put the literal word "None" where a user expects
# a reason. A verdict has to name something true even when the driver says
# nothing -- rule 2: the failure names what is actually known.
_NO_PROBE_ERROR = "the connection completed but the readiness query did not return"


def _probe_reason(probe) -> str:
    return probe.error or _NO_PROBE_ERROR


def _sweep_ticks() -> int:
    """Ticks between sweeps. Read fresh (not cached) so a test can
    monkeypatch the env var, the same convention `compute/instances.py`'s
    `_default_max_concurrent_boots` and `agent/translate.py`'s
    `_default_timeout` already use."""
    return max(1, int(os.environ.get("ODIN_DRIFT_SWEEP_TICKS", str(_DEFAULT_SWEEP_TICKS))))


def _label(tags: dict[str, str], natural: str | None = None) -> str | None:
    """The SAME label rule `tf_status.py::_label` projects with (and
    `api/logs.py::_tagged_label` resolves with) -- it has to be, or a drift
    verdict would be keyed to a label that never appears in World."""
    return tags.get("odin:node") or natural


def _vm_records(stores: SynthStores, env: str) -> list[tuple[str, str, str]]:
    """(label, instance id, real VM name) for every ec2 record claiming
    `running`. `ec2` has no AWS-native name field, so the `odin:node` tag is
    the only route back to a label (tf_status.py's own note) -- an untagged
    instance isn't projected into World either, so there'd be nothing to
    report drift on. The instance id rides along because a confirmed-gone VM
    gets its RECORD corrected too (`mark_instance_terminated`)."""
    out: list[tuple[str, str, str]] = []
    for key, record in stores.ec2compute.items(env).items():
        if not key.startswith("instance:") or record["state_name"] != "running":
            continue
        label = _label(stores.tags.get(env, f"ec2:{record['instance_id']}", {}))
        if label:
            out.append((label, record["instance_id"], vm_name(env, record["instance_id"])))
    return out


def _function_records(stores: SynthStores, env: str) -> list[tuple[str, str, str]]:
    """(label, function name, RIE container name) for every function claiming
    `Active` and NOT mid-redeploy. `LastUpdateStatus == "InProgress"` is the
    exempt window: `FunctionRuntime.ensure` deliberately `stop`s (rm -f) the
    old container before running the new one, so the container is legitimately
    absent for a moment while `State` still reads Active (lambdactl.py's two
    independent state machines). The function name rides along because a
    confirmed-gone container gets its RECORD corrected too
    (`mark_function_failed`)."""
    out: list[tuple[str, str, str]] = []
    for key, record in stores.lambdactl.items(env).items():
        if not key.startswith("fn:") or record["state"] != "Active":
            continue
        if record.get("last_update_status") == "InProgress":
            continue
        label = _label(
            stores.tags.get(env, f"lambda:{record['function_arn']}", {}), record["function_name"],
        )
        if label:
            out.append((label, record["function_name"], function_container_name(env, record["function_name"])))
    return out


def _task_records(stores: SynthStores, env: str) -> list[tuple[str, str, str]]:
    """(cluster, task_id, task container name) for every task record claiming
    `RUNNING`. `PROVISIONING` is mid-launch (`ecsctl._launch_task` writes the
    record before `docker run` returns) and `STOPPED` is already terminal.
    The `"task:"` prefix never matches `"taskdef:"`/`"taskdef-rev:"` -- the
    5th character disagrees (ecsctl.py's own store-key convention)."""
    out: list[tuple[str, str, str]] = []
    for key, record in stores.ecsctl.items(env).items():
        if not key.startswith("task:") or record["last_status"] != "RUNNING":
            continue
        container = task_container_name(env, record["task_id"], record["container_name"])
        out.append((record["cluster_name"], record["task_id"], container))
    return out


def _db_records(stores: SynthStores, env: str) -> list[tuple[str, dict]]:
    """(label, record) for every DB instance claiming `available` and carrying a
    real endpoint port. `creating` is mid-boot (`rdsctl._finish_create` is still
    polling its own `pg_ready`), `deleting` is mid-teardown and `failed` is
    already terminal -- the same "only sweep what CLAIMS to be up" rule every
    other kind here follows."""
    out: list[tuple[str, dict]] = []
    for record in rdsctl.records(stores, env):
        if record["status"] != rdsctl.AVAILABLE or not record.get("endpoint_port"):
            continue
        identifier = record["db_instance_identifier"]
        label = _label(stores.tags.get(env, f"rds:{rdsctl.db_arn(identifier)}", {}), identifier)
        if label:
            out.append((label, record))
    return out


async def _listing(read):
    """One bulk listing, or None when the CLI call itself failed. LOAD-
    BEARING: an empty listing and a failed listing are NOT the same thing --
    reading "docker isn't answering" as "every container is gone" would flip
    a whole env to `crashed` over a transient hiccup. None means "unknown",
    and an unknown sweep reports no drift at all."""
    try:
        return await read()
    except Exception as exc:  # noqa: BLE001 -- any CLI/parse failure means "unknown"
        log.warning("drift sweep listing failed (%s); reporting no drift this pass", exc)
        return None


# --- THE LIVE HALF: no cadence, no cache, one bulk listing -----------------
#
# Field test 5, and the reason this module has two halves at all. `DriftSweeper`
# runs on a CADENCE and is CACHED between sweeps, which is right for a
# background loop and wrong for anything that has to be sure right now: measured
# at the default cadence, four consecutive `applied`/exit-0 applies landed over
# ~8s with the function's container already removed, and `/world` read green for
# the same window, because both the apply's own verification and the projection
# read a RECORD this loop only refreshes every ~10 ticks. A guard that depends
# on a signal produced on a cadence inherits the cadence (.claude/CLAUDE.md
# honesty rule 1b).
#
# `_dead` is that same reality check with the cadence taken out: one bulk
# listing for EXISTENCE plus one `inspect` per container that exists, no cache,
# no waiting on anyone else's loop, and nothing at all for an env with no
# lambda/rds records. TWO CALLERS SHARE THAT ONE READ, and only one of them
# writes:
#
#   * `reconcile/tf_status.py::project()` calls `live_verdicts`, which is
#     READ-ONLY: `/world` goes honest on the very next tick (~1s) without
#     touching a single record.
#   * `server.py`'s /apply-full calls `sweep_compute` -- the same read plus the
#     record correction -- so an apply establishes liveness ITSELF instead of
#     believing a stored status, and the correction it writes is what makes the
#     NEXT apply's `converge_*` actually recreate the resource.
#
# WHY THE PROJECTION MAY NOT WRITE, though writing would be convenient:
# `converge_db_instances` recreates a `failed` database, and recreating a
# database DESTROYS ITS DATA. If a background tick corrected the record, the
# very next apply -- an unrelated canvas edit, seconds after something killed
# the container -- would silently delete and re-create that database and report
# `applied`, exit 0. So the rule is: NOTHING IS RECREATED UNTIL AN APPLY HAS
# REPORTED IT DEAD. The apply that finds it says so and stops; the apply after
# that recovers it (`converge_*`, the "re-Apply to recreate" the verdict
# promises). A read-only projection is what makes that deterministic instead of
# a race against the tick.
#
# WHY A BULK LISTING IS SAFE TO CORRECT FROM, even mid-apply while the daemon is
# pinned (the hazard `_sweep_databases`' confirm-before-correcting note names):
# `docker ps -a` either answers authoritatively or fails, and a failure is read
# as "unknown" above and corrects nothing. That is categorically different from
# `docker inspect <name>`, whose empty answer cannot distinguish "no such
# container" from "docker didn't answer" -- which is why the probe half below
# still needs its confirm delay and this half does not.


# The states that mean "this container exists and is not serving". `paused` is
# the one field test 5 turned up and the reason this reads a STATE at all: a
# paused container is present, is listed by a plain `docker ps`, and answers
# nothing. `created`/`restarting` are deliberately NOT here -- they are a
# container on its way up, and "still starting" must never fail an apply.
_NOT_SERVING = frozenset({"exited", "dead", "removing", "paused"})


def _not_serving(state: str | None) -> bool:
    """`None` -- the bulk listing did not return this container at all -- is the
    honest "it is GONE", and the listing is authoritative about that.

    Everything else has to be IN `_NOT_SERVING` to count. In particular
    `status`'s own `absent` does not: that is what it answers when `inspect`
    printed nothing, which it cannot distinguish from "docker didn't answer"
    (its own docstring), and for a name the listing JUST returned that is an
    ambiguity, not a death -- skipped, never corrected. `_listing`'s "unknown is
    not gone" rule, one level down."""
    return state is None or state in _NOT_SERVING


async def _live_states(runtime, names: list[str]) -> dict[str, str] | None:
    """`name -> the container's real state` for the containers asked about, or
    None when the runtime itself did not answer.

    TWO SEAMS, BOTH PROVEN ON BOTH RUNTIMES -- and that is why it is shaped like
    this rather than as one `docker ps --format '{{.Names}}\\t{{.State}}'`. That
    single call works on docker and FAILS OUTRIGHT on nerdctl (probed on a real
    Lima VM: nerdctl's ps ListItem has no `.State` field at all -- `can't
    evaluate field State`, rc 1), and `LimaRuntime` inherits every method here.
    With `check=True` that raises, `_listing` reads it as "unknown", and this
    whole guard would have silently never fired on Lima -- the exact failure
    shape it exists to remove (honesty rule 1). nerdctl's `.Status` is not a
    drop-in either: it says a bare `Paused` where docker says
    `Up 2 seconds (Paused)`. So:

      * `container_names()` -- one bulk `ps -a --format '{{.Names}}'`, identical
        on both, and the AUTHORITY on existence: a name it does not return was
        really removed. It raises on a CLI failure, so a hiccup is "unknown"
        rather than "everything is gone".
      * `status()` -- `inspect -f '{{.State.Status}}'`, verified to answer the
        same `running`/`paused`/`exited` vocabulary on docker AND nerdctl, and
        asked ONLY for containers the listing just proved exist (so a healthy
        env pays one listing plus one inspect per lambda/rds node, and a
        removed container costs no inspect at all)."""
    # `_listing` takes the coroutine FUNCTION, not a lambda: an `await`
    # inside a lambda is a syntax error, and the frozenset belongs after
    # the None check anyway (None means "unknown", not "empty").
    present = await _listing(runtime.container_names)
    if present is None:
        return None
    present = frozenset(present)
    return {name: await runtime.status(name) for name in names if name in present}


async def _dead_verdict(containers, name: str, state: str | None) -> str:
    """WHY this container isn't serving, in the SAME sentences the cadence half
    has always used -- one down container, one vocabulary, whichever surface an
    operator is looking at. `state is None` is the container that no longer
    exists at all (never an invented exit code for it, `_sweep_databases`' own
    rule); `exited` is worth the one extra `inspect` its real exit code costs,
    because 137 vs 1 is the whole diagnosis; anything else names docker's own
    state (`paused` is the case that defeats every record-trusting check).

    An exit code the runtime would not give up (`exit_code`'s negative sentinel
    -- a `docker inspect` that answered nothing, or a runtime whose inspect
    template differs) is reported as no number at all rather than as "exit -1":
    field test 2 LOW-17's rule, that odin never invents a code nothing
    reported."""
    code = await containers.exit_code(name) if state == "exited" else -1
    detail = (
        "removed outside odin" if state is None
        else f"is not running (exit {code})" if code >= 0
        else "is not running" if state == "exited"
        else f"is {state}"
    )
    return f"container {name} {detail} — re-Apply to recreate"


class Dead(NamedTuple):
    """One lambda/rds whose record claims it is up while its container is not
    running: the canvas LABEL (what /world and an apply both name it by), the
    identity its own model corrects by (function name / DB identifier), which
    kind it is, and WHY -- the verdict, already worded."""

    label: str
    identity: str
    kind: str
    verdict: str


async def _dead(stores: SynthStores, env: str, containers=None) -> list[Dead]:
    """THE read, shared by both halves so they cannot disagree: `_live_states`
    against every record that CLAIMS to be up.

    Every transitional state is exempt for the reason the module docstring
    gives: a function mid-redeploy (`InProgress`) and a database mid-boot
    (`creating`) genuinely have no container for a moment, and a resource that
    is merely still starting must never be called dead. `created`/`restarting`
    are exempt for the same reason (`_NOT_SERVING` lists only the states that
    mean a container really has stopped serving), and so is a container the
    listing found but `inspect` then declined to describe -- ambiguity, not
    death.

    Costs nothing at all for an env with no lambda/rds record, and one bulk
    listing plus one `inspect` per such record otherwise."""
    functions = _function_records(stores, env)
    databases = _db_records(stores, env)
    if not functions and not databases:
        return []
    # (label, identity, kind, the container that has to be running for it)
    claimed = [(label, function_name, "lambda", name) for label, function_name, name in functions]
    claimed += [
        (label, record["db_instance_identifier"], "rds",
         db_container_name(env, record["db_instance_identifier"]))
        for label, record in databases
    ]
    runtime = containers or ColimaRuntime()
    states = await _live_states(runtime, [container for *_rest, container in claimed])
    if states is None:  # the runtime didn't answer: unknown is not "gone"
        return []
    return [
        Dead(label, identity, kind, await _dead_verdict(runtime, container, states.get(container)))
        for label, identity, kind, container in claimed
        if _not_serving(states.get(container))
    ]


async def live_verdicts(stores: SynthStores, env: str, containers=None) -> dict[str, str]:
    """`label -> verdict` for every lambda/rds whose container is not running
    RIGHT NOW. READ-ONLY: not one record is touched (the module docstring says
    why the projection must not write). `tf_status.project()`'s caller."""
    return {entry.label: entry.verdict for entry in await _dead(stores, env, containers)}


# label -> the model call that writes this kind's failure into its own record.
_CORRECT = {"lambda": mark_function_failed, "rds": rdsctl.mark_instance_failed}


async def sweep_compute(stores: SynthStores, env: str, containers=None) -> dict[str, str]:
    """`live_verdicts` PLUS the record correction -- the same verdict written
    into the record it just proved wrong, which is what makes "re-Apply to
    recreate" true (`converge_functions`/`converge_db_instances` only ever act
    on a `Failed`/`failed` record).

    /apply-full and `DriftSweeper` are its only callers, and they are exactly
    the two places allowed to write: an apply REPORTING the death is what has to
    happen before anything recreates the resource."""
    dead = await _dead(stores, env, containers)
    for entry in dead:
        _CORRECT[entry.kind](stores, env, entry.identity, entry.verdict)
    return {entry.label: entry.verdict for entry in dead}


class DriftSweeper:
    """The cadence + the cache around one sweep. One instance per Reconciler
    (so per env), but the cache is env-keyed anyway so sharing one is safe.

    `containers`/`vms` are injectable seams (the same shape `TaskRuntime`/
    `FunctionRuntime`/`InstanceVm` already use) so the whole sweep is unit-
    testable with no real Docker or Lima involved. They default to the SAME
    substrates the containers/VMs being swept were created on -- a real
    `ColimaRuntime` (ECS tasks and Lambda RIE containers always run on
    Colima, per `TaskRuntime`/`FunctionRuntime`'s own defaults) and a real
    `InstanceVm` (limactl)."""

    def __init__(self, containers=None, vms=None, probe=None) -> None:
        self._containers = containers or ColimaRuntime()
        self._vms = vms or InstanceVm()
        # W2.7: rds's reality check is a real Postgres connection, not a
        # listing (module docstring). Injectable for the same reason the two
        # above are -- a unit test proves the sweep with no database running.
        # `pg_ready`, the COROUTINE form -- `_probe_db` awaits this. It used to
        # default to `pg_ready_sync`, a plain function returning a PgReady
        # dataclass, so every rds half of every drift sweep raised
        # `TypeError: object PgReady can't be used in 'await' expression`.
        # Unnoticed because the only tests reaching the async form are
        # integration-marked.
        self._probe = probe or pg_ready
        self._ticks: dict[str, int] = {}
        self._cache: dict[str, dict[str, str]] = {}

    async def verdicts(self, stores: SynthStores, env: str, sweep: bool = True) -> dict[str, str]:
        """`label -> drift verdict` for every ec2/lambda/rds resource whose real
        VM/container is GONE, or whose database has stopped answering (ecs
        reports through its own task records instead -- see the module
        docstring). Sweeps on the first call and
        every `_sweep_ticks()` calls after; every other call answers from the
        last sweep's cache, so a reported drift stays reported between
        sweeps instead of flapping back to healthy on the very next tick.

        `sweep=False` is cache-ONLY, and it costs nothing: v0.7.3's
        observe-during-apply tick (`Reconciler._watch`) reports what the last
        sweep found without taking a new one, because a sweep does not just
        look -- it CORRECTS records, and doing that off a sample taken while
        tofu has the daemon pinned is the hazard `_sweep_databases`'s own
        confirm-before-correcting note describes. The cadence counter is not
        advanced either, so a suspended apply neither delays nor triggers the
        next real sweep. What that costs -- ec2/lambda/rds go unchecked for the
        rest of the apply plus up to one cadence after it, while ECS does not
        (a vanished task container is caught live by `ecsctl.sweep_tasks`
        inside `tf_status.project` itself) -- is written down as a limit in
        ROADMAP, not just here (field test 4, P4-4).

        This overlay is the FIRST report, not the lasting one: the same sweep
        corrects the underlying record, and a corrected record is no longer a
        candidate -- so the next sweep goes quiet and `tf_status.project()`'s
        own `crashed` + real-reason projection carries the drift from there
        on. Both say the same thing; only the store's version survives a
        restart."""
        count = self._ticks.get(env, 0)
        if sweep:
            self._ticks[env] = count + 1
            if count % _sweep_ticks() == 0:
                self._cache[env] = await self._sweep(stores, env)
        return self._cache.get(env, {})

    async def _sweep(self, stores: SynthStores, env: str) -> dict[str, str]:
        vms = _vm_records(stores, env)
        tasks = _task_records(stores, env)
        # The lambda + rds container check is the LIVE half above, called here
        # rather than reimplemented: one function, one set of sentences, one
        # correction -- so the cadence sweep, the projection and an apply can
        # never disagree about whether a container is up (field test 5).
        out: dict[str, str] = dict(await sweep_compute(stores, env, self._containers))
        # At most ONE listing per substrate, and none at all when this env has
        # nothing of that shape to check (the common case: a canvas with no
        # ec2 node never shells out to limactl).
        # `partial`, not a lambda: `list_names` is a coroutine function now and a
        # lambda cannot hold an `await`. The frozenset moves AFTER the None check
        # because None means "the listing failed" -- reading that as "empty" would
        # flip a whole env to crashed over a transient limactl hiccup.
        live_vms = await _listing(partial(self._vms.list_names, check=True)) if vms else None
        if live_vms is not None:
            live_vms = frozenset(live_vms)
        live_containers = await _listing(self._containers.container_names) if tasks else None
        for label, instance_id, name in vms:
            if live_vms is not None and name not in live_vms:
                out[label] = f"VM {name} deleted outside odin — re-Apply to recreate"
                # The same sentence goes into the RECORD, so tofu's next
                # refresh learns the instance is gone and re-Apply really does
                # recreate it (see the module docstring).
                mark_instance_terminated(stores, env, instance_id, out[label])
        for cluster, task_id, name in tasks:
            if live_containers is not None and name not in live_containers:
                # Same wording as ecsctl's own passive sweep, which races this
                # one for the identical event -- see `container_gone_reason`.
                await mark_task_stopped(stores, env, cluster, task_id, container_gone_reason(name))
        await self._sweep_databases(stores, env, out)
        return out

    async def _probe_db(self, record: dict):
        return await self._probe(
            "127.0.0.1", record["endpoint_port"],
            record["master_username"], record["master_password"],
        )

    async def _sweep_databases(self, stores: SynthStores, env: str, out: dict[str, str]) -> None:
        """rds's REMAINING half: one real `pg_ready` per available instance, and
        a `status` call only when that fails.

        `sweep_compute` has already run, so every record still `available` here
        has a container docker itself calls `running` -- which leaves exactly
        the one failure a listing genuinely cannot see: the container is up and
        Postgres inside it is wedged. That case is still REPORTED and never
        written into the record (a transient probe failure must self-heal
        rather than need a human Apply), so it remains a verdict on this
        cadence, not an apply-failing fault -- see ROADMAP's limits."""
        for label, record in _db_records(stores, env):
            # ONE probe, and the verdict quotes THAT probe. Re-probing to fetch
            # the error text let the reported reason come from a different
            # sample than the one that failed: a second probe that SUCCEEDED
            # carries no error, so the verdict asserted a failure its own
            # newest evidence had just disproved -- and rendered the reason as
            # the literal string "None".
            probe = await self._probe_db(record)
            if probe.ok:
                continue
            identifier = record["db_instance_identifier"]
            name = db_container_name(env, identifier)
            if await self._containers.status(name) == "running":
                # The container is up but Postgres isn't answering. Reported,
                # NOT written into the record: this may be transient, and a
                # corrected record would need a human Apply to undo.
                out[label] = f"Postgres on {name} is not accepting connections: {_probe_reason(probe)}"
                continue
            # CONFIRM BEFORE CORRECTING (found running the real thing): a
            # single failed sample is not proof. Under real load -- a `tofu
            # apply` pulling a 250MB image while the daemon is busy -- a probe
            # can fail AND `docker inspect` can come back empty (which
            # `ColimaRuntime.status` honestly reports as "absent", since it
            # cannot tell "no such container" from "docker didn't answer") for
            # a container that is perfectly alive. Writing `failed` on that
            # sample corrupts the record, and only a human Apply undoes it --
            # the exact failure mode `_listing`'s "unknown is not gone" rule
            # exists to prevent, which this path has to honor too. So: sleep a
            # beat and ask BOTH questions again, and only correct the record
            # when both still say down.
            await asyncio.sleep(_CONFIRM_DELAY)
            if (await self._probe_db(record)).ok or await self._containers.status(name) == "running":
                log.info("drift sweep: %s answered on re-check; treating the first sample as a blip", name)
                continue
            # Field test 2 LOW-17: say the same thing the ecs/lambda halves
            # above say when the container is GONE, and never invent an exit
            # code that was never reported -- `exit_code`'s negative sentinel
            # means "there was nothing to read", which for a container that no
            # longer exists is the truth, not "exit -1".
            exit_code = await self._containers.exit_code(name)
            out[label] = (
                container_gone_reason(name) if exit_code < 0
                else f"container {name} is not running (exit {exit_code}) — re-Apply to recreate"
            )
            # A confirmed death: the same sentence goes into the RECORD, so the
            # canvas keeps showing it after a restart and the next Apply's
            # `converge_db_instances` genuinely re-creates the database.
            rdsctl.mark_instance_failed(stores, env, identifier, out[label])
