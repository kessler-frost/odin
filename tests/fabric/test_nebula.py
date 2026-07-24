"""Nebula mesh fabric foundation: resolve drop-in parity + recovered primitives.

Cert ops use an injected fake runner, so no nebula-cert binary is required.
"""
from __future__ import annotations

import json
import os

import pytest
import yaml

from odin.fabric.localhost import Unresolved
from odin.fabric.models import CertPaths, FirewallRule, FirewallRules, MeshNetwork
from odin.fabric.nebula import (
    DEFAULT_FIREWALL,
    NEBULA_CTL_PATH,
    LighthouseManager,
    NebulaFabric,
    NebulaManager,
    ensure_network,
    mesh_state,
    sg_rules_to_firewall,
)
from odin.spec.models import Ref, ResourceObserved, World

REF = Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL")
URL = "postgresql://app:pw@10.42.1.7:5432/postgres"


def _world(phase: str, facts: dict) -> World:
    return World(resources=(ResourceObserved(id="db", kind="rds", phase=phase, facts=facts),))


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        # nebula-cert writes -out-crt/-out-key files; create them so existence checks pass.
        for flag in ("-out-crt", "-out-key"):
            if flag in args:
                from pathlib import Path
                Path(args[args.index(flag) + 1]).write_text("CERT")

        from odin.fabric.nebula import _Proc
        return _Proc(0, "")


# --- resolve() is a byte-identical drop-in for LocalhostFabric (the seam) ---

def test_resolves_overlay_address_when_target_healthy():
    assert NebulaFabric().resolve(REF, _world("healthy", {"DATABASE_URL": URL})) == URL


def test_unresolved_when_not_healthy_absent_or_attr_missing():
    fabric = NebulaFabric()
    for world in (_world("starting", {"DATABASE_URL": URL}), World(), _world("healthy", {})):
        with pytest.raises(Unresolved):
            fabric.resolve(REF, world)


# --- recovered nebula-cert primitives ---

def test_create_ca_and_sign_cert_build_commands(tmp_path):
    runner = FakeRunner()
    mgr = NebulaManager(tmp_path / "nebula", runner=runner)
    ca = mgr.create_ca("prod")
    assert ca.network == "prod" and ca.ca_crt.exists()

    certs = mgr.sign_cert("mac-1", "10.42.1.7/24", groups=["host", "service"])
    sign = next(c for c in runner.calls if "sign" in c)
    assert "-ip" in sign and "10.42.1.7/24" in sign
    assert "-groups" in sign and "host,service" in sign
    assert certs.crt.exists() and certs.key.exists()


def test_revoke_cert_removes_files(tmp_path):
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    mgr.create_ca("prod")
    certs = mgr.sign_cert("mac-1", "10.42.1.7/24")
    mgr.revoke_cert("mac-1")
    assert not certs.crt.exists() and not certs.key.exists()


def test_generate_config_shape(tmp_path):
    import yaml
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    member = yaml.safe_load(mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL))
    assert member["listen"] == {"host": "0.0.0.0", "port": 4242}
    assert member["lighthouse"]["am_lighthouse"] is False
    assert member["static_host_map"] == {"10.42.0.1": ["192.168.1.10:4242"]}
    light = yaml.safe_load(mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=True))
    assert light["lighthouse"]["am_lighthouse"] is True and "static_host_map" not in light


