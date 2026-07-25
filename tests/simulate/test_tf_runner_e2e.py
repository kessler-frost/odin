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

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.gateway.keys import OPERATOR_NODE_ID
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


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for name in own_containers(rt, ENV):
        rt.stop(name)
    shutil.rmtree(_ODIN_ENV_DIR, ignore_errors=True)


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
    }


def test_tf_apply_zero_drift_destroy(tmp_path, runtime):
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
            aws.ensure_backing(service)
        app.state.gateway.update(ENV, {}, aws.backing_ports())

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
        # (the research bar -- "apply -> zero-drift plan -> destroy").
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        workspace = store.root / ENV / "tf"
        plan = subprocess.run(
            ["tofu", "plan", "-input=false", "-no-color", "-detailed-exitcode"],
            cwd=workspace, env=_tf_env(gateway_port, access_key, secret_key),
            capture_output=True, text=True, timeout=120,
        )
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        destroy_resp = client.post("/tf/destroy", params={"env": ENV})
        assert destroy_resp.status_code == 200, destroy_resp.json()
        assert destroy_resp.json()["status"] == "destroyed"

        assert not aws.exists("s3", "uploads")
        assert not aws.exists("sqs", "jobs")
        assert not aws.exists("sns", "alerts")
        assert not aws.exists("dynamodb", "items")

        aws.gc(set())  # stop the backing containers -- nothing else owns them for this env

    assert own_containers(runtime, ENV) == [], "every container this test made is gone"
    print(f"\ntofu apply wall time: {wall_apply:.2f}s")
