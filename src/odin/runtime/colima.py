"""The single-host container runtime: real containers via Colima's `docker`.

For the walking skeleton the Runtime driver's "host" is the local Colima
container engine (run containers directly), not a per-node Lima VM — fast, real
containers, and matches the project's "Colima as the container runtime" rule.
A Lima-VM Runtime impl (VM isolation / remote hosts) is a later milestone behind
the same interface.

Every container allfather runs is labelled ``allfather=1`` (deliberately NOT
``ministack=…``, so MiniStack's own container reaper never touches them).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

LABEL = "allfather"


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    ports: dict[int, int] = field(default_factory=dict)  # container_port -> host_port
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunHandle:
    id: str
    name: str


class ColimaRuntime:
    """Drives `docker` (Colima) to run/inspect/stop labelled containers."""

    def _docker(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def run_container(self, spec: ContainerSpec) -> RunHandle:
        args = [
            "run", "-d",
            "--name", spec.name,
            "--label", f"{LABEL}=1",
            "--label", f"{LABEL}.name={spec.name}",
        ]
        for key, value in spec.labels.items():
            args += ["--label", f"{key}={value}"]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        for cport, hport in spec.ports.items():
            args += ["-p", (f"{hport}:{cport}" if hport else str(cport))]
        args.append(spec.image)
        cid = self._docker(*args)
        return RunHandle(id=cid, name=spec.name)

    def status(self, name: str) -> str:
        """Container state: running / exited / created / … / absent."""
        out = self._docker("inspect", "-f", "{{.State.Status}}", name, check=False)
        return out or "absent"

    def host_port(self, name: str, container_port: int) -> int:
        out = self._docker("port", name, str(container_port), check=False)
        return int(out.splitlines()[0].rsplit(":", 1)[-1]) if out else 0

    def logs(self, name: str, tail: int = 20) -> str:
        return self._docker("logs", "--tail", str(tail), name, check=False)

    def stats(self, name: str) -> dict[str, float]:
        """One-shot cpu% + memory (MiB) for a running container."""
        out = self._docker(
            "stats", "--no-stream", "--format",
            "{{.CPUPerc}} {{.MemUsage}}", name, check=False,
        )
        if not out:
            return {"cpu": 0.0, "ram": 0.0}
        cpu_s, mem_s = out.split(" ", 1)
        cpu = float(cpu_s.strip().rstrip("%") or 0)
        mem = mem_s.split("/")[0].strip()  # e.g. "23.5MiB"
        ram = _to_mib(mem)
        return {"cpu": cpu, "ram": ram}

    def stop(self, name: str) -> None:
        self._docker("rm", "-f", name, check=False)

    def list_allfather(self) -> list[str]:
        out = self._docker("ps", "-aq", "--filter", f"label={LABEL}=1", check=False)
        return [line for line in out.splitlines() if line]


def _to_mib(value: str) -> float:
    # Longest suffixes first: "MiB" also ends with "B".
    units = [("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024), ("B", 1 / 1024 / 1024)]
    for unit, factor in units:
        if value.endswith(unit):
            return float(value[: -len(unit)] or 0) * factor
    return 0.0
