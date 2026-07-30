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


async def _authorize_golden_ingress(stores, sink, ec2, group_id: str) -> None:
    """The research's own golden example: tcp/443 from 10.0.0.0/16 + an
    SG-ref rule (self-referencing here -- any sg id works)."""
    req = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": group_id}]},
    ]))
    assert (await _answer(stores, req)).status_code == 200


def _stored_firewall(stores, group_id: str) -> FirewallRules:
    return FirewallRules.model_validate(stores.ec2net.get(ENV, f"sg:{group_id}")["firewall"])


# --- CreateVpc -> ensure_network (real artifacts, no daemon) ------------------


async def test_create_vpc_bootstraps_the_envs_real_nebula_network(sink, ec2, stores, tmp_path):
    vpc_id = await _create_vpc(stores, sink, ec2)
    nebula_dir = tmp_path / ENV / "nebula"
    assert (nebula_dir / "ca.crt").exists() and (nebula_dir / "ca.key").exists()
    assert (nebula_dir / "hosts" / "lighthouse.crt").exists()
    assert (nebula_dir / "overlay.json").exists()
    assert stores.ec2net.get(ENV, f"vpc:{vpc_id}")["nebula_network"] == ENV


async def test_second_vpc_reuses_the_env_network(sink, ec2, stores, tmp_path):
    """Per-ENV mesh, 1:1 VPC<->network for now: a second CreateVpc must not
    re-mint the CA (ensure_network is idempotent)."""
    vpc_a = await _create_vpc(stores, sink, ec2, "10.0.0.0/16")
    ca_bytes = (tmp_path / ENV / "nebula" / "ca.crt").read_bytes()
    vpc_b = await _create_vpc(stores, sink, ec2, "10.1.0.0/16")
    assert (tmp_path / ENV / "nebula" / "ca.crt").read_bytes() == ca_bytes
    assert stores.ec2net.get(ENV, f"vpc:{vpc_a}")["nebula_network"] == ENV
    assert stores.ec2net.get(ENV, f"vpc:{vpc_b}")["nebula_network"] == ENV


# --- SG mutations -> sg_rules_to_firewall recompile ---------------------------


async def test_create_security_group_compiles_an_empty_inbound_firewall(sink, ec2, stores):
    """THE upgrade-safety test for v0.8.17's egress compilation: a group that
    says nothing about egress must still allow ALL outbound, because that is
    what AWS's seeded rule means and what every canvas drawn before
    `egressRules` existed relies on.

    The outbound rule's SHAPE changed even though its meaning did not, and
    that is worth stating rather than hiding. It used to be a hardcoded
    `FirewallRule(port="any", proto="any")` -- a constant that described no
    group at all. It is now the real compilation of the seeded egress rule,
    which carries `0.0.0.0/0`, so the cidr is populated. On odin's IPv4-only
    overlay those admit the same packets (MEASURED, 2026-07-30: a member whose
    only outbound rule was `port/proto any, cidr 0.0.0.0/0` reached a live
    listener on a peer), and it is the identical form the INBOUND side has
    always compiled a `0.0.0.0/0` rule to.

    The cost, which is real and is in docs/limits.md: an environment that was
    already on the mesh before this change sees its config text move once, so
    every member takes one firewall-only reload (a SIGHUP, no tunnel dropped).
    Byte-identical was not achievable without special-casing the wide-open
    rule back into `host: any`, which would have made this the one rule odin
    compiles differently from every other."""
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    firewall = _stored_firewall(stores, group_id)
    assert firewall.inbound == []  # no ingress yet; the seeded rule is egress
    assert firewall.outbound == [FirewallRule(port="any", proto="any", cidr="0.0.0.0/0")]


async def test_authorize_recompiles_the_golden_firewall(sink, ec2, stores):
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    await _authorize_golden_ingress(stores, sink, ec2, group_id)
    assert _stored_firewall(stores, group_id) == FirewallRules(
        inbound=[
            FirewallRule(port="443", proto="tcp", cidr="10.0.0.0/16"),
            FirewallRule(port="443", proto="tcp", group=group_id),
        ],
        outbound=[FirewallRule(port="any", proto="any", cidr="0.0.0.0/0")],
    )


