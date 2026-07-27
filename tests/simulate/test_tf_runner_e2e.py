"""S2 -- real tofu apply/destroy through the REAL gateway onto REAL
substitute containers (CONTRACT ADDENDUM: "the research main.tf equivalents
for s3+sqs+sns+subscription+dynamodb ... /tf/apply -> all four exist ...
second /tf/apply or a `tofu plan` = zero drift ... /tf/destroy cleans").

Deliberately independent of the canvas /apply/reconciler path: env
"tf-e2e" never touches `/apply`, `/destroy`, or `/world`, so no Reconciler
is ever created for it and nothing gc()s the backing containers out from
under tofu -- proving Simulate's own provisioning boundary (runner.py's
module docstring: "Simulate provisions the SAME backings via tofu ->
gateway", independent of the canvas) actually holds for real.

Marked integration: needs Colima/Docker with the backing images pulled +
tofu (OpenTofu 1.12.3) on PATH. BackingAws's goaws config mount needs a
path under $HOME (Colima only shares that tree into the VM -- see
aws/backings.py's `ensure_backing` docstring), so this test uses the repo's
own `.odin/tf-e2e/` (like every other AWS-backing integration test),
cleaned up at the end.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack
from tests.containers import own_containers

pytestmark = pytest.mark.integration

ENV = "tf-e2e"
_ODIN_ENV_DIR = Path(".odin") / ENV  # BackingAws's default root -- see module docstring

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "s3", "data": {"label": "uploads"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
        {"id": "n3", "type": "sns", "data": {"label": "alerts"}},
        {"id": "n4", "type": "dynamodb", "data": {"label": "items", "hashKey": "pk"}},
    ],
    "edges": [{"source": "n3", "target": "n2"}],  # alerts -> jobs subscription
}

# The same canvas plus one bucket -- the "someone edited the canvas" half of
# the drift check (field test 3): `odin tf plan` must answer exit 2.
EDITED_CANVAS = {
    "nodes": [*CANVAS["nodes"], {"id": "n5", "type": "s3", "data": {"label": "reports"}}],
    "edges": CANVAS["edges"],
}


@pytest.fixture
async def runtime():
    rt = ColimaRuntime()
    yield rt
    for name in await own_containers(rt, ENV):
        await rt.stop(name)
    shutil.rmtree(_ODIN_ENV_DIR, ignore_errors=True)


async def test_tf_apply_zero_drift_destroy(tmp_path, runtime):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(runtime=runtime, store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]

        # Simulate's OWN provisioning boundary: boot the backing containers +
        # register them with the gateway directly -- no canvas /apply, no
        # Reconciler for this env at all.
        aws = BackingAws(runtime, ENV, gateway_port=gateway_port)
        for service in ("s3", "sqs", "sns", "dynamodb"):
            await aws.ensure_backing(service)
        app.state.gateway.update(ENV, {}, await aws.backing_ports())

        stack = canvas_to_stack(CANVAS, env=ENV)
        store.apply(stack)  # sets this env's HEAD Stack -- /tf/apply's generate_tf input

        start = time.monotonic()
        resp = client.post("/tf/apply", params={"env": ENV})
        wall_apply = time.monotonic() - start
        assert resp.status_code == 200, resp.json()
        assert resp.json()["status"] == "applied", resp.json()

        assert aws.exists("s3", "uploads")
        assert aws.exists("sqs", "jobs")
        assert aws.exists("sns", "alerts")
        assert aws.exists("dynamodb", "items")

        # zero drift: a plan against the just-applied state changes nothing
        # (the research bar -- "apply -> zero-drift plan -> destroy"). Through
        # `/tf/plan`, which is the field-test-3 fix: the hand-run `tofu plan`
        # this used to be is exactly the mistake that reached real AWS, because
        # main.tf (deliberately portable) carries no endpoint.
        plan_resp = client.post("/tf/plan", params={"env": ENV})
        assert plan_resp.status_code == 200, plan_resp.json()
        assert plan_resp.json()["status"] == "no_changes", plan_resp.json()
        assert plan_resp.json()["exit_code"] == 0, plan_resp.json()

        # ...and the same plan sees a canvas edit as changes: exit 2, tofu's
        # own `-detailed-exitcode` contract, straight through to the caller.
        store.apply(canvas_to_stack(EDITED_CANVAS, env=ENV))
        drift_resp = client.post("/tf/plan", params={"env": ENV})
        assert drift_resp.status_code == 200, drift_resp.json()
        assert drift_resp.json()["status"] == "changes", drift_resp.json()
        assert drift_resp.json()["exit_code"] == 2, drift_resp.json()
        # the plan itself created nothing -- it is a read
        assert not aws.exists("s3", "reports")

        store.apply(stack)  # back to the applied canvas, so destroy tears down what exists
        destroy_resp = client.post("/tf/destroy", params={"env": ENV})
        assert destroy_resp.status_code == 200, destroy_resp.json()
        assert destroy_resp.json()["status"] == "destroyed"

        assert not aws.exists("s3", "uploads")
        assert not aws.exists("sqs", "jobs")
        assert not aws.exists("sns", "alerts")
        assert not aws.exists("dynamodb", "items")

        await aws.gc(set())  # stop the backing containers -- nothing else owns them for this env

    assert await own_containers(runtime, ENV) == [], "every container this test made is gone"
    print(f"\ntofu apply wall time: {wall_apply:.2f}s")
