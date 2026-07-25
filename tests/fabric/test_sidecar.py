"""W2.6 piece 2 -- `fabric/sidecar.py`: a backing container joins the env's
Nebula mesh through a companion `nebula` container sharing its network
namespace. Unit-level: a fake runtime records the container spec, a fake
nebula-cert runner writes real files (the same trick tests/fabric/test_nebula.py
uses), so the whole join is asserted without Docker or a real daemon.

The REAL thing (an unmodified upstream image answering on the overlay, and a
compiled SG refusing a real Postgres connection) is proven by
tests/aws/test_backing_mesh_e2e.py and tests/simulate/test_sg_gates_backing_e2e.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from odin.fabric.models import FirewallRule, FirewallRules
from odin.fabric.nebula import FIREWALL_REVISION_KEY, LIGHTHOUSE_PORT, NebulaManager, ensure_network
from odin.fabric.sidecar import NEBULA_IMAGE, MeshSidecar
from odin.runtime.colima import ContainerSpec

ENV = "prod"
TARGET = "odin-rds-prod-db"
MEMBER = TARGET


@pytest.fixture(autouse=True)
def pinned_lighthouse_port(monkeypatch):
    """Port allocation PROBES the real machine (`fabric/nebula.py::_port_free`
    binds a UDP socket), so a rendered `static_host_map` asserted against a
    literal 4342 fails whenever any live env on this Mac happens to hold that
    port -- a unit test failing on the state of somebody else's running
    server. `ODIN_LIGHTHOUSE_PORT` is the seam that already exists for
    exactly this ("honoured verbatim: no probing, no reallocation"), so
    pinning it makes these assertions about the RENDERER again."""
    monkeypatch.setenv("ODIN_LIGHTHOUSE_PORT", str(LIGHTHOUSE_PORT))


class FakeRunner:
    """`nebula-cert` stand-in that WRITES the files the real CLI would (so
    `sign_cert`/`create_ca` produce readable cert material)."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None):
        self.calls.append(args)
        for flag in ("-out-crt", "-out-key"):
            if flag in args:
                path = Path(args[args.index(flag) + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"---{flag}---\n")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()


class FakeRuntime:
    """Models the ONE runtime behaviour the namespace check turns on: a
    container gets a FRESH id every `run_container`, and a container started
    with `--network container:<name>` records the target's id AS IT WAS THEN
    (exactly what docker's `HostConfig.NetworkMode` holds)."""

    def __init__(self, images=("odin-nebula:1.10.3",)):
        self.specs: list = []
        self.stopped: list[str] = []
        self.built: list[tuple[str, str]] = []
        self.signals: list[tuple[str, str]] = []
        self.probes: list[tuple[str, str]] = []
        self.probe_reply = ""
        self._images = set(images)
        self._running: set[str] = set()
        self._ids: dict[str, str] = {}
        self._netmode: dict[str, str] = {}
        self._next_id = 0

    def run_container(self, spec):
        self.specs.append(spec)
        self._running.add(spec.name)
        self._next_id += 1
        self._ids[spec.name] = f"{self._next_id:064x}"
        target = (spec.network or "").partition("container:")[2]
        self._netmode[spec.name] = f"container:{self._ids.get(target, target)}" if target else "default"

    def stop(self, name):
        self.stopped.append(name)
        self._running.discard(name)

    def status(self, name):
        return "running" if name in self._running else "absent"

    def container_id(self, name):
        return self._ids.get(name, "") if name in self._running else ""

    def network_mode(self, name):
        return self._netmode.get(name, "")

    def signal(self, name, sig):
        self.signals.append((name, sig))

    def exec_sh(self, name, script):
        self.probes.append((name, script))
        return self.probe_reply

    def start_target(self, name):
        """The BACKING container's own lifecycle (rds.py/backings.py own it,
        not the sidecar) -- called in the tests that care about which
        namespace the sidecar landed in."""
        self.run_container(ContainerSpec(name=name, image="postgres:16-alpine"))
        self.specs.pop()  # not a sidecar spec; keep `specs` about the sidecar

    def image_exists(self, tag):
        return tag in self._images

    def build(self, tag, dockerfile):
        self.built.append((tag, dockerfile))
        self._images.add(tag)


class FakeLighthouse:
    def __init__(self):
        self.started: list[tuple] = []

    def ensure_started(self, root, env, underlay):
        self.started.append((root, env, underlay))
        return True


def _sidecar(tmp_path, runtime=None, lighthouse=None, runner=None) -> MeshSidecar:
    return MeshSidecar(
        runtime or FakeRuntime(), ENV, tmp_path,
        lighthouse=lighthouse or FakeLighthouse(), runner=runner or FakeRunner(),
    )


DB_FIREWALL = FirewallRules(
    inbound=[FirewallRule(port="5432", proto="tcp", group="sg-web")],
    outbound=[FirewallRule(port="any", proto="any")],
)


# --- the "no mesh in this env" path: completely inert --------------------------


def test_disabled_without_a_nebula_network(tmp_path):
    """No VPC drawn -> no CA -> mesh membership is off, and `ensure` is a
    total no-op: the AWS-substitute path for a canvas with no network is
    byte-for-byte what it was."""
    runtime = FakeRuntime()
    mesh = _sidecar(tmp_path, runtime)
    assert mesh.enabled() is False
    assert mesh.ensure(TARGET, MEMBER) is None
    assert runtime.specs == [] and runtime.stopped == []


def test_env_var_disables_even_with_a_network(tmp_path, monkeypatch):
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    monkeypatch.setenv("ODIN_BACKING_MESH", "0")
    runtime = FakeRuntime()
    assert _sidecar(tmp_path, runtime).ensure(TARGET, MEMBER) is None
    assert runtime.specs == []


# --- joining ------------------------------------------------------------------


def test_ensure_joins_the_backing_via_a_shared_network_namespace(tmp_path):
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime, lighthouse = FakeRuntime(), FakeLighthouse()
    mesh = MeshSidecar(runtime, ENV, tmp_path, lighthouse=lighthouse, runner=FakeRunner())

    ip = mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert ip and ip.startswith("10.42.")

    (spec,) = runtime.specs
    assert spec.name == f"{TARGET}-mesh"
    assert spec.image == NEBULA_IMAGE
    # THE mechanism: no network of its own -- it lives in the backing's
    # namespace, so the tun device it creates is the backing's.
    assert spec.network == f"container:{TARGET}"
    assert spec.cap_add == ("NET_ADMIN",) and spec.devices == ("/dev/net/tun",)
    assert spec.ports == {}, "a namespace-sharing container can publish nothing"
    member_dir = tmp_path / ENV / "nebula" / "members" / MEMBER
    assert spec.volumes == {str(member_dir.resolve()): "/etc/nebula"}
    # The lighthouse is ensured by the BACKING too: a backing can be the
    # first mesh member in an env with no EC2 instance at all.
    assert lighthouse.started and lighthouse.started[0][1] == ENV


def test_ensure_writes_cert_material_and_a_gated_config(tmp_path):
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    mesh = _sidecar(tmp_path)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)

    member_dir = tmp_path / ENV / "nebula" / "members" / MEMBER
    for name in ("ca.crt", "host.crt", "host.key", "config.yml"):
        assert (member_dir / name).exists(), f"{name} never landed for the sidecar to read"
    config = yaml.safe_load((member_dir / "config.yml").read_text())
    # The drawn SG, compiled -- the whole point of putting the DB on the mesh.
    assert config["firewall"]["inbound"] == [{"port": "5432", "proto": "tcp", "group": "sg-web"}]
    assert config["pki"] == {"ca": "/etc/nebula/ca.crt", "cert": "/etc/nebula/host.crt", "key": "/etc/nebula/host.key"}
    # The host as seen from INSIDE a container, and the relay path every
    # NAT'd member needs (R5) -- a container and a VM have no direct path.
    assert config["static_host_map"] == {"10.42.0.1": ["192.168.5.2:4342"]}
    assert config["relay"] == {"use_relays": True, "relays": ["10.42.0.1"]}


