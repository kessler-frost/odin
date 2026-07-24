"""InstanceVm -- the substrate binding for gateway/models/ec2compute.py's
EC2 instances: a REAL Lima VM per instance (NORTHSTAR directive 5 resolution
4, "EC2 instances = real Lima VMs"). Wraps `limactl` via an injectable
subprocess seam -- the same shape `runtime/lima.py`'s `LimaRuntime` uses
(tests/runtime/test_lima.py's FakeRunner method) -- but does NOT reuse
`LimaRuntime` itself: that class manages ONE shared host VM running
containers via nerdctl; `InstanceVm` manages MANY per-instance VMs, one full
VM each -- create/start/stop/delete a whole VM, never `nerdctl run` inside a
shared one.

VM naming: `odin-ec2-{env}-{instance_id}` (`vm_name` below) -- the ONLY
name this module ever passes to `limactl`. NEVER touch a VM outside this
convention (a user's own Lima VMs, e.g. `veronica`, are off-limits; every
`limactl` call here names one exact VM, never a wildcard/`--all`).

vzNAT IP discovery (directive 5's "real IPs"): `generate_lima_yaml(...,
shared_network=True)` adds Lima's `shared` (vmnet/vzNAT) network -- the only
one of Lima's networks reachable from the host (the default network is
user-mode/slirp, 192.168.5.0/24, egress-only, never host-reachable).
`hostname -I` inside the VM lists every interface; `_pick_shared_ip` returns
whichever non-loopback address ISN'T that slirp subnet, rather than a
hardcoded prefix -- prior-odin's own notes record the vzNAT allocation as
typically 192.168.64.x-family, but the exact /24 can vary by Lima version, so
excluding the one KNOWN-wrong subnet is more robust than matching one exact
right one.

Nebula join (directive 6, the payoff): every VPC gets a Nebula network
unconditionally (V1b's `ec2net.py::_create_vpc`), so an instance launched
into a subnet always has one to join. `_nebula_files` signs a REAL host cert
via `fabric.nebula`'s existing nebula-cert primitives -- landed on the VM's
disk via cloud-init (real files, provable with `limactl shell ... cat
/etc/nebula/host.crt`), along with the nebula BINARY itself (`cloud_init.py`'s
`install_nebula`, same download-a-release-tarball shape as `install_nerdctl`)
and a registered-but-not-started systemd unit.

R3 (single-host mesh activation) finishes what this module's own docstring
used to flag as deliberately unbuilt: `config.yml` is NOT written at
cloud-init time (its `static_host_map` needs a host-reachable underlay
address that doesn't exist until the VM is actually up on vzNAT -- a genuine
chicken/egg, not a decoration). Instead `boot()` writes it POST-boot, once
`_discover_ip` has confirmed the VM is networked:
`_discover_host_underlay` derives the host's own address on the VM's vzNAT
/24 (correlated to the VM's OWN observed IP via a live `ifconfig`, not a
hardcoded subnet -- vzNAT's exact /24 is a macOS implementation detail,
empirically 192.168.64.0/24 today but not a contract), `_activate_nebula`
then writes the real config (the VPC's compiled SG firewall included, off
`NebulaJoin.firewall`) via `limactl shell ... sudo tee` and starts the
daemon. `fabric.nebula.LighthouseManager` supervises the HOST-side
lighthouse process this all connects to -- runs unprivileged (R4: `tun:
disabled: true`, no root, no sudo) since it only coordinates; the VM's own
`nebula` daemon (started here as root INSIDE the VM via systemd) is the
mesh's real data plane. Both halves are best-effort: a mesh-wiring failure
never fails the AWS instance boot itself (`_activate_nebula` never raises).

R5 (relay): stock Lima `vz` NATs every VM into its OWN isolated address
space -- confirmed live, a raw ping between two VMs' vzNAT addresses is
100% loss, so a direct VM-to-VM handshake can never succeed regardless of
config. Every VM CAN reach the host, though (it already handshakes with the
lighthouse), so `relay_enabled=True` here routes VM-to-VM traffic THROUGH
the lighthouse instead -- still rootless (a relay forwards opaque encrypted
UDP between two peers it already has sessions with; it never needs a tun
device to do it, empirically confirmed in `fabric/nebula.py`).
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from odin.compute.cloud_init import generate_cloud_init
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import VmConfig
from odin.fabric.models import FirewallRules
from odin.fabric.nebula import DEFAULT_FIREWALL, LighthouseManager, NebulaManager, ensure_network

log = logging.getLogger("odin.compute.instances")

_SLIRP_PREFIX = "192.168.5."  # Lima's built-in user-mode network -- never host-reachable
BOOT_TIMEOUT = 300.0


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


def _default_runner(args: list[str], input: str | None = None) -> _Proc:
    proc = subprocess.run(args, capture_output=True, text=True, input=input)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


def vm_name(env: str, instance_id: str) -> str:
    return f"odin-ec2-{env}-{instance_id}"


@dataclass(frozen=True)
class NebulaJoin:
    """What `InstanceVm.boot` needs to land THIS instance's cert+config onto
    the VM's disk -- `root`/`env` locate the env's Nebula network (V1b: one
    per VPC, keyed by env -- see `ec2net.py`'s module docstring), `host_id`
    is the instance id (the cert's `-name`, and the sticky-IP allocation
    key). `firewall` (R3) is the containing VPC's default security group's
    ALREADY-compiled Nebula firewall (`ec2net.py::_compiled_firewall`,
    resolved by `gateway/models/ec2compute.py`'s boot path) -- `None` falls
    back to `DEFAULT_FIREWALL` (allow-all), matching v1's own "no
    SecurityGroupIds modeled yet, every instance inherits the VPC default
    SG" limit exactly as real AWS does for an instance launched with none
    specified."""
    root: Path
    env: str
    host_id: str
    firewall: FirewallRules | None = None


def _pick_shared_ip(hostname_i_output: str) -> str | None:
    """`hostname -I`'s space-separated IPv4 list -> the vzNAT/shared-network
    address, or None if only loopback/slirp addresses are up yet (the VM's
    shared interface hasn't come up -- the caller retries)."""
    candidates = [tok for tok in hostname_i_output.split() if tok and ":" not in tok and not tok.startswith("127.")]
    shared = [ip for ip in candidates if not ip.startswith(_SLIRP_PREFIX)]
    return shared[0] if shared else None


def _write_files_script(files: dict[str, str]) -> str:
    """Bash heredoc lines writing `files` into `/etc/nebula/` -- the same
    heredoc-block style cloud_init.py's buildkit unit already uses, so this
    stays one provision script, not a second cloud-init module."""
    lines = ["mkdir -p /etc/nebula"]
    for filename, content in files.items():
        marker = f"ODIN_NEBULA_{filename.upper().replace('.', '_')}"
        lines += [f"cat > /etc/nebula/{filename} << '{marker}'", content.rstrip("\n"), marker]
    lines.append("chmod 600 /etc/nebula/host.key")
    return "\n".join(lines)


def _extra_provision_script(nebula_files: dict[str, str] | None, user_data: str | None) -> str | None:
    parts = [p for p in (_write_files_script(nebula_files) if nebula_files else None, user_data) if p]
    return "\n\n".join(parts) if parts else None


class InstanceVm:
    """limactl-backed per-instance VM lifecycle. The subprocess runner is
    injectable (`tests/runtime/test_lima.py`'s FakeRunner shape), so a REAL
    `InstanceVm` is unit-testable without booting anything (V3b's tests
    inject a fake runner and assert the yaml/name/args it builds); V3a's
    gateway-model tests instead inject a wholly fake stand-in object with
    this same method shape, so `gateway/models/ec2compute.py`'s state
    machine is testable without either a real VM OR a real subprocess."""

    def __init__(self, runner=None, poll_interval: float = 2.0, lighthouse: LighthouseManager | None = None) -> None:
        self._run = runner or _default_runner
        # A constructor knob (not a hardcoded sleep) purely for testability --
        # `_discover_ip`'s real pacing is 2s between `hostname -I` polls, but
        # a unit test asserting the timeout PATH shouldn't have to wait for it.
        self._poll_interval = poll_interval
        # R3: injectable so a unit test can assert `_activate_nebula`'s
        # lighthouse-ensure call without spawning a real `sudo nebula`.
        self._lighthouse = lighthouse or LighthouseManager()

    def _lima(self, *args: str, check: bool = True, input: str | None = None) -> _Proc:
        proc = self._run(["limactl", *args], input=input)
        if check and proc.returncode != 0:
            raise RuntimeError(f"limactl {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def boot(
        self,
        name: str,
        vm_config: VmConfig,
        *,
        hostname: str,
        ssh_pubkey: str | None = None,
        user_data: str | None = None,
        nebula: NebulaJoin | None = None,
        timeout: float = BOOT_TIMEOUT,
        env_vars: dict[str, str] | None = None,
    ) -> str:
        """Create + start a fresh VM, wait for its vzNAT IP, return it.
        Raises on any failure (boot timeout, a `limactl` error) -- the
        caller (ec2compute.py) turns that into the instance's terminal
        StateReason, never a silent hang. `env_vars` (the workload's gateway
        identity, see `gateway/keys.py::workload_env`) is baked into the
        VM's cloud-init -- /etc/environment + ~/.aws/credentials."""
        extra = _extra_provision_script(self._nebula_files(nebula), user_data)
        script = generate_cloud_init(
            hostname=hostname, ssh_pubkey=ssh_pubkey, extra_script=extra, env_vars=env_vars,
            install_nebula=nebula is not None,
        )
        yaml_doc = generate_lima_yaml(vm_config, cloud_init_script=script, shared_network=True)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(yaml_doc)
            yaml_path = handle.name
        try:
            self._lima("create", "--tty=false", f"--name={name}", yaml_path)
            self._lima("start", f"--timeout={int(timeout)}s", name)
        finally:
            Path(yaml_path).unlink(missing_ok=True)
        ip = self._discover_ip(name, timeout)
        if nebula is not None:
            self._activate_nebula(name, nebula, ip)
        return ip

    def _nebula_files(self, nebula: NebulaJoin | None) -> dict[str, str] | None:
        """Cloud-init-time only: cert material, which doesn't depend on the
        underlay address -- `config.yml` is deliberately NOT here (see the
        module docstring's chicken/egg note); `_activate_nebula` writes it
        post-boot. Reuses a PREVIOUSLY-discovered real underlay if this env
        already has one (so a second instance's cert-signing pass doesn't
        regress `overlay.json` back to the "127.0.0.1" bootstrap placeholder)
        -- `_activate_nebula` re-derives + persists the real value
        regardless, so THIS instance's own connectivity never depends on
        that cache.

        Uses `NebulaManager.allocate_host_ip` (not a bare `MeshNetwork.
        cert_ip` + `save_overlay` pair) so this instance's sticky overlay IP
        is allocated AND persisted as one locked operation -- two instances
        booting concurrently in the same env (this method runs on each
        instance's own boot thread) would otherwise race on the shared
        `overlay.json`: both could read the same pre-allocation snapshot,
        collide on the SAME next IP, and whichever saved last would
        silently erase the other's assignment entirely (empirically
        confirmed while proving the R4 rootless mesh with two real VMs)."""
        if nebula is None:
            return None
        manager = NebulaManager(Path(nebula.root) / nebula.env / "nebula", runner=self._run)
        existing = manager.load_overlay()
        underlay = (existing.lighthouse_underlay_ip if existing else None) or "127.0.0.1"
        ensure_network(nebula.root, nebula.env, underlay, runner=self._run)
        overlay_ip = manager.allocate_host_ip(nebula.host_id)
        cert = manager.sign_cert(nebula.host_id, overlay_ip, groups=["ec2"])
        return {
            "ca.crt": cert.ca_crt.read_text(),
            "host.crt": cert.crt.read_text(),
            "host.key": cert.key.read_text(),
        }

    def _discover_host_underlay(self, vm_ip: str) -> str | None:
        """The host's own address on the VM's vzNAT /24 -- the address a
        nebula daemon INSIDE the VM can reach the host lighthouse at.
        Derived by correlating to the VM's OWN just-discovered IP (e.g.
        `192.168.64.10` -> look for a host interface in `192.168.64.0/24`)
        rather than a hardcoded subnet: vzNAT's exact /24 is a macOS
        implementation detail (`_pick_shared_ip`'s own docstring makes the
        same call for the VM side). Empirically this is the host's vzNAT
        bridge interface (`bridge100`, gateway `.1`) -- verified by booting a
        real Lima vz VM and reading `ifconfig` on both sides; it only exists
        on the host while at least one vz VM is running, which is always
        true here (this VM is the one that just booted)."""
        proc = self._run(["ifconfig"])
        if proc.returncode != 0:
            return None
        prefix = f"{vm_ip.rsplit('.', 1)[0]}."
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith(f"inet {prefix}"):
                return line.split()[1]
        return None

    def _activate_nebula(self, name: str, nebula: NebulaJoin, vm_ip: str) -> None:
        """Post-boot (the VM is up and vzNAT-networked, so the real underlay
        is now discoverable): write the FINAL nebula config -- the real
        underlay address and the VPC's compiled SG firewall -- and start the
        daemon; also ensures the env's HOST-side lighthouse is up (the
        "first VM joins" half of `LighthouseManager`'s lifecycle --
        `gateway/models/ec2compute.py::_finish_terminate` covers "last VM
        leaves"). Best-effort throughout: NEVER raises. RunInstances has
        already succeeded and the instance is really running regardless of
        mesh state -- a wiring failure here must not strand a healthy
        instance in a failure path over something the AWS API contract
        doesn't even surface."""
        try:
            underlay = self._discover_host_underlay(vm_ip)
            if underlay is None:
                log.warning("could not derive a host underlay address for %s; nebula not activated", name)
                return
            self._lighthouse.ensure_started(nebula.root, nebula.env, underlay)
            manager = NebulaManager(Path(nebula.root) / nebula.env / "nebula", runner=self._run)
            network = ensure_network(nebula.root, nebula.env, underlay, runner=self._run)
            config = manager.generate_config(
                lighthouse_ip=network.lighthouse_ip, lighthouse_underlay=underlay,
                firewall=nebula.firewall or DEFAULT_FIREWALL, is_lighthouse=False, relay_enabled=True,
            )
            self._lima("shell", name, "--", "sudo", "tee", "/etc/nebula/config.yml", input=config, check=False)
            self._lima("shell", name, "--", "sudo", "systemctl", "enable", "--now", "nebula", check=False)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            log.warning("nebula activation failed for %s: %s", name, exc)

    def _discover_ip(self, name: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = self._lima("shell", name, "--", "hostname", "-I", check=False)
            ip = _pick_shared_ip(proc.stdout) if proc.returncode == 0 else None
            if ip:
                return ip
            time.sleep(self._poll_interval)
        raise TimeoutError(f"{name} did not report a reachable IP within {timeout}s")

    def stop(self, name: str) -> None:
        self._lima("stop", name, check=False)

    def start(self, name: str, timeout: float = BOOT_TIMEOUT) -> str:
        self._lima("start", f"--timeout={int(timeout)}s", name)
        return self._discover_ip(name, timeout)

    def delete(self, name: str) -> None:
        self._lima("stop", "--force", name, check=False)
        self._lima("delete", "--force", name, check=False)

    def logs(self, name: str, tail: int = 20) -> str:
        """The VM's systemd journal tail -- the closest honest equivalent to
        a container's `docker logs` for a real Lima VM (there's no single
        process to attach to; journalctl aggregates every unit, including
        cloud-init's own, so a boot failure shows up here too). Never
        raises: an unreachable VM (not up yet, already deleted, `limactl`
        itself missing) answers with a clear message instead of a stack
        trace, matching every other observability read in this app
        (`_ContainerRuntime.logs`'s own `check=False` contract)."""
        proc = self._lima("shell", name, "--", "sudo", "journalctl", "-n", str(tail), "--no-pager", check=False)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or "no output"
            return f"[{name}: VM not reachable ({detail})]"
        return proc.stdout

    def status(self, name: str) -> str:
        """`limactl list --json` filtered by the EXACT name -- 'absent' if
        gone. `--json` emits one JSON object per line (JSON Lines), not a
        JSON array."""
        out = self._lima("list", "--json", check=False).stdout
        for line in out.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("name") == name:
                return str(record.get("status", "Unknown")).lower()
        return "absent"

    def list_names(self) -> list[str]:
        """Every VM name `limactl list --json` currently reports -- read-only
        (never touches a VM), the one non-exact-name limactl call this class
        makes. The startup reaper
        (`gateway/models/ec2compute.py::reap_orphaned_vms`) is the only
        caller; it still only ever calls `delete(name)` with an exact name
        it has already validated against the store."""
        out = self._lima("list", "--json", check=False).stdout
        names = []
        for line in out.splitlines():
            if not line.strip():
                continue
            name = json.loads(line).get("name")
            if name:
                names.append(name)
        return names
