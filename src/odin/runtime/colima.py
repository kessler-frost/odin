"""Container runtimes: a shared base + the default Colima (`docker`) driver.

For the walking skeleton the Runtime driver's "host" is the local Colima
container engine (run containers directly), fast and real. `LimaRuntime`
(runtime/lima.py) runs the same containers inside a Lima VM for isolation,
reusing the `_ContainerRuntime` base here — they differ only in the CLI seam
(`docker` vs `nerdctl`-in-VM) and Colima's host-gateway run flag.

Every container allfather runs is labelled ``allfather=1`` (deliberately NOT
``ministack=…``, so MiniStack's own container reaper never touches them).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from odin.spec.models import Phase

LABEL = "allfather"


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    ports: dict[int, int] = field(default_factory=dict)  # container_port -> host_port
    labels: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunHandle:
    id: str
    name: str


@dataclass(frozen=True)
class ContainerFacts:
    phase: Phase
    host_port: int = 0
    cpu: float = 0.0
    ram: float = 0.0
    logtail: str = ""


@dataclass(frozen=True)
class HostFacts:
    total_mem_mib: float = 0.0
    cpu_count: int = 0
    # This host's Nebula overlay IP, when it's a mesh member (M7 multi-Mac).
    # None on a single host => producers publish 127.0.0.1, behavior unchanged.
    overlay_ip: str | None = None


# Docker/nerdctl container state -> coarse runtime phase ("healthy" is an assertion's call).
_STATUS_TO_PHASE: dict[str, Phase] = {
    "running": "starting",
    "restarting": "starting",
    "paused": "starting",
    "created": "starting",  # booting, not gone — distinct from "absent" (=pending)
    "exited": "crashed",
    "dead": "crashed",
    "removing": "crashed",
    "absent": "pending",
}


def _to_mib(value: str) -> float:
    # Longest suffixes first: "MiB" also ends with "B".
    units = [("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024), ("B", 1 / 1024 / 1024)]
    for unit, factor in units:
        if value.endswith(unit):
            return float(value[: -len(unit)] or 0) * factor
    return 0.0


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


def _default_runner(args: list[str]) -> _Proc:
    proc = subprocess.run(args, capture_output=True, text=True)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


class _ContainerRuntime:
    """Run/inspect/stop labelled containers. Subclasses supply `_cli` (the
    container-CLI seam) and optionally `_run_flags` (runtime-specific run args).
    The subprocess runner is injectable, so subclasses are unit-testable."""

    def __init__(self, runner=None) -> None:
        self._run = runner or _default_runner

    def _cli(self, *args: str, check: bool = True) -> str:
        raise NotImplementedError

    def _run_flags(self) -> list[str]:
        return []

    def run_container(self, spec: ContainerSpec) -> RunHandle:
        args = [
            "run", "-d", "--name", spec.name, *self._run_flags(),
            "--label", f"{LABEL}=1", "--label", f"{LABEL}.name={spec.name}",
        ]
        for key, value in spec.labels.items():
            args += ["--label", f"{key}={value}"]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        for cport, hport in spec.ports.items():
            args += ["-p", (f"{hport}:{cport}" if hport else str(cport))]
        args.append(spec.image)
        args += list(spec.command)
        return RunHandle(id=self._cli(*args), name=spec.name)

    def status(self, name: str) -> str:
        """Container state: running / exited / created / … / absent."""
        return self._cli("inspect", "-f", "{{.State.Status}}", name, check=False) or "absent"

    def exit_code(self, name: str) -> int:
        out = self._cli("inspect", "-f", "{{.State.ExitCode}}", name, check=False)
        return int(out) if out.lstrip("-").isdigit() else -1

    def host_port(self, name: str, container_port: int) -> int:
        out = self._cli("port", name, str(container_port), check=False)
        return int(out.splitlines()[0].rsplit(":", 1)[-1]) if out else 0

    def logs(self, name: str, tail: int = 20) -> str:
        return self._cli("logs", "--tail", str(tail), name, check=False)

    def stats(self, name: str) -> dict[str, float]:
        """One-shot cpu% + memory (MiB) for a running container."""
        out = self._cli(
            "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", name, check=False,
        )
        if not out:
            return {"cpu": 0.0, "ram": 0.0}
        cpu_s, mem_s = out.split(" ", 1)
        return {"cpu": float(cpu_s.strip().rstrip("%") or 0), "ram": _to_mib(mem_s.split("/")[0].strip())}

    def facts(self, name: str, container_port: int = 0) -> ContainerFacts:
        status = self.status(name)
        stats = self.stats(name) if status == "running" else {"cpu": 0.0, "ram": 0.0}
        return ContainerFacts(
            phase=_STATUS_TO_PHASE.get(status, "pending"),
            host_port=self.host_port(name, container_port) if container_port else 0,
            cpu=stats["cpu"], ram=stats["ram"],
            logtail=self.logs(name, tail=5) if status != "absent" else "",
        )

    def stop(self, name: str) -> None:
        self._cli("rm", "-f", name, check=False)

    def list_allfather(self) -> list[str]:
        out = self._cli("ps", "-aq", "--filter", f"label={LABEL}=1", check=False)
        return [line for line in out.splitlines() if line]


class ColimaRuntime(_ContainerRuntime):
    """Drives `docker` (Colima) directly on the host."""

    def _cli(self, *args: str, check: bool = True) -> str:
        proc = self._run(["docker", *args])
        if check and proc.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _run_flags(self) -> list[str]:
        # Reach the host-side AWS embed + RDS from inside containers.
        return ["--add-host", "host.docker.internal:host-gateway"]

    def ensure_host(self) -> HostFacts:
        out = self._cli("info", "--format", "{{.MemTotal}} {{.NCPU}}", check=False)
        if not out:
            return HostFacts()
        mem_bytes, ncpu = out.split()
        return HostFacts(total_mem_mib=int(mem_bytes) / 1024 / 1024, cpu_count=int(ncpu))
