"""Self-hosted Nebula mesh fabric — the multi-Mac (M7) cross-host path.

Chosen over Tailscale: Nebula runs inside your private network, YOU own the
lighthouse, and a control plane / UI can be built on top of the mesh. One
Nebula network == one allfather environment, so a host in `prod` cannot present
a valid cert to `staging`'s lighthouse — cross-env isolation is free at the PKI
layer, mirroring the per-env MiniStack account boundary.

This module is the FOUNDATION: the cert/lighthouse/config primitives (recovered
from the retired `network/` module, now sync + with an injectable subprocess
seam for deterministic tests), a `NebulaFabric` that is a verified drop-in for
`LocalhostFabric.resolve`, and a `mesh_state` read model for the UI. The
producer-side wiring (a host's overlay IP entering World facts) and cross-Mac
World replication are M7 — see the spec §3.7; `resolve()` itself is unchanged
because the overlay address rides in through the same World-facts channel.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from odin.fabric.localhost import LocalhostFabric
from odin.fabric.models import (
    CaInfo,
    CertPaths,
    FirewallRule,
    FirewallRules,
    HostMembership,
    MeshNetwork,
    MeshResource,
    MeshState,
)
from odin.spec.models import World

NEBULA_PORT = 4242

# A documented allow-all default. Real per-kind/group ACLs (derived from canvas
# security-group / IAM edges via sg_rules_to_firewall) are an M7 hardening item;
# PKI already gives the per-env boundary, the firewall scopes traffic on-mesh.
DEFAULT_FIREWALL = FirewallRules(
    inbound=[FirewallRule(port="any", proto="any")],
    outbound=[FirewallRule(port="any", proto="any")],
)
LIGHTHOUSE_FIREWALL = DEFAULT_FIREWALL


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


def _default_runner(args: list[str]) -> _Proc:
    proc = subprocess.run(args, capture_output=True, text=True)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


class NebulaManager:
    """nebula-cert primitives + config generation for one env's network.

    `data_dir` is that env's nebula directory (e.g. `.odin/<env>/nebula`), so CA
    + overlay state live inside the env's append-only lineage — not a shared
    `~/.odin` (the path bug the design review caught).
    """

    def __init__(self, data_dir: Path, runner=None) -> None:
        self._dir = Path(data_dir)  # created on first WRITE, not construction, so
        self._run = runner or _default_runner  # a read (mesh_state) has no side effect

    @property
    def _ca_crt(self) -> Path:
        return self._dir / "ca.crt"

    @property
    def _ca_key(self) -> Path:
        return self._dir / "ca.key"

    def _hosts_dir(self) -> Path:
        d = self._dir / "hosts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create_ca(self, network: str) -> CaInfo:
        self._dir.mkdir(parents=True, exist_ok=True)
        proc = self._run([
            "nebula-cert", "ca", "-name", network,
            "-out-crt", str(self._ca_crt), "-out-key", str(self._ca_key),
        ])
        if proc.returncode != 0:
            raise RuntimeError(f"nebula-cert ca failed: {proc.stderr.strip()}")
        return CaInfo(network=network, ca_crt=self._ca_crt, ca_key=self._ca_key)

    def sign_cert(self, hostname: str, ip: str, groups: list[str] | None = None) -> CertPaths:
        """`ip` must be CIDR form (e.g. 10.42.1.7/24) — Nebula requires the mask."""
        host_crt = self._hosts_dir() / f"{hostname}.crt"
        host_key = self._hosts_dir() / f"{hostname}.key"
        cmd = [
            "nebula-cert", "sign",
            "-ca-crt", str(self._ca_crt), "-ca-key", str(self._ca_key),
            "-name", hostname, "-ip", ip,
            "-out-crt", str(host_crt), "-out-key", str(host_key),
        ]
        if groups:
            cmd += ["-groups", ",".join(groups)]
        proc = self._run(cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"nebula-cert sign failed: {proc.stderr.strip()}")
        return CertPaths(crt=host_crt, key=host_key, ca_crt=self._ca_crt)

    def revoke_cert(self, hostname: str) -> None:
        # NOTE: deletes the local cert only. A real nebula-cert CRL (so a drained
        # host stops being trusted before its cert expires) is an M7 item.
        (self._hosts_dir() / f"{hostname}.crt").unlink(missing_ok=True)
        (self._hosts_dir() / f"{hostname}.key").unlink(missing_ok=True)

    def generate_config(
        self,
        lighthouse_ip: str,
        lighthouse_underlay: str,
        firewall: FirewallRules,
        is_lighthouse: bool = False,
    ) -> str:
        config: dict = {
            "pki": {
                "ca": "/etc/nebula/ca.crt",
                "cert": "/etc/nebula/host.crt",
                "key": "/etc/nebula/host.key",
            },
            "lighthouse": {"am_lighthouse": is_lighthouse},
            "listen": {"host": "0.0.0.0", "port": NEBULA_PORT},
            "firewall": {
                "inbound": [_rule_to_dict(r) for r in firewall.inbound],
                "outbound": [_rule_to_dict(r) for r in firewall.outbound],
            },
        }
        if not is_lighthouse:
            config["static_host_map"] = {lighthouse_ip: [f"{lighthouse_underlay}:{NEBULA_PORT}"]}
            config["lighthouse"]["hosts"] = [lighthouse_ip]
        return yaml.dump(config, default_flow_style=False, sort_keys=False)

    def _overlay_path(self) -> Path:
        return self._dir / "overlay.json"

    def save_overlay(self, overlay: MeshNetwork) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._overlay_path().write_text(overlay.model_dump_json(indent=2))

    def load_overlay(self) -> MeshNetwork | None:
        path = self._overlay_path()
        if not path.exists():
            return None
        return MeshNetwork.model_validate_json(path.read_text())


def _rule_to_dict(rule: FirewallRule) -> dict:
    d: dict = {"port": rule.port, "proto": rule.proto}
    if rule.cidr:
        d["cidr"] = rule.cidr
    if rule.group:
        d["group"] = rule.group
    if not rule.cidr and not rule.group:
        d["host"] = "any"
    return d


def sg_rules_to_firewall(permissions: list[dict]) -> FirewallRules:
    """Translate AWS security-group IpPermissions (canvas SG edges) to Nebula
    firewall rules — recovered, for deriving per-env ACLs from the canvas."""
    inbound: list[FirewallRule] = []
    for perm in permissions:
        proto = perm.get("IpProtocol", "-1")
        from_port, to_port = perm.get("FromPort"), perm.get("ToPort")
        nebula_proto = "any" if proto == "-1" else proto
        nebula_port = "any"
        if proto != "-1" and from_port is not None:
            nebula_port = str(from_port) if from_port == to_port else f"{from_port}-{to_port}"
        for ip_range in perm.get("IpRanges", []):
            inbound.append(FirewallRule(port=nebula_port, proto=nebula_proto, cidr=ip_range.get("CidrIp")))
        for group_ref in perm.get("UserIdGroupPairs", []):
            inbound.append(FirewallRule(port=nebula_port, proto=nebula_proto, group=group_ref.get("GroupId", "")))
        if not perm.get("IpRanges") and not perm.get("UserIdGroupPairs"):
            inbound.append(FirewallRule(port=nebula_port, proto=nebula_proto))
    return FirewallRules(inbound=inbound, outbound=[FirewallRule(port="any", proto="any")])


def _nebula_dir(root: Path, env: str) -> Path:
    return Path(root) / env / "nebula"


def ensure_network(root: Path, env: str, lighthouse_underlay: str, runner=None) -> MeshNetwork:
    """Lazily bootstrap an env's Nebula network: CA + lighthouse cert + overlay,
    persisted under `.odin/<env>/nebula/`. Idempotent (sticky overlay)."""
    manager = NebulaManager(_nebula_dir(root, env), runner=runner)
    overlay = manager.load_overlay() or MeshNetwork(network=env)
    overlay.lighthouse_underlay_ip = lighthouse_underlay
    if not manager._ca_crt.exists():
        manager.create_ca(env)
        manager.sign_cert("lighthouse", f"{overlay.lighthouse_ip}/{overlay.mask}", groups=["lighthouse"])
    manager.save_overlay(overlay)
    return overlay


def mesh_state(root: Path, env: str, world: World | None = None) -> MeshState:
    """The UI read model: the env's overlay membership joined with the observed
    World (resources + their published endpoints). Both sides are optional — an
    env with no joined host (no overlay file) and/or no World still renders."""
    resources = [
        MeshResource(id=r.id, kind=r.kind, phase=r.phase, endpoint=r.facts.get("endpoint"))
        for r in (world.resources if world else ())
    ]
    overlay = NebulaManager(_nebula_dir(root, env)).load_overlay()
    if overlay is None:
        return MeshState(network=env, resources=resources)
    hosts_subnet = overlay.subnets.get("hosts")
    hosts = [
        HostMembership(hostname=name, overlay_ip=ip, groups=["host"])
        for name, ip in (hosts_subnet.assignments.items() if hosts_subnet else {}.items())
    ]
    return MeshState(
        network=overlay.network, base_cidr=overlay.base_cidr,
        lighthouse_ip=overlay.lighthouse_ip, lighthouse_underlay=overlay.lighthouse_underlay_ip,
        hosts=hosts, resources=resources,
    )


class NebulaFabric(LocalhostFabric):
    """resolve() is byte-identical to LocalhostFabric: the producer publishes its
    reachable address into World facts, and resolve() returns it verbatim. On a
    mesh that address is the producer host's overlay IP:port (e.g.
    10.42.1.7:6379) instead of 127.0.0.1:port — the difference is in what the
    PRODUCER published (its host overlay IP), not in resolve. Inheriting
    guarantees the drop-in parity the design review required."""
