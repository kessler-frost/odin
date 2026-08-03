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

`refresh_nebula` (field test 2 HIGH-1) closes the gap all of the above left:
every one of those writes happened ONCE, at boot. Editing a security group
afterwards never reached an already-running VM -- Apply reported `applied`,
the gateway recorded the new rule, and the VM's `/etc/nebula/config.yml`
still held the old one with `NRestarts=0`. Two VMs in the SAME drawn group
enforced DIFFERENT firewalls depending on when each was created, which is
precisely the failure security groups exist to prevent. Now an Apply
re-renders each running instance's config and pushes it when it actually
changed -- SIGHUP for a firewall-only edit (verified against the nebula
version odin ships: "Caught HUP, reloading config" -> "New firewall has been
installed", and a previously-blocked port starts answering with the tunnel
never dropping), a restart for anything else, because nebula's reload
deliberately does NOT cover `static_host_map`/`lighthouse`/`relay` -- and the
only thing that changes those is a moved lighthouse port, where the tunnel is
already dead and a restart costs nothing.

Field test 3 HIGH-1 found the hole `refresh_nebula` left, and it was the worst
possible one: an instance MOVED BETWEEN groups. A group's RULES live in the
config, but an instance's MEMBERSHIP lives in its nebula CERTIFICATE, so
re-rendering a config could never see the change -- Apply reported `applied`
with zero warnings while the VM kept the cert (and therefore the access) it
was born with. In the REVOKE direction that is a security hole, not an
annoyance: the engineer took `web1` out of the group `db-sg` admits and web1
went on reaching the database. `_reissue_cert` closes it -- the instance is
signed a NEW certificate carrying its current groups (same sticky overlay IP,
so nothing published goes stale), the new identity is landed on the VM, and
the daemon is RESTARTED, because a cert only reaches the wire when every
tunnel re-handshakes under it. Nothing is recreated: same VM, same instance
id, same address.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from odin.compute.cloud_init import generate_cloud_init, hosts_block_script
from odin.compute.lima_yaml import additional_disks, generate_lima_yaml
from odin.compute.models import VmConfig
from odin.fabric.models import FirewallRules
from odin.fabric.nebula import (
    DEFAULT_FIREWALL,
    LighthouseManager,
    NebulaManager,
    ensure_network,
    firewall_only_change,
    peer_overlay_ips,
    rehandshake_script,
)
from odin.runtime.colima import _failure_reason
from odin.settings import ComputeSettings, env_name, settings
from odin.util import atomic_write_text, run_command_async

log = logging.getLogger("odin.compute.instances")

_SLIRP_PREFIX = "192.168.5."  # Lima's built-in user-mode network -- never host-reachable
# The name, for the sentence a timed-out boot prints. `settings.py` owns both
# it and the 300s default, along with the measurement that justifies the
# ceiling and the argument for why it deliberately does not move.
_BOOT_TIMEOUT_ENV = env_name(ComputeSettings, "boot_timeout")


def boot_timeout() -> float:
    """How long a VM may take to report a running guest, in seconds.

    300s is generous for a healthy boot -- the nebula mesh e2e boots two VMs
    and finishes ENTIRELY in 74.6s on an idle machine. It is not generous on a
    busy one. Measured at the tail of a 57-minute integration suite (dozens of
    VMs and containers created and destroyed before it), a VM reached
    `[VZ] - vm state change: running` in one second and then never signalled a
    running guest: `limactl start --timeout=300s` gave up at exactly 300s, the
    instance went `terminated`, and the whole apply failed with it.

    So the ceiling is real work-in-progress, not a bug -- but a hard constant
    left a user on a loaded Mac with no recourse at all, which is the part
    worth fixing. Read per call (never at import) so it can be raised for one
    slow run without a restart, matching `rdsctl.available_timeout()` and
    `simulate/runner.py`'s own timeout knob.

    The default deliberately does NOT move. Raising it for everyone would make
    a genuinely hung boot take longer to report, and a slow boot and a dead one
    look identical until the clock runs out.
    """
    return settings.compute.boot_timeout


def _default_max_concurrent_boots() -> int:
    """Owner directive B2: draw N EC2 nodes and RunInstances spawns N
    daemon threads (`gateway/models/ec2compute.py::_spawn`), each calling
    `InstanceVm.boot` -- with nothing bounding them, N concurrent
    `limactl create`/`start` calls stampede the Mac at once. `vm or
    InstanceVm()` (ec2compute.py's own `pure_answer`) constructs a FRESH
    `InstanceVm` per gateway call, so the bound has to live at module scope
    (`_BOOT_SEMAPHORE` below), not on the instance -- a per-instance
    semaphore would reset every call and bound nothing. `ODIN_MAX_CONCURRENT_VM_BOOTS`
    overrides; read fresh (not cached) so tests can monkeypatch it, same
    convention as `agent/translate.py`'s `_default_timeout`."""
    return settings.compute.max_concurrent_vm_boots


# Process-wide: every `InstanceVm` -- including the fresh one each gateway
# call constructs -- shares this ONE semaphore by default (`boot`/`start`
# below fall back to it when no per-call override is given).
#
# v0.7.7 DE-THREADING VERDICT (verified): becomes an `asyncio.Semaphore`, and
# unlike the five `Lock`s this one is not even arguable. It is not guarding a
# data race, so the "does the critical section await?" test does not apply to
# it at all -- it is a RESOURCE BOUND on how many `limactl create`/`start`
# runs may be in flight against one Mac. That bound is still needed when the
# boots are tasks rather than threads; N concurrent awaits stampede the host
# exactly as N concurrent threads do.
_BOOT_SEMAPHORE = asyncio.Semaphore(_default_max_concurrent_boots())


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


async def _default_runner(args: list[str], input: str | None = None) -> _Proc:
    # `run_command_async`: `limactl` is genuinely optional (doctor reports it
    # as such), so "not installed" must surface as a nonzero result every
    # caller already handles, never a FileNotFoundError. Async because `_lima`
    # awaits this seam (v0.7.7 de-threading) -- `limactl create`/`start` are
    # the longest subprocesses odin runs, so blocking the loop on them is the
    # single worst stall available.
    proc = await run_command_async(args, input=input)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


