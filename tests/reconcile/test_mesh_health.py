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
    def __init__(self, running=True):
        self.running = running

    def is_running(self, root, env):
        return self.running


@pytest.fixture(autouse=True)
def _clean_cache():
    mesh_health.reset_cache()
    yield
    mesh_health.reset_cache()


def _meshed_env(tmp_path):
    """What `MeshSidecar.enabled()` reads: this env has a Nebula network."""
    nebula = tmp_path / ENV / "nebula"
    nebula.mkdir(parents=True)
    (nebula / "ca.crt").write_text("---ca---\n")
    return tmp_path


def _gate(tmp_path, entry, *, runtime=None, lighthouse=None, now=0.0, overlay_ip=OVERLAY_IP):
    return mesh_health.gate(
        entry, root=tmp_path, env=ENV, target=TARGET, member=MEMBER,
        overlay_ip=overlay_ip, port=PORT, mesh_keys=MESH_KEYS,
        runtime=runtime or FakeRuntime(), lighthouse=lighthouse or FakeLighthouse(), now=now,
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
    kind, phase, facts, verdict = _gate(
        _meshed_env(tmp_path), ("rds", "healthy", dict(MESH_FACTS), None),
        lighthouse=FakeLighthouse(running=False),
    )
    assert phase == "crashed"
    assert "DATABASE_URL_MESH" not in facts and "endpoint_mesh" not in facts
    assert facts["DATABASE_URL"] == HOST_FACTS["DATABASE_URL"], "the host path is untouched"
    assert "lighthouse is not running" in verdict and "lighthouse.log" in verdict


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
