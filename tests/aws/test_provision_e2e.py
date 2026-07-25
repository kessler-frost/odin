"""M5 — a canvas AWS node provisions a real resource in a real backing."""
import time

import pytest
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.containers import own_containers

pytestmark = pytest.mark.integration

ENV = "default"  # this test posts to /apply with no env param
CANVAS = {"nodes": [{"type": "s3", "data": {"label": "uploads"}}], "edges": []}


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for name in own_containers(rt, ENV):
        rt.stop(name)


def test_s3_node_provisions_bucket(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ph = {r["id"]: r["phase"] for r in client.get("/world").json()["resources"]}
            if ph.get("uploads") == "healthy":
                break
            time.sleep(1)
        assert ph.get("uploads") == "healthy"
        s3 = BackingAws(runtime, ENV).client("s3")
        names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert "uploads" in names