def test_generate_config_with_pki_uses_the_real_paths_not_the_vm_placeholder(tmp_path):
    """R3: the HOST lighthouse reads its cert from wherever it actually
    lives (`.odin/{env}/nebula/hosts/...`), never the `/etc/nebula/...`
    paths that are only real inside a VM."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    pki = CertPaths(crt=tmp_path / "hosts/lighthouse.crt", key=tmp_path / "hosts/lighthouse.key", ca_crt=tmp_path / "ca.crt")
    config = yaml.safe_load(mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=True, pki=pki))
    assert config["pki"] == {
        "ca": str(tmp_path / "ca.crt"), "cert": str(tmp_path / "hosts/lighthouse.crt"), "key": str(tmp_path / "hosts/lighthouse.key"),
    }


def test_cert_paths_is_pure_no_io(tmp_path):
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    paths = mgr.cert_paths("lighthouse")
    assert paths.crt == tmp_path / "nebula" / "hosts" / "lighthouse.crt"
    assert not (tmp_path / "nebula").exists()  # no mkdir, no I/O


# --- overlay IP allocation is sticky (re-applies must not churn IPs) ---

def test_host_ip_allocation_is_sticky():
    net = MeshNetwork(network="prod")
    ip = net.allocate_host("mac-1")
    assert net.allocate_host("mac-1") == ip            # same host -> same IP
    assert net.allocate_host("mac-2") != ip            # different host -> different IP
    assert net.cert_ip("mac-1") == f"{ip}/16"          # CIDR form for nebula-cert -- must match base_cidr


def test_overlay_save_load_roundtrip(tmp_path):
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    net = MeshNetwork(network="prod")
    net.allocate_host("mac-1")
    mgr.save_overlay(net)
    assert mgr.load_overlay().subnets["hosts"].assignments == net.subnets["hosts"].assignments


def test_overlay_save_crash_leaves_prior_overlay_intact(tmp_path, monkeypatch):
    # Release finding #2: save_overlay is atomic -- a crash mid-write must
    # not corrupt or drop the previously-saved overlay.
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    original = MeshNetwork(network="prod")
    original.allocate_host("mac-1")
    mgr.save_overlay(original)

    def boom(*a, **k):
        raise OSError("simulated crash")

    monkeypatch.setattr("odin.util.os.replace", boom)
    replacement = MeshNetwork(network="prod")
    replacement.allocate_host("mac-2")
    with pytest.raises(OSError):
        mgr.save_overlay(replacement)

    reloaded = mgr.load_overlay()
    assert reloaded.subnets["hosts"].assignments == original.subnets["hosts"].assignments


def test_sg_rules_to_firewall_translates():
    rules = sg_rules_to_firewall([{"IpProtocol": "tcp", "FromPort": 6379, "ToPort": 6379,
                                   "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}])
    assert isinstance(rules, FirewallRules)
    assert rules.inbound[0].port == "6379" and rules.inbound[0].cidr == "10.0.0.0/8"


# --- V1b golden: the research's own example, byte-for-byte on the fields ---
# "SG with tcp/443 from 10.0.0.0/16 + an SG-ref rule -> expected
# FirewallRule list" -- the exact IpPermissions wire dicts the gateway's
# EC2-network model stores/aggregates, and the exact compiled output.

GOLDEN_PERMISSIONS = [
    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]},
    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
     "UserIdGroupPairs": [{"GroupId": "sg-0123456789abcdef0"}]},
]

GOLDEN_FIREWALL = FirewallRules(
    inbound=[
        FirewallRule(port="443", proto="tcp", cidr="10.0.0.0/16"),
        FirewallRule(port="443", proto="tcp", group="sg-0123456789abcdef0"),
    ],
    outbound=[FirewallRule(port="any", proto="any")],
)


def test_sg_rules_to_firewall_golden():
    assert sg_rules_to_firewall(GOLDEN_PERMISSIONS) == GOLDEN_FIREWALL


def test_compiled_firewall_round_trips_through_generate_config(tmp_path):
    """REAL but dormant (V1): the compiled FirewallRules must be EXACTLY what
    a Nebula node config consumes at V3 -- proven by feeding them through
    generate_config itself and reading the YAML a nebula daemon would."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    config = yaml.safe_load(mgr.generate_config("10.42.0.1", "127.0.0.1", GOLDEN_FIREWALL))
    assert config["firewall"]["inbound"] == [
        {"port": "443", "proto": "tcp", "cidr": "10.0.0.0/16"},
        {"port": "443", "proto": "tcp", "group": "sg-0123456789abcdef0"},
    ]
    assert config["firewall"]["outbound"] == [{"port": "any", "proto": "any", "host": "any"}]


def test_sg_rules_to_firewall_edge_cases():
    # proto "-1" (all) -> any/any; a port range; a security-group reference
    rules = sg_rules_to_firewall([
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8100, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": "sg-1"}]},
    ])
    by = {(r.port, r.proto): r for r in rules.inbound}
    assert by[("any", "any")].cidr == "0.0.0.0/0"          # -1 -> any proto/port
    assert ("8000-8100", "tcp") in by                       # range preserved
    assert by[("443", "tcp")].group == "sg-1"               # group ref carried
    assert rules.outbound[0].port == "any"                  # default allow-all out


# --- mesh read model (the UI hook) + lazy bootstrap ---

