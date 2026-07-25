"""Pre-apply admission control (owner directive B): a local dev tool has no
scheduler and no business pretending to -- this is a guardrail, not a
bin-packer. `check_admission` sums the desired Stack's ESTIMATED resource
footprint and compares it against real headroom BEFORE Apply spawns a single
container or VM; a stack that would exceed a budget is rejected with an honest
message naming the numbers, not discovered only after 20 concurrent EC2 boots
have already started thrashing the Mac.

TWO DISJOINT POOLS (field test 2, finding MEDIUM-9). Everything used to be
charged against `HostFacts.total_mem_mib`, which is `docker info`'s MemTotal
i.e. COLIMA'S VM (`runtime/colima.py::ensure_host`) -- so on a 48 GiB Mac the
rejection said "the admission budget is 4.0 GiB (5.8 GiB total on this host)",
which is false, and a 5 x t3.micro canvas was rejected as too big for a machine
with 43 GiB to spare. The two substrates are genuinely separate:

- CONTAINER pool -- `rds`/`ecs`/`lambda`/`elasticache`/`alb` and the shared
  `s3`/`sqs`/`sns`/`dynamodb` backings all run as containers inside the
  container runtime, so `HostFacts.total_mem_mib` really is their ceiling.
- HOST/VM pool -- an `ec2` node is a REAL Lima VM created by `limactl`
  (`compute/instances.py` -> `compute/lima_yaml.py`), sized straight from
  `INSTANCE_TYPES`. It is allocated by Virtualization.framework from the Mac's
  own RAM and consumes ZERO of the container runtime's memory, so it must be
  charged against, and quoted against, real host memory
  (`host_total_mem_mib()`).

Each pool is checked against its own budget, and a rejection quotes only that
pool's numbers, described as what they actually are. An unknown total (either
pool) SKIPS that pool's check rather than printing a confident wrong figure.

Memory estimation, by kind:
- `ec2`: the real per-instance-type memory (`compute.models.INSTANCE_TYPES`,
  the SAME table `gateway/models/ec2compute.py` uses for the real Lima VM) --
  this is exact, not a guess. Charged to the HOST pool.
- `rds`/`ecs`/`lambda`/`elasticache`/`alb`: a modest FIXED estimate per node --
  each spawns its OWN container (a Postgres, a task container, an RIE
  container, a Redis, an nginx reverse proxy), so per-node charging is right.
  The ecs, elasticache and alb figures mirror `compute/tasks.py`'s,
  `aws/cache.py`'s and `compute/proxy.py`'s own default container memory caps,
  so each estimate and the actual runtime ceiling agree.
- `s3`/`sqs`/`sns`/`dynamodb`: charged ONCE PER ENV, not per node -- these
  ride a single shared per-env backing container (RustFS/goaws/dynalite)
  regardless of how many buckets/queues/topics/tables are drawn, so per-node
  charging would wildly over-count a canvas with many small resources.
- `vpc`/`subnet`/`sg`/`iam_role`/`ecr`: no separate container/VM of their
  own (Nebula/gateway-model bookkeeping only) -- zero footprint. (`ecr`'s
  registry:2 is a shared per-env backing, not charged per node.)

Each budget is a percentage of its pool's TOTAL memory, not currently-free
memory, matching the owner's own framing ("70% of total RAM"): simple, doesn't
need to sum every already-running container's live usage, and leaves headroom
for the host OS + the runtime itself. **Recorded limit:** it is a static
per-canvas estimate, so it does not see memory actually in use -- two envs can
each pass this check and jointly overcommit. Making it cross-env would mean
summing every OTHER env's applied Stack here, which needs the caller to hand
this function those stacks; not wired today, and said out loud in ROADMAP
rather than implied away.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage

from odin.aws.cache import DEFAULT_MEMORY_MIB as CACHE_MEMORY_MIB
from odin.compute.models import get_instance_type
from odin.runtime.driver import HostFacts
from odin.spec.models import ResourceDesired, Stack

# rds/ecs/lambda/elasticache/alb: a modest fixed estimate per NODE (each spawns
# its own container). ecs's figure matches compute/tasks.py's own
# `_DEFAULT_MEMORY_MIB` fallback cap and elasticache's matches
# `aws/cache.py::DEFAULT_MEMORY_MIB` -- the estimate and the real runtime
# ceiling agree.
_PER_NODE_MEMORY_MIB: dict[str, float] = {
    "rds": 256.0,
    "ecs": 512.0,
    "lambda": 256.0,
    "elasticache": CACHE_MEMORY_MIB,
    # W2.5: one nginx reverse-proxy container per load balancer
    # (compute/proxy.py) -- this figure IS that module's own `_MEMORY_MIB` cap,
    # so the estimate and the real runtime ceiling agree, same rule as ecs.
    "alb": 64.0,
}

# s3/sqs/sns/dynamodb: a modest fixed estimate per ENV (a SHARED backing
# container regardless of node count) -- charged once, the first time the
# stack draws on it, never per node.
_BACKING_MEMORY_MIB: dict[str, float] = {
    "s3": 256.0, "sqs": 128.0, "sns": 128.0, "dynamodb": 256.0,
}

_DEFAULT_BUDGET_RATIO = 0.7  # >70% of a pool's total memory -- the owner's own number
_DEFAULT_MIN_DISK_GIB = 10.0  # matches cli/doctor.py's own MIN_DISK_GIB
_CONTAINER_BUDGET_ENV = "ODIN_MEMORY_BUDGET_MIB"
_VM_BUDGET_ENV = "ODIN_VM_MEMORY_BUDGET_MIB"


def _gib_str_to_mib(value: str) -> float:
    """`VmConfig.memory` is always `"<N>GiB"` (`compute/models.py`'s own
    table) -- the one unit this module ever needs to parse."""
    return float(value.removesuffix("GiB")) * 1024.0


def _ec2_memory_mib(res: ResourceDesired) -> float:
    instance_type_field = res.fields.get("instanceType")
    instance_type = instance_type_field.value if instance_type_field is not None else "t3.micro"
    return _gib_str_to_mib(get_instance_type(instance_type).memory)


def host_total_mem_mib() -> float:
    """The REAL machine's total RAM, in MiB -- the ceiling an `ec2` node's Lima
    VM is actually allocated from.

    `os.sysconf` rather than a `sysctl hw.memsize` subprocess or a new
    dependency: it is stdlib, non-blocking (so `check_admission` stays cheap
    enough to run on every Apply), and answers on both macOS and Linux. A
    platform that doesn't define these keys returns 0.0 -- the same "unknown"
    sentinel `HostFacts()` uses -- which SKIPS the VM check rather than
    inventing a number for the rejection message to state as fact."""
    names = os.sysconf_names
    if "SC_PHYS_PAGES" not in names or "SC_PAGE_SIZE" not in names:
        return 0.0
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**20


@dataclass(frozen=True)
class StackFootprint:
    """A canvas's estimated memory footprint, split by SUBSTRATE -- the two
    pools are disjoint (module docstring), so they are never summed for a
    budget comparison, only for display."""
    container_mib: float = 0.0
    vm_mib: float = 0.0

    @property
    def total_mib(self) -> float:
        return self.container_mib + self.vm_mib


def estimate_stack_footprint(stack: Stack) -> StackFootprint:
    """The Stack's estimated footprint per pool -- see the module docstring for
    the per-kind rules. Pure and total: every resource kind lands in exactly one
    of ec2 (VM pool) / per-node / backing / zero-footprint."""
    container = 0.0
    vm = 0.0
    backings_needed: set[str] = set()
    for res in stack.resources:
        if res.kind == "ec2":
            vm += _ec2_memory_mib(res)
        elif res.kind in _PER_NODE_MEMORY_MIB:
            container += _PER_NODE_MEMORY_MIB[res.kind]
        elif res.kind in _BACKING_MEMORY_MIB:
            backings_needed.add(res.kind)
    container += sum(_BACKING_MEMORY_MIB[kind] for kind in backings_needed)
    return StackFootprint(container_mib=container, vm_mib=vm)


def estimate_stack_memory_mib(stack: Stack) -> float:
    """The Stack's estimated TOTAL memory footprint across both pools, in MiB --
    the honest "how much RAM does this canvas want" figure for display. Never
    compared against a budget (the pools are disjoint); `check_admission` uses
    `estimate_stack_footprint`."""
    return estimate_stack_footprint(stack).total_mib


def default_memory_budget_mib(total_mem_mib: float) -> float:
    """The CONTAINER pool's budget. `ODIN_MEMORY_BUDGET_MIB` overrides it
    outright (an absolute MiB figure); otherwise it's `_DEFAULT_BUDGET_RATIO` of
    the container runtime's total memory (`HostFacts.total_mem_mib`). Read fresh
    on every call (not cached at import), same convention as
    `agent/translate.py`'s `_default_timeout`."""
    override = os.environ.get(_CONTAINER_BUDGET_ENV)
    if override:
        return float(override)
    return total_mem_mib * _DEFAULT_BUDGET_RATIO


