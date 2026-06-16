from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class CaInfo(BaseModel):
    vpc_name: str
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
    vpc_name: str
    subnet_name: str
    cidr: str
    next_ip: int = 1
    assignments: dict[str, str] = {}

    def allocate(self, resource_name: str) -> str:
        base = self.cidr.rsplit(".", 1)[0]
        ip = f"{base}.{self.next_ip}"
        self.assignments[resource_name] = ip
        self.next_ip += 1
        return ip

    def release(self, resource_name: str) -> str | None:
        return self.assignments.pop(resource_name, None)


class VpcOverlay(BaseModel):
    vpc_name: str
    base_cidr: str = "10.42.0.0/16"
    lighthouse_ip: str = "10.42.0.1"
    lighthouse_underlay_ip: str | None = None
    next_subnet: int = 1
    subnets: dict[str, SubnetAllocation] = {}

    def allocate_subnet(self, subnet_name: str) -> SubnetAllocation:
        cidr = f"10.42.{self.next_subnet}.0/24"
        allocation = SubnetAllocation(
            vpc_name=self.vpc_name,
            subnet_name=subnet_name,
            cidr=cidr,
        )
        self.subnets[subnet_name] = allocation
        self.next_subnet += 1
        return allocation

    def get_subnet(self, subnet_name: str) -> SubnetAllocation | None:
        return self.subnets.get(subnet_name)
