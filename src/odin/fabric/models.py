"""Data models for the self-hosted Nebula mesh fabric.

Recovered from the retired `network/` module (the primitives were sound) and
re-homed under `fabric/`, rekeyed from the old per-VPC model to allfather's
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
    """One Nebula network per environment (env = allfather's isolation unit)."""
    network: str
    base_cidr: str = "10.42.0.0/16"
    mask: str = "24"
    lighthouse_ip: str = "10.42.0.1"
    lighthouse_underlay_ip: str | None = None
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


class MeshState(BaseModel):
    """The read model a mesh UI / control plane builds on (the reason Nebula
    was chosen over Tailscale: a self-owned, introspectable mesh)."""
    network: str
    base_cidr: str = "10.42.0.0/16"
    lighthouse_ip: str = "10.42.0.1"
    lighthouse_underlay: str | None = None
    hosts: list[HostMembership] = []
    resources: list[MeshResource] = []
