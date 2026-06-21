"""Unit tests for SimulationRunner orchestration (managers mocked — no real VMs)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from odin.api.canvas import CanvasGraph
from odin.network.models import CertPaths
from odin.simulator.registry import ResourceRegistry
from odin.simulator.runner import SimulationRunner


@pytest.fixture
def registry(tmp_path) -> ResourceRegistry:
    path = tmp_path / "registry.json"
    path.write_text('{"resources": {}}')
    return ResourceRegistry(path)


def _build(tmp_path, registry):
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAA")
    ca = tmp_path / "ca.crt"
    ca.write_text("CA")
    crt = tmp_path / "h.crt"
    crt.write_text("CRT")
    key = tmp_path / "h.key"
    key.write_text("KEY")

    vm = MagicMock()
    vm.generate_ssh_keypair = MagicMock(return_value=(tmp_path / "id", pub))
    vm.create_vm_from_yaml = AsyncMock()
    vm.start_vm = AsyncMock()
    vm.stop_vm = AsyncMock()
    vm.delete_vm = AsyncMock()

    nebula = MagicMock()
    nebula.create_ca = AsyncMock()
    nebula.sign_cert = AsyncMock(return_value=CertPaths(crt=crt, key=key, ca_crt=ca))
    nebula.generate_config = MagicMock(return_value="nebula-config")
    nebula.save_overlay = MagicMock()

    container = MagicMock()
    container.run_container = AsyncMock()
    container.stop_container = AsyncMock()
    container.remove_container = AsyncMock()

    runner = SimulationRunner(vm, container, nebula, registry, state_path=tmp_path / "sim.json")
    return runner, vm, nebula, container


async def test_standalone_ec2_creates_one_vm(registry, tmp_path):
    runner, vm, nebula, container = _build(tmp_path, registry)
    graph = CanvasGraph(nodes=[
        {"id": "e", "type": "ec2", "position": {"x": 0, "y": 0}, "data": {"label": "web"}},
    ])
    result = await runner.simulate(graph)
    assert result["simulated"] == ["ec2_web"]
    vm.create_vm_from_yaml.assert_awaited_once()
    vm.start_vm.assert_awaited_once_with("ec2_web")
    nebula.create_ca.assert_not_called()  # no VPC → no overlay
    assert registry.get("ec2_web").status == "simulated"


async def test_vpc_ec2_lambda_full_flow(registry, tmp_path):
    runner, vm, nebula, container = _build(tmp_path, registry)
    graph = CanvasGraph(nodes=[
        {"id": "v", "type": "vpc", "position": {"x": 0, "y": 0}, "size": {"width": 600, "height": 400}, "data": {"label": "prod"}},
        {"id": "e", "type": "ec2", "position": {"x": 40, "y": 60}, "size": {"width": 200, "height": 80}, "data": {"label": "web", "instanceType": "t2.micro"}},
        {"id": "l", "type": "lambda", "position": {"x": 40, "y": 220}, "size": {"width": 200, "height": 80}, "data": {"label": "fn", "runtime": "python3.12"}},
    ])
    result = await runner.simulate(graph)
    assert set(result["simulated"]) == {"ec2_web", "lambda_fn"}
    nebula.create_ca.assert_awaited_once_with("vpc_prod")
    nebula.sign_cert.assert_awaited()  # EC2 in the VPC gets a Nebula cert
    container.run_container.assert_awaited_once()
    assert vm.create_vm_from_yaml.await_count == 2  # the EC2 VM + the Lambda host VM
    assert registry.get("ec2_web").status == "simulated"
    assert registry.get("lambda_fn").status == "simulated"


async def test_cleanup_tears_everything_down(registry, tmp_path):
    runner, vm, nebula, container = _build(tmp_path, registry)
    graph = CanvasGraph(nodes=[
        {"id": "e", "type": "ec2", "position": {"x": 0, "y": 0}, "data": {"label": "web"}},
    ])
    await runner.simulate(graph)
    res = await runner.cleanup()
    assert "ec2_web" in res["destroyed"]
    vm.delete_vm.assert_awaited_with("ec2_web")  # --force stops + deletes
    assert registry.get("ec2_web").status == "draft"


async def test_cleanup_without_state_is_noop(registry, tmp_path):
    runner, *_ = _build(tmp_path, registry)
    assert await runner.cleanup() == {"destroyed": []}


async def test_s3_node_runs_rustfs_container(registry, tmp_path):
    """A stateful service node (S3) runs as a real container (RustFS) in Simulate."""
    runner, vm, nebula, container = _build(tmp_path, registry)
    graph = CanvasGraph(nodes=[
        {"id": "1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "data"}},
    ])
    result = await runner.simulate(graph)
    assert result["simulated"] == ["s3_data"]
    container.run_container.assert_awaited_once()
    assert "rustfs" in container.run_container.call_args.kwargs["image"]
    assert registry.get("s3_data").status == "simulated"
