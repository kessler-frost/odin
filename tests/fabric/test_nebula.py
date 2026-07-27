"""Nebula mesh fabric foundation: resolve drop-in parity + recovered primitives.

Cert ops use an injected fake runner, so no nebula-cert binary is required.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading

import pytest
import yaml

import odin.fabric.nebula as nebula_module
from odin.fabric.localhost import Unresolved
from odin.fabric.models import CertPaths, FirewallRule, FirewallRules, MeshNetwork
from odin.fabric.nebula import (
    DEFAULT_FIREWALL,
    FIREWALL_REVISION_KEY,
    LighthouseManager,
    NebulaFabric,
    NebulaManager,
    ensure_network,
    firewall_only_change,
    mesh_state,
    sg_rules_to_firewall,
    union_firewalls,
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
    # The lighthouse is dialed on ITS port (4342), not the members' 4242 --
    # Lima forwards a VM's own 4242 to the host's loopback and would otherwise
    # swallow every container→lighthouse packet (fabric/nebula.py::LIGHTHOUSE_PORT).
    assert member["static_host_map"] == {"10.42.0.1": ["192.168.1.10:4342"]}
    assert "tun" not in member  # a VM member keeps its real tun device
    # Members advertise ONLY the vzNAT subnet — a Lima VM's slirp address
    # (identical on every VM → self-handshake hairpin) and IPv6 ULA
    # (unsendable from an IPv4 listener) both poisoned discovery when
    # advertised (R4 live diagnosis: 100% overlay ping loss).
    assert member["lighthouse"]["local_allow_list"] == {"192.168.1.0/24": True, "::/0": False}
    assert member["preferred_ranges"] == ["192.168.1.0/24"]
    light = yaml.safe_load(mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=True))
    assert light["lighthouse"]["am_lighthouse"] is True and "static_host_map" not in light
    assert light["listen"] == {"host": "0.0.0.0", "port": 4342}, "the host lighthouse owns a port no guest listens on"
    assert "local_allow_list" not in light["lighthouse"]  # lighthouse advertises nothing anyway (no tun)
    assert "tun" not in light  # tun_disabled defaults False even for a lighthouse


def test_the_firewall_revision_is_rendered_inside_the_block_nebula_reloads(tmp_path):
    """Field test 4. The value has to sit INSIDE `firewall`, because that is
    the only section `reloadFirewall` hashes -- a key one level up would leave
    the daemon answering "No firewall config change detected" and the ruleset
    version parked, which is the whole thing that lets an already-open flow
    survive a revoke.

    Measured against the shipped nebula 1.10.3, one SIGHUP with only this key
    changed: `New firewall has been installed ... rulesVersion=1` with
    firewallHashes EQUAL to oldFirewallHashes -- same rules, new version."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    config = yaml.safe_load(mgr.generate_config(
        "10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, firewall_revision="roster-7",
    ))
    assert config["firewall"][FIREWALL_REVISION_KEY] == "roster-7"
    assert config["firewall"]["inbound"] == [{"port": "any", "proto": "any", "host": "any"}]


def test_no_revision_renders_the_config_byte_identically_to_before(tmp_path):
    """Shipping this must not itself churn a single member: an env that has
    never seen a membership change renders exactly the bytes it always did."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    plain = mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL)
    assert plain == mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, firewall_revision="")
    assert FIREWALL_REVISION_KEY not in plain


def test_firewall_only_change_sees_the_revision_as_reloadable(tmp_path):
    """The revision moves far more often than a rule does (any membership
    change anywhere in the env), so it MUST classify as reloadable -- if it
    read as "restart", every member would drop every tunnel it holds each time
    anyone's groups moved."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    before = mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, firewall_revision="one")
    after = mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, firewall_revision="two")
    assert firewall_only_change(before, after) is True
    # ...and a change nebula does NOT reload still reads as a restart.
    moved_port = mgr.generate_config(
        "10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, firewall_revision="one", lighthouse_port=4399,
    )
    assert firewall_only_change(before, moved_port) is False
    assert firewall_only_change(None, after) is False, "no evidence is not evidence of no change"