def test_ensure_network_bootstraps_and_is_idempotent(tmp_path):
    runner = FakeRunner()
    net = ensure_network(tmp_path, "prod", "192.168.1.10", runner=runner)
    assert net.lighthouse_underlay_ip == "192.168.1.10"
    ca_calls = sum(1 for c in runner.calls if "ca" in c)
    ensure_network(tmp_path, "prod", "192.168.1.10", runner=runner)  # again
    assert sum(1 for c in runner.calls if "ca" in c) == ca_calls     # CA not re-minted


def test_mesh_state_read_has_no_filesystem_side_effect(tmp_path):
    mesh_state(tmp_path, "prod")
    assert not (tmp_path / "prod" / "nebula").exists()  # a GET must not mkdir


def test_mesh_state_projects_world_resources(tmp_path):
    world = _world("healthy", {"endpoint": "10.42.1.7:5432"})
    state = mesh_state(tmp_path, "prod", world)
    assert [(r.id, r.phase, r.endpoint) for r in state.resources] == [("db", "healthy", "10.42.1.7:5432")]


def test_mesh_state_projects_ec2net_vpcs_and_sg_firewalls(tmp_path):
    """V1b: mesh_state reads the EC2-network model's sidecar file (the JSON
    file is the fabric<->gateway boundary -- no gateway import here) and
    projects per-VPC networks + compiled SG firewalls."""
    gateway_dir = tmp_path / "prod" / "gateway"
    gateway_dir.mkdir(parents=True)
    firewall = sg_rules_to_firewall(GOLDEN_PERMISSIONS)
    (gateway_dir / "ec2net.json").write_text(json.dumps({
        "vpc:vpc-1": {"vpc_id": "vpc-1", "cidr_block": "10.0.0.0/16", "nebula_network": "prod"},
        "sg:sg-1": {"group_id": "sg-1", "group_name": "web", "vpc_id": "vpc-1",
                    "is_default": False, "rules": {}, "firewall": firewall.model_dump()},
    }))
    state = mesh_state(tmp_path, "prod")
    assert [(v.vpc_id, v.cidr_block, v.network) for v in state.vpcs] == [("vpc-1", "10.0.0.0/16", "prod")]
    (sg,) = state.security_groups
    assert (sg.sg_id, sg.vpc_id, sg.group_name) == ("sg-1", "vpc-1", "web")
    assert sg.firewall == GOLDEN_FIREWALL


def test_mesh_state_without_ec2net_file_has_no_networks(tmp_path):
    state = mesh_state(tmp_path, "prod")
    assert state.vpcs == [] and state.security_groups == []


def test_mesh_state_empty_then_populated(tmp_path):
    assert mesh_state(tmp_path, "prod").hosts == []   # no overlay yet
    mgr = NebulaManager(tmp_path / "prod" / "nebula", runner=FakeRunner())
    net = MeshNetwork(network="prod", lighthouse_underlay_ip="192.168.1.10")
    net.allocate_host("mac-1")
    mgr.save_overlay(net)
    state = mesh_state(tmp_path, "prod")
    assert state.lighthouse_underlay == "192.168.1.10"
    assert [h.hostname for h in state.hosts] == ["mac-1"]


# --- R3: LighthouseManager -- real process supervision, fake popen/runner ------


class FakePopen:
    """`returncode=None` while "running" (`poll()` -> None), matching a real
    `subprocess.Popen` -- `_fake_popen`'s `exits_immediately` flips it to
    simulate a REJECTED `sudo -n` (fails fast, no pidfile ever written)."""

    def __init__(self, pid: int = 424242, exits_immediately: bool = False) -> None:
        self.pid = pid
        self.returncode = 1 if exits_immediately else None

    def poll(self):
        return self.returncode


def _fake_popen(calls: list[list[str]], pidfile: object = None, pid: int = 424242, exits_immediately: bool = False):
    """Stands in for `subprocess.Popen`. When `pidfile` is given, mimics
    `allfather-nebula-ctl start`'s own behavior of writing its pid there
    itself (real `LighthouseManager.ensure_started` polls for exactly this,
    never trusting `Popen.pid` directly -- see its docstring)."""
    def popen(args, **kwargs):
        calls.append(args)
        if pidfile is not None and not exits_immediately:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(str(pid))
        return FakePopen(pid=pid, exits_immediately=exits_immediately)
    return popen


def test_lighthouse_is_running_false_with_no_pidfile(tmp_path):
    assert LighthouseManager().is_running(tmp_path, "prod") is False