def vm_name(env: str, instance_id: str) -> str:
    return f"odin-ec2-{env}-{instance_id}"


# `limactl disk` names live in ONE flat machine-wide namespace
# (`$LIMA_HOME/_disks/`), not per-instance -- so the env belongs in the name
# for the same reason it belongs in `vm_name`: it is what makes a reclaim
# scopable to one env instead of a machine-wide sweep over every disk the
# user owns. Every disk odin creates matches this prefix and nothing else is
# ever deleted (see `env_disk_prefix`).
def disk_name(env: str, volume_id: str) -> str:
    return f"odin-ebs-{env}-{volume_id}"


def env_disk_prefix(env: str) -> str:
    return f"odin-ebs-{env}-"


# --- how long an env name may be, before limactl refuses every boot ---------
#
# `limactl` refuses an instance whose SSH control socket would not fit in a
# unix socket address:
#
#   instance name "odin-ec2-<env>-<id>" too long: "/Users/you/.lima/
#   odin-ec2-<env>-<id>/ssh.sock.1234567890123456" must be less than
#   UNIX_PATH_MAX=104 characters, but is 107
#
# It is a HARD, TOTAL failure of every EC2 boot in that env -- and the message
# names nothing the user chose, arriving only after a ~60s boot that was never
# going to work. `max_env_name_len` is what lets Apply refuse it up front
# instead (`reconcile/admission.py`).
#
# DERIVED, never hardcoded, because the limit is machine-specific: it is
# `104` minus `$LIMA_HOME` (default `~/.lima`, so it moves with the username),
# minus the two `/` separators, minus Lima's socket filename, minus odin's own
# `odin-ec2-` + `-` + instance id. On a `/Users/fimbulwinter/.lima` home that
# works out to 22 -- exactly the value the mesh work measured by hand.
LIMA_UNIX_PATH_MAX = 104

# Lima's SSH control socket, with the 16-digit suffix its own check appends.
# The literal digits are Lima's placeholder, not a real value: only the LENGTH
# is load-bearing here.
LIMA_SSH_SOCK = "ssh.sock.1234567890123456"

# Lima's own identifier cap, reported as `greater than maximum length (76
# characters)`. In practice the socket-path rule above always bites first (any
# non-empty LIMA_HOME makes it tighter), but taking the min keeps this honest
# if Lima ever moves its sockets elsewhere.
LIMA_MAX_IDENTIFIER_LEN = 76

# An EC2 instance id is `i-` + 17 hex characters -- AWS's own modern shape,
# minted by `gateway/models/ec2compute.py::_mint`. Pinned by
# `tests/test_compute/test_vm_name_limit.py`, so the derivation can't drift
# from the ids actually minted.
INSTANCE_ID_LEN = 19


def lima_home() -> Path:
    """Where `limactl` keeps its instances: `$LIMA_HOME`, else `~/.lima` --
    Lima's own resolution order, and the variable half of the length limit."""
    return Path(os.environ.get("LIMA_HOME") or Path.home() / ".lima")


def max_vm_name_len(home: Path | None = None) -> int:
    """The longest instance name `limactl` will accept on this machine.

    `len(f"{LIMA_HOME}/{name}/{LIMA_SSH_SOCK}") < LIMA_UNIX_PATH_MAX`, i.e.
    at most `LIMA_UNIX_PATH_MAX - 1` bytes of path -- rearranged for `name`,
    and capped by Lima's identifier limit."""
    root = str(lima_home() if home is None else home)
    fits_socket = LIMA_UNIX_PATH_MAX - 1 - len(root) - 2 - len(LIMA_SSH_SOCK)
    return min(fits_socket, LIMA_MAX_IDENTIFIER_LEN)


def max_env_name_len(home: Path | None = None) -> int:
    """The longest env name whose EC2 VMs can boot on this machine -- the VM
    name limit less everything `vm_name` adds around the env (`odin-ec2-`,
    the separator, and the instance id)."""
    return max_vm_name_len(home) - len(vm_name("", "")) - INSTANCE_ID_LEN


def instance_config_path(root: Path, env: str, host_id: str) -> Path:
    """Where odin records the nebula config it LAST PUT ON a given VM.

    The host-side copy is what makes `refresh_nebula` free when nothing
    changed: comparing against a local file costs no `limactl shell` at all
    (the same trick `fabric/sidecar.py` uses for a backing's config). It can
    never go stale across a recreate either -- a recreated instance is a NEW
    instance id, so it gets a fresh path."""
    return Path(root) / env / "nebula" / "instances" / host_id / "config.yml"


def instance_hosts_path(root: Path, env: str, host_id: str) -> Path:
    """`instance_config_path`'s twin for the route53 entries odin last landed
    in this VM's `/etc/hosts`.

    Same trick, same reason: comparing against a local file makes an unchanged
    record set cost ZERO `limactl shell` calls, which is what lets
    `push_hosts` run for every instance on every Apply without churning. And
    the same safety rule -- written only AFTER the guest has taken the change,
    so a push that failed half-way is retried by the next Apply instead of
    being remembered as done."""
    return instance_config_path(root, env, host_id).with_name("hosts.json")


def instance_membership_path(root: Path, env: str, host_id: str) -> Path:
    """`instance_config_path`'s twin for the OTHER half of an instance's
    security state: the security-group ids baked into the certificate odin
    last successfully LANDED on this VM.

    Membership cannot be read back out of the config -- it is in the cert -- so
    without a record of it there is nothing to compare a canvas edit against,
    which is exactly why a revoked group used to be invisible (field test 3
    HIGH-1). Recording the groups rather than the cert keeps the no-churn
    contract intact: an unchanged membership stays ONE local file read, no
    `nebula-cert` subprocess and no `limactl`.

    Written only AFTER the new cert is on the VM, so a push that failed
    half-way is retried by the next Apply instead of being remembered as done.
    No record at all (a VM booted before this existed) is not evidence of
    anything, and `_reissue_cert` treats it the safe way: re-issue."""
    return instance_config_path(root, env, host_id).with_name("membership.json")


