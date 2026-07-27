"""`reconcile/mesh_health.py` -- the assertion odin was missing: is the
`*_MESH` endpoint it advertises actually alive?

Field test 2 found the same root cause twice (a sidecar left in a dead
container's namespace; an env whose lighthouse never started), and both
presented identically because EVERY probe in the product dialled the
published HOST port. These tests pin the three properties that matter: an env
with no mesh pays nothing, a dead overlay path takes the resource out of
`healthy` AND withholds the address, and the check runs on a cadence rather
than every tick.

The REAL thing -- a killed Postgres whose mesh endpoint works again from
inside a Lima VM after one Apply -- is proven by
tests/simulate/test_mesh_recovery_e2e.py.
"""
from __future__ import annotations

import pytest

from odin.fabric.nebula import LighthouseAbsence
from odin.reconcile import mesh_health
from odin.reconcile.assertions import MESH_PROBE_TOKEN, mesh_probe_script, mesh_ready_sync

ENV = "prod"
TARGET = "odin-rds-prod-db"
SIDECAR = f"{TARGET}-mesh"
MEMBER = TARGET
OVERLAY_IP = "10.42.1.3"
PORT = 5432

HOST_FACTS = {
    "DATABASE_URL": "postgresql://app:pw@host.docker.internal:33363/appdb",
    "endpoint": "host.docker.internal:33363",
    "DATABASE_URL_VM": "postgresql://app:pw@host.lima.internal:33363/appdb",
    "endpoint_vm": "host.lima.internal:33363",
}
MESH_KEYS = ("DATABASE_URL_MESH", "endpoint_mesh")
MESH_FACTS = {
    **HOST_FACTS,
    "DATABASE_URL_MESH": f"postgresql://app:pw@{OVERLAY_IP}:{PORT}/appdb",
    "endpoint_mesh": f"{OVERLAY_IP}:{PORT}",
}


class FakeRuntime:
    """Only the four calls a mesh check makes, each counted -- the cost of the
    sweep is a property these tests assert, not a detail."""

    def __init__(self, *, sidecar_running=True, target_id="a" * 64, netns=None, probe_ok=True):
        self.calls: list[tuple[str, str]] = []
        self._sidecar_running = sidecar_running
        self._target_id = target_id
        self._netns = netns if netns is not None else f"container:{target_id}"
        self.probe_ok = probe_ok

    def status(self, name):
        self.calls.append(("status", name))
        return "running" if self._sidecar_running else "absent"

    def container_id(self, name):
        self.calls.append(("container_id", name))
        return self._target_id

    def network_mode(self, name):
        self.calls.append(("network_mode", name))
        return self._netns

    def exec_sh(self, name, script):
        self.calls.append(("exec_sh", name))
        assert OVERLAY_IP in script and str(PORT) in script, "the probe must dial the OVERLAY address"
        return f"{MESH_PROBE_TOKEN}\n" if self.probe_ok else "nc: 10.42.1.3 (10.42.1.3:5432): Operation timed out"


class FakeLighthouse:
    """Stands in for `LighthouseManager`, including the DIAGNOSIS half: a gate
    that knows the lighthouse is down must also be able to say why, so
    `why_not_running` is part of the contract this fake has to honour.

    `absence` defaults to the real thing the REAL manager returns for the case
    these tests reproduce -- the process ran and is gone -- so a test that does
    not care still asserts against a shape production really produces."""

    def __init__(self, running=True, absence=None):
        self.running = running
        self.absence = absence or LighthouseAbsence(
            reason="the 'prod' nebula lighthouse is not running: its process is gone",
            fix="see /tmp/lighthouse.log for how it ended, then Apply again to restart it",
        )
        self.asked = 0

    def is_running(self, root, env):
        return self.running

    def why_not_running(self, root, env):
        self.asked += 1
        return self.absence


NO_NEBULA = LighthouseAbsence(
    reason="the 'prod' nebula lighthouse is not running: the `nebula` binary is not on odin's PATH, "
           "so it was never spawned",
    fix="run `brew install nebula`, then Apply again (`odin doctor` shows this as its `nebula` row)",
)


@pytest.fixture(autouse=True)
def _clean_cache():
    mesh_health.reset_cache()
    yield
    mesh_health.reset_cache()


