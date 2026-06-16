from __future__ import annotations

from odin.network.vpc_mapper import LIGHTHOUSE_FIREWALL, sg_rules_to_firewall


def test_all_traffic_rule():
    permissions = [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    rules = sg_rules_to_firewall(permissions)
    assert len(rules.inbound) == 1
    assert rules.inbound[0].port == "any"
    assert rules.inbound[0].proto == "any"
    assert rules.inbound[0].cidr == "0.0.0.0/0"


def test_tcp_single_port():
    permissions = [{
        "IpProtocol": "tcp",
        "FromPort": 80,
        "ToPort": 80,
        "IpRanges": [{"CidrIp": "10.42.1.0/24"}],
    }]
    rules = sg_rules_to_firewall(permissions)
    assert rules.inbound[0].port == "80"
    assert rules.inbound[0].proto == "tcp"
    assert rules.inbound[0].cidr == "10.42.1.0/24"


def test_port_range():
    permissions = [{
        "IpProtocol": "tcp",
        "FromPort": 8000,
        "ToPort": 9000,
        "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
    }]
    rules = sg_rules_to_firewall(permissions)
    assert rules.inbound[0].port == "8000-9000"


def test_security_group_source():
    permissions = [{
        "IpProtocol": "tcp",
        "FromPort": 443,
        "ToPort": 443,
        "UserIdGroupPairs": [{"GroupId": "sg-webservers"}],
    }]
    rules = sg_rules_to_firewall(permissions)
    assert rules.inbound[0].group == "sg-webservers"
    assert rules.inbound[0].cidr is None


def test_multiple_sources():
    permissions = [{
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
        "IpRanges": [
            {"CidrIp": "10.42.1.0/24"},
            {"CidrIp": "10.42.2.0/24"},
        ],
    }]
    rules = sg_rules_to_firewall(permissions)
    assert len(rules.inbound) == 2


def test_no_source_allows_any():
    permissions = [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80}]
    rules = sg_rules_to_firewall(permissions)
    assert len(rules.inbound) == 1
    assert rules.inbound[0].cidr is None
    assert rules.inbound[0].group is None


def test_outbound_always_any():
    permissions = [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]
    rules = sg_rules_to_firewall(permissions)
    assert len(rules.outbound) == 1
    assert rules.outbound[0].port == "any"
    assert rules.outbound[0].proto == "any"


def test_empty_permissions():
    rules = sg_rules_to_firewall([])
    assert rules.inbound == []
    assert len(rules.outbound) == 1


def test_lighthouse_firewall_allows_all():
    assert len(LIGHTHOUSE_FIREWALL.inbound) == 1
    assert LIGHTHOUSE_FIREWALL.inbound[0].port == "any"
    assert len(LIGHTHOUSE_FIREWALL.outbound) == 1