# --- route53 on a VM: the four answers, and why silence is not one of them ---
#
# `push_hosts` returns the first three; `HOSTS_NO_MESH` is the resolver's, for
# the case where there is nothing to push BECAUSE no resolvable address exists.
#
# That fourth value is the one that earns this block. A VM cannot reach another
# VM's vzNAT `private_ip` at all -- stock Lima `vz` NATs each VM into its own
# address space and a raw ping between two of them is 100% loss, before nebula
# is involved (`fabric/nebula.py`'s R5 note, confirmed live). So the only
# address that works VM-to-VM is the Nebula OVERLAY one, and an env with no
# mesh has none to give. Withholding the entry is therefore CORRECT -- writing
# `private_ip` into that VM's /etc/hosts would produce a name that resolves and
# then never connects, which is worse than one that does not resolve.
#
# But withholding cannot be the WHOLE story, and that is this repo's own
# scar tissue: honesty rule 1 lists "the mesh gate withheld facts that never
# reached World" as one of four guards that silently never fired. A withheld
# entry that nobody is told about is indistinguishable from a working one until
# someone's connection fails. So the resolver reports this verdict, and
# `reconcile/tf_status.py` (owned elsewhere) projects it as a non-healthy phase
# carrying `hosts_reason` verbatim.
HOSTS_UNCHANGED = "unchanged"
HOSTS_PUSHED = "pushed"
HOSTS_FAILED = "failed"
HOSTS_NO_MESH = "no_mesh"
# A name that could not be resolved for a reason OTHER than a missing mesh --
# it points at no instance, at two, or carries several addresses. The resolver
# (`compute/hosts.py`) has already produced the exact sentence for each, so
# this action carries THOSE rather than a template of its own.
#
# It exists because the alternative was reporting every unresolvable name as
# `no_mesh`, which is a FALSE reason: a record pointing at a terminated
# instance in a fully-meshed env would have been explained as "this
# environment has no mesh". A wrong reason is worse than a generic one --
# it sends the reader to fix something that is not broken.
HOSTS_UNRESOLVABLE = "unresolvable"

_HOSTS_HEALTHY = (HOSTS_UNCHANGED, HOSTS_PUSHED)

# Keyed on the OUTCOME, never initialised optimistically -- honesty rule 2's
# "what finally worked" for `/destroy` after four rounds. An action this map
# does not know falls through to a failure that NAMES the unknown action,
# rather than inheriting a success it was never granted.
_HOSTS_REASON = {
    HOSTS_FAILED: (
        "odin could not write this instance's /etc/hosts, so {names} still "
        "resolve to whatever the VM last had (or to nothing)"
    ),
    HOSTS_NO_MESH: (
        "{names} cannot be resolved on this instance: the record points at another "
        "EC2 instance, a VM can only reach another VM over the Nebula overlay "
        "(a VM-to-VM vzNAT address is 100% loss), and this environment has no "
        "mesh. Draw the instances into a VPC so the env gets one, or reach the "
        "target from a container instead"
    ),
    # The resolver's own sentences, verbatim. Nothing is re-derived here: it
    # already knows exactly why each name failed, and re-deciding would make
    # two components answer the same question from different inputs.
    HOSTS_UNRESOLVABLE: "{details}",
}


@dataclass(frozen=True)
class HostsVerdict:
    """What really happened to one VM's route53 entries, in a form
    `reconcile/tf_status.py` can project without re-deriving anything.

    `names` is what could NOT be made to resolve -- empty on the healthy
    actions. It is carried rather than recomputed so the projection and the
    substrate can never disagree about which names are affected."""

    vm: str
    action: str
    names: tuple[str, ...] = ()
    # Per-name sentences from `compute/hosts.py`, carried rather than
    # regenerated. Only `HOSTS_UNRESOLVABLE` uses them.
    details: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.action in _HOSTS_HEALTHY

    @property
    def reason(self) -> str:
        """Empty when healthy; otherwise names the resource, what is still
        standing, and the real cause. Never empty for a non-healthy action --
        an unmapped action reports ITSELF as the bug rather than passing."""
        if self.healthy:
            return ""
        template = _HOSTS_REASON.get(
            self.action,
            "odin reported an unrecognised route53 hosts outcome ({action!r}) for this "
            "instance, so it cannot say whether {names} resolve",
        )
        listed = ", ".join(self.names) or "its route53 names"
        # `details` falls back to the name list rather than rendering empty:
        # a reason slot with nothing in it is the dangling-colon failure
        # `_failure_reason` exists to prevent, one layer up.
        detailed = "; ".join(self.details) or f"{listed} could not be resolved"
        return template.format(names=listed, action=self.action, details=detailed)


@dataclass(frozen=True)
class NebulaJoin:
    """What `InstanceVm.boot` needs to land THIS instance's cert+config onto
    the VM's disk -- `root`/`env` locate the env's Nebula network (V1b: one
    per VPC, keyed by env -- see `ec2net.py`'s module docstring), `host_id`
    is the instance id (the cert's `-name`, and the sticky-IP allocation
    key). `firewall` is the ALREADY-compiled Nebula firewall of this
    instance's ASSIGNED security groups, unioned (W2.6 piece 1 --
    `ec2compute.py::_instance_firewall`, which falls back to the containing
    VPC's DEFAULT security group for an instance launched with none, exactly
    as real AWS does); `None` falls back to `DEFAULT_FIREWALL` (allow-all)
    for an instance whose groups have no compiled firewall at all.

    `groups` are the instance's security-group IDS, which become its nebula
    CERT groups (`_nebula_files`). That's the other half of making SG-to-SG
    rules real: another node's "allow 5432 from sg-web" compiles to a nebula
    `group: sg-web` rule, and nebula matches that against the PEER's
    certificate groups -- so without this, an SG-referencing rule could never
    match anything.

    `revision` is the env's whole MEMBERSHIP roster, digested (field test 4).
    Rendered inside the firewall block, where nebula ignores it but a change
    to it makes a reload count -- which is what closes flows this VM had
    ALREADY admitted from a member that has since left the group
    (`fabric/nebula.py::FIREWALL_REVISION_KEY`). It is deliberately about the
    WHOLE env, not this instance: the member that has to act on a revoke is
    the one that was ADMITTING, and its own groups did not change at all."""
    root: Path
    env: str
    host_id: str
    firewall: FirewallRules | None = None
    groups: tuple[str, ...] = ()
    revision: str = ""


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


