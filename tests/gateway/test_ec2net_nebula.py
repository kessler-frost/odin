"""V1b -- ec2net's Nebula compile: CreateVpc bootstraps the env's REAL
Nebula network (nebula-cert is on PATH; CA/cert artifacts only, no
lighthouse process), and every SG mutation recompiles the group's ingress
rules through `fabric.nebula.sg_rules_to_firewall` onto the SG record,
where `mesh_state` (the `GET /mesh?env=` read model) picks them up.

Requests are real boto3-signed captures driven through classify() ->
synth.pure_answer(), reusing test_ec2net's helpers.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from odin.fabric.models import FirewallRule, FirewallRules
from odin.fabric.nebula import NebulaManager, mesh_state
from odin.gateway.stores import SynthStores

from .test_ec2net import ENV, _answer, _create_sg, _create_vpc


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


def _authorize_golden_ingress(stores, sink, ec2, group_id: str) -> None:
    """The research's own golden example: tcp/443 from 10.0.0.0/16 + an
    SG-ref rule (self-referencing here -- any sg id works)."""
    req = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": group_id}]},
    ]))
    assert _answer(stores, req).status_code == 200


def _stored_firewall(stores, group_id: str) -> FirewallRules:
    return FirewallRules.model_validate(stores.ec2net.get(ENV, f"sg:{group_id}")["firewall"])


# --- CreateVpc -> ensure_network (real artifacts, no daemon) ------------------


def test_create_vpc_bootstraps_the_envs_real_nebula_network(sink, ec2, stores, tmp_path):
    vpc_id = _create_vpc(stores, sink, ec2)
    nebula_dir = tmp_path / ENV / "nebula"
    assert (nebula_dir / "ca.crt").exists() and (nebula_dir / "ca.key").exists()
    assert (nebula_dir / "hosts" / "lighthouse.crt").exists()
    assert (nebula_dir / "overlay.json").exists()
    assert stores.ec2net.get(ENV, f"vpc:{vpc_id}")["nebula_network"] == ENV


def test_second_vpc_reuses_the_env_network(sink, ec2, stores, tmp_path):
    """Per-ENV mesh, 1:1 VPC<->network for now: a second CreateVpc must not
    re-mint the CA (ensure_network is idempotent)."""
    vpc_a = _create_vpc(stores, sink, ec2, "10.0.0.0/16")
    ca_bytes = (tmp_path / ENV / "nebula" / "ca.crt").read_bytes()
    vpc_b = _create_vpc(stores, sink, ec2, "10.1.0.0/16")
    assert (tmp_path / ENV / "nebula" / "ca.crt").read_bytes() == ca_bytes
    assert stores.ec2net.get(ENV, f"vpc:{vpc_a}")["nebula_network"] == ENV
    assert stores.ec2net.get(ENV, f"vpc:{vpc_b}")["nebula_network"] == ENV


# --- SG mutations -> sg_rules_to_firewall recompile ---------------------------


def test_create_security_group_compiles_an_empty_inbound_firewall(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    firewall = _stored_firewall(stores, group_id)
    assert firewall.inbound == []  # no ingress yet; the seeded rule is egress
    assert firewall.outbound == [FirewallRule(port="any", proto="any")]


def test_authorize_recompiles_the_golden_firewall(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    _authorize_golden_ingress(stores, sink, ec2, group_id)
    assert _stored_firewall(stores, group_id) == FirewallRules(
        inbound=[
            FirewallRule(port="443", proto="tcp", cidr="10.0.0.0/16"),
            FirewallRule(port="443", proto="tcp", group=group_id),
        ],
        outbound=[FirewallRule(port="any", proto="any")],
    )


def test_revoke_recompiles_down_to_the_remaining_rules(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    _authorize_golden_ingress(stores, sink, ec2, group_id)
    revoke = sink.call(lambda: ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]},
    ]))
    assert _answer(stores, revoke).status_code == 200
    assert _stored_firewall(stores, group_id).inbound == [
        FirewallRule(port="443", proto="tcp", group=group_id),
    ]


def test_compiled_firewall_is_what_a_v3_node_config_consumes(sink, ec2, stores, tmp_path):
    """REAL but dormant: round-trip the STORED compile through
    generate_config -- the YAML a nebula daemon would read at V3."""
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    _authorize_golden_ingress(stores, sink, ec2, group_id)
    manager = NebulaManager(tmp_path / ENV / "nebula")
    config = yaml.safe_load(manager.generate_config("10.42.0.1", "127.0.0.1", _stored_firewall(stores, group_id)))
    assert config["firewall"]["inbound"] == [
        {"port": "443", "proto": "tcp", "cidr": "10.0.0.0/16"},
        {"port": "443", "proto": "tcp", "group": group_id},
    ]
    assert config["firewall"]["outbound"] == [{"port": "any", "proto": "any", "host": "any"}]


# --- mesh_state: the GET /mesh?env= read model --------------------------------


def test_mesh_state_shows_per_vpc_networks_and_compiled_rules(sink, ec2, stores, tmp_path):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    _authorize_golden_ingress(stores, sink, ec2, group_id)

    state = mesh_state(tmp_path, ENV)
    assert [(v.vpc_id, v.network) for v in state.vpcs] == [(vpc_id, ENV)]
    assert {sg.group_name for sg in state.security_groups} == {"default", "web"}  # incl. the vpc's sidecar
    (web,) = [sg for sg in state.security_groups if sg.sg_id == group_id]
    assert web.vpc_id == vpc_id and web.group_name == "web"
    assert web.firewall == _stored_firewall(stores, group_id)


def test_deleted_sg_leaves_mesh_state(sink, ec2, stores, tmp_path):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    delete_req = sink.call(lambda: ec2.delete_security_group(GroupId=group_id))
    assert _answer(stores, delete_req).status_code == 200
    assert group_id not in {sg.sg_id for sg in mesh_state(tmp_path, ENV).security_groups}
