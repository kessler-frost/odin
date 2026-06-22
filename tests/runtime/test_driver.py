"""S1.3 — ColimaRuntime conforms to the RuntimeDriver Protocol + phase mapping."""
from __future__ import annotations

from odin.runtime.colima import ColimaRuntime, _STATUS_TO_PHASE
from odin.runtime.driver import RuntimeDriver


def test_colima_satisfies_runtime_driver_protocol():
    rt: RuntimeDriver = ColimaRuntime()  # structural — must have all methods
    for method in ("ensure_host", "run_container", "stop", "facts", "stats"):
        assert callable(getattr(rt, method))


def test_status_to_phase_mapping():
    assert _STATUS_TO_PHASE["running"] == "starting"   # healthy is an assertion's call
    assert _STATUS_TO_PHASE["exited"] == "crashed"
    assert _STATUS_TO_PHASE["absent"] == "pending"
