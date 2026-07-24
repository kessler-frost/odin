"""Pre-apply admission control (owner directive B): a local dev tool has no
scheduler and no business pretending to -- this is a guardrail, not a
bin-packer. `check_admission` sums the desired Stack's ESTIMATED resource
footprint and compares it against real host headroom BEFORE Apply spawns a
single container or VM; a stack that would exceed the budget is rejected with
an honest message naming the numbers, not discovered only after 20 concurrent
EC2 boots have already started thrashing the Mac.

Memory estimation, by kind:
- `ec2`: the real per-instance-type memory (`compute.models.INSTANCE_TYPES`,
  the SAME table `gateway/models/ec2compute.py` uses for the real Lima VM) --
  this is exact, not a guess.
- `rds`/`ecs`/`lambda`: a modest FIXED estimate per node -- each spawns its
  OWN container (a Postgres, a task container, an RIE container), so per-node
  charging is right. The ecs figure mirrors `compute/tasks.py`'s own default
  container memory cap, so the estimate and the actual runtime ceiling agree.
- `s3`/`sqs`/`sns`/`dynamodb`: charged ONCE PER ENV, not per node -- these
  ride a single shared per-env backing container (RustFS/goaws/dynalite)
  regardless of how many buckets/queues/topics/tables are drawn, so per-node
  charging would wildly over-count a canvas with many small resources.
- `vpc`/`subnet`/`sg`/`iam_role`/`ecr`: no separate container/VM of their
  own (Nebula/gateway-model bookkeeping only) -- zero footprint.

The budget itself is against `HostFacts.total_mem_mib` (`runtime.ensure_host()`
-- collected today, never used until now) -- a percentage of TOTAL memory,
not currently-free memory, matching the owner's own framing ("70% of total
RAM"): simple, doesn't need to sum every already-running container's live
usage, and leaves headroom for the host OS + Colima itself.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage

from odin.compute.models import get_instance_type
from odin.runtime.driver import HostFacts
from odin.spec.models import ResourceDesired, Stack

# rds/ecs/lambda: a modest fixed estimate per NODE (each spawns its own
# container). ecs's figure matches compute/tasks.py's own `_DEFAULT_MEMORY_MIB`
# fallback cap -- the estimate and the real runtime ceiling agree.
_PER_NODE_MEMORY_MIB: dict[str, float] = {
    "rds": 256.0,
    "ecs": 512.0,
    "lambda": 256.0,
}

# s3/sqs/sns/dynamodb: a modest fixed estimate per ENV (a SHARED backing
# container regardless of node count) -- charged once, the first time the
# stack draws on it, never per node.
_BACKING_MEMORY_MIB: dict[str, float] = {
    "s3": 256.0, "sqs": 128.0, "sns": 128.0, "dynamodb": 256.0,
}

_DEFAULT_BUDGET_RATIO = 0.7  # >70% of total host memory -- the owner's own number
_DEFAULT_MIN_DISK_GIB = 10.0  # matches cli/doctor.py's own MIN_DISK_GIB


def _gib_str_to_mib(value: str) -> float:
    """`VmConfig.memory` is always `"<N>GiB"` (`compute/models.py`'s own
    table) -- the one unit this module ever needs to parse."""
    return float(value.removesuffix("GiB")) * 1024.0


def _ec2_memory_mib(res: ResourceDesired) -> float:
    instance_type_field = res.fields.get("instanceType")
    instance_type = instance_type_field.value if instance_type_field is not None else "t3.micro"
    return _gib_str_to_mib(get_instance_type(instance_type).memory)


def estimate_stack_memory_mib(stack: Stack) -> float:
    """The Stack's estimated total memory footprint, in MiB -- see the
    module docstring for the per-kind rules. Pure and total: every resource
    kind lands in exactly one of ec2/per-node/backing/zero-footprint."""
    total = 0.0
    backings_needed: set[str] = set()
    for res in stack.resources:
        if res.kind == "ec2":
            total += _ec2_memory_mib(res)
        elif res.kind in _PER_NODE_MEMORY_MIB:
            total += _PER_NODE_MEMORY_MIB[res.kind]
        elif res.kind in _BACKING_MEMORY_MIB:
            backings_needed.add(res.kind)
    total += sum(_BACKING_MEMORY_MIB[kind] for kind in backings_needed)
    return total


def default_memory_budget_mib(total_mem_mib: float) -> float:
    """`ODIN_MEMORY_BUDGET_MIB` overrides the budget outright (an absolute
    MiB figure); otherwise it's `_DEFAULT_BUDGET_RATIO` of the host's total
    memory (`HostFacts.total_mem_mib`). Read fresh on every call (not cached
    at import), same convention as `agent/translate.py`'s `_default_timeout`."""
    override = os.environ.get("ODIN_MEMORY_BUDGET_MIB")
    if override:
        return float(override)
    return total_mem_mib * _DEFAULT_BUDGET_RATIO


