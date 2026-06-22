"""A second Runtime impl: containers inside a shared Lima VM (VM isolation).

Same RuntimeDriver interface as ColimaRuntime, but workloads run via
`nerdctl` inside one allfather Lima host VM rather than on the host's container
engine. Lima auto-forwards VM-bound ports to the Mac, so host-side probes and
references work the same. Heavier than Colima (a VM boot), so it's an opt-in
runtime for VM-level isolation. The subprocess seam is injectable for testing;
the multi-Mac fleet (a Lima VM per remote Mac) is explicitly out of scope here.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from odin.compute.cloud_init import generate_cloud_init
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import get_instance_type
from odin.runtime.colima import (
    LABEL,
    ContainerFacts,
    ContainerSpec,
    HostFacts,
    RunHandle,
    _STATUS_TO_PHASE,
    _to_mib,
)


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


def _default_runner(args: list[str]) -> _Proc:
    proc = subprocess.run(args, capture_output=True, text=True)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


class LimaRuntime:
    VM = "allfather-host"

    def __init__(self, runner=None) -> None:
        self._run = runner or _default_runner

    def _lima(self, *args: str, check: bool = True) -> str:
        proc = self._run(["limactl", *args])
        if check and proc.returncode != 0:
            raise RuntimeError(f"limactl {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _nerdctl(self, *args: str, check: bool = True) -> str:
        return self._lima("shell", self.VM, "sudo", "nerdctl", *args, check=check)

    def ensure_host(self) -> HostFacts:
        if self.VM not in self._lima("list", "-q", check=False).split():
            cloud_init = generate_cloud_init(hostname=self.VM, install_nerdctl=True)
            yaml = generate_lima_yaml(
                get_instance_type("t2.medium"), cloud_init_script=cloud_init,
                shared_network=False,
            )
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
                handle.write(yaml)
                yaml_path = handle.name
            self._lima("create", "--tty=false", f"--name={self.VM}", yaml_path)
            self._lima("start", self.VM)
            Path(yaml_path).unlink(missing_ok=True)
        self._wait_for_nerdctl()
        out = self._nerdctl("info", "--format", "{{.MemTotal}} {{.NCPU}}", check=False)
        if not out:
            return HostFacts()
        mem_bytes, ncpu = out.split()
        return HostFacts(total_mem_mib=int(mem_bytes) / 1024 / 1024, cpu_count=int(ncpu))

    def _wait_for_nerdctl(self, timeout: float = 360.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "server version" in self._nerdctl("info", check=False).lower():
                return
            time.sleep(5)
        raise RuntimeError(f"nerdctl not ready in {self.VM} within {timeout}s")

    def run_container(self, spec: ContainerSpec) -> RunHandle:
        args = [
            "run", "-d", "--name", spec.name,
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
        return RunHandle(id=self._nerdctl(*args), name=spec.name)

    def status(self, name: str) -> str:
        out = self._nerdctl("inspect", "-f", "{{.State.Status}}", name, check=False)
        return out or "absent"

    def exit_code(self, name: str) -> int:
        out = self._nerdctl("inspect", "-f", "{{.State.ExitCode}}", name, check=False)
        return int(out) if out.lstrip("-").isdigit() else -1

    def host_port(self, name: str, container_port: int) -> int:
        out = self._nerdctl("port", name, str(container_port), check=False)
        return int(out.splitlines()[0].rsplit(":", 1)[-1]) if out else 0

    def logs(self, name: str, tail: int = 20) -> str:
        return self._nerdctl("logs", "--tail", str(tail), name, check=False)

    def stats(self, name: str) -> dict[str, float]:
        out = self._nerdctl(
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
        self._nerdctl("rm", "-f", name, check=False)

    def list_allfather(self) -> list[str]:
        out = self._nerdctl("ps", "-aq", "--filter", f"label={LABEL}=1", check=False)
        return [line for line in out.splitlines() if line]