def _meshed_env(tmp_path):
    """What `MeshSidecar.enabled()` reads: this env has a Nebula network."""
    nebula = tmp_path / ENV / "nebula"
    nebula.mkdir(parents=True, exist_ok=True)
    (nebula / "ca.crt").write_text("---ca---\n")
    return tmp_path


def _gate(tmp_path, entry, *, runtime=None, lighthouse=None, now=0.0, overlay_ip=OVERLAY_IP, **kwargs):
    return mesh_health.gate(
        entry, root=tmp_path, env=ENV, member=MEMBER, overlay_ip=overlay_ip, mesh_keys=MESH_KEYS,
        sidecar_target=kwargs.pop("sidecar_target", TARGET),
        sidecar_port=kwargs.pop("sidecar_port", PORT),
        runtime=runtime or FakeRuntime(), lighthouse=lighthouse or FakeLighthouse(), now=now, **kwargs,
    )


# --- an env with no mesh pays NOTHING -----------------------------------------


def test_no_mesh_fact_means_no_check_at_all(tmp_path):
    """The cost gate: a resource that publishes no overlay address is returned
    untouched without one call to the runtime."""
    runtime = FakeRuntime()
    entry = ("rds", "healthy", dict(HOST_FACTS), None)
    assert _gate(tmp_path, entry, runtime=runtime) == entry
    assert runtime.calls == []


def test_no_overlay_ip_means_no_check(tmp_path):
    runtime = FakeRuntime()
    entry = ("rds", "healthy", dict(MESH_FACTS), None)
    assert _gate(_meshed_env(tmp_path), entry, runtime=runtime, overlay_ip=None) == entry
    assert runtime.calls == []


def test_an_env_without_a_nebula_ca_is_never_faulted(tmp_path):
    """No CA -> no network was ever bootstrapped here, so there is nothing to
    verify and nothing to withhold (a unit-test-shaped record, and the
    reconciler's own first tick before ec2net has run)."""
    entry = ("rds", "healthy", dict(MESH_FACTS), None)
    assert _gate(tmp_path, entry)[1] == "healthy"


# --- a live overlay path is left alone -----------------------------------------


def test_a_live_mesh_endpoint_keeps_the_fact_and_the_phase(tmp_path):
    runtime = FakeRuntime(probe_ok=True)
    entry = ("rds", "healthy", dict(MESH_FACTS), None)
    assert _gate(_meshed_env(tmp_path), entry, runtime=runtime) == entry
    assert ("exec_sh", SIDECAR) in runtime.calls
    assert len(runtime.calls) == 4, "four calls per sweep: status, id, netmode, probe"


# --- each failure mode, with its own honest verdict ---------------------------


def test_a_dead_lighthouse_withholds_the_mesh_fact_and_ends_healthy(tmp_path):
    """Field test 2 B8: `nebula lighthouse exited immediately for env 'wa'` was
    a log line and nothing else -- /world kept publishing DATABASE_URL_MESH
    and every node stayed healthy."""
    lighthouse = FakeLighthouse(running=False)
    kind, phase, facts, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None), lighthouse=lighthouse,
    )
    assert phase == "crashed"
    assert "DATABASE_URL_MESH" not in facts and "endpoint_mesh" not in facts
    assert facts["DATABASE_URL"] == HOST_FACTS["DATABASE_URL"], "the host path is untouched"
    assert "lighthouse is not running" in verdict
    # The CAUSE is asked of the component that refuses to start it, never
    # guessed here -- and both halves of its answer reach the user.
    assert lighthouse.asked == 1, "the gate must ask WHY, not just whether"
    assert lighthouse.absence.reason in verdict
    assert lighthouse.absence.fix in verdict


def test_a_missing_nebula_binary_is_named_with_the_command_that_fixes_it(tmp_path):
    """THE residual. In the two most likely dead-lighthouse cases the old
    verdict sent the user to `{root}/{env}/nebula/lighthouse.log` -- a file
    `_start_locked` returns BEFORE creating -- and then told them to re-Apply,
    which re-enters the same `shutil.which` miss forever. Probed: three
    consecutive `ensure_started` calls on a PATH without `nebula`, all False,
    no log written, so the advice named a nonexistent file AND looped.

    `tests/fabric/test_nebula.py::test_why_not_running_*` pins the other end of
    this (that the REAL manager returns exactly this absence)."""
    _, _, _, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None),
        lighthouse=FakeLighthouse(running=False, absence=NO_NEBULA),
    )
    assert "brew install nebula" in verdict, "the one command that actually fixes it"
    assert "odin doctor" in verdict, "and where to confirm it before drawing anything"
    assert "lighthouse.log" not in verdict, "that file has never been written in this case"
    assert "re-Apply to re-join" not in verdict, "re-Applying re-enters the same PATH miss"