def default_vm_budget_mib(host_mem_mib: float) -> float:
    """The HOST/VM pool's budget (`ec2` nodes = real Lima VMs).
    `ODIN_VM_MEMORY_BUDGET_MIB` overrides it outright; otherwise it's
    `_DEFAULT_BUDGET_RATIO` of REAL host memory."""
    override = os.environ.get(_VM_BUDGET_ENV)
    if override:
        return float(override)
    return host_mem_mib * _DEFAULT_BUDGET_RATIO


def default_min_disk_gib() -> float:
    """`ODIN_MIN_DISK_GIB` overrides the free-disk floor -- default matches
    `cli/doctor.py`'s own `MIN_DISK_GIB`, so `odin doctor` and the live
    admission check agree on what "enough disk" means."""
    return float(os.environ.get("ODIN_MIN_DISK_GIB", str(_DEFAULT_MIN_DISK_GIB)))


@dataclass(frozen=True)
class AdmissionResult:
    """`estimated_mib` is always the canvas's TOTAL estimate across both pools.
    `budget_mib` is the budget of the pool the `reason` names on a rejection,
    and the container pool's budget when nothing was rejected -- the two
    per-pool pairs below carry the full, unambiguous truth."""
    ok: bool
    reason: str = ""
    estimated_mib: float = 0.0
    budget_mib: float = 0.0
    free_disk_gib: float = 0.0
    min_disk_gib: float = 0.0
    container_mib: float = 0.0
    container_budget_mib: float = 0.0
    vm_mib: float = 0.0
    vm_budget_mib: float = 0.0


