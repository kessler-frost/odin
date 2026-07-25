"""A second Runtime impl: containers inside a shared Lima VM (VM isolation).

Same interface as ColimaRuntime via the shared `_ContainerRuntime` base — the
only differences are the CLI seam (`nerdctl` inside one odin Lima host VM
instead of host `docker`) and that it omits Colima's host-gateway flag. Lima
auto-forwards VM-bound ports to the Mac, so host-side probes and references work
the same. Heavier than Colima (a VM boot), so it's an opt-in runtime for
VM-level isolation. The subprocess seam is injectable for testing; the multi-Mac
fleet (a Lima VM per remote Mac) is explicitly out of scope here.

Observability v1: `logs()` (inherited from `_ContainerRuntime`, unchanged) is
a real `nerdctl logs` against a container inside this shared VM -- already a
full container-level log surface. The DIFFERENT gap this feature
closes is a real per-instance EC2 VM (one whole VM per instance, managed by
`compute/instances.py::InstanceVm`, NOT this class): that has no container
to attach to at all, so `InstanceVm.logs` reads the VM's systemd journal
instead (`limactl shell <vm> -- journalctl ...`).

Field test 2 (HIGH-3): that inherited `logs()` now keeps BOTH of the
container's streams. Here the outer process is `limactl`, so its own
diagnostics (a `WARN[0000] …` line) share the stderr pipe with the
container's stderr; they carry no `--timestamps` prefix, so
`_merge_log_streams` keeps them visibly at the top rather than dropping them
-- an odd line in the log beats a silently truncated log.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from odin.compute.cloud_init import generate_cloud_init
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import get_instance_type
from odin.runtime.colima import HostFacts, _ContainerRuntime

# The Mac host as seen from INSIDE a Lima VM (an EC2 node is a Lima VM): Lima
# auto-provides this alias, the guest-side counterpart to Colima's
# `host.docker.internal` (`colima.CONTAINER_HOST`). A host-published port (an
# rds Postgres container) is reachable from an EC2 VM at `host.lima.internal:
# <port>` -- `host.docker.internal` does NOT resolve inside a Lima VM
# (field-test finding #5), so an rds fact meant for an EC2 consumer must use
# this form.
LIMA_HOST = "host.lima.internal"


class LimaRuntime(_ContainerRuntime):
    VM = "odin-host"

    def _lima(self, *args: str, check: bool = True, input: str | None = None) -> str:
        proc = self._run(["limactl", *args], input=input)
        if check and proc.returncode != 0:
            raise RuntimeError(f"limactl {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _argv(self, *args: str) -> list[str]:
        # the base seam: nerdctl inside the VM
        return ["limactl", "shell", self.VM, "sudo", "nerdctl", *args]

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
