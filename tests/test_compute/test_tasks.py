"""compute/tasks.py::TaskRuntime -- focused unit tests for `run`'s
`extra_env` layering (the workload-creds injection seam ecsctl.py's
`_reconcile_service_tasks` feeds). The ecsctl.py-level FakeTaskRuntime tests
in tests/gateway/test_ecsctl.py are the primary end-to-end coverage; this
file pins the merge semantics at the substrate itself, on an injected fake
runtime driver (the same pattern test_functions.py/test_instances.py use --
no real Docker involved).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import yaml

from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import VmConfig
from odin.compute.tasks import TaskRuntime, container_name
from odin.runtime.colima import ContainerSpec

ENV = "default"
TASK_ID = "0123456789abcdef0123456789abcdef"


@dataclass
class FakeRuntime:
    runs: list[ContainerSpec] = field(default_factory=list)

    async def run_container(self, spec: ContainerSpec) -> None:
        self.runs.append(spec)

    async def host_port(self, name: str, container_port: int) -> int:
        return 10_080


def _container_def() -> dict:
    return {
        "name": "app",
        "image": "nginx:alpine",
        "environment": [
            {"name": "FOO", "value": "bar"},
            {"name": "AWS_ACCESS_KEY_ID", "value": "user-set"},
        ],
    }


async def test_run_without_extra_env_uses_the_taskdef_environment_alone():
    runtime = FakeRuntime()
    await TaskRuntime(runtime).run(ENV, TASK_ID, _container_def())
    (spec,) = runtime.runs
    assert spec.name == container_name(ENV, TASK_ID, "app")
    assert spec.env == {"FOO": "bar", "AWS_ACCESS_KEY_ID": "user-set"}


async def test_run_layers_extra_env_on_top_and_odin_wins_name_collisions():
    runtime = FakeRuntime()
    container_def = _container_def()
    await TaskRuntime(runtime).run(ENV, TASK_ID, container_def, extra_env={
        "AWS_ACCESS_KEY_ID": "AKODINxxxxxxxxxxxxxx",
        "AWS_ENDPOINT_URL": "http://host.docker.internal:4266",
    })
    (spec,) = runtime.runs
    assert spec.env == {
        "FOO": "bar",
        "AWS_ACCESS_KEY_ID": "AKODINxxxxxxxxxxxxxx",  # odin's injected var wins the collision
        "AWS_ENDPOINT_URL": "http://host.docker.internal:4266",
    }
    # Zero-drift guarantee: the taskdef's own container definition is never
    # mutated -- the injection exists ONLY in the real container's env vars.
    assert container_def == _container_def()


# --- owner directive B4: memory/cpu caps, from the taskdef when present -----


async def test_run_defaults_memory_and_cpu_when_the_taskdef_sets_neither():
    # v1's ECS canvas builder (iac/hcl.py) never emits cpu/memory on the
    # taskdef today -- this default is what actually caps every canvas-drawn
    # ECS node until it does.
    runtime = FakeRuntime()
    await TaskRuntime(runtime).run(ENV, TASK_ID, _container_def())
    (spec,) = runtime.runs
    assert spec.memory_mib == 512.0
    assert spec.cpus == 1.0


async def test_run_uses_the_taskdefs_own_cpu_and_memory_when_set():
    runtime = FakeRuntime()
    await TaskRuntime(runtime).run(ENV, TASK_ID, _container_def(), cpu="2048", memory="1024")
    (spec,) = runtime.runs
    assert spec.memory_mib == 1024.0
    assert spec.cpus == 2.0  # 2048 CPU units == 2 vCPUs


# --- efs: the mount reaches the real ContainerSpec, and a placed task cannot
# have one -------------------------------------------------------------------


async def test_efs_mounts_reach_the_container_spec_verbatim():
    """`ContainerSpec.volumes` is rendered by ONE code path shared by both
    drivers (`runtime/colima.py`: `-v {source}:{container_path}`), so landing
    the mount here is what makes it a real bind mount. Already RESOLVED by the
    caller: the fs-id -> directory mapping lives in the gateway's efsctl store
    and `compute/` sits below `gateway/`."""
    runtime = FakeRuntime()
    await TaskRuntime(runtime).run(
        ENV, TASK_ID, _container_def(), volumes={"/Users/x/.odin/e/gateway/efs/fs-1": "/mnt/efs"},
    )
    (spec,) = runtime.runs
    assert spec.volumes == {"/Users/x/.odin/e/gateway/efs/fs-1": "/mnt/efs"}


async def test_a_task_with_no_mounts_still_gets_an_empty_volume_map():
    """The other half: a task that mounts nothing must not acquire a volume,
    and `None` must not reach `ContainerSpec`."""
    runtime = FakeRuntime()
    await TaskRuntime(runtime).run(ENV, TASK_ID, _container_def())
    assert runtime.runs[0].volumes == {}


async def test_a_placed_task_that_mounts_efs_is_refused_and_never_started():
    """THE guard for the case the contract says must be refused, never silently
    mounted empty.

    A PLACED service runs inside its EC2 node's own Lima VM
    (`ecsctl.runtime_for_service`), and odin's Lima VMs share NO host paths --
    so a `-v` there goes to nerdctl INSIDE the guest, where the host path does
    not exist, and nerdctl creates a fresh EMPTY directory and reports nothing
    wrong. The task would come up, mount something, read nothing, and every
    status would be green.

    Mutation-test: delete the `self._placed_on` half of the condition in
    `_refuse_unmountable` and this fails; delete the `volumes` half and
    `test_a_placed_task_with_no_mounts_still_runs` below fails."""
    runtime = FakeRuntime()
    placed = TaskRuntime(runtime, placed_on="web-vm")

    with pytest.raises(RuntimeError) as raised:
        await placed.run(ENV, TASK_ID, _container_def(), volumes={"/host/efs/fs-1": "/mnt/efs"})

    assert not runtime.runs, "the task was STARTED despite the refusal"
    message = str(raised.value)
    assert "web-vm" in message, "the refusal does not name the instance the user drew"
    assert "/mnt/efs" in message, "the refusal does not name the mount"
    assert "mounts: []" in message, "the refusal does not say WHY, so a user cannot check it"


async def test_a_placed_task_with_no_mounts_still_runs():
    """Placement itself is not the problem -- the combination is. A guard that
    refused every placed task would pass the test above and break the whole
    placement feature."""
    runtime = FakeRuntime()
    await TaskRuntime(runtime, placed_on="web-vm").run(ENV, TASK_ID, _container_def())
    assert len(runtime.runs) == 1


async def test_an_unplaced_task_may_mount_efs():
    """...and the third corner: the ordinary case must not be refused."""
    runtime = FakeRuntime()
    await TaskRuntime(runtime).run(ENV, TASK_ID, _container_def(), volumes={"/host/efs/fs-1": "/mnt/efs"})
    assert runtime.runs[0].volumes == {"/host/efs/fs-1": "/mnt/efs"}


def test_odins_lima_vms_really_share_no_host_directories():
    """THE PREMISE the guard above rests on, pinned so it cannot go stale
    silently.

    `_refuse_unmountable` is correct only because odin's Lima VMs are created
    sharing nothing. That is a fact about `compute/lima_yaml.py`, not about
    `compute/tasks.py`, so it is asserted against the REAL generated document
    rather than trusted. If odin's VMs ever gain a host mount, this fails and
    points at a guard that has become wrong -- instead of leaving a refusal
    standing over something newly possible."""
    document = yaml.safe_load(generate_lima_yaml(VmConfig(cpus=2, memory="2GiB", disk="20GiB")))
    assert document["mounts"] == [], (
        "odin's Lima VMs now share a host directory -- `TaskRuntime._refuse_unmountable` "
        "refuses placed EFS mounts on the strength of them sharing none"
    )
