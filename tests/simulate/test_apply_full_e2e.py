"""S5 -- POST /apply-full: the real end-to-end pipeline (canvas -> Stack ->
reconciler tick -> translate -> tofu apply through the gateway), against
REAL Colima backings, a REAL gateway, and REAL tofu. This is the UI's single
Apply button exercised for real -- the acceptance bar from task-s5-brief.md:
apply a mixed canvas (s3/sqs/sns+edge/dynamodb/rds) -> all five healthy,
tf.status ok, rds correctly flagged unsupported; re-apply -> zero drift;
empty canvas -> full teardown (the "no Destroy button" NORTHSTAR amendment
promise -- verified for real against the actual backing containers, not
just the World projection).

Marked integration: needs Colima/Docker with the backing images pulled +
tofu on PATH.
"""
from __future__ import annotations

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.containers import own_containers

pytestmark = pytest.mark.integration

ENV = "apply-full-e2e"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "s3", "data": {"label": "uploads"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
        {"id": "n3", "type": "sns", "data": {"label": "alerts"}},
        {"id": "n4", "type": "dynamodb", "data": {"label": "items", "hashKey": "pk"}},
        {"id": "n5", "type": "rds", "data": {"label": "db"}},
    ],
    "edges": [{"source": "n3", "target": "n2"}],  # alerts -> jobs subscription
}

EMPTY_CANVAS = {"nodes": [], "edges": []}

_FIVE = ("uploads", "jobs", "alerts", "items", "db")


@pytest.fixture
async def runtime():
    rt = ColimaRuntime()
    yield rt
    for name in await own_containers(rt, ENV):
        await rt.stop(name)


def _phases(client) -> dict:
    return {r["id"]: r["phase"] for r in client.get("/world", params={"env": ENV}).json()["resources"]}


def _wait(client, predicate, timeout=180.0, step=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        phases = _phases(client)
        if predicate(phases):
            return phases
        time.sleep(step)
    raise AssertionError(f"not met within {timeout}s (last={_phases(client)})")


async def test_apply_full_converges_reapplies_zero_drift_and_tears_down(tmp_path, runtime):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["tf"] is not None and body["tf"]["status"] == "ok", body
        assert body["unsupported"] == ["db (rds): Simulate v1 — stays on the reconciler path"], body

        _wait(client, lambda p: all(p.get(n) == "healthy" for n in _FIVE))

        # The physical backing resources exist for real (not just the World
        # projection) -- checked directly against RustFS/goaws/dynalite.
        aws = BackingAws(runtime, ENV, gateway_port=client.get("/health").json()["gateway"]["port"])
        assert aws.exists("s3", "uploads")
        assert aws.exists("sqs", "jobs")
        assert aws.exists("sns", "alerts")
        assert aws.exists("dynamodb", "items")

        # Re-apply the identical canvas: zero drift, still healthy, tofu still ok.
        start = time.monotonic()
        resp2 = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        wall = time.monotonic() - start
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        assert body2["status"] == "applied", body2
        assert body2["tf"] is not None and body2["tf"]["status"] == "ok", body2
        print(f"\nre-apply wall time: {wall:.2f}s")
        assert all(_phases(client).get(n) == "healthy" for n in _FIVE)

        # Empty canvas: the amendment's "no Destroy button" promise -- the
        # canvas alone is the source of truth, Apply on an empty canvas is
        # full teardown. LOAD-BEARING: verify this actually converges rather
        # than assuming the reconciler-only path leaves anything behind.
        #
        # tofu now ALSO runs here (V1 cross-layer finding, task-v1-report.md):
        # the gate in `create_apply_full_router` used to be
        # `resource_set(translated.files)` alone, which is empty for an empty
        # canvas -- so tofu was NEVER invoked on teardown, only skipped
        # straight to the reconciler's prune. That's harmless for THIS test's
        # kinds (s3/sqs/sns/dynamodb tear down for real via
        # BackingAws.deprovision wiping the whole backing container -- tofu's
        # own state file just went stale, silently), but for vpc/subnet/sg
        # (task V1) there IS no reconciler-driven teardown at all (plan.py
        # NoOps them forever), so tofu was the ONLY thing that could ever
        # remove them -- skipping it orphaned them permanently. The gate is
        # now `resource_set(translated.files) or
        # runner.status(env)["workspace_exists"]`, so tofu also runs (an
        # empty-project apply -> a destroy-everything plan against its own
        # prior state) whenever this env has ANY prior tofu-managed state,
        # ordered entirely inside the same reconciler.hold() as before this
        # env's reconciler prune step -- no double-teardown race.
        resp3 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert resp3.status_code == 200, resp3.text
        body3 = resp3.json()
        assert body3["status"] == "applied", body3
        assert body3["tf"] is not None and body3["tf"]["status"] == "ok", body3

        _wait(client, lambda p: not p)
        world = client.get("/world", params={"env": ENV}).json()
        assert world["resources"] == []

        # The physical resources are REALLY gone too, not just absent from World.
        assert not aws.exists("s3", "uploads")
        assert not aws.exists("sqs", "jobs")
        assert not aws.exists("sns", "alerts")
        assert not aws.exists("dynamodb", "items")

        await aws.gc(set())  # stop this env's backing containers -- nothing else owns them

    assert await own_containers(runtime, ENV) == [], "every container this test made is gone"