def test_lighthouse_is_running_false_for_a_dead_pid(tmp_path):
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("999999999")  # astronomically unlikely to be a live pid
    assert LighthouseManager().is_running(tmp_path, "prod") is False


def test_lighthouse_is_running_true_for_a_live_pid(tmp_path):
    # Signal 0 to OUR OWN pid succeeds without PermissionError (same uid) --
    # stands in for "alive but root-owned", which raises PermissionError and
    # is ALSO treated as running (see the class docstring: can't signal it,
    # but it's definitely there).
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(str(os.getpid()))
    assert LighthouseManager().is_running(tmp_path, "prod") is True


def test_lighthouse_is_running_false_for_garbage_pidfile_content(tmp_path):
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("not-a-pid")
    assert LighthouseManager().is_running(tmp_path, "prod") is False


def test_lighthouse_ensure_started_spawns_via_the_ctl_script_and_writes_pidfile(tmp_path):
    """The sudoers grant is scoped to `allfather-nebula-ctl`'s fixed,
    root-owned path -- NEVER a raw `nebula` invocation (that would need a
    NOPASSWD grant on brew's user-writable path, a root-escalation hole)."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)  # bootstraps the lighthouse cert
    calls: list[list[str]] = []
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    mgr = LighthouseManager(popen=_fake_popen(calls, pidfile=pidfile), runner=runner)

    started = mgr.ensure_started(tmp_path, "prod", "192.168.64.1")
    assert started is True
    config_path = tmp_path / "prod" / "nebula" / "lighthouse-config.yml"
    assert calls == [["sudo", "-n", NEBULA_CTL_PATH, "start", str(config_path), str(pidfile)]]
    assert pidfile.read_text() == "424242"
    config = yaml.safe_load(config_path.read_text())
    assert config["lighthouse"]["am_lighthouse"] is True
    assert config["pki"]["cert"] == str(tmp_path / "prod" / "nebula" / "hosts" / "lighthouse.crt")


def test_lighthouse_ensure_started_is_idempotent_when_already_running(tmp_path):
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(str(os.getpid()))
    calls: list[list[str]] = []
    assert LighthouseManager(popen=_fake_popen(calls)).ensure_started(tmp_path, "prod", "192.168.64.1") is True
    assert calls == []  # never spawned a second one


def test_lighthouse_ensure_started_false_when_network_not_bootstrapped_yet(tmp_path):
    """No VPC ever created for this env -> no lighthouse cert -> best-effort
    False, no spawn attempt (matches `_activate_nebula`'s own "log and move
    on" rule -- never a crash over an unmet precondition)."""
    calls: list[list[str]] = []
    assert LighthouseManager(popen=_fake_popen(calls)).ensure_started(tmp_path, "prod", "192.168.64.1") is False
    assert calls == []


def test_lighthouse_ensure_started_false_when_sudo_rejects_the_one_time_setup_is_missing(tmp_path):
    """The realistic failure mode: `sudo -n` exits immediately (no TTY, no
    NOPASSWD grant yet) -- no pidfile is ever written, so `ensure_started`
    must detect the FAST exit rather than blocking for the full poll
    window."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    calls: list[list[str]] = []
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    mgr = LighthouseManager(popen=_fake_popen(calls, pidfile=pidfile, exits_immediately=True), runner=runner)

    started = mgr.ensure_started(tmp_path, "prod", "192.168.64.1")
    assert started is False
    assert not pidfile.exists()


def test_lighthouse_ensure_stopped_kills_via_the_ctl_script_by_exact_pidfile(tmp_path):
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("424242")
    runner = FakeRunner()
    LighthouseManager(runner=runner).ensure_stopped(tmp_path, "prod")
    assert runner.calls == [["sudo", "-n", NEBULA_CTL_PATH, "stop", str(pidfile)]]
    assert not pidfile.exists()


def test_lighthouse_ensure_stopped_is_a_noop_without_a_pidfile(tmp_path):
    runner = FakeRunner()
    LighthouseManager(runner=runner).ensure_stopped(tmp_path, "prod")  # must not raise
    assert runner.calls == []


def test_mesh_state_reports_lighthouse_running(tmp_path):
    assert mesh_state(tmp_path, "prod").lighthouse_running is False
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(str(os.getpid()))
    assert mesh_state(tmp_path, "prod").lighthouse_running is True
