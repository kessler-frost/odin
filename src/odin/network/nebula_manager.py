from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from odin.network.models import CaInfo, CertPaths, FirewallRule, FirewallRules, VpcOverlay

NEBULA_VERSION = "1.9.5"


class NebulaManager:
    """Async wrapper around nebula-cert CLI for Nebula certificate operations."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path.home() / ".odin"
        (self._data_dir / "nebula").mkdir(parents=True, exist_ok=True)

    def _vpc_dir(self, vpc_name: str) -> Path:
        d = self._data_dir / "nebula" / vpc_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _hosts_dir(self, vpc_name: str) -> Path:
        d = self._vpc_dir(vpc_name) / "hosts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _run(self, *args: str) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode(), stderr.decode(), proc.returncode

    async def create_ca(self, vpc_name: str) -> CaInfo:
        vpc_dir = self._vpc_dir(vpc_name)
        ca_crt = vpc_dir / "ca.crt"
        ca_key = vpc_dir / "ca.key"

        _, stderr, returncode = await self._run(
            "nebula-cert", "ca",
            "-name", vpc_name,
            "-out-crt", str(ca_crt),
            "-out-key", str(ca_key),
        )
        if returncode != 0:
            raise RuntimeError(f"nebula-cert ca failed: {stderr}")

        return CaInfo(vpc_name=vpc_name, ca_crt=ca_crt, ca_key=ca_key)

    async def sign_cert(
        self,
        vpc_name: str,
        hostname: str,
        ip: str,
        groups: list[str] | None = None,
    ) -> CertPaths:
        vpc_dir = self._vpc_dir(vpc_name)
        hosts_dir = self._hosts_dir(vpc_name)

        ca_crt = vpc_dir / "ca.crt"
        ca_key = vpc_dir / "ca.key"
        host_crt = hosts_dir / f"{hostname}.crt"
        host_key = hosts_dir / f"{hostname}.key"

        cmd = [
            "nebula-cert", "sign",
            "-ca-crt", str(ca_crt),
            "-ca-key", str(ca_key),
            "-name", hostname,
            "-ip", ip,
            "-out-crt", str(host_crt),
            "-out-key", str(host_key),
        ]
        if groups:
            cmd.extend(["-groups", ",".join(groups)])

        _, stderr, returncode = await self._run(*cmd)
        if returncode != 0:
            raise RuntimeError(f"nebula-cert sign failed: {stderr}")

        return CertPaths(crt=host_crt, key=host_key, ca_crt=ca_crt)

    async def revoke_cert(self, vpc_name: str, hostname: str) -> None:
        hosts_dir = self._hosts_dir(vpc_name)
        (hosts_dir / f"{hostname}.crt").unlink(missing_ok=True)
        (hosts_dir / f"{hostname}.key").unlink(missing_ok=True)

    def generate_config(
        self,
        lighthouse_ip: str,
        lighthouse_underlay: str,
        cert_paths: CertPaths,
        firewall_rules: FirewallRules,
        is_lighthouse: bool = False,
    ) -> str:
        config: dict = {
            "pki": {
                "ca": "/etc/nebula/ca.crt",
                "cert": "/etc/nebula/host.crt",
                "key": "/etc/nebula/host.key",
            },
            "lighthouse": {
                "am_lighthouse": is_lighthouse,
            },
            "listen": {
                "host": "0.0.0.0",
                "port": 4242,
            },
            "firewall": {
                "inbound": [_rule_to_dict(r) for r in firewall_rules.inbound],
                "outbound": [_rule_to_dict(r) for r in firewall_rules.outbound],
            },
        }

        if not is_lighthouse:
            config["static_host_map"] = {
                lighthouse_ip: [f"{lighthouse_underlay}:4242"],
            }
            config["lighthouse"]["hosts"] = [lighthouse_ip]

        return yaml.dump(config, default_flow_style=False, sort_keys=False)

    def save_overlay(self, overlay: VpcOverlay) -> None:
        overlay_path = self._vpc_dir(overlay.vpc_name) / "overlay.json"
        overlay_path.write_text(overlay.model_dump_json(indent=2))

    def load_overlay(self, vpc_name: str) -> VpcOverlay | None:
        overlay_path = self._vpc_dir(vpc_name) / "overlay.json"
        if not overlay_path.exists():
            return None
        return VpcOverlay.model_validate_json(overlay_path.read_text())


def _rule_to_dict(rule: FirewallRule) -> dict:
    d: dict = {"port": rule.port, "proto": rule.proto}
    if rule.cidr:
        d["cidr"] = rule.cidr
    if rule.group:
        d["group"] = rule.group
    if not rule.cidr and not rule.group:
        d["host"] = "any"
    return d
