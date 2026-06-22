"""S0.2 — ColimaRuntime runs real containers and reports their state.

Marked `integration`: needs a running Colima/Docker. Run with `-m integration`.
"""
from __future__ import annotations

import time

import pytest

from odin.runtime.colima import ColimaRuntime, ContainerSpec

pytestmark = pytest.mark.integration

NAME = "allfather-test-nginx"


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    rt.stop(NAME)  # ensure clean
    yield rt
    rt.stop(NAME)


def _wait_running(rt: ColimaRuntime, name: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rt.status(name) == "running":
            return
        time.sleep(0.5)
    raise AssertionError(f"{name} not running within {timeout}s (status={rt.status(name)})")


def test_run_status_port_stats_stop(runtime):
    handle = runtime.run_container(
        ContainerSpec(name=NAME, image="nginx:alpine", ports={80: 0})
    )
    assert handle.name == NAME and handle.id
    _wait_running(runtime, NAME)

    assert runtime.status(NAME) == "running"
    assert runtime.host_port(NAME, 80) > 0
    stats = runtime.stats(NAME)
    assert "cpu" in stats and "ram" in stats and stats["ram"] >= 0

    runtime.stop(NAME)
    assert runtime.status(NAME) == "absent"