def test_every_failure_verdict_names_a_reachable_fix(tmp_path):
    """The SHAPE guard, not one instance of it (honesty rule 2): a future
    branch that reports a fault without a remedy must be visible here rather
    than inheriting whatever generic advice the wrapper last had. `gate` no
    longer supplies one, so a forgotten `fix` prints NO advice -- honest, but
    still a hole, and this is what finds it."""
    root = _meshed_env(tmp_path)
    faults = {
        "dead lighthouse": {"lighthouse": FakeLighthouse(running=False)},
        "stopped sidecar": {"runtime": FakeRuntime(sidecar_running=False)},
        "replaced namespace": {"runtime": FakeRuntime(netns="container:" + "b" * 64)},
        "unanswered overlay": {"runtime": FakeRuntime(probe_ok=False)},
    }
    for name, kwargs in faults.items():
        mesh_health.reset_cache()
        _, phase, _, verdict = _gate(root, ("rds", "healthy", dict(MESH_FACTS), None), **kwargs)
        assert phase == "crashed", name
        # Every remedy this module can offer, and nothing may fall through to none.
        assert any(hint in verdict for hint in ("re-Apply", "brew install", "Apply again", "odin doctor")), \
            f"{name}: a fault with no next move for the user: {verdict!r}"


def test_a_check_that_explodes_does_not_tell_the_user_to_re_apply(tmp_path):
    """A dead docker daemon is not repaired by an Apply -- and an Apply against
    a runtime that cannot answer `status` will not fix the mesh either. This
    branch is the one whose old inherited advice was actively wrong."""
    class Exploding(FakeRuntime):
        def status(self, name):
            raise RuntimeError("docker daemon is not responding")

    _, _, _, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None), runtime=Exploding(),
    )
    assert "odin doctor" in verdict
    assert "re-Apply" not in verdict, "there is nothing for an Apply to fix here"


def test_a_sidecar_in_a_replaced_namespace_is_reported(tmp_path):
    """HIGH-2 as the World sees it: the DB is up on its host port, the sidecar
    is running, and the overlay is dead."""
    runtime = FakeRuntime(netns="container:" + "b" * 64)
    _, phase, facts, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None), runtime=runtime,
    )
    assert phase == "crashed"
    assert "REPLACED container's network namespace" in verdict
    assert "endpoint_mesh" not in facts
    assert ("exec_sh", SIDECAR) not in runtime.calls, "no point probing a stranded namespace"


def test_a_stopped_sidecar_is_reported(tmp_path):
    _, phase, _, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None),
        runtime=FakeRuntime(sidecar_running=False),
    )
    assert phase == "crashed" and f"{SIDECAR} is not running" in verdict


def test_an_unanswered_overlay_address_carries_the_probes_real_words(tmp_path):
    _, phase, _, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None),
        runtime=FakeRuntime(probe_ok=False),
    )
    assert phase == "crashed"
    assert "Operation timed out" in verdict, "the real probe output, never invented text"
    assert "host port is unaffected" in verdict


def test_an_already_crashed_resource_keeps_its_own_verdict(tmp_path):
    """A failed database explains itself better than its mesh does -- but the
    dead address is still withheld."""
    _, phase, facts, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "crashed", dict(MESH_FACTS), "container exited (exit 137)"),
        runtime=FakeRuntime(probe_ok=False),
    )
    assert (phase, verdict) == ("crashed", "container exited (exit 137)")
    assert "endpoint_mesh" not in facts


# --- cadence: cheap on a 1s tick, and quick to forgive ------------------------


def test_a_passing_check_is_cached_for_the_sweep_window(tmp_path):
    root = _meshed_env(tmp_path)
    runtime = FakeRuntime(probe_ok=True)
    entry = ("rds", "healthy", dict(MESH_FACTS), None)
    for tick in range(30):  # 30 reconciler ticks at 1s
        _gate(root, entry, runtime=runtime, now=float(tick))
    assert len([c for c in runtime.calls if c[0] == "exec_sh"]) == 1
    _gate(root, entry, runtime=runtime, now=31.0)  # past ODIN_MESH_SWEEP_SECONDS
    assert len([c for c in runtime.calls if c[0] == "exec_sh"]) == 2


