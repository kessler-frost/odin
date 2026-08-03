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

    def __init__(self, runtime=None, placed_on: str = "") -> None:
        self._rt = runtime or ColimaRuntime()
        # The EC2 instance this runtime was bound to, if any. Used ONLY to
        # phrase failures -- see `run`. `docs/intelligence-layer.md` named this
        # as one of placement's four costs: "the VM is not up" and "the task
        # failed" must not collapse into one status, because they need opposite
        # responses from a person.
        self._placed_on = placed_on

    async def run(
        self, env: str, task_id: str, container_def: dict, extra_env: dict[str, str] | None = None,
        cpu: str | int | None = None, memory: str | int | None = None,
        volumes: dict[str, str] | None = None,
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
        set them, else this module's own default cap -- never unbounded.

        `volumes` is `{host path -> container path}`, already RESOLVED by the
        caller (`gateway/models/efsctl.py::task_mounts` joins the task
        definition's `volumes[].efsVolumeConfiguration.fileSystemId` with this
        container's `mountPoints[]`). It is resolved up there rather than here
        on purpose: the file-system id -> directory mapping lives in the
        gateway's efsctl store, and `compute/` sits BELOW `gateway/` -- importing
        upward would be a cycle. This module stays what it is, a substrate
        binding that mounts what it is handed."""
        name = container_name(env, task_id, container_def["name"])
        self._refuse_unmountable(volumes)
        env_vars = {kv["name"]: kv.get("value", "") for kv in container_def.get("environment") or []}
        if extra_env:
            env_vars.update(extra_env)
        ports = {pm["containerPort"]: pm.get("hostPort") or 0 for pm in container_def.get("portMappings") or []}
        spec = ContainerSpec(
            name=name, image=container_def["image"], env=env_vars, ports=ports,
            command=tuple(container_def.get("command") or []),
            labels={"odin-env": env, "odin-ecs-task": task_id},
            memory_mib=_memory_mib(memory), cpus=_cpus(cpu),
            volumes=dict(volumes or {}),
        )
        if not self._placed_on:
            await self._rt.run_container(spec)
        else:
            # A PLACED task runs inside its instance's VM, so a failure here has
            # two very different causes and they must not read alike: the
            # workload is broken (bad image, bad command), or the instance it
            # was drawn inside is not up. The second is not a task failure at
            # all -- the user's fix is to bring the instance back, not to touch
            # the workload -- so it says which instance, in the message that
            # becomes the task's terminal STOPPED reason.
            try:
                await self._rt.run_container(spec)
            except Exception as exc:  # noqa: BLE001 -- re-raised, only re-phrased
                raise RuntimeError(
                    f"could not start this task on the {self._placed_on!r} instance: {exc}. "
                    f"The workload is drawn inside that instance, so it can only run once the "
                    f"instance is up -- check it before changing the task."
                ) from exc
        host_ports = {cport: await self._rt.host_port(name, cport) for cport in ports}
        return TaskContainerHandle(name=name, host_ports=host_ports)

    def _refuse_unmountable(self, volumes: dict[str, str] | None) -> None:
        """A PLACED task cannot bind-mount a host directory, so it is refused
        rather than started with an empty one.

        THE SUBSTRATE FACT, and it is checkable: `compute/lima_yaml.py` emits
        `"mounts": []` for every VM odin creates, so a Lima guest shares NO host
        paths at all. A placed service runs under `LimaRuntime`
        (`ecsctl.runtime_for_service`), whose `-v` goes to nerdctl INSIDE that
        guest -- where the host path does not exist, so nerdctl creates a fresh
        empty directory and reports nothing wrong. The task would come up, mount
        something, read nothing, and every status would be green. That is the
        exact failure this repo names "reports success it did not achieve", and
        the only honest answers are to refuse or to report; refusing is louder,
        and the caller (`ecsctl._launch_task`) already turns this into a STOPPED
        task carrying the reason, which fails the apply and puts a real verdict
        on the canvas.

        `tests/test_compute/test_tasks.py` pins the premise as well as the
        behaviour: it asserts `generate_lima_yaml` really does emit no mounts, so
        if odin's VMs ever gain one, that test fails and points here instead of
        leaving a guard that refuses something newly possible.

        `_placed_on` is a real arriving signal, not a sniff: `runtime_for_service`
        sets it from the service record's own placement and this class already
        uses it to phrase boot failures."""
        if volumes and self._placed_on:
            raise RuntimeError(
                f"this task is drawn INSIDE the {self._placed_on!r} instance and also mounts an EFS "
                f"file system ({', '.join(sorted(volumes.values()))}), and odin cannot make both true: "
                f"a placed task runs in that instance's own Lima VM, and odin's VMs are created sharing "
                f"NO host directories (compute/lima_yaml.py emits `mounts: []`), so the mount would be "
                f"an EMPTY directory that reports no error. Nothing was started. Draw the task outside "
                f"the instance to mount the file system, or drop the mount edge to keep the placement."
            )

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
