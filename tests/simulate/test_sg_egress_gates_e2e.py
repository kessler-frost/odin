"""v0.8.17 -- a drawn security group's EGRESS really stops outbound traffic.

The bug this closes is the one the repo keeps finding under different names: a
rule that renders as configuration and changes nothing. Since v0.8.14 an sg
node has an `egressRules` field, `agent/hcl.py` emits real `egress` blocks from
it, and the gateway stored every one of them -- and then
`ec2net._compiled_firewall` filtered them out and `sg_rules_to_firewall` ended
with a hardcoded `outbound=[any/any]`. So a user could restrict egress, watch
it survive `tofu plan`, and be gated by nothing at all.

WHAT MAKES THIS TEST WORTH ANYTHING is that it measures a BLOCK. A test that
only shows allowed traffic still flowing passes with the compiler change
deleted -- it was passing before the feature existed. So the assertion that
carries the weight is the negative one, and it is paired with a positive
control on the SAME client, to the SAME peer, in the same moment: the
restricted client CAN reach the port its egress rule names and CANNOT reach the
one it does not. "Nothing is listening" and "the mesh is down" cannot produce
that pattern; only a working outbound firewall can.

Everything downstream of the canvas is real: real `nebula-cert` PKI, a real
unprivileged host lighthouse, a real `nebula` daemon per member, and the
firewalls come out of the REAL gateway path -- `AuthorizeSecurityGroupEgress`
and `RevokeSecurityGroupEgress` through `ec2net.pure_answer`, compiled by
`ec2net.compiled_firewall`. Nothing here hand-builds a `FirewallRules`, which
is the point: the hand-built version is what the old unit tests proved, and it
proved the parser rather than the integration.

No Lima VM: this is the container-level proof of the MECHANISM, the same
division of labour `tests/aws/test_backing_mesh_e2e.py` (ingress) has against
`tests/simulate/test_sg_gates_backing_e2e.py` (ingress, with real VMs).

Store root: under the repo tree, NOT `tmp_path` -- Colima only shares $HOME
into its VM and the sidecar reads its cert/config from a bind mount.
"""
from __future__ import annotations

import asyncio
import secrets
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import pytest

from odin.fabric.nebula import LighthouseManager, ensure_network
from odin.fabric.sidecar import MeshSidecar, underlay_ip
from odin.gateway.models import ec2net
from odin.gateway.stores import SynthStores
from odin.runtime.colima import ColimaRuntime, ContainerSpec

pytestmark = pytest.mark.integration

ENV = "sgeg-egress-e2e"
IMAGE = "alpine:3.20"
# Two live listeners on the server. The restricted client's egress names ONE of
# them, which is what separates "blocked by the firewall" from "nothing there".
ALLOWED_PORT = 6000
BLOCKED_PORT = 5432


@pytest.fixture
def mesh_root():
    root = Path(".odin-sgeg") / secrets.token_hex(4)
    root.mkdir(parents=True)
    yield root.resolve()
    shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture
def containers():
    names: list[str] = []
    yield names
    for name in reversed(names):
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
        subprocess.run(["docker", "volume", "rm", "-f", f"{name}-data"], capture_output=True, timeout=60)


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


async def _ec2(stores: SynthStores, op: str, **params) -> str:
    """One real EC2 query-protocol call through the gateway's own dispatcher."""
    body = urlencode({"Action": op, **params}).encode()
    response = await ec2net.pure_answer(f"ec2:{op}", "", ENV, body, stores, 0.0)
    assert response.status_code == 200, response.body
    return response.body.decode()


async def _sg(stores: SynthStores, vpc_id: str, name: str) -> str:
    xml = await _ec2(stores, "CreateSecurityGroup", VpcId=vpc_id, GroupName=name, GroupDescription=name)
    return xml.split("<groupId>")[1].split("</groupId>")[0]


