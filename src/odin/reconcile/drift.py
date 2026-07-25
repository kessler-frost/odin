"""W2.2 -- the reality sweep for TF-owned COMPUTE: does the VM/container a
synth record CLAIMS to be running actually still exist?

`reconcile/tf_status.py` projects the TF-owned kinds out of odin's OWN synth
stores. That makes the canvas honest about what tofu created, but not about
what's still THERE: `limactl delete odin-ec2-...` a VM or `docker rm -f` an
ECS task container out of band and the record still reads `running`, so
/world reported `healthy` forever (the only reality cross-check was the
startup-only EC2 reaper). This module closes that: one bulk `limactl list`
+ one bulk `docker ps` per sweep -- TWO subprocess calls TOTAL regardless of
how many resources exist, on a cadence (default every 10 reconciler ticks,
~10s at the production 1s poll; `ODIN_DRIFT_SWEEP_TICKS` overrides), with
every other tick answering from the last sweep's cached result.

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

RDS IS CHECKED BY ITS HEALTH PROBE, NOT BY A LISTING -- the one deliberate
departure from the bulk-listing shape, and it's what preserves the pre-W2.7
crash/recover behavior exactly (the reconciler used to run `pg_ready` against
every rds node on EVERY tick; running it on the sweep cadence is strictly
cheaper). Two reasons a listing can't do this job:
  - `ColimaRuntime.container_names` lists exited containers too, so a
    `docker kill`ed Postgres -- the canonical way a database dies -- would look
    perfectly present in it.
  - A container that's up while Postgres inside it is wedged is ALSO down as
    far as any consumer of the DATABASE_URL fact is concerned, and only a real
    connection can tell.
So each `available` instance gets one `pg_ready_sync` against its stored port
(no subprocess at all), and only when that FAILS does the sweep spend a single
`status` call to find out which failure it is:
  - container gone/exited -> a real crash: the record is corrected to `failed`
    (with the container's real exit code) and the next Apply's converge
    genuinely brings it back.
  - container still running -> reported as a verdict but the RECORD IS LEFT
    ALONE, so a transient probe failure self-heals on the next sweep instead of
    needing a human Apply. (This mirrors the old reconciler exactly: it only
    cleared and recreated the container on a real exit, and otherwise just
    surfaced the connection error.)

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
import os
import time

from odin.aws.rds import container_name as db_container_name
from odin.compute.functions import container_name as function_container_name
from odin.compute.instances import InstanceVm, vm_name
from odin.compute.tasks import container_name as task_container_name
from odin.gateway.models import rdsctl
from odin.gateway.models.ec2compute import mark_instance_terminated
from odin.gateway.models.ecsctl import mark_task_stopped
from odin.gateway.models.lambdactl import mark_function_failed
from odin.gateway.stores import SynthStores
from odin.reconcile.assertions import pg_ready_sync
from odin.runtime.colima import ColimaRuntime

log = logging.getLogger("odin.reconcile.drift")

_DEFAULT_SWEEP_TICKS = 10

# How long the rds half waits before re-asking "is it really down?" (see
# `_sweep_databases`). Short enough that a genuinely dead database is still
# reported on this same sweep, long enough to outlast a busy-daemon blip. Only
# ever paid on the failure path, and the whole sweep already runs off the event
# loop (`Reconciler._drift_verdicts` uses asyncio.to_thread).
_CONFIRM_DELAY = 1.0


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


def _listing(read) -> frozenset[str] | None:
    """One bulk listing, or None when the CLI call itself failed. LOAD-
    BEARING: an empty listing and a failed listing are NOT the same thing --
    reading "docker isn't answering" as "every container is gone" would flip
    a whole env to `crashed` over a transient hiccup. None means "unknown",
    and an unknown sweep reports no drift at all."""
    try:
        return frozenset(read())
    except Exception as exc:  # noqa: BLE001 -- any CLI/parse failure means "unknown"
        log.warning("drift sweep listing failed (%s); reporting no drift this pass", exc)
        return None


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
        self._probe = probe or pg_ready_sync
        self._ticks: dict[str, int] = {}
        self._cache: dict[str, dict[str, str]] = {}

    def verdicts(self, stores: SynthStores, env: str, sweep: bool = True) -> dict[str, str]:
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
                self._cache[env] = self._sweep(stores, env)
        return self._cache.get(env, {})

    def _sweep(self, stores: SynthStores, env: str) -> dict[str, str]:
        vms = _vm_records(stores, env)
        functions = _function_records(stores, env)
        tasks = _task_records(stores, env)
        # At most ONE listing per substrate, and none at all when this env has
        # nothing of that shape to check (the common case: a canvas with no
        # ec2 node never shells out to limactl).
        live_vms = _listing(lambda: self._vms.list_names(check=True)) if vms else None
        live_containers = _listing(self._containers.container_names) if functions or tasks else None
        out: dict[str, str] = {}
        for label, instance_id, name in vms:
            if live_vms is not None and name not in live_vms:
                out[label] = f"VM {name} deleted outside odin — re-Apply to recreate"
                # The same sentence goes into the RECORD, so tofu's next
                # refresh learns the instance is gone and re-Apply really does
                # recreate it (see the module docstring).
                mark_instance_terminated(stores, env, instance_id, out[label])
        for label, function_name, name in functions:
            if live_containers is not None and name not in live_containers:
                out[label] = f"container {name} removed outside odin — re-Apply to recreate"
                # Same sentence into the RECORD: a function whose sandbox is
                # gone is `Failed`, and an Apply's `converge_functions` is what
                # actually brings it back (see the module docstring).
                mark_function_failed(stores, env, function_name, out[label])
        for cluster, task_id, name in tasks:
            if live_containers is not None and name not in live_containers:
                mark_task_stopped(
                    stores, env, cluster, task_id,
                    f"container {name} removed outside odin — re-Apply to recreate",
                )
        self._sweep_databases(stores, env, out)
        return out

    def _probe_db(self, record: dict):
        return self._probe(
            "127.0.0.1", record["endpoint_port"],
            record["master_username"], record["master_password"],
        )

    def _sweep_databases(self, stores: SynthStores, env: str, out: dict[str, str]) -> None:
        """rds's half: one real `pg_ready` per available instance, and a
        `status` call only when that fails (see the module docstring for why a
        listing can't answer this)."""
        for label, record in _db_records(stores, env):
            if self._probe_db(record).ok:
                continue
            identifier = record["db_instance_identifier"]
            name = db_container_name(env, identifier)
            if self._containers.status(name) == "running":
                # The container is up but Postgres isn't answering. Reported,
                # NOT written into the record: this may be transient, and a
                # corrected record would need a human Apply to undo.
                out[label] = f"Postgres on {name} is not accepting connections: {self._probe_db(record).error}"
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
            time.sleep(_CONFIRM_DELAY)
            if self._probe_db(record).ok or self._containers.status(name) == "running":
                log.info("drift sweep: %s answered on re-check; treating the first sample as a blip", name)
                continue
            # Field test 2 LOW-17: say the same thing the ecs/lambda halves
            # above say when the container is GONE, and never invent an exit
            # code that was never reported -- `exit_code`'s negative sentinel
            # means "there was nothing to read", which for a container that no
            # longer exists is the truth, not "exit -1".
            exit_code = self._containers.exit_code(name)
            out[label] = (
                f"container {name} removed outside odin — re-Apply to recreate" if exit_code < 0
                else f"container {name} is not running (exit {exit_code}) — re-Apply to recreate"
            )
            # A confirmed death: the same sentence goes into the RECORD, so the
            # canvas keeps showing it after a restart and the next Apply's
            # `converge_db_instances` genuinely re-creates the database.
            rdsctl.mark_instance_failed(stores, env, identifier, out[label])