def test_a_failing_check_is_re_checked_sooner_so_a_recovery_is_not_stale(tmp_path):
    root = _meshed_env(tmp_path)
    runtime = FakeRuntime(probe_ok=False)
    entry = ("rds", "healthy", dict(MESH_FACTS), None)
    assert _gate(root, entry, runtime=runtime, now=0.0)[1] == "crashed"
    assert _gate(root, entry, runtime=runtime, now=3.0)[1] == "crashed"
    assert len([c for c in runtime.calls if c[0] == "exec_sh"]) == 1
    runtime.probe_ok = True  # the Apply re-joined it
    assert _gate(root, entry, runtime=runtime, now=6.0)[1] == "healthy"


def test_the_cadence_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_MESH_SWEEP_SECONDS", "120")
    root = _meshed_env(tmp_path)
    runtime = FakeRuntime(probe_ok=True)
    entry = ("rds", "healthy", dict(MESH_FACTS), None)
    _gate(root, entry, runtime=runtime, now=0.0)
    _gate(root, entry, runtime=runtime, now=100.0)
    assert len([c for c in runtime.calls if c[0] == "exec_sh"]) == 1


def test_a_check_that_explodes_is_a_verdict_not_a_crashed_tick(tmp_path):
    class Exploding(FakeRuntime):
        def status(self, name):
            raise RuntimeError("docker daemon is not responding")

    _, phase, facts, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None), runtime=Exploding(),
    )
    assert phase == "crashed" and "docker daemon is not responding" in verdict
    assert "endpoint_mesh" not in facts


# --- an EC2 member: nebula inside a VM, so only the lighthouse is affordable --


def test_a_vm_member_is_checked_against_the_lighthouse_only(tmp_path):
    """An EC2 node's nebula is a systemd unit inside a Lima VM -- there is no
    container to stand inside, and a `limactl shell` per VM per sweep is not a
    tick's price. The lighthouse half IS affordable and IS decisive: without
    it no peer can find or relay to the VM's overlay address."""
    runtime = FakeRuntime()
    entry = ("ec2", "healthy", {"PRIVATE_IP": "192.168.64.7", "MESH_IP": "10.42.1.2"}, None)
    kwargs = {"sidecar_target": None, "sidecar_port": None}
    ok = mesh_health.gate(
        entry, root=_meshed_env(tmp_path), env=ENV, member="i-abc", overlay_ip="10.42.1.2",
        mesh_keys=("MESH_IP",), runtime=runtime, lighthouse=FakeLighthouse(), now=0.0, **kwargs,
    )
    assert ok == entry and runtime.calls == [], "no docker call for a VM member"

    mesh_health.reset_cache()
    _, phase, facts, verdict = mesh_health.gate(
        entry, root=_meshed_env(tmp_path), env=ENV, member="i-abc", overlay_ip="10.42.1.2",
        mesh_keys=("MESH_IP",), runtime=runtime, lighthouse=FakeLighthouse(running=False), now=0.0, **kwargs,
    )
    assert phase == "crashed"
    assert facts == {"PRIVATE_IP": "192.168.64.7"}, "the VM's own address stays; the overlay claim goes"
    assert "10.42.1.2 is unreachable" in verdict and "lighthouse is not running" in verdict


# --- the probe itself ---------------------------------------------------------


def test_the_probe_is_bounded_twice_and_dials_the_overlay(tmp_path):
    script = mesh_probe_script(OVERLAY_IP, PORT, timeout=3.0)
    assert script.startswith("timeout 5 nc -vz -w 3 ")
    assert f"{OVERLAY_IP} {PORT}" in script
    assert "127.0.0.1" not in script and "host.docker.internal" not in script


def test_mesh_ready_reports_the_probes_own_failure_text():
    class Silent:
        def exec_sh(self, name, script):
            return ""

    result = mesh_ready_sync(Silent(), SIDECAR, OVERLAY_IP, PORT)
    assert result.ok is False
    assert f"{OVERLAY_IP}:{PORT}" in result.error