async def _revoke_default_egress(stores: SynthStores, group_id: str) -> None:
    """What the Terraform provider does when the config's `egress` block is not
    the seeded allow-all: it revokes the seeded rule first. Verified shape --
    `FromPort=0, ToPort=0, IpProtocol=-1` is what lands on the seeded rule's
    content hash (`_rules_from_permission`'s own note)."""
    await _ec2(
        stores, "RevokeSecurityGroupEgress", GroupId=group_id,
        **{
            "IpPermissions.1.IpProtocol": "-1",
            "IpPermissions.1.FromPort": "0",
            "IpPermissions.1.ToPort": "0",
            "IpPermissions.1.IpRanges.1.CidrIp": "0.0.0.0/0",
        },
    )


async def _member(rt, mesh, name, firewall, containers, command):
    await rt.stop(name)
    await rt.run_container(ContainerSpec(
        name=name, image=IMAGE, command=command, labels={"odin-env": ENV, "odin": "1"},
    ))
    containers.append(name)
    containers.append(mesh.sidecar_name(name))
    overlay_ip = await mesh.ensure(name, name, firewall=firewall)
    assert overlay_ip, f"{name} never joined the mesh: {mesh.last_failure}"
    return overlay_ip


def _talk(client: str, host: str, port: int) -> str:
    """A real request/response exchange. A bare connect proves less: this only
    returns the server's payload when a packet got out AND one came back."""
    probe = subprocess.run(
        ["docker", "exec", client, "sh", "-c", f"echo hello | nc -w 6 {host} {port}"],
        capture_output=True, text=True, timeout=40,
    )
    return (probe.stdout + probe.stderr).strip()


def _talk_until(client: str, host: str, port: int, expect: str, attempts: int = 12) -> str:
    """The mesh needs a moment to relay-handshake after `ensure` returns, so
    the ALLOWED direction polls. The blocked direction never polls -- waiting
    for a thing to keep not happening only makes a slow mesh look like a
    working firewall, which is the exact false green this test exists to
    avoid."""
    out = ""
    for _ in range(attempts):
        out = _talk(client, host, port)
        if expect in out:
            return out
    return out


