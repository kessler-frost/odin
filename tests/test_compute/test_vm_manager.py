from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from odin.compute.vm_manager import VmManager


@pytest.fixture
def vm_manager(tmp_path):
    return VmManager(data_dir=tmp_path / ".odin")


def _mock_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.returncode = returncode
    return proc


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_create_vm(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process()
    info = await vm_manager.create_vm("ec2-web", instance_type="t2.micro")
    assert info.name == "odin-ec2-web"

    call_args = mock_exec.call_args_list[0]
    assert call_args[0][0] == "limactl"
    assert call_args[0][1] == "create"
    assert "--tty=false" in call_args[0]


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_create_vm_generates_yaml(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process()
    await vm_manager.create_vm("ec2-test", instance_type="t2.small")

    yaml_files = list((vm_manager._data_dir / "vms").glob("*.yaml"))
    assert len(yaml_files) == 1
    assert "odin-ec2-test" in yaml_files[0].name


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_delete_vm(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process()
    await vm_manager.delete_vm("ec2-web")

    call_args = mock_exec.call_args_list[0]
    assert call_args[0][0] == "limactl"
    assert call_args[0][1] == "delete"
    assert "--force" in call_args[0]
    assert "odin-ec2-web" in call_args[0]


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_stop_vm(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process()
    await vm_manager.stop_vm("ec2-web")

    call_args = mock_exec.call_args_list[0]
    assert call_args[0][0] == "limactl"
    assert call_args[0][1] == "stop"
    assert "odin-ec2-web" in call_args[0]


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_start_vm(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process()
    await vm_manager.start_vm("ec2-web")

    call_args = mock_exec.call_args_list[0]
    assert call_args[0][0] == "limactl"
    assert call_args[0][1] == "start"
    assert "--tty=false" in call_args[0]
    assert "odin-ec2-web" in call_args[0]


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_list_vms(mock_exec, vm_manager):
    # limactl list --json outputs newline-delimited JSON (NDJSON)
    lima_output = "\n".join([
        json.dumps({"name": "odin-ec2-web", "status": "Running", "sshLocalPort": 60022, "cpus": 1, "memory": 1073741824, "disk": 10737418240}),
        json.dumps({"name": "other-vm", "status": "Running", "sshLocalPort": 60023, "cpus": 2, "memory": 2147483648, "disk": 21474836480}),
    ])
    mock_exec.return_value = _mock_process(stdout=lima_output)

    vms = await vm_manager.list_vms()
    assert len(vms) == 1
    assert vms[0].name == "odin-ec2-web"
    assert vms[0].status == "Running"
    assert vms[0].ssh_port == 60022


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_get_vm_found(mock_exec, vm_manager):
    # limactl list --json outputs newline-delimited JSON (NDJSON)
    lima_output = json.dumps({"name": "odin-ec2-web", "status": "Running", "sshLocalPort": 60022, "cpus": 1, "memory": 1073741824, "disk": 10737418240})
    mock_exec.return_value = _mock_process(stdout=lima_output)
    info = await vm_manager.get_vm("ec2-web")
    assert info is not None
    assert info.name == "odin-ec2-web"


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_get_vm_not_found(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process(stdout="")
    info = await vm_manager.get_vm("ec2-nope")
    assert info is None


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_exec_in_vm(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process(stdout="hello world\n")
    output = await vm_manager.exec_in_vm("ec2-web", "echo hello world")
    assert output.strip() == "hello world"

    call_args = mock_exec.call_args_list[0]
    assert call_args[0][0] == "limactl"
    assert call_args[0][1] == "shell"
    assert "--tty=false" in call_args[0]
    assert "odin-ec2-web" in call_args[0]


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_create_vm_failure_raises(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process(
        stderr="FATAL: cannot create VM", returncode=1
    )
    with pytest.raises(RuntimeError, match="limactl create failed"):
        await vm_manager.create_vm("ec2-bad", instance_type="t2.micro")


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_create_vm_generates_ssh_keypair(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process()
    await vm_manager.create_vm("ec2-keys", instance_type="t2.micro")

    keys_dir = vm_manager._data_dir / "keys" / "odin-ec2-keys"
    assert keys_dir.exists()
    assert (keys_dir / "id_ed25519").exists()
    assert (keys_dir / "id_ed25519.pub").exists()


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_create_vm_from_yaml(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process()
    yaml_content = "cpus: 1\nmemory: 1GiB\n"
    info = await vm_manager.create_vm_from_yaml("lighthouse-vpc", yaml_content)
    assert info.name == "odin-lighthouse-vpc"

    yaml_path = vm_manager._data_dir / "vms" / "odin-lighthouse-vpc.yaml"
    assert yaml_path.exists()
    assert yaml_path.read_text() == yaml_content


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_get_vm_network_ip(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process(stdout="192.168.105.3 10.42.0.1 \n")
    ip = await vm_manager.get_vm_network_ip("lighthouse-vpc")
    assert ip == "192.168.105.3"


@patch("odin.compute.vm_manager.asyncio.create_subprocess_exec")
async def test_get_vm_network_ip_not_found(mock_exec, vm_manager):
    mock_exec.return_value = _mock_process(stdout="", returncode=1)
    ip = await vm_manager.get_vm_network_ip("nonexistent")
    assert ip is None
