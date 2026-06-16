from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from odin.compute.container_manager import ContainerManager
from odin.compute.models import VmInfo
from odin.network.models import VpcOverlay
from odin.network.nebula_manager import NebulaManager
from odin.orchestrator import Orchestrator


@pytest.fixture
def infra_dir(tmp_path):
    odin_dir = tmp_path / ".odin"
    odin_dir.mkdir()
    d = odin_dir / "infra"
    d.mkdir()
    (odin_dir / "registry.json").write_text('{"resources": {}}')
    return d


async def test_deploy_non_compute_flips_status(infra_dir):
    orch = Orchestrator(infra_dir=infra_dir)
    orch.start_engine()

    orch.registry.register("s3_bucket", service="s3", file_path=".odin/infra/s3_bucket.py")
    orch.registry.update_status("s3_bucket", "validated")

    await orch.deploy("s3_bucket")

    entry = orch.registry.get("s3_bucket")
    assert entry.status == "live"

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_deploy_ec2_creates_vm(mock_vm_cls, infra_dir):
    mock_vm = AsyncMock()
    mock_vm.create_vm = AsyncMock(
        return_value=VmInfo(name="odin-ec2-web", status="Created")
    )
    mock_vm.start_vm = AsyncMock()
    mock_vm_cls.return_value = mock_vm

    orch = Orchestrator(infra_dir=infra_dir)
    orch.start_engine()

    orch.registry.register("ec2_web", service="ec2", file_path=".odin/infra/ec2_web.py")
    orch.registry.update_status("ec2_web", "validated")

    await orch.deploy("ec2_web")

    mock_vm.create_vm.assert_called_once_with("ec2_web", instance_type="t2.micro")
    mock_vm.start_vm.assert_called_once_with("ec2_web")

    entry = orch.registry.get("ec2_web")
    assert entry.status == "live"

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_destroy_ec2_deletes_vm(mock_vm_cls, infra_dir):
    mock_vm = AsyncMock()
    mock_vm.delete_vm = AsyncMock()
    mock_vm_cls.return_value = mock_vm

    orch = Orchestrator(infra_dir=infra_dir)
    orch.start_engine()

    orch.registry.register("ec2_web", service="ec2", file_path=".odin/infra/ec2_web.py")
    orch.registry.update_status("ec2_web", "live")

    await orch.destroy("ec2_web")

    mock_vm.delete_vm.assert_called_once_with("ec2_web")

    entry = orch.registry.get("ec2_web")
    assert entry.status == "draft"

    orch.stop_engine()


async def test_deploy_all(infra_dir):
    orch = Orchestrator(infra_dir=infra_dir)
    orch.start_engine()

    orch.registry.register("s3_a", service="s3", file_path=".odin/infra/s3_a.py")
    orch.registry.update_status("s3_a", "validated")
    orch.registry.register("iam_role", service="iam", file_path=".odin/infra/iam_role.py")
    orch.registry.update_status("iam_role", "validated")

    results = await orch.deploy_all()
    assert len(results) == 2

    assert orch.registry.get("s3_a").status == "live"
    assert orch.registry.get("iam_role").status == "live"

    orch.stop_engine()


async def test_deploy_already_deployed_is_noop(infra_dir):
    orch = Orchestrator(infra_dir=infra_dir)
    orch.start_engine()

    orch.registry.register("s3_x", service="s3", file_path=".odin/infra/s3_x.py")
    orch.registry.update_status("s3_x", "live")

    await orch.deploy("s3_x")
    assert orch.registry.get("s3_x").status == "live"

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_deploy_vpc_creates_lighthouse(mock_vm_cls, infra_dir, tmp_path):
    mock_vm = AsyncMock()
    mock_vm.create_vm_from_yaml = AsyncMock(
        return_value=VmInfo(name="odin-lighthouse-vpc_main", status="Created")
    )
    mock_vm.start_vm = AsyncMock()
    mock_vm.get_vm_network_ip = AsyncMock(return_value="192.168.105.2")
    mock_vm_cls.return_value = mock_vm

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")

    orch = Orchestrator(infra_dir=infra_dir, vm_manager=mock_vm, nebula_manager=nebula)
    orch.start_engine()

    orch.registry.register("vpc_main", service="vpc", file_path=".odin/infra/vpc_main.py")
    orch.registry.update_status("vpc_main", "validated", metadata={"vpc_id": "vpc-123"})

    with patch.object(nebula, "create_ca", new_callable=AsyncMock) as mock_ca, \
         patch.object(nebula, "sign_cert", new_callable=AsyncMock) as mock_sign:
        mock_ca.return_value = type("CaInfo", (), {
            "vpc_name": "vpc_main",
            "ca_crt": tmp_path / "ca.crt",
            "ca_key": tmp_path / "ca.key",
        })()
        # Write dummy cert files for cloud-init embedding
        (tmp_path / "ca.crt").write_text("CA_CERT")
        mock_sign.return_value = type("CertPaths", (), {
            "crt": tmp_path / "host.crt",
            "key": tmp_path / "host.key",
            "ca_crt": tmp_path / "ca.crt",
        })()
        (tmp_path / "host.crt").write_text("HOST_CERT")
        (tmp_path / "host.key").write_text("HOST_KEY")

        await orch.deploy("vpc_main")

        mock_ca.assert_called_once_with("vpc_main")
        mock_sign.assert_called_once()
        mock_vm.create_vm_from_yaml.assert_called_once()
        mock_vm.start_vm.assert_called_once_with("lighthouse-vpc_main")

    entry = orch.registry.get("vpc_main")
    assert entry.status == "live"

    orch.stop_engine()