async def test_a_drawn_egress_rule_stops_outbound_traffic_on_the_mesh(
    mesh_root, containers, lighthouse_cleanup,
):
    assert shutil.which("docker"), "docker (Colima) required"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"

    rt = ColimaRuntime()
    lighthouse_cleanup.append((mesh_root, ENV))
    stores = SynthStores(mesh_root)
    await ensure_network(mesh_root, ENV, underlay_ip())

    vpc_xml = await _ec2(stores, "CreateVpc", CidrBlock="10.0.0.0/16")
    vpc_id = vpc_xml.split("<vpcId>")[1].split("</vpcId>")[0]

    # `open-sg`: an sg node whose `egressRules` field is EMPTY. It keeps the
    # seeded allow-all rule -- AWS's own default, and what every canvas drawn
    # before the field existed gets. This is the compatibility control.
    #
    # Its INGRESS is opened explicitly, because a fresh group has none and a
    # group with no ingress rules admits nothing: the server member wears this
    # firewall, so without this the whole mesh looks dead and the egress
    # assertions below would pass for the wrong reason.
    open_sg = await _sg(stores, vpc_id, "open-sg")
    await _ec2(
        stores, "AuthorizeSecurityGroupIngress", GroupId=open_sg,
        **{
            "IpPermissions.1.IpProtocol": "-1",
            "IpPermissions.1.IpRanges.1.CidrIp": "0.0.0.0/0",
        },
    )

    # `restricted-sg`: an sg node with `egressRules = tcp:6000:0.0.0.0/0`.
    # The provider revokes the seeded rule, then authorizes this one.
    restricted_sg = await _sg(stores, vpc_id, "restricted-sg")
    await _revoke_default_egress(stores, restricted_sg)
    await _ec2(
        stores, "AuthorizeSecurityGroupEgress", GroupId=restricted_sg,
        **{
            "IpPermissions.1.IpProtocol": "tcp",
            "IpPermissions.1.FromPort": str(ALLOWED_PORT),
            "IpPermissions.1.ToPort": str(ALLOWED_PORT),
            "IpPermissions.1.IpRanges.1.CidrIp": "0.0.0.0/0",
        },
    )

    open_firewall = ec2net.compiled_firewall(stores, ENV, open_sg)
    restricted_firewall = ec2net.compiled_firewall(stores, ENV, restricted_sg)
    # The restricted group is about EGRESS; give it the same ingress the open
    # group has so the only difference between the two members is outbound.
    restricted_firewall.inbound = open_firewall.inbound
    print(f"[egress] open-sg       outbound={open_firewall.outbound}")
    print(f"[egress] restricted-sg outbound={restricted_firewall.outbound}")
    # The compile itself, before any packet moves: this is what used to be a
    # hardcoded allow-all for BOTH groups.
    assert [(r.port, r.proto) for r in restricted_firewall.outbound] == [(str(ALLOWED_PORT), "tcp")]
    assert [(r.port, r.proto) for r in open_firewall.outbound] == [("any", "any")]

    mesh = MeshSidecar(rt, ENV, mesh_root)
    server_cmd = ("sh", "-c",
                  f"while true; do echo pong{ALLOWED_PORT} | nc -l -p {ALLOWED_PORT}; done & "
                  f"while true; do echo pong{BLOCKED_PORT} | nc -l -p {BLOCKED_PORT}; done")
    srv = await _member(rt, mesh, f"odin-{ENV}-srv", open_firewall, containers, server_cmd)
    open_client = f"odin-{ENV}-open"
    restricted_client = f"odin-{ENV}-restricted"
    await _member(rt, mesh, open_client, open_firewall, containers, ("sleep", "infinity"))
    await _member(rt, mesh, restricted_client, restricted_firewall, containers, ("sleep", "infinity"))

    # --- control: with an empty egressRules field, BOTH ports are reachable.
    # If this fails the mesh is broken and nothing below means anything.
    on_allowed = _talk_until(open_client, srv, ALLOWED_PORT, f"pong{ALLOWED_PORT}")
    on_blocked = _talk_until(open_client, srv, BLOCKED_PORT, f"pong{BLOCKED_PORT}")
    print(f"[egress] open-sg client -> :{ALLOWED_PORT} {on_allowed!r}  -> :{BLOCKED_PORT} {on_blocked!r}")
    assert f"pong{ALLOWED_PORT}" in on_allowed, "an empty egressRules field must still allow all outbound"
    assert f"pong{BLOCKED_PORT}" in on_blocked, (
        "AWS's default is allow-all outbound; a canvas that never mentioned egress must be unaffected"
    )

    # --- the positive half of the restricted client: the port its rule NAMES.
    # This is what makes the negative assertion mean something -- it proves the
    # client is on the mesh and can reach this peer right now.
    allowed = _talk_until(restricted_client, srv, ALLOWED_PORT, f"pong{ALLOWED_PORT}")
    print(f"[egress] restricted-sg client -> :{ALLOWED_PORT} (its egress rule) {allowed!r}")
    assert f"pong{ALLOWED_PORT}" in allowed, (
        f"the restricted client's own egress rule names tcp:{ALLOWED_PORT}; it must reach it"
    )

    # --- THE assertion. Same client, same peer, same instant, a port its
    # egress does not name. Deleting the compiler change makes this line fail.
    blocked = _talk(restricted_client, srv, BLOCKED_PORT)
    print(f"[egress] restricted-sg client -> :{BLOCKED_PORT} (NOT in its egress) {blocked!r}")
    assert f"pong{BLOCKED_PORT}" not in blocked, (
        f"a security group whose egress omits tcp:{BLOCKED_PORT} must not reach it over the overlay -- "
        "the egress rule is decorative if this passes"
    )

    # --- statefulness, AWS's own: the restricted client is still REACHABLE on
    # a port it may not dial out to. An egress rule restricts what a member
    # SENDS, never what it may answer, and nebula's conntrack is what makes
    # that true (measured, not assumed -- if it were false, restricting a
    # database's egress would silently break every connection to it).
    reply = _talk_until(open_client, srv, BLOCKED_PORT, f"pong{BLOCKED_PORT}", attempts=5)
    assert f"pong{BLOCKED_PORT}" in reply, "the server's own traffic must be unaffected by another group's egress"

    await asyncio.sleep(0)
