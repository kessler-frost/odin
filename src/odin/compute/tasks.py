"""TaskRuntime -- the substrate binding for gateway/models/ecsctl.py's ECS
tasks: a REAL Colima container per task (NORTHSTAR directive 5 / research
§2e §3: "RunTask -> ColimaRuntime.run_container from the task-def image").

Shape mirrors compute/functions.py's `FunctionRuntime` (V4b), not
aws/backings.py: this is a MANY-per-resource binding (one container per
TASK, like one RIE container per Lambda function), not aws/backings.py's
one-shared-container-per-env-per-kind shape. v1 single-container taskdefs
(V5c: "the drawn node IS the service+taskdef pair") -- one task, one
container. Container naming: `odin-ecs-{env}-{task_id8}-{container_name}`
-- the ONLY name this module ever passes to the runtime driver.

Unlike Lambda's RIE readiness probe (a real TCP-connect wait -- a function's
own container must actually be SERVING before Invoke can work), an ECS
task's "RUNNING" state needs no such wait: real ECS itself considers a task
RUNNING the moment its container process starts (health checks are a
SEPARATE, optional concept neither this module nor the research capture
models in v1) -- so `run` returns as soon as `docker run -d` returns, and
the gateway model's own lazy sweep (ecsctl.py's `_sweep_tasks`, driven by
DescribeServices/DescribeTasks/ListTasks, matching the MiniStack digest's
`_maybe_mark_stopped`) is what promotes/demotes state from there by polling
real container status -- exactly what a real ECS agent would report back.
"""
from __future__ import annotations

from dataclasses import dataclass

from odin.runtime.colima import ColimaRuntime, ContainerSpec

# Owner directive B4: a runaway task container can't eat the host. Real ECS
# taskdef `cpu`/`memory` are strings -- `cpu` in CPU units (1024 == 1 vCPU),
# `memory` in MiB -- when present (bridge/EC2 launch type, v1's ONLY mode:
# `agent/hcl.py`'s `_ecs` builder doesn't emit either field today, so this
# fallback is what actually caps every canvas-drawn ECS node until it does).
_DEFAULT_MEMORY_MIB = 512.0
_DEFAULT_CPUS = 1.0
_CPU_UNITS_PER_VCPU = 1024.0


def _memory_mib(taskdef_memory: str | int | None) -> float:
    return float(taskdef_memory) if taskdef_memory else _DEFAULT_MEMORY_MIB


def _cpus(taskdef_cpu: str | int | None) -> float:
    return float(taskdef_cpu) / _CPU_UNITS_PER_VCPU if taskdef_cpu else _DEFAULT_CPUS


def container_name(env: str, task_id: str, container_def_name: str) -> str:
    return f"odin-ecs-{env}-{task_id[:8]}-{container_def_name}"


@dataclass(frozen=True)
class TaskContainerHandle:
    name: str
    host_ports: dict[int, int]  # containerPort -> the host port Docker actually published


class TaskRuntime:
    """Per-task container lifecycle, on an injectable `RuntimeDriver` (the
    same seam FunctionRuntime/InstanceVm use, so a test can inject a fake
    runtime with no real Docker involved)."""

    def __init__(self, runtime=None) -> None:
        self._rt = runtime or ColimaRuntime()

    async def run(
        self, env: str, task_id: str, container_def: dict, extra_env: dict[str, str] | None = None,
        cpu: str | int | None = None, memory: str | int | None = None,
    ) -> TaskContainerHandle:
        """Boot the task's single container from its taskdef container
        definition (image/environment/portMappings/command) -- returns as
        soon as the container process starts (module docstring: ECS's own
        RUNNING semantics need no readiness wait, unlike Lambda's RIE).
        Raises on a boot failure; the caller (ecsctl.py's background
        reconcile) turns that into the task's terminal STOPPED state, same
        contract as `InstanceVm.boot`/`FunctionRuntime.ensure`.

        `extra_env` layers odin-injected vars (the workload's own gateway
        creds -- `gateway/keys.py::workload_env`) on top of the taskdef's
        environment, WINNING any same-named collision -- into the REAL
        container's env only, never back into the stored taskdef (ecsctl.py's
        byte-for-byte TASK-DEFINITION DRIFT mandate).

        `cpu`/`memory` (owner directive B4): the taskdef's own top-level
        fields (real ECS CPU units / MiB, as strings) when RegisterTaskDefinition
        set them, else this module's own default cap -- never unbounded."""
        name = container_name(env, task_id, container_def["name"])
        env_vars = {kv["name"]: kv.get("value", "") for kv in container_def.get("environment") or []}
        if extra_env:
            env_vars.update(extra_env)
        ports = {pm["containerPort"]: pm.get("hostPort") or 0 for pm in container_def.get("portMappings") or []}
        await self._rt.run_container(ContainerSpec(
            name=name, image=container_def["image"], env=env_vars, ports=ports,
            command=tuple(container_def.get("command") or []),
            labels={"odin-env": env, "odin-ecs-task": task_id},
            memory_mib=_memory_mib(memory), cpus=_cpus(cpu),
        ))
        host_ports = {cport: await self._rt.host_port(name, cport) for cport in ports}
        return TaskContainerHandle(name=name, host_ports=host_ports)

    async def status(self, env: str, task_id: str, container_def_name: str) -> str:
        return await self._rt.status(container_name(env, task_id, container_def_name))

    async def exit_code(self, env: str, task_id: str, container_def_name: str) -> int:
        return await self._rt.exit_code(container_name(env, task_id, container_def_name))

    async def logs(self, env: str, task_id: str, container_def_name: str, tail: int = 20) -> str:
        """The task container's own log tail -- what `gateway/models/
        ecsctl.py`'s sweep ships into `/ecs/{service}`. Never raises: the
        driver's `logs` is a `check=False` CLI call, so an already-removed
        container answers with "" (`_ContainerRuntime.logs`'s contract)."""
        return await self._rt.logs(container_name(env, task_id, container_def_name), tail)

    async def stop(self, env: str, task_id: str, container_def_name: str) -> None:
        await self._rt.stop(container_name(env, task_id, container_def_name))