async def test_deploy_subnet_allocates_cidr(infra_dir, tmp_path):
    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")

    # Pre-create a deployed VPC with overlay
    overlay = VpcOverlay(vpc_name="vpc_main")
    nebula.save_overlay(overlay)

    orch = Orchestrator(infra_dir=infra_dir, nebula_manager=nebula)
    orch.start_engine()

    orch.registry.register("vpc_main", service="vpc", file_path=".odin/infra/vpc_main.py")
    orch.registry.update_status("vpc_main", "live", metadata={"vpc_id": "vpc-123"})

    orch.registry.register("subnet_pub", service="subnet", file_path=".odin/infra/subnet_pub.py")
    orch.registry.update_status("subnet_pub", "validated", metadata={"subnet_id": "subnet-456", "vpc_id": "vpc-123"})

    await orch.deploy("subnet_pub")

    entry = orch.registry.get("subnet_pub")
    assert entry.status == "live"

    # Verify overlay was updated
    updated_overlay = nebula.load_overlay("vpc_main")
    assert "subnet_pub" in updated_overlay.subnets
    assert updated_overlay.subnets["subnet_pub"].cidr == "10.42.1.0/24"

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_deploy_ec2_in_vpc_provisions_nebula(mock_vm_cls, infra_dir, tmp_path):
    mock_vm = AsyncMock()
    mock_vm.create_vm_from_yaml = AsyncMock(
        return_value=VmInfo(name="odin-ec2-web", status="Created")
    )
    mock_vm.start_vm = AsyncMock()
    mock_vm.generate_ssh_keypair = lambda vm_name: _create_dummy_keypair(tmp_path, vm_name)
    mock_vm._vm_name = lambda name: f"odin-{name}"
    mock_vm._data_dir = tmp_path / ".odin"
    mock_vm_cls.return_value = mock_vm

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")

    # Pre-create deployed VPC overlay with lighthouse IP and subnet
    overlay = VpcOverlay(vpc_name="vpc_main", lighthouse_underlay_ip="192.168.105.2")
    overlay.allocate_subnet("subnet_pub")
    nebula.save_overlay(overlay)

    # Write dummy CA + cert files
    vpc_dir = nebula._vpc_dir("vpc_main")
    (vpc_dir / "ca.crt").write_text("CA_CERT")
    (vpc_dir / "ca.key").write_text("CA_KEY")

    orch = Orchestrator(infra_dir=infra_dir, vm_manager=mock_vm, nebula_manager=nebula)
    orch.start_engine()

    # Register VPC and subnet as deployed
    orch.registry.register("vpc_main", service="vpc", file_path=".odin/infra/vpc_main.py")
    orch.registry.update_status("vpc_main", "live", metadata={"vpc_id": "vpc-abc"})
    orch.registry.register("subnet_pub", service="subnet", file_path=".odin/infra/subnet_pub.py")
    orch.registry.update_status("subnet_pub", "live", metadata={"subnet_id": "subnet-123", "vpc_id": "vpc-abc"})

    # Register EC2 with VPC metadata
    orch.registry.register("ec2_web", service="ec2", file_path=".odin/infra/ec2_web.py")
    orch.registry.update_status("ec2_web", "validated", metadata={
        "instance_id": "i-111",
        "instance_type": "t2.micro",
        "vpc_id": "vpc-abc",
        "subnet_id": "subnet-123",
        "security_groups": [],
    })

    with patch.object(nebula, "sign_cert", new_callable=AsyncMock) as mock_sign:
        mock_sign.return_value = type("CertPaths", (), {
            "crt": tmp_path / "ec2.crt",
            "key": tmp_path / "ec2.key",
            "ca_crt": vpc_dir / "ca.crt",
        })()
        (tmp_path / "ec2.crt").write_text("EC2_CERT")
        (tmp_path / "ec2.key").write_text("EC2_KEY")

        await orch.deploy("ec2_web")

        mock_sign.assert_called_once()
        mock_vm.create_vm_from_yaml.assert_called_once()
        mock_vm.start_vm.assert_called_once_with("ec2_web")

    entry = orch.registry.get("ec2_web")
    assert entry.status == "live"

    # Verify overlay IP was allocated
    updated_overlay = nebula.load_overlay("vpc_main")
    assert "ec2_web" in updated_overlay.subnets["subnet_pub"].assignments

    orch.stop_engine()