def test_generate_config_tun_disabled_for_the_rootless_lighthouse(tmp_path):
    """R4: only ever set for the HOST lighthouse -- the flag that lets it
    run unprivileged (verified empirically, see LighthouseManager's
    docstring)."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    config = yaml.safe_load(mgr.generate_config(
        "10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=True, tun_disabled=True,
    ))
    assert config["tun"] == {"disabled": True}


def test_generate_config_relay_enabled_lighthouse_offers_itself_as_a_relay(tmp_path):
    """R5: stock Lima `vz` gives every VM its own isolated address space --
    no VM-to-VM underlay path exists at all -- so the lighthouse offers
    itself as a relay (`am_relay: true`) rather than using one itself
    (empirically verified to work with `tun: disabled: true`, no root
    needed: a relay only ever forwards opaque encrypted UDP between two
    peers it already has live sessions with)."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    config = yaml.safe_load(mgr.generate_config(
        "10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=True, tun_disabled=True, relay_enabled=True,
    ))
    assert config["relay"] == {"am_relay": True, "use_relays": False}
    assert config["tun"] == {"disabled": True}  # relay-enabled never implies a real tun device


def test_generate_config_relay_enabled_member_uses_the_lighthouse_as_its_relay(tmp_path):
    """R5: every VM member routes to every OTHER VM through the lighthouse
    -- `lighthouse_ip` doubles as the relay address (one node, two roles)."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    config = yaml.safe_load(mgr.generate_config(
        "10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=False, relay_enabled=True,
    ))
    assert config["relay"] == {"use_relays": True, "relays": ["10.42.0.1"]}


def test_generate_config_relay_disabled_by_default(tmp_path):
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    lighthouse = yaml.safe_load(mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=True))
    member = yaml.safe_load(mgr.generate_config("10.42.0.1", "192.168.1.10", DEFAULT_FIREWALL, is_lighthouse=False))
    assert "relay" not in lighthouse and "relay" not in member


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


def test_sg_rules_to_firewall_icmp_has_no_ports(tmp_path):
    """AWS represents ICMP type/code via FromPort/ToPort (-1 = "all"), but
    nebula's `port` field is strictly an L4 TCP/UDP port -- it has no ICMP
    type/code concept. Feeding it AWS's literal "-1" made a real `nebula`
    refuse to start ("port appears to be a range but could not be parsed"),
    empirically confirmed while building the R4 rootless mesh proof (a ping
    is ICMP, so an ICMP SG rule that breaks the daemon breaks the proof)."""
    rules = sg_rules_to_firewall([
        {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "icmpv6", "FromPort": -1, "ToPort": -1, "IpRanges": [{"CidrIp": "::/0"}]},
    ])
    assert [(r.proto, r.port) for r in rules.inbound] == [("icmp", "any"), ("icmpv6", "any")]
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    config = yaml.safe_load(mgr.generate_config("10.42.0.1", "127.0.0.1", rules))
    assert config["firewall"]["inbound"][0] == {"port": "any", "proto": "icmp", "cidr": "0.0.0.0/0"}


# --- W2.6 piece 1: the union of a node's ASSIGNED security groups ---


def test_union_firewalls_merges_every_assigned_group():
    """AWS SG rules are permissive-only, so a node carrying several groups
    gets the UNION of their rules -- that's what an instance's assigned
    groups compile to (`ec2compute.py::_instance_firewall`)."""
    web = sg_rules_to_firewall([
        {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ])
    ops = sg_rules_to_firewall([
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]},
    ])
    merged = union_firewalls([web, ops])
    assert merged.inbound == [
        FirewallRule(port="8080", proto="tcp", cidr="0.0.0.0/0"),
        FirewallRule(port="22", proto="tcp", cidr="10.0.0.0/8"),
    ]
    assert merged.outbound == [FirewallRule(port="any", proto="any")]  # deduped, not doubled


def test_union_firewalls_dedupes_identical_rules():
    same = sg_rules_to_firewall([
        {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [{"GroupId": "sg-web"}]},
    ])
    merged = union_firewalls([same, same])
    assert merged.inbound == [FirewallRule(port="5432", proto="tcp", group="sg-web")]


def test_union_firewalls_of_nothing_is_deny_all_inbound():
    """An empty union is an empty inbound list -- exactly what a compiled SG
    with no ingress rules already means to nebula. Callers that want a
    fallback (the VPC default SG) must choose it explicitly."""
    assert union_firewalls([]) == FirewallRules(inbound=[], outbound=[])


# --- mesh read model (the UI hook) + lazy bootstrap ---

def test_ensure_network_bootstraps_and_is_idempotent(tmp_path):
    runner = FakeRunner()
    net = ensure_network(tmp_path, "prod", "192.168.1.10", runner=runner)
    assert net.lighthouse_underlay_ip == "192.168.1.10"
    ca_calls = sum(1 for c in runner.calls if "ca" in c)
    ensure_network(tmp_path, "prod", "192.168.1.10", runner=runner)  # again
    assert sum(1 for c in runner.calls if "ca" in c) == ca_calls     # CA not re-minted


def test_allocate_host_ip_persists_immediately(tmp_path):
    """The real bug the R4 two-VM mesh proof found: a bare
    `MeshNetwork.cert_ip()` mutates its object in memory only -- without
    `allocate_host_ip`'s own save, the allocation would live only in memory
    and vanish the moment the caller returns."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "192.168.1.10", runner=runner)
    mgr = NebulaManager(tmp_path / "prod" / "nebula", runner=runner)
    ip = mgr.allocate_host_ip("i-aaa")  # CIDR form, e.g. "10.42.1.1/16" -- what nebula-cert sign needs
    reloaded = NebulaManager(tmp_path / "prod" / "nebula").load_overlay()
    assert reloaded.subnets["hosts"].assignments["i-aaa"] == ip.split("/")[0]  # assignments store the bare IP


