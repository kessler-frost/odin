"""TaskRuntime -- the substrate binding for gateway/models/ecsctl.py's ECS
tasks: a REAL Colima container per task (NORTHSTAR directive 5 / research
§2e §3: "RunTask -> ColimaRuntime.run_container from the task-def image").

Shape mirrors compute/functions.py's `FunctionRuntime` (V4b), not
aws/backings.py: this is a MANY-per-resource binding (one container per
TASK, like one RIE container per Lambda function), not aws/backings.py's
one-shared-container-per-env-per-kind shape. v1 single-container taskdefs
(V5c: "the drawn node IS the service+taskdef pair") -- one task, one
container. Container naming: `allfather-ecs-{env}-{task_id8}-{container_name}`
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


def container_name(env: str, task_id: str, container_def_name: str) -> str:
    return f"allfather-ecs-{env}-{task_id[:8]}-{container_def_name}"


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

    def run(self, env: str, task_id: str, container_def: dict) -> TaskContainerHandle:
        """Boot the task's single container from its taskdef container
        definition (image/environment/portMappings/command) -- returns as
        soon as the container process starts (module docstring: ECS's own
        RUNNING semantics need no readiness wait, unlike Lambda's RIE).
        Raises on a boot failure; the caller (ecsctl.py's background
        reconcile) turns that into the task's terminal STOPPED state, same
        contract as `InstanceVm.boot`/`FunctionRuntime.ensure`."""
        name = container_name(env, task_id, container_def["name"])
        env_vars = {kv["name"]: kv.get("value", "") for kv in container_def.get("environment") or []}
        ports = {pm["containerPort"]: pm.get("hostPort") or 0 for pm in container_def.get("portMappings") or []}
        self._rt.run_container(ContainerSpec(
            name=name, image=container_def["image"], env=env_vars, ports=ports,
            command=tuple(container_def.get("command") or []),
            labels={"allfather-env": env, "allfather-ecs-task": task_id},
        ))
        host_ports = {cport: self._rt.host_port(name, cport) for cport in ports}
        return TaskContainerHandle(name=name, host_ports=host_ports)

    def status(self, env: str, task_id: str, container_def_name: str) -> str:
        return self._rt.status(container_name(env, task_id, container_def_name))

    def exit_code(self, env: str, task_id: str, container_def_name: str) -> int:
        return self._rt.exit_code(container_name(env, task_id, container_def_name))

    def stop(self, env: str, task_id: str, container_def_name: str) -> None:
        self._rt.stop(container_name(env, task_id, container_def_name))
