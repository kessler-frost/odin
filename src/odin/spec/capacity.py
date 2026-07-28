"""Can the instances a canvas draws actually hold the workloads placed in them?

The third of the four costs `docs/intelligence-layer.md` named when ECS-on-EC2
placement was designed: *"A VM has finite memory; several tasks placed in one
instance can exhaust it."*

An EC2 node is a real Lima VM sized by its instance type (`t3.micro` -> 1GiB,
`compute/models.py::INSTANCE_TYPES`), and every ECS task gets a real memory cap
(its task definition's `memory`, else 512 MiB --
`compute/tasks.py::_DEFAULT_MEMORY_MIB`). So "three services of two tasks each,
drawn inside a t3.micro" is not a configuration odin can honour, and the
question is only whether it says so or discovers it as OOM-killed containers
some minutes later.

Real ECS answers this with capacity providers, which odin has no model for. What
it CAN do honestly is arithmetic: refuse before applying, naming the instance,
what was asked of it and what it has. That is the same shape as the wiring guard
(`agent/hcl.py`'s unresolvable-ref refusal, a 409 before tofu runs) rather than
a new mechanism.

## What this deliberately does NOT do

It does not reserve, schedule, or pack. The parked app layer
(`app-layer-parked`) had a memory-aware scheduler and this is not a revival of
it -- it is one sum per instance, checked once, before anything is built. A
canvas that fits proceeds exactly as before.

It also does not count the VM's own overhead. The guest kernel, containerd and
nerdctl all take memory the tasks cannot use, so a canvas that exactly fills an
instance on paper may still struggle. `_HEADROOM_MIB` reserves a little for
that, and the message reports the number it actually used rather than pretending
the whole VM is available to workloads.
"""
from __future__ import annotations

from odin.compute.models import get_instance_type
from odin.spec.models import ResourceDesired, Stack

# Task memory, in MiB, when the task definition does not set one. Mirrors
# `compute/tasks.py::_DEFAULT_MEMORY_MIB` -- kept in step by
# `tests/spec/test_capacity.py`, which fails if the two drift.
DEFAULT_TASK_MEMORY_MIB = 512.0

# Held back for the guest kernel, containerd and nerdctl. A guess, but an
# explicit one that the message shows its working for, rather than a silent
# fudge inside the comparison.
_HEADROOM_MIB = 256.0

_UNITS = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "TiB": 1024.0 * 1024}


def _mib(size: str) -> float:
    """Lima's `"1GiB"` as MiB. Unparseable sizes read as 0, which reports the
    instance as unable to hold anything -- loud, and true, rather than silently
    treating an unknown instance as infinite."""
    text = size.strip()
    for suffix, factor in _UNITS.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(text) / (1024 * 1024)  # bare bytes
    except ValueError:
        return 0.0


def _int_field(res: ResourceDesired, key: str, default: int) -> int:
    raw = res.fields.get(key)
    if raw is None:
        return default
    try:
        return int(str(raw.value).strip())
    except (TypeError, ValueError):
        return default


def _str_field(res: ResourceDesired, key: str) -> str:
    raw = res.fields.get(key)
    return str(raw.value).strip() if raw is not None else ""


def instance_capacity_mib(instance: ResourceDesired) -> float:
    """Memory a placed workload may actually use on this instance."""
    config = get_instance_type(_str_field(instance, "instance_type"))
    return max(0.0, _mib(config.memory) - _HEADROOM_MIB)


def workload_demand_mib(service: ResourceDesired) -> float:
    """`count` tasks at the taskdef's memory, or the default cap each."""
    per_task = float(_int_field(service, "memory", int(DEFAULT_TASK_MEMORY_MIB)))
    return per_task * max(1, _int_field(service, "count", 1))


def overcommitted(stack: Stack) -> list[str]:
    """One message per over-subscribed instance, ready to refuse an apply with.

    Empty for every canvas that fits -- including every canvas that places
    nothing, which is the overwhelming majority and must pay nothing for this.
    """
    instances = {r.id: r for r in stack.resources if r.kind == "ec2"}
    if not instances:
        return []

    placed: dict[str, list[ResourceDesired]] = {}
    for res in stack.resources:
        if res.kind != "ecs":
            continue
        host = _str_field(res, "host")
        if host in instances:
            placed.setdefault(host, []).append(res)

    problems: list[str] = []
    for host, services in sorted(placed.items()):
        capacity = instance_capacity_mib(instances[host])
        demand = sum(workload_demand_mib(s) for s in services)
        if demand <= capacity:
            continue
        detail = ", ".join(
            f"{s.id} ({_int_field(s, 'count', 1)} x "
            f"{_int_field(s, 'memory', int(DEFAULT_TASK_MEMORY_MIB))} MiB)"
            for s in sorted(services, key=lambda s: s.id)
        )
        problems.append(
            f"instance {host!r} cannot hold the workloads drawn inside it: "
            f"{detail} needs {demand:.0f} MiB, but a "
            f"{_str_field(instances[host], 'instance_type') or 't2.micro'} leaves "
            f"{capacity:.0f} MiB after {_HEADROOM_MIB:.0f} MiB of VM overhead. "
            f"Use a larger instance type, lower the task count, or drag a service out."
        )
    return problems
