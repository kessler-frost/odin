"""Self-hosted Nebula mesh fabric — the multi-Mac (M7) cross-host path.

Chosen over Tailscale: Nebula runs inside your private network, YOU own the
lighthouse, and a control plane / UI can be built on top of the mesh. One
Nebula network == one allfather environment, so a host in `prod` cannot present
a valid cert to `staging`'s lighthouse — cross-env isolation is free at the PKI
layer, mirroring the per-env isolation of the AWS backing containers.

This module is the FOUNDATION: the cert/lighthouse/config primitives (recovered
from the retired `network/` module, now sync + with an injectable subprocess
seam for deterministic tests), a `NebulaFabric` that is a verified drop-in for
`LocalhostFabric.resolve`, and a `mesh_state` read model for the UI. The
producer-side wiring (a host's overlay IP entering World facts) and cross-Mac
World replication are M7 — see the spec §3.7; `resolve()` itself is unchanged
because the overlay address rides in through the same World-facts channel.

R3 (single-host mesh activation) added `LighthouseManager`: a REAL `nebula`
lighthouse process now runs on the host per env, and
`compute/instances.py::InstanceVm` installs + starts a REAL `nebula` daemon
inside each VM with the compiled SG firewall — the single-host half of M7,
proven by an actual overlay ping + a real SG-rule-filtered connection (see
`tests/simulate/test_nebula_mesh_e2e.py`). Cross-Mac membership/placement is
still the M7 remainder (ROADMAP.md).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
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
    SgFirewall,
    VpcNetwork,
)
from odin.spec.models import World
from odin.util import atomic_write_text

log = logging.getLogger("odin.fabric.nebula")

NEBULA_PORT = 4242

# The ONE-TIME, hand-installed, ROOT-OWNED control script LighthouseManager
# invokes -- see its class docstring for the full setup command and why a
# NOPASSWD grant on the raw (user-writable) brew `nebula` path would be a
# root-escalation hole. Fixed paths, not derived: the whole point is that
# neither the script nor the nebula copy it execs can be swapped by anyone
# but root, so these constants must match `scripts/allfather-nebula-ctl`'s
# own `NEBULA_BIN`/`SELF` exactly.
NEBULA_CTL_PATH = "/usr/local/libexec/allfather-nebula-ctl"
NEBULA_BIN_PATH = "/usr/local/libexec/allfather-nebula"

# How long `ensure_started` waits for `allfather-nebula-ctl start` to either
# write the pidfile (success) or exit (failure, e.g. `sudo -n` rejected) --
# both happen in well under a second in practice; generous headroom for a
# loaded box, not a boot-time budget.
_LIGHTHOUSE_START_TIMEOUT = 3.0

# A documented allow-all default. Real per-kind/group ACLs (derived from canvas
# security-group / IAM edges via sg_rules_to_firewall) are an M7 hardening item;
# PKI already gives the per-env boundary, the firewall scopes traffic on-mesh.
DEFAULT_FIREWALL = FirewallRules(
    inbound=[FirewallRule(port="any", proto="any")],
    outbound=[FirewallRule(port="any", proto="any")],
)


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

    def cert_paths(self, hostname: str) -> CertPaths:
        """Pure path lookup for an ALREADY-signed host (no I/O, no mkdir) --
        `sign_cert`'s own output paths, so a caller (`LighthouseManager`) can
        check existence / build a config without re-signing or risking a
        side-effecting read."""
        hosts_dir = self._dir / "hosts"
        return CertPaths(crt=hosts_dir / f"{hostname}.crt", key=hosts_dir / f"{hostname}.key", ca_crt=self._ca_crt)

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
        pki: CertPaths | None = None,
    ) -> str:
        """`pki=None` (the default, unchanged): the VM-side fixed paths a
        node's own cloud-init writes its cert to (`/etc/nebula/...`). A REAL
        `CertPaths` (R3: `LighthouseManager` passes its own
        `NebulaManager.cert_paths("lighthouse")`) points at wherever the
        cert ACTUALLY lives instead -- the host lighthouse process reads its
        cert straight from `.odin/{env}/nebula/hosts/`, never `/etc/nebula`
        (that path is only ever real inside a VM)."""
        config: dict = {
            "pki": {
                "ca": str(pki.ca_crt) if pki else "/etc/nebula/ca.crt",
                "cert": str(pki.crt) if pki else "/etc/nebula/host.crt",
                "key": str(pki.key) if pki else "/etc/nebula/host.key",
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
        atomic_write_text(self._overlay_path(), overlay.model_dump_json(indent=2))

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


class LighthouseManager:
    """R3 (single-host mesh activation): supervises ONE real `nebula`
    lighthouse PROCESS per env on the HOST -- the piece `ensure_network`'s
    own docstring has always flagged as missing ("no lighthouse PROCESS ever
    starts"). Plain-subprocess supervision: a `Popen` + a pidfile under
    `.odin/{env}/nebula/lighthouse.pid`, started when the first VM joins an
    env's mesh and stopped when the last one leaves --
    `compute/instances.py::InstanceVm._activate_nebula` is the only
    production caller (co-located with the VM-side activation it exists to
    make truthful; `gateway/models/ec2compute.py::_finish_terminate` calls
    `ensure_stopped` on the "last VM leaves" side -- both are guarded so a
    fresh `tmp_path` in every unit test, which never has a pidfile, makes
    every call here a true no-op without a real process or `sudo`).

    macOS root requirement (verified empirically, not assumed): creating a
    utun device needs root -- an unprivileged `nebula -config ...` exits
    "operation not permitted" immediately. This therefore shells to `sudo -n
    <NEBULA_CTL_PATH> ...` (non-interactive: fails fast and loud rather than
    hanging on a password prompt nothing can answer from a background
    thread) -- NEVER `sudo -n nebula` directly against the brew path: brew
    installs to a USER-writable prefix (`/opt/homebrew` or `/usr/local`), so
    a NOPASSWD grant scoped to that path would let anyone who can write
    there run arbitrary code as root by swapping the binary -- a real
    root-escalation hole, not a theoretical one. `scripts/
    allfather-nebula-ctl` (shipped in this repo) is the fix: a small,
    reviewable, ROOT-OWNED script at a FIXED path only root can ever
    replace, which execs a SEPARATE root-owned copy of nebula (also only
    root-replaceable) -- the sudoers grant is scoped to exactly that one
    script's path, nothing else, never a raw binary or a generic command
    like `kill`.

    One-time host setup the operator runs themselves (mirrors this same
    machine's own pre-existing `/private/etc/sudoers.d/lima` entry for
    socket_vmnet -- same pattern: a fixed, root-owned, narrowly-scoped
    target, never a user-writable path):

        sudo install -o root -g wheel -m 755 scripts/allfather-nebula-ctl /usr/local/libexec/allfather-nebula-ctl
        sudo install -o root -g wheel -m 755 "$(which nebula)" /usr/local/libexec/allfather-nebula
        echo "$(whoami) ALL=(root) NOPASSWD: /usr/local/libexec/allfather-nebula-ctl" | sudo tee /etc/sudoers.d/allfather-nebula

    Without it, `ensure_started` logs a clear warning and returns False --
    mesh activation is best-effort throughout (matching
    `_activate_nebula`'s own "never fail the instance boot over this" rule).
    """

    def __init__(self, popen=None, runner=None) -> None:
        self._popen = popen or subprocess.Popen
        self._run = runner or _default_runner

    def _pidfile(self, root: Path, env: str) -> Path:
        return _nebula_dir(root, env) / "lighthouse.pid"

    def _config_path(self, root: Path, env: str) -> Path:
        return _nebula_dir(root, env) / "lighthouse-config.yml"

    def _log_path(self, root: Path, env: str) -> Path:
        return _nebula_dir(root, env) / "lighthouse.log"

    def is_running(self, root: Path, env: str) -> bool:
        """Read-only: no filesystem write (mirrors `mesh_state`'s own "a GET
        must not mkdir" rule). Signal 0 checks liveness without signaling --
        a root-owned lighthouse makes our own unprivileged process get
        `PermissionError` for a LIVE pid (still running, just not ours to
        signal) vs `ProcessLookupError` for a gone one; both are real
        outcomes of the same check, not an error to swallow blindly."""
        pidfile = self._pidfile(root, env)
        if not pidfile.exists():
            return False
        text = pidfile.read_text().strip()
        if not text.isdigit():
            return False
        try:
            os.kill(int(text), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def ensure_started(self, root: Path, env: str, underlay: str) -> bool:
        """Idempotent no-op if already running. Never raises -- returns
        False when the network isn't bootstrapped yet or `sudo`/`nebula`
        aren't set up; the caller logs and moves on rather than failing an
        otherwise-successful instance boot over mesh wiring."""
        if self.is_running(root, env):
            return True
        manager = NebulaManager(_nebula_dir(root, env), runner=self._run)
        cert = manager.cert_paths("lighthouse")
        if not cert.crt.exists():
            log.warning("no lighthouse cert for env %r yet (no VPC created?); lighthouse not started", env)
            return False
        overlay = manager.load_overlay()
        lighthouse_ip = overlay.lighthouse_ip if overlay else MeshNetwork(network=env).lighthouse_ip
        config_text = manager.generate_config(
            lighthouse_ip=lighthouse_ip, lighthouse_underlay=underlay,
            firewall=DEFAULT_FIREWALL, is_lighthouse=True, pki=cert,
        )
        config_path = self._config_path(root, env)
        atomic_write_text(config_path, config_text)
        pidfile = self._pidfile(root, env)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path(root, env)
        try:
            with log_path.open("ab") as handle:
                proc = self._popen(
                    ["sudo", "-n", NEBULA_CTL_PATH, "start", str(config_path), str(pidfile)],
                    stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
                )
        except OSError as exc:
            log.warning("failed to spawn nebula lighthouse for env %r: %s", env, exc)
            return False
        # allfather-nebula-ctl writes `pidfile` itself (via $$, right before
        # it execs into the root-owned nebula copy -- exec never changes
        # the pid) -- authoritative, independent of whatever we'd otherwise
        # assume about pid tracking across the sudo/exec chain. A rejected
        # `sudo -n` (the one-time setup missing) exits FAST with no pidfile
        # ever written, so a short poll distinguishes the two outcomes
        # cleanly instead of trusting `proc.pid` blindly.
        deadline = time.monotonic() + _LIGHTHOUSE_START_TIMEOUT
        while time.monotonic() < deadline:
            if pidfile.exists():
                log.info(
                    "started nebula lighthouse for env %r (pid %s, underlay %s)",
                    env, pidfile.read_text().strip(), underlay,
                )
                return True
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        log.warning(
            "nebula lighthouse did not start for env %r -- is the one-time sudoers setup done? "
            "see LighthouseManager's docstring (sudo install ... allfather-nebula-ctl)", env,
        )
        return False

    def ensure_stopped(self, root: Path, env: str) -> None:
        """Exact-pid only, read from THIS env's own pidfile -- never a
        pattern/blanket kill (the same discipline `InstanceVm`'s VM teardown
        uses for `limactl`). Delegates the actual kill to
        `allfather-nebula-ctl stop`, which re-verifies the pid is really our
        nebula copy before signaling it (a pidfile is user-writable; this
        process being unprivileged means it can't `kill` a root-owned
        process directly regardless)."""
        pidfile = self._pidfile(root, env)
        if not pidfile.exists():
            return
        self._run(["sudo", "-n", NEBULA_CTL_PATH, "stop", str(pidfile)])
        pidfile.unlink(missing_ok=True)
        log.info("stopped nebula lighthouse for env %r", env)


def _ec2net_networks(root: Path, env: str) -> tuple[list[VpcNetwork], list[SgFirewall]]:
    """The EC2-network model's per-VPC networks + compiled SG firewalls (task
    V1b), read straight off the gateway's sidecar file
    (`.odin/{env}/gateway/ec2net.json`). The JSON file is the boundary --
    fabric deliberately does NOT import gateway code (the import direction is
    gateway -> fabric: `gateway/models/ec2net.py` calls `ensure_network` /
    `sg_rules_to_firewall` above)."""
    path = Path(root) / env / "gateway" / "ec2net.json"
    data: dict = json.loads(path.read_text()) if path.exists() else {}
    vpcs = [
        VpcNetwork(vpc_id=v["vpc_id"], cidr_block=v["cidr_block"], network=v.get("nebula_network", env))
        for key, v in data.items() if key.startswith("vpc:")
    ]
    security_groups = [
        SgFirewall(
            sg_id=g["group_id"], vpc_id=g["vpc_id"], group_name=g["group_name"],
            firewall=FirewallRules.model_validate(g.get("firewall", {})),
        )
        for key, g in data.items() if key.startswith("sg:")
    ]
    return vpcs, security_groups


def mesh_state(root: Path, env: str, world: World | None = None) -> MeshState:
    """The UI read model: the env's overlay membership joined with the observed
    World (resources + their published endpoints) and the EC2-network model's
    per-VPC networks + compiled SG firewalls. All sides are optional — an env
    with no joined host (no overlay file), no World, and/or no VPCs still
    renders."""
    resources = [
        MeshResource(id=r.id, kind=r.kind, phase=r.phase, endpoint=r.facts.get("endpoint"))
        for r in (world.resources if world else ())
    ]
    vpcs, security_groups = _ec2net_networks(root, env)
    # Read-only (R3): LighthouseManager.is_running is a pidfile + signal-0
    # liveness check, no filesystem write -- keeps this function's own
    # "a GET must not mkdir" contract (test_mesh_state_read_has_no_
    # filesystem_side_effect).
    lighthouse_running = LighthouseManager().is_running(root, env)
    overlay = NebulaManager(_nebula_dir(root, env)).load_overlay()
    if overlay is None:
        return MeshState(
            network=env, resources=resources, vpcs=vpcs, security_groups=security_groups,
            lighthouse_running=lighthouse_running,
        )
    hosts_subnet = overlay.subnets.get("hosts")
    hosts = [
        HostMembership(hostname=name, overlay_ip=ip, groups=["host"])
        for name, ip in (hosts_subnet.assignments.items() if hosts_subnet else {}.items())
    ]
    return MeshState(
        network=overlay.network, base_cidr=overlay.base_cidr,
        lighthouse_ip=overlay.lighthouse_ip, lighthouse_underlay=overlay.lighthouse_underlay_ip,
        lighthouse_running=lighthouse_running,
        hosts=hosts, resources=resources, vpcs=vpcs, security_groups=security_groups,
    )


class NebulaFabric(LocalhostFabric):
    """resolve() is byte-identical to LocalhostFabric: the producer publishes its
    reachable address into World facts, and resolve() returns it verbatim. On a
    mesh that address is the producer host's overlay IP:port (e.g.
    10.42.1.7:6379) instead of 127.0.0.1:port — the difference is in what the
    PRODUCER published (its host overlay IP), not in resolve. Inheriting
    guarantees the drop-in parity the design review required."""
