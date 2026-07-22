"""S4 mode (b) -- live-state import against the REAL gateway + backings
(CONTRACT: "boto3 creates a bucket out-of-band in RustFS -> live import
returns an s3 node"). Mirrors tests/simulate/test_tf_runner_e2e.py's own
provisioning boundary: this env never touches `/apply`/the Reconciler, so
nothing but this test owns the backing container it starts.

Marked integration: needs Colima/Docker with RustFS pulled + tofu on PATH.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "import-tf-e2e"
_ODIN_ENV_DIR = Path(".odin") / ENV


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for cid in rt.list_allfather():
        rt.stop(cid)
    shutil.rmtree(_ODIN_ENV_DIR, ignore_errors=True)


def test_live_import_of_an_out_of_band_bucket_returns_an_s3_node(tmp_path, runtime):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(runtime=runtime, store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]

        # Simulate's own provisioning boundary (test_tf_runner_e2e.py's
        # pattern): no canvas /apply, no Reconciler for this env at all.
        aws = BackingAws(runtime, ENV, gateway_port=gateway_port)
        aws.ensure_backing("s3")
        app.state.gateway.update(ENV, {}, aws.backing_ports())

        # "boto3 creates a bucket out-of-band" -- odin never authored this
        # bucket as a canvas node or a tofu resource.
        aws.client("s3").create_bucket(Bucket="uploads")
        assert aws.exists("s3", "uploads")

        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        result = client.post("/import-tf", params={"env": ENV}, json={
            "source": "live", "resources": [{"type": "s3", "id": "uploads"}],
        }).json()

        assert result["unsupported"] == []
        assert len(result["nodes"]) == 1
        node = result["nodes"][0]
        assert node["type"] == "s3"
        assert node["id"] == "uploads"

        aws.gc(set())  # stop the backing container -- nothing else owns it for this env

    assert runtime.list_allfather() == []
