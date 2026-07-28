"""One Apply must recreate a container that died — not two.

`converge_services`/`converge_functions`/`converge_db_instances` only ever act
on a record already marked failed, and the only things that write that mark are
`drift.sweep_compute` and `DriftSweeper`'s background cadence. The sweep inside
/apply-full used to run ONLY AFTER those converges, so every Apply converged
whatever the PREVIOUS sweep had marked. Recovery therefore took two Applies,
and the first one silently did nothing.

Measured end to end on a killed database (`test_mesh_recovery_e2e.py`, 399s,
real containers):

    after docker kill:        phase=crashed
                              verdict='container odin-rds-<env>-db is not
                                       running (exit 137) — re-Apply to recreate'
    the Apply that followed:  no convergence logged at all
    300s later:               still crashed

The verdict told the user to do the exact thing they had just done -- this
repo's false-status shape (honesty rule 2) sitting on top of honesty rule 1b, a
recovery gated on a signal produced by a different loop's cadence.

The fix is ORDERING, not new machinery: sweep before converging as well as
after. This file pins the order, because the order IS the fix and it is
invisible to any test that only checks the end state of a healthy env.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import odin.server as server_mod
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime

pytestmark = pytest.mark.anyio

CONVERGES = ("converge:ecs", "converge:lambda", "converge:rds")


def _record_call_order(monkeypatch) -> list[str]:
    """Spy on the sweep and each converge, preserving real behaviour."""
    order: list[str] = []

    real_sweep = server_mod.drift.sweep_compute

    async def sweep(stores, env, containers=None):
        order.append("sweep")
        return await real_sweep(stores, env, containers)

    real_services = server_mod.ecsctl.converge_services

    async def services(*args, **kwargs):
        order.append("converge:ecs")
        return await real_services(*args, **kwargs)

    real_functions = server_mod.lambdactl.converge_functions

    def functions(*args, **kwargs):
        order.append("converge:lambda")
        return real_functions(*args, **kwargs)

    real_dbs = server_mod.rdsctl.converge_db_instances

    def dbs(*args, **kwargs):
        order.append("converge:rds")
        return real_dbs(*args, **kwargs)

    monkeypatch.setattr(server_mod.drift, "sweep_compute", sweep)
    monkeypatch.setattr(server_mod.ecsctl, "converge_services", services)
    monkeypatch.setattr(server_mod.lambdactl, "converge_functions", functions)
    monkeypatch.setattr(server_mod.rdsctl, "converge_db_instances", dbs)
    return order


def _apply(tmp_path, monkeypatch, env: str) -> list[str]:
    order = _record_call_order(monkeypatch)
    app = server_mod.create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False,
    )
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": env}, json={"nodes": [], "edges": []})
        assert resp.status_code == 200, resp.text
    return order


async def test_the_sweep_runs_before_the_converges_so_one_apply_recovers(tmp_path, monkeypatch):
    order = _apply(tmp_path, monkeypatch, "order")
    assert "sweep" in order, f"an Apply must establish liveness itself: {order}"
    first_sweep = order.index("sweep")
    for converge in CONVERGES:
        assert converge in order, f"{converge} never ran: {order}"
        assert first_sweep < order.index(converge), (
            f"{converge} ran before any sweep, so it could only converge what a PREVIOUS "
            f"Apply's sweep had marked — recovery would need a second Apply: {order}"
        )


async def test_a_sweep_still_runs_after_the_converges_to_verify_them(tmp_path, monkeypatch):
    """The pre-sweep enables recovery; the post-sweep is what makes the apply's
    own success claim honest (field test 5: four consecutive applies reported
    `applied`/exit 0 with the container already gone). Losing either is a
    regression, so both are pinned."""
    order = _apply(tmp_path, monkeypatch, "order2")
    last_converge = max(order.index(c) for c in CONVERGES if c in order)
    assert any(i > last_converge for i, name in enumerate(order) if name == "sweep"), (
        f"no sweep after the converges — the apply would report on stale records: {order}"
    )
