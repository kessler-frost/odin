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
turns that into a WorldDelta + an error log line, and nothing else happens.

WHICH KINDS: only the three with a real runtime footprint -- ec2 (a real Lima
VM), ecs (a real task container), lambda (a real RIE container). vpc/subnet/
sg/iam_role have NO runtime footprint at all (a VPC is a Nebula network
config + a JSON record, an SG is a compiled firewall, an IAM role is a policy
document -- there is nothing to `docker ps` or `limactl list` for them, so a
reality sweep could neither confirm nor deny them). `ecr` is out too: an ECR
*repository* is a control-plane record (gateway/models/ecr.py), and the
registry:2 CONTAINER its image bytes live in is a per-env BACKING, already
supervised by `aws/backings.py`'s own ensure/gc lifecycle -- not this
projection's business.

MID-BOOT IS NOT DRIFT (the sharpest edge here): a record is only swept when
it CLAIMS to be up -- ec2 `running`, lambda `Active` and not mid-redeploy,
ecs task `RUNNING`. Every transitional state is exempt, because odin's real
substrates all boot on background threads (`ec2compute._finish_boot`,
`lambdactl`'s v0.5.4 create/update thread, `ecsctl._launch_task`) and a VM or
container that limactl/docker hasn't registered YET is not gone -- it's
starting. Same in reverse for `shutting-down`/`stopping` (mid-delete) and
`stopped`/`Failed`/`STOPPED` (already crashed, with their own real reasons).

THE ECS EXCEPTION -- why one kind writes reality back into its store instead
of just overlaying a verdict: an ECS *task* is not a TF resource (real
terraform never manages one; real ECS's own service scheduler replaces a lost
task). So for ecs the honest record of "this task's container is gone" is the
task record itself, marked STOPPED exactly as `ecsctl.sweep_tasks` already
marks a container that exited on its own -- which (a) gives the crashed
projection + verdict through the machinery wave 1 already built, and (b) is
what makes re-Apply actually converge: `ecsctl.converge_services` relaunches
the missing task, while an `aws_ecs_service` whose config never changed would
give tofu an empty plan forever. ec2/lambda get a verdict OVERLAY only,
never a store rewrite: writing `terminated` into an ec2 record would EXCLUDE
it from the projection entirely (tf_status.py's own release-sweep finding #2)
-- odin would silently forget a VM tofu still believes exists, which is the
opposite of honest.
"""
from __future__ import annotations

import logging
import os

from odin.compute.functions import container_name as function_container_name
from odin.compute.instances import InstanceVm, vm_name
from odin.compute.tasks import container_name as task_container_name
from odin.gateway.models.ecsctl import mark_task_stopped
from odin.gateway.stores import SynthStores
from odin.runtime.colima import ColimaRuntime

log = logging.getLogger("odin.reconcile.drift")

_DEFAULT_SWEEP_TICKS = 10


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


def _vm_records(stores: SynthStores, env: str) -> list[tuple[str, str]]:
    """(label, real VM name) for every ec2 record claiming `running`. `ec2`
    has no AWS-native name field, so the `odin:node` tag is the only route
    back to a label (tf_status.py's own note) -- an untagged instance isn't
    projected into World either, so there'd be nothing to report drift on."""
    out: list[tuple[str, str]] = []
    for key, record in stores.ec2compute.items(env).items():
        if not key.startswith("instance:") or record["state_name"] != "running":
            continue
        label = _label(stores.tags.get(env, f"ec2:{record['instance_id']}", {}))
        if label:
            out.append((label, vm_name(env, record["instance_id"])))
    return out


def _function_records(stores: SynthStores, env: str) -> list[tuple[str, str]]:
    """(label, RIE container name) for every function claiming `Active` and
    NOT mid-redeploy. `LastUpdateStatus == "InProgress"` is the exempt
    window: `FunctionRuntime.ensure` deliberately `stop`s (rm -f) the old
    container before running the new one, so the container is legitimately
    absent for a moment while `State` still reads Active (lambdactl.py's two
    independent state machines)."""
    out: list[tuple[str, str]] = []
    for key, record in stores.lambdactl.items(env).items():
        if not key.startswith("fn:") or record["state"] != "Active":
            continue
        if record.get("last_update_status") == "InProgress":
            continue
        label = _label(
            stores.tags.get(env, f"lambda:{record['function_arn']}", {}), record["function_name"],
        )
        if label:
            out.append((label, function_container_name(env, record["function_name"])))
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

    def __init__(self, containers=None, vms=None) -> None:
        self._containers = containers or ColimaRuntime()
        self._vms = vms or InstanceVm()
        self._ticks: dict[str, int] = {}
        self._cache: dict[str, dict[str, str]] = {}

    def verdicts(self, stores: SynthStores, env: str) -> dict[str, str]:
        """`label -> drift verdict` for every ec2/lambda resource whose real
        VM/container is GONE (ecs reports through its own task records
        instead -- see the module docstring). Sweeps on the first call and
        every `_sweep_ticks()` calls after; every other call answers from the
        last sweep's cache, so a reported drift stays reported between
        sweeps instead of flapping back to healthy on the very next tick."""
        count = self._ticks.get(env, 0)
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
        for label, name in vms:
            if live_vms is not None and name not in live_vms:
                out[label] = f"VM {name} deleted outside odin — re-Apply to recreate"
        for label, name in functions:
            if live_containers is not None and name not in live_containers:
                out[label] = f"container {name} removed outside odin — re-Apply to recreate"
        for cluster, task_id, name in tasks:
            if live_containers is not None and name not in live_containers:
                mark_task_stopped(
                    stores, env, cluster, task_id,
                    f"container {name} removed outside odin — re-Apply to recreate",
                )
        return out