def _gib(mib: float) -> float:
    return mib / 1024.0


@dataclass(frozen=True)
class _Pool:
    """One of the two disjoint memory pools, with everything the rejection
    message needs to be TRUE: what was estimated, the budget, and an honest
    description of where that budget came from (never "total on this host" for
    a number that is actually the container runtime's)."""
    wants: str        # what this pool's share of the canvas IS
    ceiling: str      # an honest description of the budget's origin
    advice: str
    estimated_mib: float
    budget_mib: float

    @property
    def exceeded(self) -> bool:
        # A zero budget means "unknown total" (docker info failed / sysconf
        # has no answer) -- skip, rather than reject on a nonsense 0 GiB.
        return self.budget_mib > 0 and self.estimated_mib > self.budget_mib

    @property
    def reason(self) -> str:
        return (
            f"this canvas needs ~{_gib(self.estimated_mib):.1f} GiB {self.wants}; the admission "
            f"budget is {_gib(self.budget_mib):.1f} GiB {self.ceiling} -- {self.advice}"
        )


def _budget_origin(env_var: str, total_mib: float, described: str) -> str:
    """Either the env override that set the budget outright, or the pool total
    the default ratio was taken from -- so the parenthetical in a rejection is
    never a number nobody can check."""
    if os.environ.get(env_var):
        return f"({env_var})"
    return f"({_gib(total_mib):.1f} GiB {described})"


