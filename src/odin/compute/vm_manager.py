from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from odin.compute.cloud_init import generate_cloud_init
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import VmInfo, get_instance_type

ODIN_VM_PREFIX = "odin-"


class VmManager:
    """Async wrapper around limactl CLI for Lima VM lifecycle."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path.home() / ".odin"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        (self._data_dir / "vms").mkdir(exist_ok=True)
        (self._data_dir / "keys").mkdir(exist_ok=True)

    def _vm_name(self, name: str) -> str:
        return f"{ODIN_VM_PREFIX}{name}"

    def generate_ssh_keypair(self, vm_name: str) -> tuple[Path, Path]:
        keys_dir = self._data_dir / "keys" / vm_name
        keys_dir.mkdir(parents=True, exist_ok=True)
        private_key = keys_dir / "id_ed25519"
        public_key = keys_dir / "id_ed25519.pub"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(private_key), "-N", "", "-q"],
            check=True,
        )
        return private_key, public_key

    async def _run(self, *args: str) -> tuple[str, str, int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return "", f"{args[0]}: command not found", 127
        stdout, stderr = await proc.communicate()
        return stdout.decode(), stderr.decode(), proc.returncode

    async def create_vm(
        self,
        name: str,
        instance_type: str = "t2.micro",
    ) -> VmInfo:
        vm_name = self._vm_name(name)
        config = get_instance_type(instance_type)

        _private_key, public_key = self.generate_ssh_keypair(vm_name)
        ssh_pubkey = public_key.read_text().strip()

        cloud_init = generate_cloud_init(hostname=name, ssh_pubkey=ssh_pubkey)
        lima_yaml = generate_lima_yaml(config, cloud_init_script=cloud_init)

        yaml_path = self._data_dir / "vms" / f"{vm_name}.yaml"
        yaml_path.write_text(lima_yaml)

        stdout, stderr, returncode = await self._run(
            "limactl", "create", "--tty=false", f"--name={vm_name}", str(yaml_path),
        )
        if returncode != 0:
            raise RuntimeError(f"limactl create failed: {stderr}")

        return VmInfo(name=vm_name, status="Created")

    async def start_vm(self, name: str) -> None:
        vm_name = self._vm_name(name)
        stdout, stderr, returncode = await self._run(
            "limactl", "start", "--tty=false", vm_name,
        )
        if returncode != 0:
            raise RuntimeError(f"limactl start failed: {stderr}")

    async def stop_vm(self, name: str) -> None:
        vm_name = self._vm_name(name)
        stdout, stderr, returncode = await self._run(
            "limactl", "stop", vm_name,
        )
        if returncode != 0:
            raise RuntimeError(f"limactl stop failed: {stderr}")

    async def delete_vm(self, name: str) -> None:
        vm_name = self._vm_name(name)
        stdout, stderr, returncode = await self._run(
            "limactl", "delete", "--force", vm_name,
        )
        if returncode != 0:
            raise RuntimeError(f"limactl delete failed: {stderr}")

    async def list_vms(self) -> list[VmInfo]:
        stdout, stderr, returncode = await self._run(
            "limactl", "list", "--json",
        )
        if returncode != 0:
            return []
        entries = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        return [
            VmInfo(
                name=e["name"],
                status=e.get("status", "Unknown"),
                ssh_address="127.0.0.1",
                ssh_port=e.get("sshLocalPort"),
                cpus=e.get("cpus", 0),
                memory=e.get("memory", 0),
                disk=e.get("disk", 0),
            )
            for e in entries
            if e["name"].startswith(ODIN_VM_PREFIX)
        ]

    async def get_vm(self, name: str) -> VmInfo | None:
        vms = await self.list_vms()
        vm_name = self._vm_name(name)
        matches = [v for v in vms if v.name == vm_name]
        return matches[0] if matches else None

    async def exec_in_vm(self, name: str, command: str) -> str:
        vm_name = self._vm_name(name)
        stdout, stderr, returncode = await self._run(
            "limactl", "shell", "--tty=false", vm_name, "--", *command.split(),
        )
        return stdout

    async def create_vm_from_yaml(self, name: str, yaml_content: str) -> VmInfo:
        """Create a VM from a pre-built Lima YAML string."""
        vm_name = self._vm_name(name)
        yaml_path = self._data_dir / "vms" / f"{vm_name}.yaml"
        yaml_path.write_text(yaml_content)

        _, stderr, returncode = await self._run(
            "limactl", "create", "--tty=false", f"--name={vm_name}", str(yaml_path),
        )
        if returncode != 0:
            raise RuntimeError(f"limactl create failed: {stderr}")

        return VmInfo(name=vm_name, status="Created")

    async def get_vm_network_ip(self, name: str) -> str | None:
        """Get the shared network IP for a VM via hostname -I."""
        vm_name = self._vm_name(name)
        stdout, _, returncode = await self._run(
            "limactl", "shell", "--tty=false", vm_name, "--", "hostname", "-I",
        )
        if returncode != 0:
            return None
        for ip in stdout.strip().split():
            if not ip.startswith("127."):
                return ip
        return None
