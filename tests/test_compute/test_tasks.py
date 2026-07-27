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


def test_run_without_extra_env_uses_the_taskdef_environment_alone():
    runtime = FakeRuntime()
    TaskRuntime(runtime).run(ENV, TASK_ID, _container_def())
    (spec,) = runtime.runs
    assert spec.name == container_name(ENV, TASK_ID, "app")
    assert spec.env == {"FOO": "bar", "AWS_ACCESS_KEY_ID": "user-set"}


def test_run_layers_extra_env_on_top_and_odin_wins_name_collisions():
    runtime = FakeRuntime()
    container_def = _container_def()
    TaskRuntime(runtime).run(ENV, TASK_ID, container_def, extra_env={
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


def test_run_defaults_memory_and_cpu_when_the_taskdef_sets_neither():
    # v1's ECS canvas builder (agent/hcl.py) never emits cpu/memory on the
    # taskdef today -- this default is what actually caps every canvas-drawn
    # ECS node until it does.
    runtime = FakeRuntime()
    TaskRuntime(runtime).run(ENV, TASK_ID, _container_def())
    (spec,) = runtime.runs
    assert spec.memory_mib == 512.0
    assert spec.cpus == 1.0


def test_run_uses_the_taskdefs_own_cpu_and_memory_when_set():
    runtime = FakeRuntime()
    TaskRuntime(runtime).run(ENV, TASK_ID, _container_def(), cpu="2048", memory="1024")
    (spec,) = runtime.runs
    assert spec.memory_mib == 1024.0
    assert spec.cpus == 2.0  # 2048 CPU units == 2 vCPUs