def _pools(footprint: StackFootprint, host: HostFacts, host_mem_mib: float) -> tuple[_Pool, _Pool]:
    return (
        _Pool(
            wants="of container memory",
            ceiling=_budget_origin(
                _CONTAINER_BUDGET_ENV, host.total_mem_mib,
                "reported by the container runtime -- that is Colima's VM, not the whole machine",
            ),
            advice=(
                "apply fewer container-backed nodes, or give the container runtime more memory "
                "(`colima stop && colima start --memory N`)"
            ),
            estimated_mib=footprint.container_mib,
            budget_mib=default_memory_budget_mib(host.total_mem_mib),
        ),
        _Pool(
            # EC2 nodes are Lima VMs allocated from the Mac's own RAM, so this
            # pool -- and only this pool -- may speak of "this host".
            wants="of memory for its EC2 instances (each one is a real Lima VM on the host)",
            ceiling=_budget_origin(_VM_BUDGET_ENV, host_mem_mib, "total on this host"),
            advice="reduce instance sizes or apply fewer nodes",
            estimated_mib=footprint.vm_mib,
            budget_mib=default_vm_budget_mib(host_mem_mib),
        ),
    )


def _existing_ancestor(path: Path) -> Path:
    """`disk_usage` needs a path that actually exists; the store root
    (`.odin/`) may not yet -- a brand-new install's very first Apply, before
    anything has ever been written -- so walk up to the nearest existing
    ancestor (eventually the filesystem root, always present) rather than
    creating a directory as a side effect of a read-only check."""
    while not path.exists():
        path = path.parent
    return path


def check_admission(
    stack: Stack, host: HostFacts, disk_path: Path, host_mem_mib: float | None = None,
) -> AdmissionResult:
    """The whole guardrail: estimate the Stack's footprint per SUBSTRATE, compare
    each against its own pool's budget, then check free disk on the volume
    holding `disk_path` (the store root -- `.odin/`, images, containers all land
    there). Memory is checked first (typically the more informative rejection
    reason for a local dev canvas); any failure is terminal -- `ok=False` with
    `reason` naming the actual numbers, never a bare "rejected".

    `host` carries the CONTAINER runtime's memory (`ensure_host()` ->
    `docker info` MemTotal). `host_mem_mib` is REAL machine memory for the
    VM pool; it defaults to `host_total_mem_mib()` and exists as a parameter so
    tests are deterministic rather than machine-dependent.

    A zero total for either pool means "unknown" (`docker info` failed because
    Colima isn't running; `os.sysconf` has no answer) and SKIPS that pool's
    check rather than rejecting on a nonsense "0 GiB budget" -- Apply will fail
    with a far clearer error at the actual container/VM step. The matching env
    override still applies in that case (a nonzero override always enforces its
    pool). The two pools are independent: Colima being down says nothing about
    the Mac's RAM, so an EC2 canvas is still checked."""
    footprint = estimate_stack_footprint(stack)
    pools = _pools(footprint, host, host_total_mem_mib() if host_mem_mib is None else host_mem_mib)
    container_pool, vm_pool = pools
    free_disk_gib = disk_usage(_existing_ancestor(disk_path)).free / 2**30
    min_disk_gib = default_min_disk_gib()
    numbers = {
        "estimated_mib": footprint.total_mib,
        "free_disk_gib": free_disk_gib,
        "min_disk_gib": min_disk_gib,
        "container_mib": footprint.container_mib,
        "container_budget_mib": container_pool.budget_mib,
        "vm_mib": footprint.vm_mib,
        "vm_budget_mib": vm_pool.budget_mib,
    }

    rejected = next((pool for pool in pools if pool.exceeded), None)
    if rejected is not None:
        return AdmissionResult(ok=False, reason=rejected.reason, budget_mib=rejected.budget_mib, **numbers)
    if free_disk_gib < min_disk_gib:
        reason = (
            f"only {free_disk_gib:.1f} GiB free disk (need >{min_disk_gib:.0f} GiB) -- "
            "free up space before applying"
        )
        return AdmissionResult(ok=False, reason=reason, budget_mib=container_pool.budget_mib, **numbers)
    return AdmissionResult(ok=True, budget_mib=container_pool.budget_mib, **numbers)