def test_ensure_without_a_firewall_is_allow_all_not_deny_all(tmp_path):
    """A backing with no SG drawn must stay reachable on the overlay -- an
    empty inbound list would silently mean "deny everything"."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    mesh = _sidecar(tmp_path)
    mesh.ensure(TARGET, MEMBER)
    config = yaml.safe_load((tmp_path / ENV / "nebula" / "members" / MEMBER / "config.yml").read_text())
    assert config["firewall"]["inbound"] == [{"port": "any", "proto": "any", "host": "any"}]


def test_overlay_ip_is_sticky_and_readable_without_side_effects(tmp_path):
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    mesh = _sidecar(tmp_path)
    first = mesh.ensure(TARGET, MEMBER)
    assert mesh.overlay_ip(MEMBER) == first
    assert mesh.ensure(TARGET, MEMBER) == first  # re-join keeps the address

    fresh = MeshSidecar(FakeRuntime(), "never-joined", tmp_path, runner=FakeRunner())
    assert fresh.overlay_ip(MEMBER) is None
    assert not (tmp_path / "never-joined").exists(), "a read must not create the env's tree"


def test_builds_the_nebula_image_once_when_missing(tmp_path):
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime(images=())
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER)
    assert [tag for tag, _ in runtime.built] == [NEBULA_IMAGE]
    assert "nebula-linux-${a}.tar.gz" in runtime.built[0][1]


# --- idempotence + reacting to a canvas edit ----------------------------------


def test_unchanged_join_does_not_restart_the_sidecar(tmp_path):
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert len(runtime.specs) == 1, "an unchanged firewall must not churn the daemon every tick"


def test_changed_firewall_reloads_the_sidecar_in_place(tmp_path):
    """An edited SG must reach the running daemon -- but nebula RELOADS its
    firewall on SIGHUP, so replacing the container (what this used to do) tore
    down every tunnel the database held for a change nebula can take in place.

    The reload path also stopped being optional with field test 4: the
    membership revision lives in this same block and moves far more often than
    a rule does, so a restart here would be a restart on every membership
    change anywhere in the env."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    widened = FirewallRules(
        inbound=[*DB_FIREWALL.inbound, FirewallRule(port="5432", proto="tcp", group="sg-ops")],
        outbound=DB_FIREWALL.outbound,
    )
    mesh.ensure(TARGET, MEMBER, firewall=widened)
    assert len(runtime.specs) == 1, "a firewall edit must not replace the daemon"
    assert runtime.signals == [(f"{TARGET}-mesh", "HUP")]
    config = yaml.safe_load((tmp_path / ENV / "nebula" / "members" / MEMBER / "config.yml").read_text())
    assert len(config["firewall"]["inbound"]) == 2, "and the new rules are on disk for it to read"


