from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from odin.network.models import CertPaths, FirewallRule, FirewallRules, VpcOverlay
from odin.network.nebula_manager import NebulaManager


@pytest.fixture
def nebula(tmp_path):
    return NebulaManager(data_dir=tmp_path / ".odin")


def _mock_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.returncode = returncode
    return proc


@patch("odin.network.nebula_manager.asyncio.create_subprocess_exec")
async def test_create_ca(mock_exec, nebula):
    mock_exec.return_value = _mock_process()
    ca_info = await nebula.create_ca("vpc_main")

    assert ca_info.vpc_name == "vpc_main"
    call_args = mock_exec.call_args[0]
    assert call_args[0] == "nebula-cert"
    assert call_args[1] == "ca"
    assert "-name" in call_args
    assert "vpc_main" in call_args


@patch("odin.network.nebula_manager.asyncio.create_subprocess_exec")
async def test_create_ca_failure_raises(mock_exec, nebula):
    mock_exec.return_value = _mock_process(stderr="ca error", returncode=1)
    with pytest.raises(RuntimeError, match="nebula-cert ca failed"):
        await nebula.create_ca("vpc_bad")


@patch("odin.network.nebula_manager.asyncio.create_subprocess_exec")
async def test_sign_cert(mock_exec, nebula):
    mock_exec.return_value = _mock_process()
    cert_paths = await nebula.sign_cert("vpc_main", "ec2_web", "10.42.1.1/24", groups=["web"])

    assert cert_paths.crt == nebula._vpc_dir("vpc_main") / "hosts" / "ec2_web.crt"
    call_args = mock_exec.call_args[0]
    assert "nebula-cert" in call_args
    assert "sign" in call_args
    assert "-groups" in call_args


@patch("odin.network.nebula_manager.asyncio.create_subprocess_exec")
async def test_sign_cert_no_groups(mock_exec, nebula):
    mock_exec.return_value = _mock_process()
    await nebula.sign_cert("vpc_main", "ec2_api", "10.42.1.2/24")

    call_args = mock_exec.call_args[0]
    assert "-groups" not in call_args


@patch("odin.network.nebula_manager.asyncio.create_subprocess_exec")
async def test_sign_cert_failure_raises(mock_exec, nebula):
    mock_exec.return_value = _mock_process(stderr="sign error", returncode=1)
    with pytest.raises(RuntimeError, match="nebula-cert sign failed"):
        await nebula.sign_cert("vpc_bad", "host", "10.42.1.1/24")


async def test_revoke_cert(nebula):
    hosts_dir = nebula._hosts_dir("vpc_main")
    (hosts_dir / "ec2_web.crt").write_text("cert")
    (hosts_dir / "ec2_web.key").write_text("key")

    await nebula.revoke_cert("vpc_main", "ec2_web")

    assert not (hosts_dir / "ec2_web.crt").exists()
    assert not (hosts_dir / "ec2_web.key").exists()


async def test_revoke_cert_missing_is_noop(nebula):
    await nebula.revoke_cert("vpc_main", "nonexistent")


def test_generate_config_lighthouse(nebula):
    cert_paths = CertPaths(
        crt=Path("/etc/nebula/host.crt"),
        key=Path("/etc/nebula/host.key"),
        ca_crt=Path("/etc/nebula/ca.crt"),
    )
    firewall = FirewallRules(
        inbound=[FirewallRule(port="any", proto="any")],
        outbound=[FirewallRule(port="any", proto="any")],
    )
    config_str = nebula.generate_config(
        lighthouse_ip="10.42.0.1",
        lighthouse_underlay="",
        cert_paths=cert_paths,
        firewall_rules=firewall,
        is_lighthouse=True,
    )
    config = yaml.safe_load(config_str)
    assert config["lighthouse"]["am_lighthouse"] is True
    assert "static_host_map" not in config


def test_generate_config_host(nebula):
    cert_paths = CertPaths(
        crt=Path("/etc/nebula/host.crt"),
        key=Path("/etc/nebula/host.key"),
        ca_crt=Path("/etc/nebula/ca.crt"),
    )
    firewall = FirewallRules(
        inbound=[FirewallRule(port="80", proto="tcp", cidr="10.42.1.0/24")],
        outbound=[FirewallRule(port="any", proto="any")],
    )
    config_str = nebula.generate_config(
        lighthouse_ip="10.42.0.1",
        lighthouse_underlay="192.168.105.2",
        cert_paths=cert_paths,
        firewall_rules=firewall,
    )
    config = yaml.safe_load(config_str)
    assert config["lighthouse"]["am_lighthouse"] is False
    assert config["static_host_map"]["10.42.0.1"] == ["192.168.105.2:4242"]
    assert config["lighthouse"]["hosts"] == ["10.42.0.1"]
    assert config["firewall"]["inbound"][0]["port"] == "80"


def test_save_and_load_overlay(nebula):
    overlay = VpcOverlay(vpc_name="vpc_main")
    overlay.allocate_subnet("subnet_pub")
    nebula.save_overlay(overlay)

    loaded = nebula.load_overlay("vpc_main")
    assert loaded is not None
    assert loaded.vpc_name == "vpc_main"
    assert "subnet_pub" in loaded.subnets
    assert loaded.subnets["subnet_pub"].cidr == "10.42.1.0/24"


def test_load_overlay_missing(nebula):
    result = nebula.load_overlay("nonexistent")
    assert result is None
