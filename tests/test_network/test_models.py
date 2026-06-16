from __future__ import annotations

from pathlib import Path

from odin.network.models import (
    CaInfo,
    CertPaths,
    FirewallRule,
    FirewallRules,
    SubnetAllocation,
    VpcOverlay,
)


def test_ca_info():
    info = CaInfo(vpc_name="vpc_main", ca_crt=Path("/tmp/ca.crt"), ca_key=Path("/tmp/ca.key"))
    assert info.vpc_name == "vpc_main"


def test_cert_paths():
    paths = CertPaths(
        crt=Path("/tmp/host.crt"),
        key=Path("/tmp/host.key"),
        ca_crt=Path("/tmp/ca.crt"),
    )
    assert paths.crt == Path("/tmp/host.crt")


def test_firewall_rule():
    rule = FirewallRule(port="80", proto="tcp", cidr="10.42.1.0/24")
    assert rule.port == "80"
    assert rule.group is None


def test_firewall_rules_defaults():
    rules = FirewallRules()
    assert rules.inbound == []
    assert rules.outbound == []


def test_subnet_allocation_allocate():
    alloc = SubnetAllocation(vpc_name="vpc_main", subnet_name="subnet_pub", cidr="10.42.1.0/24")
    ip1 = alloc.allocate("ec2_web")
    ip2 = alloc.allocate("ec2_api")
    assert ip1 == "10.42.1.1"
    assert ip2 == "10.42.1.2"
    assert alloc.assignments["ec2_web"] == "10.42.1.1"
    assert alloc.next_ip == 3


def test_subnet_allocation_release():
    alloc = SubnetAllocation(vpc_name="vpc_main", subnet_name="subnet_pub", cidr="10.42.1.0/24")
    alloc.allocate("ec2_web")
    released = alloc.release("ec2_web")
    assert released == "10.42.1.1"
    assert "ec2_web" not in alloc.assignments


def test_subnet_allocation_release_missing():
    alloc = SubnetAllocation(vpc_name="vpc_main", subnet_name="subnet_pub", cidr="10.42.1.0/24")
    released = alloc.release("nonexistent")
    assert released is None


def test_vpc_overlay_allocate_subnet():
    overlay = VpcOverlay(vpc_name="vpc_main")
    alloc1 = overlay.allocate_subnet("subnet_pub")
    alloc2 = overlay.allocate_subnet("subnet_priv")
    assert alloc1.cidr == "10.42.1.0/24"
    assert alloc2.cidr == "10.42.2.0/24"
    assert overlay.next_subnet == 3
    assert "subnet_pub" in overlay.subnets


def test_vpc_overlay_defaults():
    overlay = VpcOverlay(vpc_name="vpc_main")
    assert overlay.base_cidr == "10.42.0.0/16"
    assert overlay.lighthouse_ip == "10.42.0.1"
    assert overlay.lighthouse_underlay_ip is None
    assert overlay.next_subnet == 1


def test_vpc_overlay_get_subnet():
    overlay = VpcOverlay(vpc_name="vpc_main")
    overlay.allocate_subnet("subnet_pub")
    assert overlay.get_subnet("subnet_pub") is not None
    assert overlay.get_subnet("nonexistent") is None