async def test_authorize_egress_narrows_the_outbound_firewall(sink, ec2, stores):
    """The link v0.8.17 built. Revoking the seeded allow-all and authorizing a
    single egress rule must reach `outbound` -- before this, the whole egress
    half of the store was compiled away and every config said `outbound: any`
    no matter what the group held."""
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    revoke = sink.call(lambda: ec2.revoke_security_group_egress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "-1", "FromPort": 0, "ToPort": 0, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ]))
    assert (await _answer(stores, revoke)).status_code == 200
    # Nothing at all: AWS's own "a group whose egress was revoked blocks every
    # outbound packet", and nebula agrees -- an empty list is a real deny, not
    # an absent ruleset (MEASURED against nebula 1.10.3, 2026-07-30).
    assert _stored_firewall(stores, group_id).outbound == []

    authorize = sink.call(lambda: ec2.authorize_security_group_egress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "IpRanges": [{"CidrIp": "10.42.0.0/16"}]},
    ]))
    assert (await _answer(stores, authorize)).status_code == 200
    firewall = _stored_firewall(stores, group_id)
    assert firewall.outbound == [FirewallRule(port="5432", proto="tcp", cidr="10.42.0.0/16")]
    assert firewall.inbound == []  # the egress rule did NOT leak into inbound


async def test_egress_to_another_group_compiles_to_an_identity_rule(sink, ec2, stores):
    """An egress rule whose DESTINATION is another security group compiles to a
    nebula `group:` rule, the same identity-matching form the ingress side
    uses -- which is the only form that can gate mesh traffic, since overlay
    addresses are not VPC addresses."""
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    revoke = sink.call(lambda: ec2.revoke_security_group_egress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "-1", "FromPort": 0, "ToPort": 0, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ]))
    assert (await _answer(stores, revoke)).status_code == 200
    authorize = sink.call(lambda: ec2.authorize_security_group_egress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
         "UserIdGroupPairs": [{"GroupId": group_id}]},
    ]))
    assert (await _answer(stores, authorize)).status_code == 200
    assert _stored_firewall(stores, group_id).outbound == [
        FirewallRule(port="5432", proto="tcp", group=group_id),
    ]


async def test_revoke_recompiles_down_to_the_remaining_rules(sink, ec2, stores):
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    await _authorize_golden_ingress(stores, sink, ec2, group_id)
    revoke = sink.call(lambda: ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]},
    ]))
    assert (await _answer(stores, revoke)).status_code == 200
    assert _stored_firewall(stores, group_id).inbound == [
        FirewallRule(port="443", proto="tcp", group=group_id),
    ]


async def test_compiled_firewall_is_what_a_v3_node_config_consumes(sink, ec2, stores, tmp_path):
    """REAL but dormant: round-trip the STORED compile through
    generate_config -- the YAML a nebula daemon would read at V3."""
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    await _authorize_golden_ingress(stores, sink, ec2, group_id)
    manager = NebulaManager(tmp_path / ENV / "nebula")
    config = yaml.safe_load(manager.generate_config("10.42.0.1", "127.0.0.1", _stored_firewall(stores, group_id)))
    assert config["firewall"]["inbound"] == [
        {"port": "443", "proto": "tcp", "cidr": "10.0.0.0/16"},
        {"port": "443", "proto": "tcp", "group": group_id},
    ]
    assert config["firewall"]["outbound"] == [{"port": "any", "proto": "any", "cidr": "0.0.0.0/0"}]


# --- mesh_state: the GET /mesh?env= read model --------------------------------


async def test_mesh_state_shows_per_vpc_networks_and_compiled_rules(sink, ec2, stores, tmp_path):
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    await _authorize_golden_ingress(stores, sink, ec2, group_id)

    state = mesh_state(tmp_path, ENV)
    assert [(v.vpc_id, v.network) for v in state.vpcs] == [(vpc_id, ENV)]
    assert {sg.group_name for sg in state.security_groups} == {"default", "web"}  # incl. the vpc's sidecar
    (web,) = [sg for sg in state.security_groups if sg.sg_id == group_id]
    assert web.vpc_id == vpc_id and web.group_name == "web"
    assert web.firewall == _stored_firewall(stores, group_id)


async def test_deleted_sg_leaves_mesh_state(sink, ec2, stores, tmp_path):
    vpc_id = await _create_vpc(stores, sink, ec2)
    group_id = await _create_sg(stores, sink, ec2, vpc_id)
    delete_req = sink.call(lambda: ec2.delete_security_group(GroupId=group_id))
    assert (await _answer(stores, delete_req)).status_code == 200
    assert group_id not in {sg.sg_id for sg in mesh_state(tmp_path, ENV).security_groups}