def test_a_change_outside_the_firewall_still_replaces_the_daemon(tmp_path):
    """SIGHUP covers `firewall` and nothing else -- nebula deliberately does
    NOT reload `static_host_map`/`lighthouse`/`relay`. A moved lighthouse port
    is exactly that case, and answering it with a reload would be a lie: the
    sidecar would go on dialing a port nobody is listening on."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    manager = NebulaManager(tmp_path / ENV / "nebula")
    overlay = manager.load_overlay()
    overlay.lighthouse_port = overlay.lighthouse_port + 1
    manager.save_overlay(overlay)

    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert len(runtime.specs) == 2, "a moved lighthouse port must re-join, not SIGHUP"
    assert runtime.signals == []


def test_the_membership_revision_rides_in_the_firewall_block_and_reloads(tmp_path):
    """Field test 4. A revoke has to close flows the database ALREADY admitted,
    and nebula re-validates a conntrack entry only when its OWN ruleset version
    moves -- never when a peer's certificate changes. `revision` is what moves
    it: nebula ignores the key, so the rules are provably untouched, and a
    reload with it changed still counts as a new ruleset.

    All three properties in one test, because they are one contract: the value
    is rendered where nebula looks for firewall config, an unchanged one costs
    nothing, and a changed one is a SIGHUP rather than a restart."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL, revision="rev-one")
    config_path = tmp_path / ENV / "nebula" / "members" / MEMBER / "config.yml"
    assert yaml.safe_load(config_path.read_text())["firewall"][FIREWALL_REVISION_KEY] == "rev-one"

    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL, revision="rev-one")
    assert runtime.signals == [], "an unchanged roster must not signal anything"
    assert len(runtime.specs) == 1

    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL, revision="rev-two")
    assert runtime.signals == [(f"{TARGET}-mesh", "HUP")]
    assert len(runtime.specs) == 1, "a membership change must never restart the database's daemon"
    after = yaml.safe_load(config_path.read_text())["firewall"]
    assert after[FIREWALL_REVISION_KEY] == "rev-two"
    assert after["inbound"] == [{"port": "5432", "proto": "tcp", "group": "sg-web"}], (
        "and the RULES are untouched -- that is what makes this reload a no-op with a new version"
    )


