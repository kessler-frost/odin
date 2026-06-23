"""A second Runtime impl: containers inside a shared Lima VM (VM isolation).

Same interface as ColimaRuntime via the shared `_ContainerRuntime` base — the
only differences are the CLI seam (`nerdctl` inside one allfather Lima host VM
instead of host `docker`) and that it omits Colima's host-gateway flag. Lima
auto-forwards VM-bound ports to the Mac, so host-side probes and references work
the same. Heavier than Colima (a VM boot), so it's an opt-in runtime for
VM-level isolation. The subprocess seam is injectable for testing; the multi-Mac
fleet (a Lima VM per remote Mac) is explicitly out of scope here.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from odin.compute.cloud_init import generate_cloud_init
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import get_instance_type
from odin.runtime.colima import HostFacts, _ContainerRuntime


class LimaRuntime(_ContainerRuntime):
    VM = "allfather-host"

    def _lima(self, *args: str, check: bool = True) -> str:
        proc = self._run(["limactl", *args])
        if check and proc.returncode != 0:
            raise RuntimeError(f"limactl {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _cli(self, *args: str, check: bool = True) -> str:
        # the base seam: nerdctl inside the VM
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
        out = self._cli("info", "--format", "{{.MemTotal}} {{.NCPU}}", check=False)
        if not out:
            return HostFacts()
        mem_bytes, ncpu = out.split()
        return HostFacts(total_mem_mib=int(mem_bytes) / 1024 / 1024, cpu_count=int(ncpu))

    def _wait_for_nerdctl(self, timeout: float = 360.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "server version" in self._cli("info", check=False).lower():
                return
            time.sleep(5)
        raise RuntimeError(f"nerdctl not ready in {self.VM} within {timeout}s")
