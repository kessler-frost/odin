"""Data models for the self-hosted Nebula mesh fabric.

Recovered from the retired `network/` module (the primitives were sound) and
re-homed under `fabric/`, rekeyed from the old per-VPC model to odin's
per-environment model: one Nebula network == one environment. IP allocation is
sticky-by-host (re-applies must not churn a host's overlay IP, or already-
published consumer env vars like DATABASE_URL go stale).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class CaInfo(BaseModel):
    network: str
    ca_crt: Path
    ca_key: Path


class CertPaths(BaseModel):
    crt: Path
    key: Path
    ca_crt: Path


class FirewallRule(BaseModel):
    port: str
    proto: str
    cidr: str | None = None
    group: str | None = None


class FirewallRules(BaseModel):
    inbound: list[FirewallRule] = []
    outbound: list[FirewallRule] = []


class SubnetAllocation(BaseModel):
    network: str
    subnet: str
    cidr: str
    next_ip: int = 1
    assignments: dict[str, str] = {}

    def allocate(self, host_id: str) -> str:
        """Sticky: a host_id always maps to the same overlay IP (idempotent
        across re-applies, so published endpoints stay valid)."""
        if host_id in self.assignments:
            return self.assignments[host_id]
        base = self.cidr.rsplit(".", 1)[0]
        ip = f"{base}.{self.next_ip}"
        self.assignments[host_id] = ip
        self.next_ip += 1
        return ip


class MeshNetwork(BaseModel):
    """One Nebula network per environment (env = odin's isolation unit).

    `mask` MUST match `base_cidr`'s own prefix (both `/16`): every signed
    cert (the lighthouse's `lighthouse_ip/mask` in `ensure_network`, and
    every host/instance's `cert_ip`) embeds its `networks` list from this
    exact mask, and nebula treats any destination OUTSIDE that embedded
    CIDR as "not within our networks" -- unroutable, not merely unreachable
    (R3 finding, confirmed on a real daemon: a `/24` here put the lighthouse
    at `10.42.0.1/24` and every host in a DIFFERENT `10.42.<n>.0/24` --
    disjoint networks that could handshake but never route data to each
    other). A single shared `/16` puts every node in the SAME routable
    network regardless of which `subnets` bucket allocated its IP.
    """
    network: str
    base_cidr: str = "10.42.0.0/16"
    mask: str = "16"
    lighthouse_ip: str = "10.42.0.1"
    lighthouse_underlay_ip: str | None = None
    # THIS env's own lighthouse UDP port (field test 2 B8): it used to be one
    # machine-global constant, so only one env's lighthouse could ever bind and
    # a second env's died with `exit 1` -- silently, while odin kept publishing
    # that env's SG-gated mesh addresses. Allocated once per env by
    # `fabric/nebula.py::ensure_network` and sticky from then on (every member's
    # `static_host_map` embeds it), `None` only for an overlay.json written
    # before this existed -- which reads as the historical 4342.
    lighthouse_port: int | None = None
    next_subnet: int = 1
    subnets: dict[str, SubnetAllocation] = {}

    def allocate_subnet(self, subnet: str) -> SubnetAllocation:
        if subnet in self.subnets:
            return self.subnets[subnet]
        allocation = SubnetAllocation(
            network=self.network, subnet=subnet, cidr=f"10.42.{self.next_subnet}.0/24",
        )
        self.subnets[subnet] = allocation
        self.next_subnet += 1
        return allocation

    def allocate_host(self, host_id: str) -> str:
        """Sticky overlay IP for a host in the default 'hosts' subnet."""
        return self.allocate_subnet("hosts").allocate(host_id)

    def cert_ip(self, host_id: str) -> str:
        """Overlay IP in CIDR form for `nebula-cert sign -ip` (needs the mask)."""
        return f"{self.allocate_host(host_id)}/{self.mask}"


class HostMembership(BaseModel):
    hostname: str
    overlay_ip: str
    groups: list[str] = []
    online: bool = False


class MeshResource(BaseModel):
    id: str
    kind: str
    phase: str
    endpoint: str | None = None


class VpcNetwork(BaseModel):
    """A canvas VPC's membership in the env's Nebula network (task V1b --
    per-env mesh, so `network` == env; 1:1 while V1 canvases carry one VPC
    per env)."""
    vpc_id: str
    cidr_block: str
    network: str


class SgFirewall(BaseModel):
    """A security group's compiled Nebula firewall -- exactly what a node
    config's `firewall:` section consumes at V3 (golden-tested through
    `NebulaManager.generate_config`). REAL but dormant in V1: no VM is on
    the mesh yet."""
    sg_id: str
    vpc_id: str
    group_name: str
    firewall: FirewallRules = FirewallRules()


class MeshState(BaseModel):
    """The read model a mesh UI / control plane builds on (the reason Nebula
    was chosen over Tailscale: a self-owned, introspectable mesh).

    `vpcs` / `security_groups` (task V1b): the canvas's VPCs and security
    groups projected onto the mesh -- see `fabric/nebula.py::_ec2net_networks`.
    `lighthouse_running` (R3): whether the env's host lighthouse PROCESS is
    up right now (`fabric/lighthouse.py::LighthouseManager.is_running`) --
    distinct from `lighthouse_underlay` merely being recorded.
    `lighthouse_port`: which UDP port THIS env's lighthouse owns -- reported
    because it is now per-env rather than one machine-global constant, and
    "which env has which port" is exactly what was invisible when two envs
    fought over 4342 (field test 2 B8).
    """
    network: str
    base_cidr: str = "10.42.0.0/16"
    lighthouse_ip: str = "10.42.0.1"
    lighthouse_underlay: str | None = None
    lighthouse_running: bool = False
    lighthouse_port: int | None = None
    hosts: list[HostMembership] = []
    resources: list[MeshResource] = []
    vpcs: list[VpcNetwork] = []
    security_groups: list[SgFirewall] = []