def test_allocate_host_ip_raises_if_network_never_bootstrapped(tmp_path):
    mgr = NebulaManager(tmp_path / "prod" / "nebula")
    with pytest.raises(RuntimeError, match="ensure_network"):
        mgr.allocate_host_ip("i-aaa")


def test_allocate_host_ip_is_atomic_under_concurrent_boots(tmp_path):
    """The exact race the two-VM proof test hit for real: two instances
    booting concurrently in the SAME env must not collide on the same IP or
    silently drop one another's allocation -- see `_lock_for_dir`'s
    module-level docstring in fabric/nebula.py."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "192.168.1.10", runner=runner)
    host_ids = [f"i-{n:03d}" for n in range(20)]
    results: dict[str, str] = {}
    errors: list[Exception] = []
    results_lock = threading.Lock()

    def worker(host_id: str) -> None:
        try:
            ip = NebulaManager(tmp_path / "prod" / "nebula").allocate_host_ip(host_id)
            with results_lock:
                results[host_id] = ip
        except Exception as exc:  # pragma: no cover -- surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(host_id,)) for host_id in host_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert set(results) == set(host_ids)                # nobody's allocation was lost
    assert len(set(results.values())) == len(host_ids)  # nobody collided on the same IP (CIDR form)

    reloaded = NebulaManager(tmp_path / "prod" / "nebula").load_overlay()
    bare_ips = {host_id: ip.split("/")[0] for host_id, ip in results.items()}
    assert reloaded.subnets["hosts"].assignments == bare_ips  # matches what's actually on disk


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


# --- R4: LighthouseManager -- rootless process supervision, fake popen/runner --


class FakePopen:
    """`returncode=None` while "running" (`poll()` -> None), matching a real
    `subprocess.Popen` -- `_fake_popen`'s `exits_immediately` flips it to
    simulate an immediate crash (bad cert/config, port in use)."""

    def __init__(self, pid: int = 424242, exits_immediately: bool = False) -> None:
        self.pid = pid
        self.returncode = 1 if exits_immediately else None

    def poll(self):
        return self.returncode


def _fake_popen(calls: list[list[str]], pid: int = 424242, exits_immediately: bool = False):
    """Stands in for `subprocess.Popen`. Real `LighthouseManager.ensure_
    started` owns this process directly (no sudo/exec chain) and writes
    `Popen.pid` to the pidfile itself -- the fake never touches the pidfile."""
    def popen(args, **kwargs):
        calls.append(args)
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
    # this is the normal case now: the lighthouse always runs as us.
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(str(os.getpid()))
    assert LighthouseManager().is_running(tmp_path, "prod") is True


def test_lighthouse_is_running_false_for_garbage_pidfile_content(tmp_path):
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("not-a-pid")
    assert LighthouseManager().is_running(tmp_path, "prod") is False


def test_lighthouse_ensure_started_spawns_nebula_directly_no_sudo(tmp_path):
    """R4: a plain `[nebula_bin, "-config", ...]` Popen -- no `sudo`, no ctl
    script -- and the config carries `tun: {disabled: true}` (the rootless
    flag). R5: also carries `relay: {am_relay: true}` -- stock Lima `vz` has
    no VM-to-VM underlay path, so the lighthouse always offers itself as a
    relay (still no tun device needed, see LighthouseManager's docstring)."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)  # bootstraps the lighthouse cert
    calls: list[list[str]] = []
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    mgr = LighthouseManager(popen=_fake_popen(calls), runner=runner, nebula_bin="nebula")

    started = mgr.ensure_started(tmp_path, "prod", "192.168.64.1")
    assert started is True
    config_path = tmp_path / "prod" / "nebula" / "lighthouse-config.yml"
    assert calls == [["nebula", "-config", str(config_path)]]
    assert pidfile.read_text() == "424242"  # Popen.pid, written by us -- no exec chain to trust
    config = yaml.safe_load(config_path.read_text())
    assert config["lighthouse"]["am_lighthouse"] is True
    assert config["tun"] == {"disabled": True}
    assert config["relay"] == {"am_relay": True, "use_relays": False}
    assert config["pki"]["cert"] == str(tmp_path / "prod" / "nebula" / "hosts" / "lighthouse.crt")


def test_lighthouse_ensure_started_is_idempotent_when_already_running(tmp_path):
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(str(os.getpid()))
    calls: list[list[str]] = []
    mgr = LighthouseManager(popen=_fake_popen(calls), nebula_bin="nebula")
    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is True
    assert calls == []  # never spawned a second one


def test_lighthouse_ensure_started_false_when_network_not_bootstrapped_yet(tmp_path):
    """No VPC ever created for this env -> no lighthouse cert -> best-effort
    False, no spawn attempt (matches `_activate_nebula`'s own "log and move
    on" rule -- never a crash over an unmet precondition)."""
    calls: list[list[str]] = []
    mgr = LighthouseManager(popen=_fake_popen(calls), nebula_bin="nebula")
    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is False
    assert calls == []


def test_lighthouse_ensure_started_false_when_nebula_not_on_path(tmp_path, monkeypatch):
    """No `nebula_bin` injected and nothing named `nebula` on PATH ->
    best-effort False, never a crash."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    calls: list[list[str]] = []
    mgr = LighthouseManager(popen=_fake_popen(calls), runner=runner, nebula_bin=None)

    monkeypatch.setattr(nebula_module.shutil, "which", lambda name: None)
    started = mgr.ensure_started(tmp_path, "prod", "192.168.64.1")
    assert started is False
    assert calls == []


def test_lighthouse_ensure_started_false_when_it_exits_immediately(tmp_path):
    """The realistic failure mode: a bad cert/config or a port already in
    use makes `nebula` exit right away -- `ensure_started` must detect that
    FAST exit rather than trusting `Popen.pid` blindly."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    calls: list[list[str]] = []
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    mgr = LighthouseManager(popen=_fake_popen(calls, exits_immediately=True), runner=runner, nebula_bin="nebula")

    started = mgr.ensure_started(tmp_path, "prod", "192.168.64.1")
    assert started is False
    assert not pidfile.exists()


# --- why is it not running? the ACTIONABLE half of `is_running() is False` ---
#
# The residual these pin: `reconcile/mesh_health.py` told every user of a dead
# lighthouse to read `{root}/{env}/nebula/lighthouse.log`, and in the two most
# likely causes `_start_locked` returns BEFORE that file is opened -- so the
# advice named a path that has never existed, while the string that really
# fixes it (`brew install nebula`) went only to the server log.
#
# PROBED against the real fabric before these were written (real nebula-cert
# 1.10.3, real `nebula` 1.10.3, nothing uninstalled -- `/opt/homebrew/bin` was
# simply left off PATH):
#   cert present + no `nebula` on PATH -> ensure_started False, NO log, and
#     three further calls identically False (the "re-Apply" loop)
#   cert absent  + `nebula` on PATH    -> ensure_started False, NO log
#   nebula back on PATH                -> ensure_started True, log appears,
#     is_running True  (so "brew install nebula, then Apply again" is verified,
#     not assumed)
#   after ensure_stopped / after a real spawn -> log EXISTS, so pointing at it
#     there is correct and must survive


def _empty_path(monkeypatch, tmp_path):
    """A REAL PATH miss, not a patched `shutil.which`: an empty directory IS
    the whole PATH, so `_resolve_bin` exercises the same lookup production
    does (honesty rule 1 -- a fabricated upstream signal proves the parser,
    not the integration). Nothing on the machine is touched."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_why_not_running_names_the_binary_not_a_log_that_was_never_written(tmp_path, monkeypatch):
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)  # cert IS present
    _empty_path(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    mgr = LighthouseManager(popen=_fake_popen(calls), runner=runner, nebula_bin=None)

    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is False
    assert calls == [], "nothing was spawned"
    log_path = tmp_path / "prod" / "nebula" / "lighthouse.log"
    assert not log_path.exists(), "the refusal happens BEFORE the log is opened"

    absence = mgr.why_not_running(tmp_path, "prod")
    assert "`nebula` binary is not on odin's PATH" in absence.reason
    assert "brew install nebula" in absence.fix
    assert "odin doctor" in absence.fix
    assert "lighthouse.log" not in absence.sentence(), "never name a file that does not exist"


def test_why_not_running_names_the_missing_certificate(tmp_path):
    """No VPC ever applied for this env: `_start_locked` refuses here too, also
    before any log is written."""
    mgr = LighthouseManager(popen=_fake_popen([]), nebula_bin="nebula")
    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is False
    assert not (tmp_path / "prod" / "nebula" / "lighthouse.log").exists()

    absence = mgr.why_not_running(tmp_path, "prod")
    assert "no signed lighthouse certificate" in absence.reason
    assert str(tmp_path / "prod" / "nebula" / "hosts" / "lighthouse.crt") in absence.reason
    assert "draw a VPC node and Apply" in absence.fix
    assert "lighthouse.log" not in absence.sentence()


def test_why_not_running_points_at_the_log_once_the_process_really_ran(tmp_path):
    """The half that was always RIGHT and must survive: a lighthouse that
    actually started has a log, and its contents are the diagnosis. Probed
    against the real binary -- the file is present after a real spawn, after
    `ensure_stopped`, and after an immediate exit."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    mgr = LighthouseManager(popen=_fake_popen([]), runner=runner, nebula_bin="nebula")
    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is True
    log_path = tmp_path / "prod" / "nebula" / "lighthouse.log"
    assert log_path.exists(), "a spawn opens the log -- this is the case the old advice fit"
    (tmp_path / "prod" / "nebula" / "lighthouse.pid").unlink()  # what ensure_stopped leaves

    absence = mgr.why_not_running(tmp_path, "prod")
    assert "its process is gone" in absence.reason
    assert str(log_path) in absence.fix
    assert "Apply again" in absence.fix


def test_why_not_running_says_so_when_nothing_ever_started_it(tmp_path):
    """Preconditions met, no log, no pid: nothing has joined this env's mesh
    yet. The total-function fall-through -- it may not inherit another cause's
    advice."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    absence = LighthouseManager(runner=runner, nebula_bin="nebula").why_not_running(tmp_path, "prod")
    assert "nothing has started it yet" in absence.reason
    assert "Apply the canvas" in absence.fix


def test_why_not_running_agrees_with_what_start_actually_refuses_on(tmp_path, monkeypatch):
    """THE anti-drift property, and the reason `_blocker` exists at all: the
    explanation a user reads must be the same check that stopped the start, not
    a second copy of it that can rot. For every precondition state, `_blocker`
    is non-None if and only if `ensure_started` refuses without spawning."""
    runner = FakeRunner()
    mgr = LighthouseManager(popen=_fake_popen([]), runner=runner, nebula_bin="nebula")

    # 1. no cert -> blocked
    assert mgr._blocker(tmp_path, "prod") is not None
    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is False

    # 2. cert, but no binary -> blocked
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    _empty_path(monkeypatch, tmp_path)
    nobin = LighthouseManager(popen=_fake_popen([]), runner=runner, nebula_bin=None)
    assert nobin._blocker(tmp_path, "prod") is not None
    assert nobin.ensure_started(tmp_path, "prod", "192.168.64.1") is False

    # 3. both present -> NOT blocked, and the start really proceeds
    calls: list[list[str]] = []
    ok = LighthouseManager(popen=_fake_popen(calls), runner=runner, nebula_bin="nebula")
    assert ok._blocker(tmp_path, "prod") is None
    assert ok.ensure_started(tmp_path, "prod", "192.168.64.1") is True
    assert calls, "an unblocked start spawns"


def test_every_absence_says_the_words_two_e2e_tests_read(tmp_path, monkeypatch):
    """`tests/aws/test_mesh_health_e2e.py` and
    `tests/simulate/test_mesh_recovery_e2e.py` both assert `"lighthouse is not
    running" in verdict`, and both are `-m integration` -- deselected from the
    unit run, so a reworded branch here would go green for a long time before
    anything noticed. This is the cheap unit-speed proxy for that.

    It is also the scannable prefix a user reads first, before the cause."""
    runner = FakeRunner()
    reasons = []

    # 1. no cert
    reasons.append(LighthouseManager(runner=runner, nebula_bin="nebula").why_not_running(tmp_path, "prod").reason)

    # 2. cert, no binary
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    _empty_path(monkeypatch, tmp_path)
    reasons.append(LighthouseManager(runner=runner, nebula_bin=None).why_not_running(tmp_path, "prod").reason)

    # 3. never started (preconditions met, no log)
    mgr = LighthouseManager(popen=_fake_popen([]), runner=runner, nebula_bin="nebula")
    reasons.append(mgr.why_not_running(tmp_path, "prod").reason)

    # 4. it ran and its process is gone
    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is True
    (tmp_path / "prod" / "nebula" / "lighthouse.pid").unlink()
    reasons.append(mgr.why_not_running(tmp_path, "prod").reason)

    assert len(reasons) == 4 and len(set(reasons)) == 4, "four DISTINCT causes, not one repeated"
    for reason in reasons:
        assert "lighthouse is not running" in reason, reason


def test_why_not_running_writes_nothing(tmp_path):
    """`is_running`'s own rule ("a GET must not mkdir") applies to the
    diagnosis too -- a status projection calls this on a cadence."""
    before = sorted(p.name for p in tmp_path.iterdir())
    LighthouseManager(nebula_bin="nebula").why_not_running(tmp_path, "prod")
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "prod").exists(), "no env directory was created by a read"


def test_lighthouse_ensure_stopped_sends_sigterm_to_the_exact_pid(tmp_path):
    """R4: no `sudo`, no ctl script -- we started this process ourselves, so
    a plain `os.kill(pid, SIGTERM)` is all it takes. Proven against a REAL
    process (not a fake) so the actual signal delivery is exercised."""
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
        pidfile.parent.mkdir(parents=True)
        pidfile.write_text(str(proc.pid))

        LighthouseManager().ensure_stopped(tmp_path, "prod")

        assert not pidfile.exists()
        assert proc.wait(timeout=5) != 0  # terminated by SIGTERM, not a clean exit
    finally:
        proc.poll() is None and proc.kill()


def test_lighthouse_ensure_stopped_ignores_an_already_dead_pid(tmp_path):
    pidfile = tmp_path / "prod" / "nebula" / "lighthouse.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("999999999")  # astronomically unlikely to be a live pid
    LighthouseManager().ensure_stopped(tmp_path, "prod")  # must not raise
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


# --- one lighthouse port per ENV, not per machine (field test 2 B8) ----------


@pytest.fixture
def free_ports(monkeypatch):
    """Which ports the machine says are bindable, under test control -- the
    real probe is a live UDP bind, which would make these assertions depend on
    whatever else happens to be running."""
    state = {"busy": set()}
    monkeypatch.setattr(nebula_module, "_port_free", lambda port: port not in state["busy"])
    return state


def test_ensure_network_allocates_and_persists_this_envs_own_port(tmp_path, free_ports):
    overlay = ensure_network(tmp_path, "prod", "1.2.3.4", runner=FakeRunner())
    assert overlay.lighthouse_port == nebula_module.LIGHTHOUSE_PORT
    stored = json.loads((tmp_path / "prod" / "nebula" / "overlay.json").read_text())
    assert stored["lighthouse_port"] == nebula_module.LIGHTHOUSE_PORT
    # Sticky: every member's static_host_map embeds it, so it must not move on
    # a re-apply.
    assert ensure_network(tmp_path, "prod", "1.2.3.4", runner=FakeRunner()).lighthouse_port == overlay.lighthouse_port


def test_two_envs_in_one_store_never_get_the_same_port(tmp_path, free_ports):
    """THE bug: `LIGHTHOUSE_PORT` was one machine-global constant, so the
    second env's `nebula` exited 1 on a port the first already held -- while
    odin kept publishing that env's SG-gated mesh addresses."""
    first = ensure_network(tmp_path, "prod", "1.2.3.4", runner=FakeRunner())
    second = ensure_network(tmp_path, "staging", "1.2.3.4", runner=FakeRunner())
    assert first.lighthouse_port != second.lighthouse_port
    assert second.lighthouse_port in nebula_module.LIGHTHOUSE_PORTS


def test_allocation_skips_a_port_something_else_is_holding(tmp_path, free_ports):
    free_ports["busy"] = {nebula_module.LIGHTHOUSE_PORT, nebula_module.LIGHTHOUSE_PORT + 1}
    assert ensure_network(tmp_path, "prod", "1.2.3.4", runner=FakeRunner()).lighthouse_port == (
        nebula_module.LIGHTHOUSE_PORT + 2
    )


def test_allocation_stays_in_the_reserved_range_no_guest_listens_on(tmp_path, free_ports):
    """Deliberately not the ephemeral range: a Lima guest's own outbound
    sockets land there, and Lima force-forwards a guest's listeners onto the
    host's loopback (the reason a lighthouse needed its own port at all)."""
    port = ensure_network(tmp_path, "prod", "1.2.3.4", runner=FakeRunner()).lighthouse_port
    assert port in nebula_module.LIGHTHOUSE_PORTS
    assert nebula_module.LIGHTHOUSE_PORTS.stop < 49152


def test_an_explicit_pin_is_honoured_verbatim(tmp_path, free_ports, monkeypatch):
    monkeypatch.setenv("ODIN_LIGHTHOUSE_PORT", "4999")
    free_ports["busy"] = {4999}  # busy or not, the user asked for this one
    assert ensure_network(tmp_path, "prod", "1.2.3.4", runner=FakeRunner()).lighthouse_port == 4999


def test_the_configs_agree_on_the_port_the_lighthouse_binds(tmp_path, free_ports):
    """A member dials the lighthouse at exactly the port the lighthouse binds
    -- one number, from the env's own overlay.json, on both sides."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    light = yaml.safe_load(mgr.generate_config(
        "10.42.0.1", "192.168.64.1", DEFAULT_FIREWALL, is_lighthouse=True, lighthouse_port=4357,
    ))
    member = yaml.safe_load(mgr.generate_config(
        "10.42.0.1", "192.168.64.1", DEFAULT_FIREWALL, lighthouse_port=4357,
    ))
    assert light["listen"]["port"] == 4357
    assert member["static_host_map"] == {"10.42.0.1": ["192.168.64.1:4357"]}
    assert member["listen"]["port"] == 4242, "members keep their own port"


def test_a_legacy_overlay_without_a_port_reads_as_the_historical_4342(tmp_path, free_ports):
    """An `overlay.json` written before per-env ports existed: members already
    in the field dial 4342, so that is what "no port recorded" has to mean."""
    mgr = NebulaManager(tmp_path / "nebula", runner=FakeRunner())
    config = yaml.safe_load(mgr.generate_config("10.42.0.1", "192.168.64.1", DEFAULT_FIREWALL, is_lighthouse=True))
    assert config["listen"]["port"] == nebula_module.LIGHTHOUSE_PORT


def test_a_lighthouse_whose_recorded_port_got_taken_moves_and_records_the_move(tmp_path, free_ports):
    """Recovery for a port that was free when the env was created and isn't
    now (another env's lighthouse, or the user's own process): take a fresh
    one and WRITE IT DOWN, so the sidecars that regenerate their configs
    follow us there instead of dialling a lighthouse that isn't listening."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    free_ports["busy"] = {nebula_module.LIGHTHOUSE_PORT}
    calls: list[list[str]] = []
    mgr = LighthouseManager(popen=_fake_popen(calls), runner=runner, nebula_bin="nebula")

    assert mgr.ensure_started(tmp_path, "prod", "192.168.64.1") is True
    config = yaml.safe_load((tmp_path / "prod" / "nebula" / "lighthouse-config.yml").read_text())
    moved = nebula_module.LIGHTHOUSE_PORT + 1
    assert config["listen"]["port"] == moved
    stored = json.loads((tmp_path / "prod" / "nebula" / "overlay.json").read_text())
    assert stored["lighthouse_port"] == moved


def test_two_concurrent_boots_in_one_env_start_exactly_one_lighthouse(tmp_path, free_ports):
    """Two VMs in one env boot on their own threads, and a backing can join at
    the same moment -- so `ensure_started` must be serialized per env. With a
    per-env port an unserialized loser would MOVE the env to a fresh port,
    spawn a SECOND lighthouse and overwrite the winner's pidfile, leaking the
    winner (caught in the field as a stray `nebula` process after a two-VM
    integration test)."""
    runner = FakeRunner()
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=runner)
    calls: list[list[str]] = []
    # A pid that is genuinely alive, so the second caller's own `is_running`
    # sees what it would see in production: the winner's live process.
    mgr = LighthouseManager(popen=_fake_popen(calls, pid=os.getpid()), runner=runner, nebula_bin="nebula")
    results: list[bool] = []

    def boot():
        results.append(mgr.ensure_started(tmp_path, "prod", "192.168.64.1"))

    threads = [threading.Thread(target=boot) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert results == [True, True]
    assert len(calls) == 1, f"exactly one nebula process, got {calls}"
    stored = json.loads((tmp_path / "prod" / "nebula" / "overlay.json").read_text())
    assert stored["lighthouse_port"] == nebula_module.LIGHTHOUSE_PORT, "and the port never moved"


def test_mesh_state_reports_which_port_this_env_owns(tmp_path, free_ports):
    ensure_network(tmp_path, "prod", "1.2.3.4", runner=FakeRunner())
    assert mesh_state(tmp_path, "prod").lighthouse_port == nebula_module.LIGHTHOUSE_PORT


# --- the leaked-lighthouse backstop (field test 3 HIGH-A) -------------------
#
# An env of a VPC + one S3 bucket -- no EC2 at all, so `ec2compute.
# _finish_terminate`'s "last VM leaves" stop never ran -- leaked one live
# lighthouse and one held UDP port on EVERY apply/destroy cycle: teardown
# deleted `.odin/<env>/nebula/` out from under a real process, taking the
# pidfile that was the only way to name it. Three orphans were measured on
# *:4343/4344/4345, one of them 8m20s old; ~100 cycles exhausts 4342-4441.


class FakePs:
    """A `ps -Ao pid=,args=` stand-in. Everything else the module might run
    answers empty, so a test only has to describe the process table."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def __call__(self, args):
        from odin.fabric.nebula import _Proc
        return _Proc(0, "\n".join(self.lines) if args[:2] == ["ps", "-Ao"] else "")


def _lighthouse_line(pid: int, config, binary: str = "/opt/homebrew/bin/nebula") -> str:
    return f"{pid} {binary} -config {config}"


def test_orphaned_lighthouses_finds_a_process_whose_env_was_destroyed(tmp_path):
    gone = tmp_path / "prod" / "nebula" / nebula_module.LIGHTHOUSE_CONFIG  # never created: the env was destroyed
    ps = FakePs([_lighthouse_line(4242, gone)])
    assert nebula_module.orphaned_lighthouses(tmp_path, runner=ps) == [(4242, gone)]


def test_a_live_envs_lighthouse_is_never_an_orphan(tmp_path):
    """The one thing this must never do: kill the lighthouse of an env that
    is still up. Its config file existing IS the evidence it is still wanted."""
    live = tmp_path / "prod" / "nebula" / nebula_module.LIGHTHOUSE_CONFIG
    live.parent.mkdir(parents=True)
    live.write_text("pki: {}\n")
    assert nebula_module.orphaned_lighthouses(tmp_path, runner=FakePs([_lighthouse_line(4242, live)])) == []


def test_another_stores_lighthouse_is_never_an_orphan(tmp_path):
    """A second odin on this Mac (its own `.odin`) owns its own processes --
    and its deleted config is none of our business."""
    other = tmp_path.parent / "somebody-elses" / "prod" / "nebula" / nebula_module.LIGHTHOUSE_CONFIG
    assert nebula_module.orphaned_lighthouses(tmp_path, runner=FakePs([_lighthouse_line(999, other)])) == []


def test_a_non_nebula_process_is_never_an_orphan(tmp_path):
    """`-config <a deleted path>` is an ordinary thing for a program to carry."""
    gone = tmp_path / "prod" / "nebula" / nebula_module.LIGHTHOUSE_CONFIG
    lines = [
        _lighthouse_line(1, gone, binary="/usr/local/bin/some-editor"),
        f"2 /opt/homebrew/bin/nebula -config {tmp_path / 'prod' / 'nebula' / 'config.yml'}",  # a MEMBER's config
        "3 /opt/homebrew/bin/nebula",  # no -config at all
    ]
    assert nebula_module.orphaned_lighthouses(tmp_path, runner=FakePs(lines)) == []


def test_reap_orphaned_lighthouses_signals_exactly_the_orphans(tmp_path, monkeypatch):
    gone = tmp_path / "gone" / "nebula" / nebula_module.LIGHTHOUSE_CONFIG
    live = tmp_path / "live" / "nebula" / nebula_module.LIGHTHOUSE_CONFIG
    live.parent.mkdir(parents=True)
    live.write_text("pki: {}\n")
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(nebula_module.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    ps = FakePs([_lighthouse_line(11, gone), _lighthouse_line(22, live)])
    assert nebula_module.reap_orphaned_lighthouses(tmp_path, runner=ps) == [11]
    assert signalled == [(11, nebula_module.signal.SIGTERM)]


def test_reaping_a_pid_that_already_died_is_not_an_error(tmp_path, monkeypatch):
    gone = tmp_path / "gone" / "nebula" / nebula_module.LIGHTHOUSE_CONFIG

    def exploding_kill(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(nebula_module.os, "kill", exploding_kill)
    assert nebula_module.reap_orphaned_lighthouses(tmp_path, runner=FakePs([_lighthouse_line(11, gone)])) == []