def test_no_revision_renders_no_key_at_all(tmp_path):
    """An env that has never seen a membership change must render exactly the
    bytes it rendered before this existed -- otherwise shipping the fix would
    itself churn every sidecar on the first Apply after an upgrade."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    mesh = _sidecar(tmp_path, FakeRuntime())
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    config = yaml.safe_load((tmp_path / ENV / "nebula" / "members" / MEMBER / "config.yml").read_text())
    assert FIREWALL_REVISION_KEY not in config["firewall"]


def test_a_replaced_target_container_gets_a_fresh_sidecar(tmp_path):
    """Field test 2 HIGH-2: `docker kill` the Postgres, Apply, and the DB comes
    back as a NEW container -- while the sidecar stayed in the DEAD container's
    network namespace (`netmode=container:<old id>`), looping "sendto: network
    is unreachable" forever. The config was byte-identical and the sidecar
    container was still `running`, so the old idempotence test short-circuited
    and no Apply could ever heal it."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    runtime.start_target(TARGET)
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert mesh.attached_to(TARGET) is True

    runtime.stop(TARGET)          # `docker kill` + the converge's own cleanup
    runtime.start_target(TARGET)  # ...and the recreated database: a NEW id
    assert mesh.attached_to(TARGET) is False, "the sidecar is in the dead container's namespace"

    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert len(runtime.specs) == 2, "a replaced target must re-join, not short-circuit"
    assert runtime.specs[1].network == f"container:{TARGET}"
    assert mesh.attached_to(TARGET) is True


def test_an_unchanged_target_does_not_churn_the_sidecar(tmp_path):
    """The other half of the same fix -- one bug must not be traded for a
    restart-every-tick bug (`ensure_db_mesh` runs on every Apply, and the
    mesh health sweep re-checks this on its own cadence)."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    runtime.start_target(TARGET)
    mesh = _sidecar(tmp_path, runtime)
    for _ in range(4):
        mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert len(runtime.specs) == 1
    assert runtime.stopped.count(f"{TARGET}-mesh") == 1  # the one pre-start clear


def test_attached_to_is_unknown_rather_than_false_when_the_target_is_gone(tmp_path):
    """A target the runtime can't report on (never started, docker hiccup) is
    NOT evidence of a replacement -- churning a sidecar on no evidence would
    be a self-inflicted restart loop."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert mesh.attached_to(TARGET) is None
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert len(runtime.specs) == 1


def test_a_dead_sidecar_is_restarted_even_with_an_unchanged_config(tmp_path):
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())
    runtime = FakeRuntime()
    mesh = _sidecar(tmp_path, runtime)
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    runtime.stop(f"{TARGET}-mesh")  # crashed / host rebooted
    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert len(runtime.specs) == 2


def test_certs_are_signed_once_per_member(tmp_path):
    """`ensure` runs on every ensure_backing and every reconciler tick -- a
    `nebula-cert` subprocess each time would be pure waste."""
    runner = FakeRunner()
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=runner)
    mesh = _sidecar(tmp_path, runner=runner)
    mesh.ensure(TARGET, MEMBER)
    mesh.ensure(TARGET, MEMBER)
    assert len([c for c in runner.calls if "sign" in c and MEMBER in c]) == 1


