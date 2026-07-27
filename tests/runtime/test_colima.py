"""S0.2 — ColimaRuntime runs real containers and reports their state.

Marked `integration`: needs a running Colima/Docker. Run with `-m integration`.
"""
from __future__ import annotations

import time

import pytest

from odin.api.logs import fetch_logs
from odin.gateway.stores import SynthStores
from odin.runtime.colima import ColimaRuntime, ContainerSpec, PortUnreadable
from odin.spec.models import ResourceDesired, Stack
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

NAME = "odin-test-nginx"


@pytest.fixture
async def runtime():
    rt = ColimaRuntime()
    await rt.stop(NAME)  # ensure clean
    yield rt
    await rt.stop(NAME)


async def _wait_running(rt: ColimaRuntime, name: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await rt.status(name) == "running":
            return
        time.sleep(0.5)
    raise AssertionError(f"{name} not running within {timeout}s (status={await rt.status(name)})")


async def test_run_status_port_stats_stop(runtime):
    handle = await runtime.run_container(
        ContainerSpec(name=NAME, image="nginx:alpine", ports={80: 0})
    )
    assert handle.name == NAME and handle.id
    await _wait_running(runtime, NAME)

    assert await runtime.status(NAME) == "running"
    assert await runtime.host_port(NAME, 80) > 0
    # Field test 5's facts audit: the three answers a REAL port read can give,
    # against a real daemon. A published port; an honest 0 for one the
    # container publishes nothing on; and a raise -- never a 0 -- when the
    # object cannot be read at all. That last case is what used to become
    # `http://host.docker.internal:0` in `world.json`, permanently.
    assert await runtime.host_port(NAME, 9999) == 0
    with pytest.raises(PortUnreadable):
        await runtime.host_port("odin-test-no-such-container", 80)
    stats = await runtime.stats(NAME)
    assert "cpu" in stats and "ram" in stats and stats["ram"] >= 0

    await runtime.stop(NAME)
    assert await runtime.status(NAME) == "absent"


# --- field test 2, HIGH-3: the stderr half of a REAL container's log ---------

PG = "odin-rds-logproof-appdb"


@pytest.fixture
async def postgres():
    """A REAL Postgres -- the field-verified stderr-only logger. Named exactly
    the way `aws/rds.py::PostgresRds` names an rds node's container, so
    `api/logs.py::fetch_logs` (the function both `odin logs` and the UI reach
    through `GET /logs`) resolves it for a node labelled `appdb` in env
    `logproof`, with no tofu/gateway involved."""
    rt = ColimaRuntime()
    await rt.stop(PG)
    yield rt
    await rt.stop(PG)


async def test_a_container_that_logs_only_to_stderr_still_yields_real_lines(postgres, tmp_path):
    await postgres.run_container(ContainerSpec(
        name=PG, image="postgres:16-alpine", ports={5432: 0},
        env={"POSTGRES_PASSWORD": "proof", "POSTGRES_DB": "appdb"},
    ))
    await _wait_running(postgres, PG)
    # A settled Postgres writes its server log (ready / checkpoint / errors) to
    # stderr; wait for it to get past initdb's stdout half.
    time.sleep(12)

    raw = await postgres._run(["docker", "logs", "--tail", "10", PG])
    assert raw.stderr.strip(), "the field premise: this container's log tail is on stderr"

    ten = postgres.logs(PG, tail=10)
    assert ten.strip(), "odin must not report an empty log for a container that logs on stderr"
    assert len(ten.splitlines()) == 10, "--tail 10 must mean 10 real lines, not 10-minus-stderr"

    store = SpecStore(tmp_path)
    store.apply(Stack(env="logproof", resources=(ResourceDesired(id="appdb", kind="rds"),)))
    result = fetch_logs(store, SynthStores(tmp_path), postgres, "logproof", "appdb", tail=10)
    assert result.found and result.running and result.sources == [PG]
    assert len(result.lines.splitlines()) == 10
    assert "database system is ready to accept connections" in result.lines
