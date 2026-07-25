"""Self-hosted Nebula mesh fabric — the multi-Mac (M7) cross-host path.

Chosen over Tailscale: Nebula runs inside your private network, YOU own the
lighthouse, and a control plane / UI can be built on top of the mesh. One
Nebula network == one odin environment, so a host in `prod` cannot present
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

R4 (rootless lighthouse) replaced R3's `sudo`/root-owned-ctl-script design
with an UNPRIVILEGED host lighthouse: `nebula` supports `tun: disabled: true`
(empirically verified — an unprivileged process with it starts, binds its UDP
port, and creates zero utun devices; the same config without it dies
immediately with "operation not permitted"), and a lighthouse's whole job is
coordination — telling mesh members where each other are — never carrying
data itself, so it never needs a tun device. `LighthouseManager` now just
`Popen`s the brew `nebula` binary directly as the invoking user; no root, no
sudoers grant, no ctl script. The mesh's actual data-plane members are the
VMs (`compute/instances.py::InstanceVm._activate_nebula`), where `nebula`
still runs as root INSIDE the VM via systemd — that costs the user nothing,
since it's a VM they already own outright.

R5 (relay) found — live, with two real VMs — that R4's design was necessary
but not sufficient: stock Lima `vz` NATs each VM into its OWN isolated
address space with NO VM-to-VM underlay path at all (confirmed: a raw ping
between two VMs' vzNAT addresses is 100% loss, independent of nebula
entirely), so no amount of nebula config tuning could make a DIRECT
VM-to-VM handshake succeed. Two real config bugs were also found and fixed
along the way (a VM advertising its Lima slirp address, identical on every
VM, hairpinned into "refusing to handshake with myself"; its IPv6 ULA burned
~6s per handshake failing before the real candidate was tried) — both
necessary, neither sufficient on their own. The actual fix: every VM CAN
reach the host (they already handshake with the lighthouse), so
`generate_config(relay_enabled=True)` routes VM-to-VM traffic THROUGH the
lighthouse — `relay: {am_relay: true}` on the lighthouse, `relay: {use_relays:
true, relays: [lighthouse_ip]}` on every VM. Still fully rootless: a relay
forwards opaque encrypted UDP between two peers it already has live sessions
with, never decrypting either side, so (empirically verified) it needs no
tun device to do it — the lighthouse's existing `tun: disabled: true` is
unaffected.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterable
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
from odin.util import atomic_write_text, private_mkdir, run_command

log = logging.getLogger("odin.fabric.nebula")

NEBULA_PORT = 4242

# The HOST lighthouse listens on its OWN port, not on the members' 4242 --
# W2.6, found live and the hard way. Lima automatically forwards every port a
# guest listens on to the host's 127.0.0.1, and that includes each EC2 VM's
# `nebula` daemon on UDP 4242: `limactl hostagent` then HOLDS host
# 127.0.0.1:4242 (confirmed with lsof, alongside the lighthouse's own
# `[::]:4242`). Colima's user-mode network maps `host.docker.internal`
# (192.168.5.2) onto the host's loopback, so a backing container's handshake
# packets to the lighthouse were being delivered INTO a VM instead --
# container↔VM mesh traffic could never work while any EC2 VM existed, while
# VM↔VM (which rides the vzNAT address, never loopback) was fine. Lima's own
# `portForwards: ignore` does NOT suppress it for UDP (tried: the rule lands
# in the instance's effective config and `limactl` binds the port anyway), and
# a user's own unrelated Lima VM could collide the same way. Giving the
# lighthouse a distinct port sidesteps the whole class: nothing in any guest
# ever listens on 4342, so nothing can forward it out from under us.
#
# ...but ONE port for the whole machine was its own bug (field test 2 B8): a
# lighthouse is PER ENV, so the second env to start one found 4342 held by the
# first, `nebula` exited 1, and the failure was a single log line while /world
# went on publishing that env's SG-gated mesh addresses. So 4342 is now only
# the FIRST CANDIDATE of a reserved range, and each env records the port it
# actually got in its own `overlay.json` (`MeshNetwork.lighthouse_port`, which
# every member's `static_host_map` then embeds).
LIGHTHOUSE_PORT = 4342
# 100 ports, all of them in the same "nothing inside a guest listens here"
# space as 4342 itself -- deliberately NOT the ephemeral range, where a Lima
# guest's own outbound sockets (and therefore Lima's automatic port
# forwarding) genuinely do land.
LIGHTHOUSE_PORTS = range(LIGHTHOUSE_PORT, LIGHTHOUSE_PORT + 100)
# An explicit pin, for reproducing a collision and for a user who needs a
# specific port open. Honoured verbatim: no probing, no reallocation.
_PORT_PIN_ENV = "ODIN_LIGHTHOUSE_PORT"

# How long `ensure_started` waits, after spawning, to catch an IMMEDIATE
# crash (bad cert/config, port already in use) before declaring success -- a
# real `nebula` binds its UDP port and logs "Nebula interface is active"
# within milliseconds (verified empirically), so this is generous headroom
# for a loaded box, not a boot-time budget.
_LIGHTHOUSE_START_TIMEOUT = 1.0

# The lighthouse's own config file, inside its env's nebula directory. Named
# once because it is now load-bearing in TWO directions: `LighthouseManager`
# writes it, and `orphaned_lighthouses` identifies a leaked process by the
# fact that its `-config` argument points at a copy of this file that no
# longer exists.
LIGHTHOUSE_CONFIG = "lighthouse-config.yml"

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
    # `run_command`: `nebula`/`nebula-cert` are downloaded on demand, so an
    # absent binary is an ordinary state -- rc 127, not a traceback.
    proc = run_command(args)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


# One `threading.Lock` per env's nebula directory (mirrors `gateway/stores.py`
# ::JsonStore's own per-env lock) -- two VMs in the SAME env can boot
# concurrently, and each independently reads-mutates-persists the ONE shared
# `overlay.json` (bootstrap in `ensure_network`, sticky-IP allocation in
# `NebulaManager.allocate_host_ip`). Without this, two concurrent callers can
# each read the same pre-mutation snapshot, allocate DIFFERENT host_ids
# against the SAME next_ip (a collision, not just a lost update), and
# whichever `save_overlay` lands last silently erases the other's
# allocation entirely -- empirically confirmed: two VMs booted in parallel
# both received the identical overlay IP and both handshook with the
# lighthouse under it, and one instance's id was never in `overlay.json` at
# all. Different envs never contend (separate locks, separate files).
_overlay_locks: dict[str, threading.Lock] = {}
_overlay_locks_guard = threading.Lock()


def _lock_for_dir(data_dir: Path) -> threading.Lock:
    key = str(Path(data_dir).resolve())
    with _overlay_locks_guard:
        return _overlay_locks.setdefault(key, threading.Lock())


def _pinned_port() -> int | None:
    pin = os.environ.get(_PORT_PIN_ENV)
    return int(pin) if pin and pin.isdigit() else None


def _bindable(family: int, port: int) -> bool:
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind(("", port))
    except OSError:
        return False
    return True


def _port_free(port: int) -> bool:
    """Can a UDP listener own this port right now, in every family a Go
    listener would take? (`nebula`'s `listen.host: 0.0.0.0` came up as
    `UDP *:4342` on IPv6 in the field, so testing IPv4 alone would have missed
    the very collision this exists to prevent.) No SO_REUSE* is set, so the
    answer is as strict as nebula's own bind. IPv6 is skipped entirely on a
    host without it, rather than making every port look taken."""
    families = (socket.AF_INET, socket.AF_INET6) if socket.has_ipv6 else (socket.AF_INET,)
    return all(_bindable(family, port) for family in families)


def _ports_taken_by_other_envs(root: Path, env: str) -> set[int]:
    """Ports other envs in THIS store have already recorded. A live
    lighthouse's port is caught by `_port_free` anyway; this is for the env
    whose lighthouse happens to be DOWN right now -- claiming its port would
    just move the collision into its next Apply."""
    return {
        overlay.lighthouse_port
        for path in sorted(Path(root).glob("*/nebula/overlay.json"))
        if path.parent.parent.name != env
        and (overlay := MeshNetwork.model_validate_json(path.read_text())).lighthouse_port
    }


def allocate_lighthouse_port(root: Path, env: str) -> int:
    """This env's own lighthouse port: the first candidate in
    `LIGHTHOUSE_PORTS` that no other env in the store has claimed and that a
    UDP socket can actually bind. Serialized on the STORE root (not the env
    dir) because the whole point is that two envs applying concurrently must
    not pick the same one."""
    pin = _pinned_port()
    if pin is not None:
        return pin
    with _lock_for_dir(root):
        taken = _ports_taken_by_other_envs(root, env)
        free = [port for port in LIGHTHOUSE_PORTS if port not in taken and _port_free(port)]
    if not free:
        log.warning(
            "no free lighthouse port in %s-%s for env %r; falling back to %s (a collision is possible)",
            LIGHTHOUSE_PORTS.start, LIGHTHOUSE_PORTS.stop - 1, env, LIGHTHOUSE_PORT,
        )
        return LIGHTHOUSE_PORT
    return free[0]


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
        # Signed host keys land here; 0700 like everything else under `.odin`.
        return private_mkdir(self._dir / "hosts")

    def create_ca(self, network: str) -> CaInfo:
        private_mkdir(self._dir)  # ca.key is written into it
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

    def reissue_cert(self, hostname: str, ip: str, groups: list[str]) -> CertPaths:
        """Sign `hostname` a NEW certificate carrying `groups`, replacing
        whatever it holds now. `ip` is its EXISTING sticky overlay address, so
        nothing already published goes stale.

        Membership is not configuration, and that distinction is this
        function's whole reason to exist. A member's security-group membership
        lives in its CERTIFICATE (`sign_cert`'s `-groups`) -- that is what
        every OTHER member's `group:` firewall rule is matched against. So
        moving a resource between groups is a re-issue, not a config edit, and
        comparing rendered configs (which is all `InstanceVm.refresh_nebula`
        and `MeshSidecar.ensure` used to do) can never see a group move at
        all. Field test 3 HIGH-1: an instance moved OUT of the group a
        database admitted kept reaching that database, with the Apply
        reporting success.

        `nebula-cert sign` refuses to overwrite an existing cert, so the old
        identity is deliberately DESTROYED first -- a re-issue is a
        replacement, never an addition.

        The wire half belongs to the caller: a running daemon holds its cert
        in memory and its PEERS cache the identity of every tunnel they have
        open, so a re-issued cert only takes effect when the daemon RESTARTS
        and every tunnel re-handshakes under the new identity (a SIGHUP is not
        enough -- see `compute/instances.py::InstanceVm._refresh`)."""
        self.revoke_cert(hostname)
        return self.sign_cert(hostname, ip, groups=groups)

    def generate_config(
        self,
        lighthouse_ip: str,
        lighthouse_underlay: str,
        firewall: FirewallRules,
        is_lighthouse: bool = False,
        pki: CertPaths | None = None,
        tun_disabled: bool = False,
        relay_enabled: bool = False,
        lighthouse_port: int | None = None,
    ) -> str:
        """`pki=None` (the default, unchanged): the VM-side fixed paths a
        node's own cloud-init writes its cert to (`/etc/nebula/...`). A REAL
        `CertPaths` (R3: `LighthouseManager` passes its own
        `NebulaManager.cert_paths("lighthouse")`) points at wherever the
        cert ACTUALLY lives instead -- the host lighthouse process reads its
        cert straight from `.odin/{env}/nebula/hosts/`, never `/etc/nebula`
        (that path is only ever real inside a VM).

        `tun_disabled` (R4): only ever set for the HOST lighthouse -- a
        coordination-only node that never needs a real tun device, so it
        never needs root (empirically verified: an unprivileged `nebula`
        with `tun: disabled: true` starts and binds its UDP port; without it,
        the same unprivileged process dies with "operation not permitted").
        Every VM member still gets a real tun device, created as root INSIDE
        the VM (systemd) -- that's the mesh's actual data plane.

        `relay_enabled` (R5): stock Lima `vz` NATs each VM into its OWN
        isolated address space -- there is NO VM-to-VM underlay path at all
        (confirmed live: a raw ping between two VMs' vzNAT addresses is
        100% loss, before nebula is even involved), so direct VM-to-VM
        handshakes can never succeed no matter how the members are
        configured. Every VM CAN reach the host (they already handshake
        with the lighthouse fine), so traffic instead relays THROUGH the
        lighthouse at the encrypted-tunnel level -- it forwards opaque UDP
        between two peers it already has live sessions with, never
        decrypting either side, so (empirically verified) it needs no tun
        device to do it: the lighthouse gets `relay: {am_relay: true,
        use_relays: false}` (offers to relay, never needs one itself), every
        VM member gets `relay: {use_relays: true, relays: [lighthouse_ip]}`
        (`lighthouse_ip` doubles as the relay's address -- one node, two
        roles).

        `lighthouse_port` (field test 2 B8): the port THIS env's lighthouse
        owns -- what it binds when `is_lighthouse`, and what every member
        dials it on. `None` keeps the historical machine-global 4342, which is
        what an `overlay.json` written before per-env ports existed means."""
        port = lighthouse_port or LIGHTHOUSE_PORT
        config: dict = {
            "pki": {
                "ca": str(pki.ca_crt) if pki else "/etc/nebula/ca.crt",
                "cert": str(pki.crt) if pki else "/etc/nebula/host.crt",
                "key": str(pki.key) if pki else "/etc/nebula/host.key",
            },
            "lighthouse": {"am_lighthouse": is_lighthouse},
            # The lighthouse gets its OWN port (see LIGHTHOUSE_PORT's comment:
            # Lima steals the host's 4242 for a VM's own nebula listener), and
            # its own per ENV (B8).
            "listen": {"host": "0.0.0.0", "port": port if is_lighthouse else NEBULA_PORT},
            "firewall": {
                "inbound": [_rule_to_dict(r) for r in firewall.inbound],
                "outbound": [_rule_to_dict(r) for r in firewall.outbound],
            },
        }
        if tun_disabled:
            config["tun"] = {"disabled": True}
        if relay_enabled:
            config["relay"] = (
                {"am_relay": True, "use_relays": False} if is_lighthouse
                else {"use_relays": True, "relays": [lighthouse_ip]}
            )
        if not is_lighthouse:
            config["static_host_map"] = {lighthouse_ip: [f"{lighthouse_underlay}:{port}"]}
            config["lighthouse"]["hosts"] = [lighthouse_ip]
            # Advertise ONLY the vzNAT address to the lighthouse. A Lima VM
            # has three local addresses and two of them poison discovery:
            # the slirp net (192.168.5.x) is IDENTICAL on every VM, so a peer
            # dialing it hairpins back to ITSELF ("Refusing to handshake with
            # myself"), and the IPv6 ULA is unsendable from nebula's IPv4
            # listener ("listener is IPv4, but writing to IPv6 remote") —
            # both observed live killing the VM↔VM tunnel (R4 diagnosis).
            # The allowed CIDR is the /24 around the lighthouse underlay,
            # i.e. the vzNAT subnet the VMs and host actually share.
            vznat = ipaddress.ip_network(f"{lighthouse_underlay}/24", strict=False)
            # "::/0": False is load-bearing — nebula applies the allow-list
            # per address family, so a v4-only list leaves the VM's IPv6 ULA
            # advertised, and every handshake then burns ~6s failing on it
            # ("listener is IPv4, but writing to IPv6 remote") before trying
            # the right candidate (observed live, R4 diagnosis round 2).
            config["lighthouse"]["local_allow_list"] = {str(vznat): True, "::/0": False}
            # Try the vzNAT candidate FIRST regardless of list order.
            config["preferred_ranges"] = [str(vznat)]
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

    def allocate_host_ip(self, host_id: str) -> str:
        """Atomic read-modify-write, under this directory's lock: allocates
        (or reuses) `host_id`'s sticky overlay IP and persists it
        immediately, in ONE locked critical section -- see `_lock_for_dir`'s
        module-level docstring for why a bare `network.cert_ip(...)` +
        `save_overlay(...)` pair (the previous shape) is racy the moment two
        VMs boot concurrently in the same env. Requires the network already
        be bootstrapped (`ensure_network` called first, as every real caller
        already does) -- there's no sensible env name to default a fresh
        `MeshNetwork` to here."""
        with _lock_for_dir(self._dir):
            overlay = self.load_overlay()
            if overlay is None:
                raise RuntimeError(f"no Nebula network bootstrapped at {self._dir} -- call ensure_network first")
            ip = overlay.cert_ip(host_id)
            self.save_overlay(overlay)
            return ip


# nebula's own default tun device name on Linux (`tun.dev`, which odin never
# overrides) -- the one file-system-visible proof that the daemon is up and has
# configured its interface, readable with no tool at all inside either a Lima
# VM or the busybox sidecar.
NEBULA_TUN = "nebula1"
# How long `rehandshake_script` waits for that device after a (re)start before
# poking anyway: 1s ticks, POSIX `sleep` only (busybox's fractional sleep is a
# compile-time option, and this script runs in alpine as well as Ubuntu).
_TUN_WAIT_TICKS = 10


def rehandshake_script(peers: Iterable[str]) -> str:
    """A member that just RESTARTED its nebula daemon, re-establishing every
    tunnel immediately instead of waiting for its peers to notice.

    Field test 3 MED-2 measured the cost of not doing this: a 10-60s window
    after a mesh restart where the member's overlay address simply did not
    answer, while `/world` said `healthy` and kept advertising it. The cause is
    nebula's own (correct) anti-DoS behaviour, not a bug: when a peer keeps
    sending into the tunnel this member just tore down, the member answers
    `recv_error`, and the peer deliberately IGNORES the first few before
    dropping its stale tunnel and re-handshaking. With a TCP probe's
    1s/2s/4s retransmit cadence that is ~10 seconds of a silently dead path.

    The fix is to make the RESTARTED side move first: one packet toward each
    peer forces a fresh handshake, and a completed handshake replaces the
    peer's cached tunnel (and therefore its cached CERTIFICATE for us) in the
    same instant. So the window closes in one round trip, in both directions,
    which is what makes a re-issued certificate take effect promptly rather
    than "eventually".

    ICMP is enough and is deliberately unprivileged: the peer's inbound
    firewall will very likely DROP the ping itself -- that is fine and even
    expected (it is the whole point of a security group), because the nebula
    HANDSHAKE happens below the firewall. We are buying tunnel state, not a
    reply. Self-bounding throughout (`-c 1 -W 1`, a capped wait loop, `exit 0`)
    so it can never hang an Apply, and it is only ever run on a real restart --
    an unchanged member never pays a millisecond of it."""
    pokes = "\n".join(f"ping -c 1 -W 1 {ip} >/dev/null 2>&1" for ip in peers)
    return (
        f"i=0; while [ $i -lt {_TUN_WAIT_TICKS} ]; do "
        f"[ -d /sys/class/net/{NEBULA_TUN} ] && break; i=$((i+1)); sleep 1; done\n"
        f"{pokes}\nexit 0\n"
    )


def peer_overlay_ips(network: MeshNetwork, member: str) -> list[str]:
    """Every OTHER member's sticky overlay address in this env's mesh --
    `rehandshake_script`'s input. The lighthouse is deliberately not in it: it
    is not a data-plane member (`tun: disabled: true`), and the restarting
    member re-registers with it as its very first act anyway."""
    hosts = network.subnets.get("hosts")
    assignments = hosts.assignments if hosts else {}
    return [ip for host, ip in assignments.items() if host != member]


def _rule_to_dict(rule: FirewallRule) -> dict:
    d: dict = {"port": rule.port, "proto": rule.proto}
    if rule.cidr:
        d["cidr"] = rule.cidr
    if rule.group:
        d["group"] = rule.group
    if not rule.cidr and not rule.group:
        d["host"] = "any"
    return d


_PORTLESS_PROTOCOLS = ("icmp", "icmpv6")  # AWS expresses ICMP type/code via FromPort/ToPort
# (-1 = "all"), but nebula's `port` field is strictly an L4 TCP/UDP port (or
# range) -- it has no ICMP type/code granularity. Feeding it AWS's literal
# "-1" verbatim (empirically confirmed) makes nebula refuse to start at all
# ("port appears to be a range but could not be parsed") -- silently taking
# the whole daemon down over a rule that was only ever meant to be a no-op.


def sg_rules_to_firewall(permissions: list[dict]) -> FirewallRules:
    """Translate AWS security-group IpPermissions (canvas SG edges) to Nebula
    firewall rules — recovered, for deriving per-env ACLs from the canvas."""
    inbound: list[FirewallRule] = []
    for perm in permissions:
        proto = perm.get("IpProtocol", "-1")
        from_port, to_port = perm.get("FromPort"), perm.get("ToPort")
        nebula_proto = "any" if proto == "-1" else proto
        nebula_port = "any"
        if proto not in ("-1", *_PORTLESS_PROTOCOLS) and from_port is not None:
            nebula_port = str(from_port) if from_port == to_port else f"{from_port}-{to_port}"
        for ip_range in perm.get("IpRanges", []):
            inbound.append(FirewallRule(port=nebula_port, proto=nebula_proto, cidr=ip_range.get("CidrIp")))
        for group_ref in perm.get("UserIdGroupPairs", []):
            inbound.append(FirewallRule(port=nebula_port, proto=nebula_proto, group=group_ref.get("GroupId", "")))
        if not perm.get("IpRanges") and not perm.get("UserIdGroupPairs"):
            inbound.append(FirewallRule(port=nebula_port, proto=nebula_proto))
    return FirewallRules(inbound=inbound, outbound=[FirewallRule(port="any", proto="any")])


def union_firewalls(firewalls: Iterable[FirewallRules]) -> FirewallRules:
    """The effective firewall for a node carrying SEVERAL security groups.

    AWS's own semantics: security groups are permissive-only (there is no
    deny rule), so a resource's effective rule set is the UNION of every
    group attached to it -- W2.6 piece 1, where an EC2 instance's ASSIGNED
    groups (not merely its VPC's default) compile into its nebula config.
    De-duplicated (two groups authorizing the identical port/proto/source is
    one nebula rule, not two) and order-preserving, so the generated config
    is stable across re-applies. An empty input yields an empty inbound list
    -- deny-all-inbound, which is exactly what a compiled SG with no ingress
    rules already means to nebula; the caller decides whether "no groups at
    all" should instead fall back to something else."""
    inbound: dict[tuple, FirewallRule] = {}
    outbound: dict[tuple, FirewallRule] = {}
    for firewall in firewalls:
        for side, rules in ((inbound, firewall.inbound), (outbound, firewall.outbound)):
            for rule in rules:
                side.setdefault((rule.port, rule.proto, rule.cidr, rule.group), rule)
    return FirewallRules(inbound=list(inbound.values()), outbound=list(outbound.values()))


def _nebula_dir(root: Path, env: str) -> Path:
    return Path(root) / env / "nebula"


def ensure_network(root: Path, env: str, lighthouse_underlay: str, runner=None) -> MeshNetwork:
    """Lazily bootstrap an env's Nebula network: CA + lighthouse cert + overlay,
    persisted under `.odin/<env>/nebula/`. Idempotent (sticky overlay).
    Locked (see `_lock_for_dir`'s module-level docstring): two VMs booting
    concurrently in the same env must not both see `manager._ca_crt.exists()`
    as False and race to create/sign the CA, nor race on persisting
    `lighthouse_underlay_ip`.

    Also where this env's own lighthouse PORT is allocated (field test 2 B8) --
    once, HERE, before any member config can embed it, and sticky from then
    on."""
    manager = NebulaManager(_nebula_dir(root, env), runner=runner)
    with _lock_for_dir(manager._dir):
        overlay = manager.load_overlay() or MeshNetwork(network=env)
        overlay.lighthouse_underlay_ip = lighthouse_underlay
        overlay.lighthouse_port = overlay.lighthouse_port or allocate_lighthouse_port(root, env)
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
    every call here a true no-op without a real process).

    R4 (rootless): a lighthouse only ever COORDINATES -- it tells mesh
    members where to find each other, but never carries their traffic, so
    it never needs a real tun device. `generate_config(..., tun_disabled=
    True)` sets nebula's own `tun: disabled: true`, and this class spawns
    the brew `nebula` binary DIRECTLY as the invoking user -- no `sudo`, no
    root-owned ctl script, no one-time host setup. Verified empirically: an
    unprivileged `nebula -config ...` with `tun: disabled: true` starts,
    logs "Nebula interface is active" with `interface=disabled`, and binds
    its UDP port; the identical config WITHOUT it dies immediately with
    "operation not permitted" trying to open a tun device. The mesh's actual
    data plane is the VMs -- `compute/instances.py::InstanceVm._
    activate_nebula` installs a REAL `nebula` daemon running as root INSIDE
    each VM (systemd), which costs the user nothing since it's a VM they
    already own outright.

    R5 (relay): stock Lima `vz` gives every VM its OWN isolated address
    space -- there's no VM-to-VM underlay path at all, so `ensure_started`
    also passes `relay_enabled=True`: the lighthouse offers itself as a
    relay (`relay: {am_relay: true}`) so VM-to-VM traffic can route THROUGH
    it instead of failing to go direct. Relaying is opaque UDP forwarding
    between two peers this lighthouse already has live sessions with --
    empirically confirmed to need no tun device either, so this stays fully
    unprivileged.

    `ensure_started` is best-effort throughout (never raises) -- `nebula`
    missing from PATH, or an immediate crash on a bad cert/config, both
    return False and log a warning rather than failing an otherwise-
    successful instance boot over mesh wiring (matching `_activate_nebula`'s
    own "never fail the instance boot over this" rule).
    """

    def __init__(self, popen=None, runner=None, nebula_bin: str | None = None) -> None:
        self._popen = popen or subprocess.Popen
        self._run = runner or _default_runner
        # Injectable (mirrors popen/runner) so unit tests never depend on the
        # real machine's PATH; production leaves it None and resolves lazily
        # via `shutil.which` in `ensure_started`.
        self._nebula_bin = nebula_bin

    def _pidfile(self, root: Path, env: str) -> Path:
        return _nebula_dir(root, env) / "lighthouse.pid"

    def _config_path(self, root: Path, env: str) -> Path:
        return _nebula_dir(root, env) / LIGHTHOUSE_CONFIG

    def _log_path(self, root: Path, env: str) -> Path:
        return _nebula_dir(root, env) / "lighthouse.log"

    def is_running(self, root: Path, env: str) -> bool:
        """Read-only: no filesystem write (mirrors `mesh_state`'s own "a GET
        must not mkdir" rule). Signal 0 checks liveness without signaling --
        `PermissionError` (a live pid we can't signal, e.g. reused by another
        user's process) is still treated as "running" defensively, distinct
        from `ProcessLookupError` (the pid is simply gone)."""
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

    def _usable_port(self, root: Path, env: str, manager: NebulaManager, overlay: MeshNetwork | None) -> int:
        """This env's recorded port -- unless something else on the machine is
        holding it, in which case take a fresh one and RECORD it, so the
        members that regenerate their configs (every sidecar, on the next
        `ensure`) follow us there.

        This is the B8 fix's teeth: the alternative is what the field saw --
        `nebula` exiting 1 on a port another env already owned, and an env
        whose whole mesh silently never worked. An explicit
        `ODIN_LIGHTHOUSE_PORT` pin is honoured as given (the user asked for
        that port; if it is busy they get the honest immediate-exit warning
        below)."""
        recorded = (overlay.lighthouse_port if overlay else None) or LIGHTHOUSE_PORT
        if _pinned_port() is not None or _port_free(recorded):
            return recorded
        fresh = allocate_lighthouse_port(root, env)
        log.warning(
            "lighthouse port %s for env %r is already in use; moving this env to %s "
            "(mesh members pick it up on their next re-join)", recorded, env, fresh,
        )
        # No lock of its own: every caller reaches this while already holding
        # THIS env's nebula-dir lock (`ensure_started`), which is what makes
        # "read the port, maybe move it, spawn, write the pidfile" one critical
        # section instead of a race.
        current = manager.load_overlay()
        if current is not None:
            current.lighthouse_port = fresh
            manager.save_overlay(current)
        return fresh

    def ensure_started(self, root: Path, env: str, underlay: str) -> bool:
        """Idempotent no-op if already running. Never raises -- returns
        False when the network isn't bootstrapped yet, `nebula` isn't on
        PATH, or it exits immediately (bad cert/config); the caller logs and
        moves on rather than failing an otherwise-successful instance boot
        over mesh wiring.

        SERIALIZED PER ENV, and that is load-bearing: two VMs in one env boot
        on their own threads (`compute/instances.py::_activate_nebula`) and a
        backing can join at the same moment (`fabric/sidecar.py`), so without
        this both can see `is_running()` False and spawn. That used to be
        self-limiting -- the loser lost the bind on the one fixed port and
        exited -- but with a per-env port the loser would instead MOVE the env
        to a fresh port, spawn a second lighthouse, and overwrite the winner's
        pidfile, leaking the winner (found by a stray `nebula` process after a
        two-VM integration test). Inside the lock the loser simply sees a
        running lighthouse and returns True."""
        with _lock_for_dir(_nebula_dir(root, env)):
            return self._start_locked(root, env, underlay)

    def _start_locked(self, root: Path, env: str, underlay: str) -> bool:
        if self.is_running(root, env):
            return True
        manager = NebulaManager(_nebula_dir(root, env), runner=self._run)
        cert = manager.cert_paths("lighthouse")
        if not cert.crt.exists():
            log.warning("no lighthouse cert for env %r yet (no VPC created?); lighthouse not started", env)
            return False
        nebula_bin = self._nebula_bin or shutil.which("nebula")
        if nebula_bin is None:
            log.warning("nebula not found on PATH; lighthouse not started for env %r (brew install nebula)", env)
            return False
        overlay = manager.load_overlay()
        lighthouse_ip = overlay.lighthouse_ip if overlay else MeshNetwork(network=env).lighthouse_ip
        port = self._usable_port(root, env, manager, overlay)
        config_text = manager.generate_config(
            lighthouse_ip=lighthouse_ip, lighthouse_underlay=underlay,
            firewall=DEFAULT_FIREWALL, is_lighthouse=True, pki=cert, tun_disabled=True, relay_enabled=True,
            lighthouse_port=port,
        )
        config_path = self._config_path(root, env)
        atomic_write_text(config_path, config_text)
        pidfile = self._pidfile(root, env)
        private_mkdir(pidfile.parent)
        log_path = self._log_path(root, env)
        try:
            with log_path.open("ab") as handle:
                proc = self._popen(
                    [nebula_bin, "-config", str(config_path)],
                    stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
                )
        except OSError as exc:
            log.warning("failed to spawn nebula lighthouse for env %r: %s", env, exc)
            return False
        # We own this process directly (no sudo/exec chain to a separate
        # copy) -- `proc.pid` IS the real nebula pid. A short poll catches an
        # IMMEDIATE crash (bad cert/config, port in use) before we trust it.
        deadline = time.monotonic() + _LIGHTHOUSE_START_TIMEOUT
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is not None:
            log.warning(
                "nebula lighthouse exited immediately for env %r on UDP %s (exit %s); see %s -- "
                "this env's mesh endpoints will report unreachable rather than being advertised",
                env, port, proc.returncode, log_path,
            )
            return False
        pidfile.write_text(str(proc.pid))
        log.info(
            "started nebula lighthouse for env %r (pid %s, underlay %s:%s, unprivileged, tun disabled)",
            env, proc.pid, underlay, port,
        )
        return True

    def ensure_stopped(self, root: Path, env: str) -> None:
        """Exact-pid only, read from THIS env's own pidfile -- never a
        pattern/blanket kill (the same discipline `InstanceVm`'s VM teardown
        uses for `limactl`). We started this process ourselves as the
        invoking user, so a plain `SIGTERM` is all it takes -- no `sudo`,
        no ctl script."""
        pidfile = self._pidfile(root, env)
        if not pidfile.exists():
            return
        text = pidfile.read_text().strip()
        if text.isdigit():
            try:
                os.kill(int(text), signal.SIGTERM)
            except ProcessLookupError:
                pass
        pidfile.unlink(missing_ok=True)
        log.info("stopped nebula lighthouse for env %r", env)


def orphaned_lighthouses(root: Path, runner=None) -> list[tuple[int, Path]]:
    """`(pid, config path)` for every live `nebula` lighthouse process THIS
    store started whose config file is GONE -- an env destroyed out from under
    a process that is still holding its UDP port.

    Field test 3 HIGH-A: a VPC + a single S3 bucket (no EC2 at all) leaked one
    lighthouse and one port on EVERY apply/destroy cycle -- three orphans
    measured on `*:4343`, `*:4344`, `*:4345`, one of them 8m20s old. About a
    hundred cycles would exhaust the 4342-4441 pool, i.e. re-create by
    accumulation exactly the class of failure per-env ports exist to prevent.
    The primary fix is that teardown now stops the lighthouse before deleting
    its directory (`gateway/models/ec2net.py::_delete_vpc`); THIS is the
    backstop for one that already leaked, and for a crash between the two.

    Identified by evidence, never by name: the process's own `-config`
    argument must point INSIDE this store's root, must be a
    `LIGHTHOUSE_CONFIG` file, and that file must no longer exist. A live env's
    lighthouse can therefore never match, another odin store's can never
    match, and a user's own unrelated `nebula` can never match. The pid comes
    from the same `ps` read that proved the process is nebula, so it cannot be
    a recycled pid belonging to something else -- which is more than the
    pidfile path can say."""
    marker = f"{Path(root).resolve()}/"
    proc = (runner or _default_runner)(["ps", "-Ao", "pid=,args="])
    found: list[tuple[int, Path]] = []
    for line in proc.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        tokens = args.split()
        if not pid.isdigit() or "-config" not in tokens[:-1] or Path(tokens[0]).name != "nebula":
            continue
        config = tokens[tokens.index("-config") + 1]
        if config.startswith(marker) and config.endswith(f"/{LIGHTHOUSE_CONFIG}") and not Path(config).exists():
            found.append((int(pid), Path(config)))
    return found


def reap_orphaned_lighthouses(root: Path, runner=None) -> list[int]:
    """SIGTERM every `orphaned_lighthouses` process, returning the pids it
    reaped. Run at server startup (`odin.server`), the same one-shot
    crash-recovery cadence as `ec2compute.reap_orphaned_vms` -- and, like it,
    never able to touch anything it has not positively identified as odin's
    own leak."""
    reaped = []
    for pid, config in orphaned_lighthouses(root, runner):
        log.warning("reaping orphaned nebula lighthouse pid %s (its config %s is gone)", pid, config)
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            log.warning("could not stop orphaned lighthouse pid %s: %s", pid, exc)
            continue
        reaped.append(pid)
    return reaped


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
        lighthouse_running=lighthouse_running, lighthouse_port=overlay.lighthouse_port,
        hosts=hosts, resources=resources, vpcs=vpcs, security_groups=security_groups,
    )


class NebulaFabric(LocalhostFabric):
    """resolve() is byte-identical to LocalhostFabric: the producer publishes its
    reachable address into World facts, and resolve() returns it verbatim. On a
    mesh that address is the producer host's overlay IP:port (e.g.
    10.42.1.7:6379) instead of 127.0.0.1:port — the difference is in what the
    PRODUCER published (its host overlay IP), not in resolve. Inheriting
    guarantees the drop-in parity the design review required."""
