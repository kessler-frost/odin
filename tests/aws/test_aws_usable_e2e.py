"""M5 — the embedded AWS services are usable BY app containers.

A batch container (amazon/aws-cli) creates an S3 bucket against the embedded
MiniStack (reached via host.docker.internal + injected AWS_ENDPOINT_URL/creds);
the host then sees the bucket the container created. Proves the AWS control
plane is real for workloads, not just for the host.

Marked `integration`: needs Colima/Docker. Run with `-m integration`.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from odin.aws.embed import ministack_boto_client
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

CANVAS = {
    "nodes": [
        {"type": "batch", "data": {
            "label": "mkbucket", "image": "amazon/aws-cli:latest",
            "command": ["s3", "mb", "s3://allfather-test"]}},
    ],
    "edges": [],
}


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for cid in rt.list_allfather():
        rt._docker("rm", "-f", cid, check=False)


def test_app_container_uses_embedded_s3(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path), embed=True, complete=False)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            phases = {r["id"]: r["phase"] for r in client.get("/world").json()["resources"]}
            if phases.get("mkbucket") in ("done", "error"):
                break
            time.sleep(2)
        assert phases.get("mkbucket") == "done"  # the container's `aws s3 mb` succeeded

        # the bucket the CONTAINER created is visible through the host's embed client
        buckets = [b["Name"] for b in ministack_boto_client("s3").list_buckets()["Buckets"]]
        assert "allfather-test" in buckets

        client.post("/destroy")
