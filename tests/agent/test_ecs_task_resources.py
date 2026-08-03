"""An ECS task's `memory`/`cpu` must be authorable, because odin ENFORCES them.

## The gap this closes

`compute/tasks.py` caps every ECS task's container at its task definition's
`memory`, falling back to `_DEFAULT_MEMORY_MIB = 512`. The gateway already
carries the value end to end: `ecsctl` stores `memory` from the
RegisterTaskDefinition payload and hands it to `run_task`. `spec/capacity.py`
already reads a `memory` field off the ecs NODE to decide whether the instance a
service was drawn inside can hold it.

Every link existed except the first two. `iac/hcl.py` emitted an
`aws_ecs_task_definition` with no `memory` attribute at all, and the catalog
offered no field to set one -- so the value was ALWAYS the 512 default, a
container needing 1 GiB was OOM-killed with no way to say otherwise, and
`capacity.py`'s arithmetic was performed against a number the user could not
influence.

## Why this is one gap and not two

`cpu` is the same missing link on the same line of the same call
(`tasks.py::_cpus`, `ecsctl` passes `cpu=taskdef.get("cpu")`), so it is fixed
here too -- honesty rule 2's "fix the SHAPE, not the instance". The difference
worth knowing: memory is a HARD cap that kills a container, cpu is a share, so
only memory has a capacity guard in front of it.

## The invariant these lock

ONE number, authored once, used everywhere: what the canvas says is what the
taskdef carries, what the runtime enforces, and what `capacity.py` does its
arithmetic with. The failure mode being prevented is a second default appearing
somewhere in that chain -- the canvas saying 1024 while admission control
reasons about 512 would make the guard's own message wrong, and it would still
look correct in every test that checked only one end.
"""
from __future__ import annotations

from odin.iac.hcl import generate_tf
from odin.compute.tasks import _DEFAULT_MEMORY_MIB, _memory_mib
from odin.spec.capacity import DEFAULT_TASK_MEMORY_MIB, workload_demand_mib
from odin.spec.models import FieldValue, ResourceDesired, Stack


def _service(**fields: str) -> ResourceDesired:
    return ResourceDesired(
        id="app", kind="ecs",
        fields={k: FieldValue(value=v, source="canvas") for k, v in fields.items()},
    )


def _taskdef_block(res: ResourceDesired) -> str:
    """The `aws_ecs_task_definition` block generated for `res`, on its own."""
    main_tf = generate_tf(Stack(resources=(res,))).files["main.tf"]
    start = main_tf.index('resource "aws_ecs_task_definition"')
    return main_tf[start:]


def test_an_authored_memory_reaches_the_task_definition():
    block = _taskdef_block(_service(image="nginx:alpine", memory="1024"))
    assert 'memory                   = "1024"' in block or 'memory = "1024"' in block, block


def test_an_authored_cpu_reaches_the_task_definition():
    block = _taskdef_block(_service(image="nginx:alpine", cpu="512"))
    assert '"512"' in block, block


def test_a_service_with_no_memory_emits_none_and_inherits_the_runtime_default():
    """Emitting an explicit 512 would look harmless and freeze the default into
    every canvas ever applied, so changing it later would silently not apply to
    them. Absent means absent, and `_memory_mib(None)` supplies the default at
    the point that actually enforces it."""
    block = _taskdef_block(_service(image="nginx:alpine"))
    assert "memory" not in block, block
    assert _memory_mib(None) == _DEFAULT_MEMORY_MIB


def test_the_canvas_number_is_the_number_the_runtime_enforces():
    """The whole chain in one assertion: no second default in the middle."""
    assert _memory_mib("1024") == 1024.0


def test_the_canvas_number_is_also_the_number_admission_control_uses():
    """`capacity.py` refuses an apply naming this figure; if it reasoned about a
    different one, its message would be wrong in a way nothing else catches."""
    assert workload_demand_mib(_service(memory="1024", count="2")) == 2048.0


def test_the_two_defaults_are_the_same_number():
    """Two modules keep their own default. They are only allowed to differ if
    someone means them to, and this is where that decision gets contested."""
    assert DEFAULT_TASK_MEMORY_MIB == _DEFAULT_MEMORY_MIB


def test_an_unparseable_memory_falls_back_rather_than_crashing_an_apply():
    """A canvas is a hand-authored input (`odin canvas set`, the translation
    agent), so a junk value must not take the apply down with it."""
    assert workload_demand_mib(_service(memory="lots", count="1")) == DEFAULT_TASK_MEMORY_MIB