def _cert_groups(nebula: NebulaJoin) -> list[str]:
    """The groups this instance's certificate must carry: `ec2` plus its
    CURRENT security-group ids. Sorted, because the order the gateway happens
    to list an instance's groups in is not meaningful -- and treating a reorder
    as a membership change would re-issue a cert and restart a daemon on every
    Apply, which is exactly the churn `refresh_nebula` promises never to
    cause."""
    return ["ec2", *sorted(nebula.groups)]


def _record_membership(nebula: NebulaJoin) -> None:
    atomic_write_text(
        instance_membership_path(nebula.root, nebula.env, nebula.host_id),
        json.dumps(_cert_groups(nebula)),
    )


def _recorded_membership(nebula: NebulaJoin) -> list[str] | None:
    path = instance_membership_path(nebula.root, nebula.env, nebula.host_id)
    return json.loads(path.read_text()) if path.exists() else None


def membership_changed(nebula: NebulaJoin) -> bool:
    """Would `refresh_nebula` have to re-issue this instance's certificate?
    One local file read, no subprocess -- `_reissue_cert`'s own test, exposed
    so a CALLER can order its work by it.

    `gateway/models/ec2compute.py::ensure_instance_mesh` is that caller, and
    the ordering is load-bearing (field test 4): the admitting member re-checks
    an already-open flow against the PEER'S CURRENT certificate, so the peer
    has to be holding its new one by then. Re-certify first, reload the
    admitters after."""
    return _recorded_membership(nebula) != _cert_groups(nebula)


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

    def __init__(
        self, runner=None, poll_interval: float = 2.0, lighthouse: LighthouseManager | None = None,
        boot_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._run = runner or _default_runner
        # A constructor knob (not a hardcoded sleep) purely for testability --
        # `_discover_ip`'s real pacing is 2s between `hostname -I` polls, but
        # a unit test asserting the timeout PATH shouldn't have to wait for it.
        self._poll_interval = poll_interval
        # R3: injectable so a unit test can assert `_activate_nebula`'s
        # lighthouse-ensure call without spawning a real `sudo nebula`.
        self._lighthouse = lighthouse or LighthouseManager()
        # Owner directive B2: bounds concurrent `limactl create`/`start`
        # calls. Defaults to the process-wide `_BOOT_SEMAPHORE` (module
        # docstring) -- a test injects its own small Semaphore to assert the
        # bound without needing `ODIN_MAX_CONCURRENT_VM_BOOTS`/a real boot.
        self._boot_semaphore = boot_semaphore or _BOOT_SEMAPHORE

    async def _lima(self, *args: str, check: bool = True, input: str | None = None) -> _Proc:
        """One `limactl` run. A failure raises a reason that is never vacuous.

        The text was `f"limactl {' '.join(args)} failed: {proc.stderr.strip()}"`,
        which renders `limactl shell odin-ec2-x -- ... failed: ` -- a sentence
        whose reason slot is a dangling colon -- for the whole class of failure
        this module actually produces. Probed against REAL limactl 2.1.3 and a
        REAL Lima VM (created, driven, deleted), not reasoned about; every one
        of these exits non-zero having written NOTHING to stderr:

            shell <vm> -- sh -c 'exit 3'                 rc=3 err='' out=''
            shell <vm> -- sh -c 'echo on-stdout; exit 7' rc=7 err='' out='on-stdout\\n'
            shell <vm> -- sudo bash -s  <<< 'exit 9'     rc=9 err='' out=''
            shell <vm> -- false                          rc=1 err='' out=''

        `limactl shell` PROPAGATES the guest's exit code (3, 7, 9), so the one
        fact that was there every time was the one being discarded -- and case
        two shows the reason can be on STDOUT, which this seam kept only on
        success. `_failure_reason` is `runtime/colima.py`'s, imported rather
        than re-spelled: `docker` and `limactl` are the same problem, and
        tests/compute/test_instances.py pins the identity.

        Unlike colima's `_command_label`, the full argv IS named here: it
        carries no secret. Every raising call site is `create`/`start`/`list`
        (VM name, `--timeout`, a YAML PATH); the cert key and the workload's
        env vars ride stdin and cloud-init files, never argv."""
        proc = await self._run(["limactl", *args], input=input)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"limactl {' '.join(args)} failed "
                f"(exit {proc.returncode}): {_failure_reason(proc)}"
            )
        return proc

    async def boot(
        self,
        name: str,
        vm_config: VmConfig,
        *,
        hostname: str,
        ssh_pubkey: str | None = None,
        user_data: str | None = None,
        nebula: NebulaJoin | None = None,
        timeout: float | None = None,
        env_vars: dict[str, str] | None = None,
        disks: list[str] | None = None,
        hosts: dict[str, str] | None = None,
    ) -> str:
        """Create + start a fresh VM, wait for its vzNAT IP, return it.
        Raises on any failure (boot timeout, a `limactl` error) -- the
        caller (ec2compute.py) turns that into the instance's terminal
        StateReason, never a silent hang. `env_vars` (the workload's gateway
        identity, see `gateway/keys.py::workload_env`) is baked into the
        VM's cloud-init -- /etc/environment + ~/.aws/credentials."""
        timeout = timeout or boot_timeout()
        extra = _extra_provision_script(await self._nebula_files(nebula), user_data)
        script = generate_cloud_init(
            hostname=hostname, ssh_pubkey=ssh_pubkey, extra_script=extra, env_vars=env_vars,
            install_nebula=nebula is not None, hosts=hosts,
        )
        yaml_doc = generate_lima_yaml(
            vm_config, cloud_init_script=script, shared_network=True, disks=disks or [],
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(yaml_doc)
            yaml_path = handle.name
        try:
            # Owner directive B2: only `_boot_semaphore`'s own limit worth of
            # VMs are ever mid-create/start at once -- a caller beyond that
            # WAITS here (v0.7.7: an `asyncio.Semaphore`, so it yields the loop
            # to every other task instead of parking a thread) until a slot
            # frees. Released before `_discover_ip`'s poll loop and nebula
            # activation, neither of which is the heavy part.
            async with self._boot_semaphore:
                await self._lima("create", "--tty=false", f"--name={name}", yaml_path)
                await self._lima("start", f"--timeout={int(timeout)}s", name)
        finally:
            Path(yaml_path).unlink(missing_ok=True)
        ip = await self._discover_ip(name, timeout)
        if nebula is not None:
            await self._activate_nebula(name, nebula, ip)
        return ip

    async def _nebula_files(self, nebula: NebulaJoin | None) -> dict[str, str] | None:
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
        await ensure_network(nebula.root, nebula.env, underlay, runner=self._run)
        overlay_ip = await manager.allocate_host_ip(nebula.host_id)
        # "ec2" plus this instance's own security-group ids (W2.6): nebula
        # matches a peer's `group:` firewall rule against THIS cert's groups,
        # so the sg ids have to be baked in at signing time -- they're what
        # another node's "allow 5432 from sg-web" rule tests against.
        cert = await manager.sign_cert(nebula.host_id, overlay_ip, groups=_cert_groups(nebula))
        # What this VM is about to be born holding -- recorded HERE so the very
        # next Apply can tell an unchanged membership from a changed one
        # without a single subprocess (see `instance_membership_path`).
        _record_membership(nebula)
        return {
            "ca.crt": cert.ca_crt.read_text(),
            "host.crt": cert.crt.read_text(),
            "host.key": cert.key.read_text(),
        }

    async def _discover_host_underlay(self, vm_ip: str) -> str | None:
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
        proc = await self._run(["ifconfig"])
        if proc.returncode != 0:
            return None
        prefix = f"{vm_ip.rsplit('.', 1)[0]}."
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith(f"inet {prefix}"):
                return line.split()[1]
        return None

    async def _activate_nebula(self, name: str, nebula: NebulaJoin, vm_ip: str) -> None:
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
            underlay = await self._discover_host_underlay(vm_ip)
            if underlay is None:
                log.warning("could not derive a host underlay address for %s; nebula not activated", name)
                return
            await self._lighthouse.ensure_started(nebula.root, nebula.env, underlay)
            network = await ensure_network(nebula.root, nebula.env, underlay, runner=self._run)
            config = self._render_config(nebula, network, underlay)
            await self._push_config(name, nebula, config)
            await self._lima("shell", name, "--", "sudo", "systemctl", "enable", "--now", "nebula", check=False)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            log.warning("nebula activation failed for %s: %s", name, exc)

    def _render_config(self, nebula: NebulaJoin, network, underlay: str) -> str:
        """The one renderer both the boot path and `refresh_nebula` use --
        identical inputs MUST produce identical bytes, or the "did anything
        change?" comparison would churn every Apply."""
        manager = NebulaManager(Path(nebula.root) / nebula.env / "nebula", runner=self._run)
        return manager.generate_config(
            lighthouse_ip=network.lighthouse_ip, lighthouse_underlay=underlay,
            firewall=nebula.firewall or DEFAULT_FIREWALL, is_lighthouse=False, relay_enabled=True,
            # THIS env's lighthouse port (fabric/nebula.py's B8 note): one
            # machine-global 4342 meant only one env's lighthouse could
            # ever bind, so a second env's mesh silently never worked.
            lighthouse_port=network.lighthouse_port,
            firewall_revision=nebula.revision,
        )

    async def push_hosts(self, name: str, root: Path, env: str, host_id: str, hosts: dict[str, str]) -> str:
        """Make a RUNNING VM's `/etc/hosts` say exactly `hosts`, without a
        reboot. Returns what it did: `unchanged` / `pushed` / `failed`.

        THIS METHOD IS WHY route53 RECORDS ARE NOT FROZEN AT BOOT.
        `generate_cloud_init` runs once, inside `limactl create` (see `boot`),
        and its bytes are then baked into the instance's own lima.yaml --
        `limactl start` re-runs the SAME script, and `limactl edit` refuses a
        running instance outright (`level=fatal msg="cannot edit a running
        instance"`, the note in the EBS block below). So a record edited after
        an instance booted could never reach it through cloud-init, which is
        exactly the shape of bug `refresh_nebula` exists to fix one layer over
        -- a control the canvas shows as applied and the guest never sees.

        NO CHURN, on the same contract `refresh_nebula` holds: an unchanged
        record set is one local file read and nothing else -- no `limactl`, no
        subprocess. That matters because this is meant to run for every
        running instance on every Apply.

        Never raises. A mesh/DNS wiring failure must not fail an Apply on its
        own (`_activate_nebula`'s rule) -- but `failed` is a real answer the
        caller is expected to act on, not a shrug, exactly as
        `refresh_nebula`'s is.
        """
        try:
            return await self._push_hosts(name, root, env, host_id, hosts)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            log.warning("hosts push failed for %s: %s", name, exc)
            return "failed"

    async def _push_hosts(self, name: str, root: Path, env: str, host_id: str, hosts: dict[str, str]) -> str:
        record = instance_hosts_path(root, env, host_id)
        # Compared as the RECORD SET, not as rendered bytes: two dicts that
        # differ only in insertion order are the same set of names, and
        # re-rendering them is churn. `hosts_block_script` sorts for the same
        # reason.
        #
        # NO RECORD => PUSH, and `boot` deliberately does not write one even
        # though it seeds the same block through cloud-init. The reason is
        # specific rather than tidy: that provision script runs under `set -ux`
        # and NOT `set -e` (`cloud_init.py`'s own note -- a failing command
        # there must not hang `limactl start` forever), so a `sed` that failed
        # inside it leaves the boot reporting success with the block never
        # written. Recording at boot would therefore record a landing nobody
        # observed. Costing one `limactl shell` per instance lifetime buys the
        # guarantee that odin only ever claims what it watched succeed --
        # `_reissue_cert` makes the same call for the same reason.
        if record.exists() and json.loads(record.read_text()) == hosts:
            return "unchanged"
        script = hosts_block_script(hosts)
        proc = await self._lima("shell", name, "--", "sudo", "bash", "-s", input=script, check=False)
        if proc.returncode != 0:
            # `_failure_reason`, not `proc.stderr or "no output"`: this is a
            # `sudo bash -s`, whose real failure mode was MEASURED as
            # `rc=9, stderr='', stdout=''` -- see `_lima`. The exit code is
            # often the whole of the answer.
            log.warning("could not write /etc/hosts on %s: %s", name, _failure_reason(proc))
            return "failed"
        # Recorded only now -- see `instance_hosts_path`.
        atomic_write_text(record, json.dumps(hosts, sort_keys=True))
        log.info("wrote %d route53 entr%s into %s's /etc/hosts", len(hosts), "y" if len(hosts) == 1 else "ies", name)
        return "pushed"

    async def _push_config(self, name: str, nebula: NebulaJoin, config: str) -> None:
        """Write the config INTO the VM and record what we put there."""
        await self._lima("shell", name, "--", "sudo", "tee", "/etc/nebula/config.yml", input=config, check=False)
        atomic_write_text(instance_config_path(nebula.root, nebula.env, nebula.host_id), config)

    async def _vm_config(self, name: str, nebula: NebulaJoin) -> str | None:
        """What is ACTUALLY on the VM right now. Only read when odin has no
        record of its own (a VM booted before `refresh_nebula` existed, or one
        somebody edited by hand) -- one `limactl shell` per VM, once, instead
        of a blind restart on no evidence."""
        recorded = instance_config_path(nebula.root, nebula.env, nebula.host_id)
        if recorded.exists():
            return recorded.read_text()
        proc = await self._lima("shell", name, "--", "sudo", "cat", "/etc/nebula/config.yml", check=False)
        return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None

    async def refresh_nebula(self, name: str, nebula: NebulaJoin) -> str:
        """Bring a RUNNING VM's nebula config AND certificate up to date with
        the canvas, and make the running daemon actually adopt them. Returns
        what it did: `unchanged` / `reloaded` / `recertified` / `restarted` /
        `skipped` / `failed`.

        This is field test 2 HIGH-1. A security-group rule edit is TF-owned, so
        it reaches the gateway only through an Apply -- and every nebula config
        write used to happen once, at boot. The result on the wire: two VMs in
        one drawn group enforcing different firewalls, forever, with `Apply`
        reporting success.

        NO CHURN is a hard requirement (this runs for every running instance on
        every Apply): an unchanged config is a single local file read and
        nothing else -- no `limactl`, no signal.

        RELOAD vs RESTART, and why it is not a free choice: nebula reloads
        `firewall` (and its certs) on SIGHUP -- verified live against the
        version odin ships, watching a previously-blocked port start answering
        with no restart and no dropped tunnel. It does NOT reload
        `static_host_map` / `lighthouse` / `relay`; the only thing that changes
        those here is a moved lighthouse port, and in exactly that case the
        tunnel is ALREADY dead, so a restart costs nothing and a SIGHUP would
        be a lie. So: firewall-only diff -> SIGHUP; anything else -> restart.
        `NebulaJoin.revision` rides inside that same firewall block, so a
        membership change ELSEWHERE in the env reaches this VM as a reload
        too -- what closes its already-open flows to the moved member without
        a restart storm (field test 4).

        MEMBERSHIP (field test 3 HIGH-1) is the third case, and the one that
        used to be missing entirely. An instance's security groups are baked
        into its CERTIFICATE, not its config, so no amount of config comparison
        could ever see a group move -- Apply said `applied`, nothing warned,
        and a `web1` taken OUT of `web-sg` went on reaching a database that
        only admits `web-sg`. `_reissue_cert` re-signs it (same sticky overlay
        IP) and lands the new identity on the VM, and this path then always
        RESTARTS: nebula's SIGHUP reloads the firewall, but a peer caches the
        certificate of every tunnel it holds open, so only a re-handshake --
        which only a restart forces -- makes the new identity real on the
        wire. A restart is honest here in a way it is not for a rule edit: the
        whole point is that the old tunnels must die.

        Never raises: mesh wiring must not fail an Apply (`_activate_nebula`'s
        rule). But `failed` is no longer a shrug -- `gateway/models/
        ec2compute.py::ensure_instance_mesh` refuses to let an Apply report
        success over it."""
        try:
            return await self._refresh(name, nebula)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            log.warning("nebula refresh failed for %s: %s", name, exc)
            return "failed"

    async def _refresh(self, name: str, nebula: NebulaJoin) -> str:
        manager = NebulaManager(Path(nebula.root) / nebula.env / "nebula", runner=self._run)
        network = manager.load_overlay()
        underlay = network.lighthouse_underlay_ip if network else None
        if network is None or underlay is None:
            return "skipped"  # this env has no bootstrapped mesh to be out of date with
        config = self._render_config(nebula, network, underlay)
        current = await self._vm_config(name, nebula)
        recertified = await self._reissue_cert(name, nebula, manager)
        if current == config and not recertified:
            return "unchanged"
        await self._push_config(name, nebula, config)
        action = (
            "recertified" if recertified
            else "reloaded" if firewall_only_change(current, config)
            else "restarted"
        )
        command = (
            ["sudo", "systemctl", "kill", "-s", "HUP", "nebula"] if action == "reloaded"
            else ["sudo", "systemctl", "restart", "nebula"]
        )
        proc = await self._lima("shell", name, "--", *command, check=False)
        if proc.returncode != 0:
            log.warning("nebula %s failed on %s: %s", action, name, proc.stderr.strip() or "no output")
            return "failed"
        _record_membership(nebula)
        await self._converge(name, nebula, network, action)
        log.info("nebula config %s on %s (its security groups changed)", action, name)
        return action

    async def _reissue_cert(self, name: str, nebula: NebulaJoin, manager: NebulaManager) -> bool:
        """Has this instance's security-group MEMBERSHIP changed, and if so,
        give it a certificate that says so. Returns whether it re-issued.

        Compared against odin's own record of what it last landed on the VM
        (`instance_membership_path`) -- one local file read for the
        overwhelmingly common no-change case, no `nebula-cert`, no `limactl`.
        A missing record (a VM booted before this existed) re-issues once, on
        the safe side: odin cannot prove what identity that VM holds, and the
        alternative is trusting a certificate it never saw.

        RAISES if the new cert cannot be landed on the VM, and the record is
        deliberately NOT updated in that case (`_refresh` writes it only after
        the daemon has taken the change). The caller turns that into `failed`
        and `ensure_instance_mesh` turns THAT into a failed Apply -- because
        an unapplied revoke that reports success is the exact defect this
        exists to fix."""
        desired = _cert_groups(nebula)
        if _recorded_membership(nebula) == desired:
            return False
        cert = await manager.reissue_cert(nebula.host_id, await manager.allocate_host_ip(nebula.host_id), desired)
        script = _write_files_script({"host.crt": cert.crt.read_text(), "host.key": cert.key.read_text()})
        proc = await self._lima("shell", name, "--", "sudo", "bash", "-s", input=script, check=False)
        if proc.returncode != 0:
            # The same `_failure_reason` `_lima` raises, for the same measured
            # reason. This site had half the treatment already (`or 'no
            # output'`, so it never trailed off) and it is the one that most
            # needed the other half: it is a `sudo bash -s`, whose real failure
            # is `rc=9, stderr='', stdout=''` -- "no output" threw away the 9,
            # and threw away a script that explains itself on stdout.
            raise RuntimeError(
                f"could not land {nebula.host_id}'s re-issued certificate (groups {desired}) "
                f"on {name} (exit {proc.returncode}): {_failure_reason(proc)}"
            )
        log.info("re-issued %s's nebula certificate on %s with groups %s", nebula.host_id, name, desired)
        return True

    async def _converge(self, name: str, nebula: NebulaJoin, network, action: str) -> None:
        """After a RESTART, make this VM re-establish its tunnels now instead
        of leaving peers to discover the change on their own schedule -- see
        `fabric/nebula.py::rehandshake_script` for the 10-60s window this
        closes and why nebula's own behaviour creates it.

        Skipped entirely for a SIGHUP (`reloaded`): a firewall reload never
        drops a tunnel, so there is nothing to re-establish, and skipped when
        this member is alone on the mesh. Best-effort and self-bounding: a
        convergence poke that fails leaves the mesh exactly where a restart
        alone would have."""
        peers = peer_overlay_ips(network, nebula.host_id)
        if action == "reloaded" or not peers:
            return
        await self._lima("shell", name, "--", "sudo", "bash", "-s", input=rehandshake_script(peers), check=False)

    async def _discover_ip(self, name: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = await self._lima("shell", name, "--", "hostname", "-I", check=False)
            ip = _pick_shared_ip(proc.stdout) if proc.returncode == 0 else None
            if ip:
                return ip
            # `await`, not `time.sleep`: this poll runs on the shared control
            # loop, where a blocking sleep freezes the reconciler and the
            # gateway together for the whole interval.
            await asyncio.sleep(self._poll_interval)
        # Name the knob IN the failure: this message is the only place a user
        # meets the ceiling, and a timeout they cannot find the dial for reads
        # as odin being broken rather than busy.
        raise TimeoutError(
            f"{name} did not report a reachable IP within {timeout}s "
            f"(raise {_BOOT_TIMEOUT_ENV} if this Mac is just slow or loaded)"
        )

    # --- EBS: real `limactl disk` volumes ----------------------------------
    #
    # Every claim in this block was PROBED against limactl 2.1.3 and real vz
    # VMs before it was coded against (honesty rule 1), and the probe output
    # is quoted in `docs/limits.md`. The three that shape the design:
    #
    #   1. There is NO attach verb. `limactl disk` offers create/delete/ls/
    #      import/resize/unlock and nothing else; attachment lives only in an
    #      instance's `additionalDisks:`.
    #   2. `limactl edit` REFUSES a running instance -- `level=fatal
    #      msg="cannot edit a running instance"`, exit 1. So attaching to a
    #      live instance is a stop/edit/start cycle: a REBOOT, which AWS does
    #      not do. `attach_disk`/`detach_disk` therefore return the instance's
    #      re-discovered IP, because a restarted VM need not come back on the
    #      address it left on.
    #   3. `limactl disk delete` REFUSES a disk an instance still holds --
    #      `fatal msg="cannot delete disk X in use by instance Y"`, exit 1.
    #      Reclaim must detach (or delete the VM) first, and `delete_disk`
    #      runs with check=True precisely so that refusal can never be
    #      mistaken for a reclaim.

    async def create_disk(self, disk: str, size_gib: int) -> None:
        """`limactl disk create` -- a real qcow2/raw file under
        `$LIMA_HOME/_disks/<disk>`. Raises on failure: a CreateVolume that
        answers `available` having created nothing is exactly the false
        success this repo keeps fixing."""
        await self._lima("disk", "create", disk, "--size", f"{size_gib}GiB")

    async def delete_disk(self, disk: str) -> None:
        """`limactl disk delete`. Raises -- see note 3 above."""
        await self._lima("disk", "delete", disk)

    async def disks(self, check: bool = False) -> list[dict]:
        """Every disk `limactl disk list --json` reports, as its own records
        (JSON Lines, like `limactl list --json`). The fields this app reads,
        all confirmed present on 2.1.3:

            {"name":"...","size":1073741824,"format":"raw","dir":"...",
             "instance":"","instanceDir":"","mountPoint":"/mnt/lima-..."}

        `instance` is the VM currently holding it ("" when free) and
        `mountPoint` is where LIMA ITSELF says it lands in a guest -- read
        rather than reconstructed, so the in-guest verification below checks
        limactl's own answer instead of odin's guess at its convention."""
        out = (await self._lima("disk", "list", "--json", check=check)).stdout
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    async def disk(self, name: str) -> dict | None:
        return next((d for d in await self.disks() if d.get("name") == name), None)

    async def set_disks(self, name: str, disks: list[str]) -> None:
        """Replace a STOPPED instance's `additionalDisks:` wholesale.

        Wholesale, not incremental, and deliberately: the gateway store is
        the single source of truth for which volumes an instance holds, so
        rewriting the whole list makes a VM whose yaml has drifted converge
        instead of accumulating the drift. `--set` takes a yq expression;
        the value is `json.dumps`'d (valid YAML, and the disk names are
        odin-minted) rather than pasted together by hand."""
        await self._lima("edit", name, "--set", f".additionalDisks = {json.dumps(additional_disks(disks))}")

    async def attach_disk(self, name: str, disks: list[str], verify: str, timeout: float | None = None) -> str:
        """Stop, rewrite `additionalDisks`, start -- and PROVE the disk
        reached the guest before returning. Returns the instance's IP.

        `disks` is the instance's full desired set; `verify` is the one disk
        this call is adding, checked by its real mount point inside the
        booted guest (`_disk_landed`). Verifying is the entire point: an
        AttachVolume that returns `attached` because a yaml edit succeeded
        would be the decorative bug -- the yaml edit succeeds happily whether
        or not a disk exists behind the name."""
        await self.stop(name)
        await self.set_disks(name, disks)
        ip = await self.start(name, timeout)
        await self._verify_disk_landed(name, verify)
        return ip

    async def detach_disk(self, name: str, disks: list[str], timeout: float | None = None) -> str:
        """The same reboot, without the verification: `disks` is what remains
        attached. Absence needs no in-guest proof -- `limactl disk list`'s own
        `instance` field is the durable witness, and `delete_disk` refuses
        anything still held, so a detach that silently did nothing cannot be
        mistaken for a reclaim later."""
        await self.stop(name)
        await self.set_disks(name, disks)
        return await self.start(name, timeout)

    async def _verify_disk_landed(self, name: str, disk: str) -> None:
        record = await self.disk(disk)
        mount = (record or {}).get("mountPoint")
        if not mount:
            raise RuntimeError(f"limactl no longer reports a disk named {disk!r}; nothing was attached to {name}")
        proc = await self._lima("shell", name, "--", "lsblk", "-J", "-b", "-o", "NAME,SIZE,MOUNTPOINT", check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"could not read block devices in {name} to confirm {disk} attached: {_failure_reason(proc)}")
        if mount not in proc.stdout:
            raise RuntimeError(
                f"{disk} is not mounted at {mount} in {name} after the attach reboot; "
                f"lsblk reported: {proc.stdout.strip()}"
            )

    async def stop(self, name: str) -> None:
        await self._lima("stop", name, check=False)

    async def start(self, name: str, timeout: float | None = None) -> str:
        timeout = timeout or boot_timeout()
        async with self._boot_semaphore:  # same bound as `boot` -- still a real VM start
            await self._lima("start", f"--timeout={int(timeout)}s", name)
        return await self._discover_ip(name, timeout)

    async def delete(self, name: str) -> None:
        await self._lima("stop", "--force", name, check=False)
        await self._lima("delete", "--force", name, check=False)

    async def logs(self, name: str, tail: int = 20) -> str:
        """The VM's systemd journal tail -- the closest honest equivalent to
        a container's `docker logs` for a real Lima VM (there's no single
        process to attach to; journalctl aggregates every unit, including
        cloud-init's own, so a boot failure shows up here too). Never
        raises: an unreachable VM (not up yet, already deleted, `limactl`
        itself missing) answers with a clear message instead of a stack
        trace, matching every other observability read in this app
        (`_ContainerRuntime.logs`'s own `check=False` contract)."""
        proc = await self._lima("shell", name, "--", "sudo", "journalctl", "-n", str(tail), "--no-pager", check=False)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or "no output"
            return f"[{name}: VM not reachable ({detail})]"
        return proc.stdout

    async def status(self, name: str) -> str:
        """`limactl list --json` filtered by the EXACT name -- 'absent' if
        gone. `--json` emits one JSON object per line (JSON Lines), not a
        JSON array."""
        out = (await self._lima("list", "--json", check=False)).stdout
        for line in out.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("name") == name:
                return str(record.get("status", "Unknown")).lower()
        return "absent"

    async def list_names(self, check: bool = False) -> list[str]:
        """Every VM name `limactl list --json` currently reports -- read-only
        (never touches a VM), the one non-exact-name limactl call this class
        makes. The startup reaper
        (`gateway/models/ec2compute.py::reap_orphaned_vms`) is one caller; it
        still only ever calls `delete(name)` with an exact name it has already
        validated against the store.

        `check=True` (W2.2's drift sweep, `reconcile/drift.py`) raises instead
        of swallowing a `limactl` failure: that caller treats "absent from
        this listing" as "the VM was really deleted", so it must be able to
        tell a genuinely empty machine from a limactl that didn't answer."""
        out = (await self._lima("list", "--json", check=check)).stdout
        names = []
        for line in out.splitlines():
            if not line.strip():
                continue
            name = json.loads(line).get("name")
            if name:
                names.append(name)
        return names
