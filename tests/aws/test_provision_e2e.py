"""M5 — a canvas AWS node provisions a real resource in the embed."""
import time
import pytest
from fastapi.testclient import TestClient
from odin.aws.embed import ministack_boto_client
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration
CANVAS = {"nodes": [{"type": "s3", "data": {"label": "uploads"}}], "edges": []}


def test_s3_node_provisions_bucket(tmp_path):
    app = create_app(runtime=ColimaRuntime(), store=SpecStore(tmp_path), embed=True, complete=False)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ph = {r["id"]: r["phase"] for r in client.get("/world").json()["resources"]}
            if ph.get("uploads") == "healthy":
                break
            time.sleep(1)
        assert ph.get("uploads") == "healthy"
        names = [b["Name"] for b in ministack_boto_client("s3").list_buckets()["Buckets"]]
        assert "uploads" in names
