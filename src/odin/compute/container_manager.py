from __future__ import annotations

import asyncio
import json
from pathlib import Path

from odin.compute.models import ContainerInfo


class ContainerManager:
    """Async wrapper around nerdctl CLI for container lifecycle inside Lima VMs."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path.home() / ".odin"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    async def _run(self, *args: str) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode(), stderr.decode(), proc.returncode

    async def _run_in_vm(self, vm_name: str, *args: str) -> tuple[str, str, int]:
        """Run a command inside a Lima VM via limactl shell.

        `nerdctl` runs under sudo: cloud-init sets up rootful containerd, and
        `limactl shell` runs as a non-root user (which would otherwise default
        nerdctl to rootless mode, needing a separate containerd-rootless setup).
        """
        if args and args[0] == "nerdctl":
            args = ("sudo", *args)
        return await self._run(
            "limactl", "shell", "--tty=false", vm_name, "--", *args,
        )

    async def copy_to_vm(self, vm_name: str, local_path: str, remote_path: str) -> None:
        """Copy a file or directory to a Lima VM via limactl copy."""
        _, stderr, returncode = await self._run(
            "limactl", "copy", local_path, f"{vm_name}:{remote_path}",
        )
        if returncode != 0:
            raise RuntimeError(f"limactl copy failed: {stderr}")

    async def build_image(self, vm_name: str, context_path: str, tag: str) -> str:
        """Build a container image inside the VM. Returns image ID."""
        stdout, stderr, returncode = await self._run_in_vm(
            vm_name, "nerdctl", "build", "-t", tag, context_path,
        )
        if returncode != 0:
            raise RuntimeError(f"nerdctl build failed: {stderr}")
        return stdout.strip()

    async def run_container(
        self,
        vm_name: str,
        name: str,
        image: str,
        env: dict[str, str] | None = None,
        volumes: list[str] | None = None,
    ) -> str:
        """Run a container in detached mode. Returns container ID."""
        cmd = ["nerdctl", "run", "-d", "--name", name]
        for k, v in (env or {}).items():
            cmd.extend(["-e", f"{k}={v}"])
        for vol in (volumes or []):
            cmd.extend(["-v", vol])
        cmd.append(image)

        stdout, stderr, returncode = await self._run_in_vm(vm_name, *cmd)
        if returncode != 0:
            raise RuntimeError(f"nerdctl run failed: {stderr}")
        return stdout.strip()

    async def stop_container(self, vm_name: str, name: str) -> None:
        _, stderr, returncode = await self._run_in_vm(vm_name, "nerdctl", "stop", name)
        if returncode != 0:
            raise RuntimeError(f"nerdctl stop failed: {stderr}")

    async def remove_container(self, vm_name: str, name: str) -> None:
        _, stderr, returncode = await self._run_in_vm(vm_name, "nerdctl", "rm", "-f", name)
        if returncode != 0:
            raise RuntimeError(f"nerdctl rm failed: {stderr}")

    async def exec_in_container(self, vm_name: str, name: str, command: str) -> str:
        """Execute a command inside a running container."""
        stdout, stderr, returncode = await self._run_in_vm(
            vm_name, "nerdctl", "exec", name, *command.split(),
        )
        return stdout

    async def get_container_logs(self, vm_name: str, name: str) -> str:
        stdout, _, _ = await self._run_in_vm(vm_name, "nerdctl", "logs", name)
        return stdout

    async def list_containers(self, vm_name: str) -> list[ContainerInfo]:
        """List all containers in the VM."""
        stdout, _, returncode = await self._run_in_vm(
            vm_name, "nerdctl", "ps", "-a", "--format", "{{json .}}",
        )
        if returncode != 0:
            return []
        results = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            results.append(ContainerInfo(
                name=data["Names"],
                image=data["Image"],
                status=data["Status"],
                container_id=data["ID"],
                vm_name=vm_name,
            ))
        return results
