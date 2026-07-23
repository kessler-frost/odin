from __future__ import annotations

import platform
from pathlib import Path

from pydantic import BaseModel


class VmConfig(BaseModel):
    cpus: int
    memory: str
    disk: str


class VmInfo(BaseModel):
    name: str
    status: str
    ssh_address: str | None = None
    ssh_port: int | None = None
    cpus: int = 0
    memory: int = 0
    disk: int = 0

    @property
    def ssh_local_port_string(self) -> str | None:
        if self.ssh_address and self.ssh_port:
            return f"ssh -p {self.ssh_port} {self.ssh_address}"
        return None


class ContainerInfo(BaseModel):
    name: str
    image: str
    status: str  # running, stopped, created
    container_id: str
    overlay_ip: str | None = None
    vm_name: str


class ContainerConfig(BaseModel):
    name: str
    image_tag: str
    dockerfile_path: Path
    env: dict[str, str] = {}
    memory_limit: str | None = None
    timeout: int = 30


INSTANCE_TYPES: dict[str, VmConfig] = {
    "t2.micro": VmConfig(cpus=1, memory="1GiB", disk="10GiB"),
    "t2.small": VmConfig(cpus=1, memory="2GiB", disk="20GiB"),
    "t2.medium": VmConfig(cpus=2, memory="4GiB", disk="20GiB"),
    # V3: the gateway's EC2-compute model (gateway/models/ec2compute.py)
    # maps aws_instance's `instance_type` through this same table -- t3.*
    # rows added here (rather than a second table) so LimaRuntime's own
    # "t2.medium" host-VM lookup and EC2's per-instance VMs share one
    # source of truth.
    "t3.micro": VmConfig(cpus=1, memory="1GiB", disk="10GiB"),
    "t3.small": VmConfig(cpus=1, memory="2GiB", disk="20GiB"),
    "t3.medium": VmConfig(cpus=2, memory="4GiB", disk="20GiB"),
}


def get_instance_type(name: str) -> VmConfig:
    return INSTANCE_TYPES.get(name, INSTANCE_TYPES["t2.micro"])


def get_host_arch() -> str:
    machine = platform.machine()
    return "aarch64" if machine == "arm64" else "x86_64"
