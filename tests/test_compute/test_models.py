from __future__ import annotations

import platform
from pathlib import Path

from odin.compute.models import (
    INSTANCE_TYPES,
    ContainerConfig,
    ContainerInfo,
    VmConfig,
    VmInfo,
    get_host_arch,
    get_instance_type,
)


def test_vm_config_defaults():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    assert config.cpus == 1
    assert config.memory == "1GiB"
    assert config.disk == "10GiB"


def test_vm_info_fields():
    info = VmInfo(
        name="ec2-test",
        status="Running",
        ssh_address="127.0.0.1",
        ssh_port=60022,
        cpus=1,
        memory=1073741824,
        disk=10737418240,
    )
    assert info.name == "ec2-test"
    assert info.status == "Running"
    assert info.ssh_local_port_string == "ssh -p 60022 127.0.0.1"


def test_vm_info_no_ssh():
    info = VmInfo(name="ec2-off", status="Stopped")
    assert info.ssh_address is None
    assert info.ssh_port is None


def test_instance_types_contains_basics():
    assert "t2.micro" in INSTANCE_TYPES
    assert "t2.small" in INSTANCE_TYPES
    assert "t2.medium" in INSTANCE_TYPES


def test_get_instance_type_known():
    config = get_instance_type("t2.micro")
    assert config.cpus == 1
    assert config.memory == "1GiB"
    assert config.disk == "10GiB"


def test_get_instance_type_unknown_returns_micro():
    config = get_instance_type("m5.xlarge")
    assert config.cpus == 1
    assert config.memory == "1GiB"


def test_get_host_arch():
    arch = get_host_arch()
    expected = "aarch64" if platform.machine() == "arm64" else "x86_64"
    assert arch == expected


def test_container_info_defaults():
    info = ContainerInfo(
        name="lambda-my-func",
        image="lambda-my-func:latest",
        status="running",
        container_id="abc123",
        vm_name="odin-container-host-vpc_main",
    )
    assert info.overlay_ip is None
    assert info.vm_name == "odin-container-host-vpc_main"


def test_container_config():
    cfg = ContainerConfig(
        name="lambda-my-func",
        image_tag="lambda-my-func:latest",
        dockerfile_path=Path("infra/lambda_my-func"),
        env={"HANDLER": "index.handler"},
        memory_limit="256m",
        timeout=30,
    )
    assert cfg.env["HANDLER"] == "index.handler"
    assert cfg.timeout == 30