def default_min_disk_gib() -> float:
    """`ODIN_MIN_DISK_GIB` overrides the free-disk floor -- default matches
    `cli/doctor.py`'s own `MIN_DISK_GIB`, so `odin doctor` and the live
    admission check agree on what "enough disk" means."""
    return float(os.environ.get("ODIN_MIN_DISK_GIB", str(_DEFAULT_MIN_DISK_GIB)))


@dataclass(frozen=True)
class AdmissionResult:
    ok: bool
    reason: str = ""
    estimated_mib: float = 0.0
    budget_mib: float = 0.0
    free_disk_gib: float = 0.0
    min_disk_gib: float = 0.0


def _gib(mib: float) -> float:
    return mib / 1024.0


def _existing_ancestor(path: Path) -> Path:
    """`disk_usage` needs a path that actually exists; the store root
    (`.odin/`) may not yet -- a brand-new install's very first Apply, before
    anything has ever been written -- so walk up to the nearest existing
    ancestor (eventually the filesystem root, always present) rather than
    creating a directory as a side effect of a read-only check."""
    while not path.exists():
        path = path.parent
    return path


def check_admission(stack: Stack, host: HostFacts, disk_path: Path) -> AdmissionResult:
    """The whole guardrail: estimate the Stack's memory footprint, compare
    against `host`'s budget, then check free disk on the volume holding
    `disk_path` (the store root -- `.odin/`, images, containers all land
    there). Memory is checked first (typically the more informative
    rejection reason for a local dev canvas); either failure is terminal --
    `ok=False` with `reason` naming the actual numbers, never a bare
    "rejected".

    `budget == 0` (host.total_mem_mib unknown -- `ensure_host()` returns this
    when `docker info` fails, e.g. Colima isn't running) skips the memory
    check entirely rather than rejecting on a nonsense "0 GiB budget": Apply
    will fail with a far clearer error at the actual container/VM step, and
    `ODIN_MEMORY_BUDGET_MIB` still overrides this (a nonzero override always
    applies the check even when the host total is unknown)."""
    estimated = estimate_stack_memory_mib(stack)
    budget = default_memory_budget_mib(host.total_mem_mib)
    free_disk_gib = disk_usage(_existing_ancestor(disk_path)).free / 2**30
    min_disk_gib = default_min_disk_gib()

    if budget > 0 and estimated > budget:
        reason = (
            f"this canvas needs ~{_gib(estimated):.1f} GiB of memory; the admission "
            f"budget is {_gib(budget):.1f} GiB ({_gib(host.total_mem_mib):.1f} GiB total on "
            "this host) -- reduce instance sizes or apply fewer nodes"
        )
        return AdmissionResult(
            ok=False, reason=reason, estimated_mib=estimated, budget_mib=budget,
            free_disk_gib=free_disk_gib, min_disk_gib=min_disk_gib,
        )
    if free_disk_gib < min_disk_gib:
        reason = (
            f"only {free_disk_gib:.1f} GiB free disk (need >{min_disk_gib:.0f} GiB) -- "
            "free up space before applying"
        )
        return AdmissionResult(
            ok=False, reason=reason, estimated_mib=estimated, budget_mib=budget,
            free_disk_gib=free_disk_gib, min_disk_gib=min_disk_gib,
        )
    return AdmissionResult(
        ok=True, estimated_mib=estimated, budget_mib=budget,
        free_disk_gib=free_disk_gib, min_disk_gib=min_disk_gib,
    )