def test_a_changed_membership_re_issues_the_cert_and_restarts_the_sidecar(tmp_path):
    """Field test 3 HIGH-1, the backing half. A member's groups are what a
    PEER's `group:` rule is matched against, and they live in its certificate
    -- so a group move is invisible in the config and used to be fixed forever
    at first join. Handled exactly as `InstanceVm._reissue_cert` handles an EC2
    VM's: re-sign, then restart, because a peer caches the identity of every
    tunnel it holds."""
    runner = FakeRunner()
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=runner)
    runtime = FakeRuntime()
    runtime.start_target(TARGET)
    mesh = _sidecar(tmp_path, runtime, runner=runner)
    mesh.ensure(TARGET, MEMBER, groups=("sg-db",), firewall=DB_FIREWALL)
    signed = [c for c in runner.calls if "sign" in c and MEMBER in c]
    assert signed[0][signed[0].index("-groups") + 1] == "backing,sg-db"

    mesh.ensure(TARGET, MEMBER, groups=("sg-internal",), firewall=DB_FIREWALL)

    signed = [c for c in runner.calls if "sign" in c and MEMBER in c]
    assert len(signed) == 2, "the moved member must be re-signed"
    assert signed[1][signed[1].index("-groups") + 1] == "backing,sg-internal"
    assert len(runtime.specs) == 2, "a re-issued cert only reaches the wire on a restart"
    # ...and the IP is untouched, so every published endpoint stays valid.
    assert signed[0][signed[0].index("-ip") + 1] == signed[1][signed[1].index("-ip") + 1]


def test_an_unchanged_membership_does_not_re_issue_or_restart(tmp_path):
    runner = FakeRunner()
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=runner)
    runtime = FakeRuntime()
    runtime.start_target(TARGET)
    mesh = _sidecar(tmp_path, runtime, runner=runner)
    for _ in range(3):
        mesh.ensure(TARGET, MEMBER, groups=("sg-db", "sg-ops"), firewall=DB_FIREWALL)
    # ...and neither does a reorder of the same groups.
    mesh.ensure(TARGET, MEMBER, groups=("sg-ops", "sg-db"), firewall=DB_FIREWALL)
    assert len([c for c in runner.calls if "sign" in c and MEMBER in c]) == 1
    assert len(runtime.specs) == 1


def test_a_restarted_sidecar_pokes_its_peers_to_re_handshake(tmp_path):
    """Field test 3 MED-2, measured on exactly this restart: ~10s where the
    overlay address did not answer because peers were still using the tunnel
    that had just died. The restarted member moves first."""
    runner = FakeRunner()
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=runner)
    runtime = FakeRuntime()
    runtime.start_target(TARGET)
    mesh = _sidecar(tmp_path, runtime, runner=runner)
    NebulaManager(tmp_path / ENV / "nebula", runner=runner).allocate_host_ip("i-web")

    mesh.ensure(TARGET, MEMBER, firewall=DB_FIREWALL)

    ((sidecar, script),) = runtime.probes
    assert sidecar == f"{TARGET}-mesh"
    assert "ping -c 1 -W 1 10.42.1." in script
    # Waits for the daemon's own tun before poking -- and for BOTH candidate
    # names. The device is `tun0` on every member odin actually runs (measured
    # inside a live EC2 VM and a live sidecar alike), so a check that only
    # looked for `nebula1` sat out its full timeout every single time.
    assert "/sys/class/net/tun*" in script and "/sys/class/net/nebula*" in script
    assert 'for d in ' in script and '[ -d "$d" ] && break 2' in script, (
        "every candidate must be tested: an unmatched glob stays literal, so testing only the "
        "first would let `nebula*` mask the `tun*` that is really there"
    )


def test_a_lone_member_has_nobody_to_poke(tmp_path):
    runner = FakeRunner()
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=runner)
    runtime = FakeRuntime()
    runtime.start_target(TARGET)
    _sidecar(tmp_path, runtime, runner=runner).ensure(TARGET, MEMBER, firewall=DB_FIREWALL)
    assert runtime.probes == []


# --- leaving + failure behavior ------------------------------------------------


def test_stop_takes_the_backing_off_the_mesh(tmp_path):
    runtime = FakeRuntime()
    _sidecar(tmp_path, runtime).stop(TARGET)
    assert runtime.stopped == [f"{TARGET}-mesh"]


def test_ensure_never_raises_when_the_runtime_explodes(tmp_path):
    """Mesh wiring must never fail an otherwise-healthy backing -- the host
    path still works (same rule as `InstanceVm._activate_nebula`)."""
    ensure_network(tmp_path, ENV, "127.0.0.1", runner=FakeRunner())

    class Exploding(FakeRuntime):
        def run_container(self, spec):
            raise RuntimeError("no /dev/net/tun")

    assert _sidecar(tmp_path, Exploding()).ensure(TARGET, MEMBER) is None