def _create_dummy_keypair(base_path, vm_name):
    keys_dir = base_path / ".odin" / "keys" / vm_name
    keys_dir.mkdir(parents=True, exist_ok=True)
    private_key = keys_dir / "id_ed25519"
    public_key = keys_dir / "id_ed25519.pub"
    private_key.write_text("PRIVATE")
    public_key.write_text("ssh-ed25519 AAAA testkey")
    return private_key, public_key


@patch("odin.orchestrator.VmManager")
async def test_destroy_vpc_deletes_lighthouse(mock_vm_cls, infra_dir, tmp_path):
    mock_vm = AsyncMock()
    mock_vm.delete_vm = AsyncMock()
    mock_vm_cls.return_value = mock_vm

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")

    # Create overlay
    overlay = VpcOverlay(vpc_name="vpc_main")
    nebula.save_overlay(overlay)

    orch = Orchestrator(infra_dir=infra_dir, vm_manager=mock_vm, nebula_manager=nebula)
    orch.start_engine()

    orch.registry.register("vpc_main", service="vpc", file_path=".odin/infra/vpc_main.py")
    orch.registry.update_status("vpc_main", "live", metadata={"vpc_id": "vpc-123"})

    await orch.destroy("vpc_main")

    mock_vm.delete_vm.assert_called_once_with("lighthouse-vpc_main")
    entry = orch.registry.get("vpc_main")
    assert entry.status == "draft"

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_deploy_all_respects_order(mock_vm_cls, infra_dir, tmp_path):
    mock_vm = AsyncMock()
    mock_vm.create_vm = AsyncMock(return_value=VmInfo(name="odin-ec2_web", status="Created"))
    mock_vm.start_vm = AsyncMock()
    mock_vm_cls.return_value = mock_vm

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")
    orch = Orchestrator(infra_dir=infra_dir, vm_manager=mock_vm, nebula_manager=nebula)
    orch.start_engine()

    # Register resources in mixed order
    orch.registry.register("ec2_web", service="ec2", file_path=".odin/infra/ec2_web.py")
    orch.registry.update_status("ec2_web", "validated")
    orch.registry.register("s3_data", service="s3", file_path=".odin/infra/s3_data.py")
    orch.registry.update_status("s3_data", "validated")
    orch.registry.register("sg_web", service="sg", file_path=".odin/infra/sg_web.py")
    orch.registry.update_status("sg_web", "validated")

    results = await orch.deploy_all()

    # SG should be deployed before EC2, S3 can be any order
    assert "sg_web" in results
    assert "ec2_web" in results
    assert "s3_data" in results
    sg_idx = results.index("sg_web")
    ec2_idx = results.index("ec2_web")
    assert sg_idx < ec2_idx

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_deploy_lambda_creates_container(mock_vm_cls, infra_dir, tmp_path):
    mock_vm = AsyncMock()
    mock_vm.create_vm_from_yaml = AsyncMock(
        return_value=VmInfo(name="odin-container-host-vpc_main", status="Created")
    )
    mock_vm.start_vm = AsyncMock()
    mock_vm.get_vm_network_ip = AsyncMock(return_value="192.168.105.5")
    mock_vm.generate_ssh_keypair = lambda vm_name: _create_dummy_keypair(tmp_path, vm_name)
    mock_vm._vm_name = lambda name: f"odin-{name}"
    mock_vm_cls.return_value = mock_vm

    mock_container = AsyncMock(spec=ContainerManager)
    mock_container.copy_to_vm = AsyncMock()
    mock_container.build_image = AsyncMock(return_value="sha256:abc")
    mock_container.run_container = AsyncMock(return_value="container123")

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")

    # Pre-create deployed VPC overlay with subnet
    overlay = VpcOverlay(vpc_name="vpc_main", lighthouse_underlay_ip="192.168.105.2")
    overlay.allocate_subnet("subnet_pub")
    nebula.save_overlay(overlay)

    # Write dummy CA files
    vpc_dir = nebula._vpc_dir("vpc_main")
    (vpc_dir / "ca.crt").write_text("CA_CERT")
    (vpc_dir / "ca.key").write_text("CA_KEY")

    orch = Orchestrator(
        infra_dir=infra_dir,
        vm_manager=mock_vm,
        nebula_manager=nebula,
        container_manager=mock_container,
    )
    orch.start_engine()

    # Register VPC and subnet as deployed
    orch.registry.register("vpc_main", service="vpc", file_path=".odin/infra/vpc_main.py")
    orch.registry.update_status("vpc_main", "live", metadata={"vpc_id": "vpc-abc"})
    orch.registry.register("subnet_pub", service="subnet", file_path=".odin/infra/subnet_pub.py")
    orch.registry.update_status("subnet_pub", "live", metadata={"subnet_id": "subnet-123", "vpc_id": "vpc-abc"})

    # Create the Dockerfile directory
    dockerfile_dir = infra_dir / "lambda_my-func"
    dockerfile_dir.mkdir()
    (dockerfile_dir / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY handler.py .\n")

    # Register Lambda with VPC metadata
    orch.registry.register("lambda_my-func", service="lambda", file_path=".odin/infra/lambda_my-func.py")
    orch.registry.update_status("lambda_my-func", "validated", metadata={
        "function_name": "my-func",
        "runtime": "python3.12",
        "handler": "index.handler",
        "role": "arn:aws:iam::123456789012:role/lambda-role",
        "timeout": 30,
        "memory_size": 256,
        "vpc_id": "vpc-abc",
        "subnet_id": "subnet-123",
        "security_groups": [],
    })

    with patch.object(nebula, "sign_cert", new_callable=AsyncMock) as mock_sign:
        mock_sign.return_value = type("CertPaths", (), {
            "crt": tmp_path / "lambda.crt",
            "key": tmp_path / "lambda.key",
            "ca_crt": vpc_dir / "ca.crt",
        })()
        (tmp_path / "lambda.crt").write_text("LAMBDA_CERT")
        (tmp_path / "lambda.key").write_text("LAMBDA_KEY")

        await orch.deploy("lambda_my-func")

        # Container host VM should have been created
        mock_vm.create_vm_from_yaml.assert_called_once()
        mock_vm.start_vm.assert_called_once()

        # Container should be built and run
        mock_container.copy_to_vm.assert_called_once()
        mock_container.build_image.assert_called_once()
        mock_container.run_container.assert_called_once()

    entry = orch.registry.get("lambda_my-func")
    assert entry.status == "live"

    orch.stop_engine()


async def test_destroy_lambda_removes_container(infra_dir, tmp_path):
    mock_container = AsyncMock(spec=ContainerManager)
    mock_container.stop_container = AsyncMock()
    mock_container.remove_container = AsyncMock()

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")

    # Pre-create overlay with a Lambda allocated
    overlay = VpcOverlay(vpc_name="vpc_main", lighthouse_underlay_ip="192.168.105.2")
    overlay.allocate_subnet("subnet_pub")
    overlay.subnets["subnet_pub"].allocate("lambda_my-func")
    nebula.save_overlay(overlay)

    orch = Orchestrator(
        infra_dir=infra_dir,
        nebula_manager=nebula,
        container_manager=mock_container,
    )
    orch.start_engine()

    # Register VPC and subnet
    orch.registry.register("vpc_main", service="vpc", file_path=".odin/infra/vpc_main.py")
    orch.registry.update_status("vpc_main", "live", metadata={"vpc_id": "vpc-abc"})
    orch.registry.register("subnet_pub", service="subnet", file_path=".odin/infra/subnet_pub.py")
    orch.registry.update_status("subnet_pub", "live", metadata={"subnet_id": "subnet-123", "vpc_id": "vpc-abc"})

    # Register Lambda as deployed
    orch.registry.register("lambda_my-func", service="lambda", file_path=".odin/infra/lambda_my-func.py")
    orch.registry.update_status("lambda_my-func", "live", metadata={
        "function_name": "my-func",
        "vpc_id": "vpc-abc",
        "subnet_id": "subnet-123",
        "security_groups": [],
        "container_id": "container123",
        "container_host_vm": "odin-container-host-vpc_main",
        "container_name": "lambda-my-func",
    })

    await orch.destroy("lambda_my-func")

    mock_container.stop_container.assert_called_once_with(
        "odin-container-host-vpc_main", "lambda-my-func",
    )
    mock_container.remove_container.assert_called_once_with(
        "odin-container-host-vpc_main", "lambda-my-func",
    )

    entry = orch.registry.get("lambda_my-func")
    assert entry.status == "draft"

    # Verify overlay IP was released
    updated_overlay = nebula.load_overlay("vpc_main")
    assert "lambda_my-func" not in updated_overlay.subnets["subnet_pub"].assignments

    orch.stop_engine()


async def test_invoke_lambda_executes_in_container(infra_dir, tmp_path):
    mock_container = AsyncMock(spec=ContainerManager)
    mock_container.exec_in_container = AsyncMock(return_value='{"statusCode": 200}\n')

    orch = Orchestrator(
        infra_dir=infra_dir,
        container_manager=mock_container,
    )
    orch.start_engine()

    orch.registry.register("lambda_my-func", service="lambda", file_path=".odin/infra/lambda_my-func.py")
    orch.registry.update_status("lambda_my-func", "live", metadata={
        "function_name": "my-func",
        "handler": "index.handler",
        "container_host_vm": "odin-container-host-vpc_main",
        "container_name": "lambda-my-func",
    })

    result = await orch.invoke_lambda("lambda_my-func", '{"key": "value"}')
    assert result == '{"statusCode": 200}\n'

    mock_container.exec_in_container.assert_called_once()

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_destroy_vpc_deletes_container_host(mock_vm_cls, infra_dir, tmp_path):
    mock_vm = AsyncMock()
    mock_vm.delete_vm = AsyncMock()
    mock_vm_cls.return_value = mock_vm

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")
    overlay = VpcOverlay(vpc_name="vpc_main")
    nebula.save_overlay(overlay)

    orch = Orchestrator(infra_dir=infra_dir, vm_manager=mock_vm, nebula_manager=nebula)
    orch._container_hosts["vpc_main"] = "odin-container-host-vpc_main"
    orch.start_engine()

    orch.registry.register("vpc_main", service="vpc", file_path=".odin/infra/vpc_main.py")
    orch.registry.update_status("vpc_main", "live", metadata={"vpc_id": "vpc-123"})

    await orch.destroy("vpc_main")

    # Should delete both lighthouse and container-host VMs
    delete_calls = [call.args[0] for call in mock_vm.delete_vm.call_args_list]
    assert "lighthouse-vpc_main" in delete_calls
    assert "container-host-vpc_main" in delete_calls

    # Container host tracking should be cleaned up
    assert "vpc_main" not in orch._container_hosts

    orch.stop_engine()


@patch("odin.orchestrator.VmManager")
async def test_deploy_all_deploys_lambda_after_ec2(mock_vm_cls, infra_dir, tmp_path):
    mock_vm = AsyncMock()
    mock_vm.create_vm = AsyncMock(return_value=VmInfo(name="odin-ec2_web", status="Created"))
    mock_vm.start_vm = AsyncMock()
    mock_vm_cls.return_value = mock_vm

    mock_container = AsyncMock(spec=ContainerManager)

    nebula = NebulaManager(data_dir=tmp_path / ".odin-nebula")
    orch = Orchestrator(
        infra_dir=infra_dir,
        vm_manager=mock_vm,
        nebula_manager=nebula,
        container_manager=mock_container,
    )
    orch.start_engine()

    # Register EC2 and Lambda
    orch.registry.register("ec2_web", service="ec2", file_path=".odin/infra/ec2_web.py")
    orch.registry.update_status("ec2_web", "validated")
    orch.registry.register("lambda_func", service="lambda", file_path=".odin/infra/lambda_func.py")
    orch.registry.update_status("lambda_func", "validated")

    results = await orch.deploy_all()

    ec2_idx = results.index("ec2_web")
    lambda_idx = results.index("lambda_func")
    assert ec2_idx < lambda_idx

    orch.stop_engine()
