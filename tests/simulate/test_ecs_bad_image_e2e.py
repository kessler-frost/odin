"""Field-test finding #3 (MED-HIGH) re-verify: a bad ECS container image must
make apply fail within a BOUNDED time with an honest error, and destroy must
complete promptly -- not silently 'succeed' with a service that never runs
(the actual current behavior on aws provider v5.100.0, whose service create
waiter keys on runningCount, not rolloutState) nor hang the pipeline.

The fix pairs an honest model (DescribeServices reflects a FAILED deployment
with the real task-failure reason) with `wait_for_steady_state` + a bounded
`timeouts.create` on the generated aws_ecs_service, so tofu apply genuinely
fails fast instead of returning a broken-but-'applied' service.
"""
from __future__ import annotations

import shutil
import subprocess
import time

import boto3
import pytest
from botocore.config import Config
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ecs-bad-image-e2e"
BAD_IMAGE = "no-such-registry.invalid/nope:latest"

CANVAS = {
    "nodes": [{"id": "n1", "type": "ecs", "data": {
        "label": "web", "image": BAD_IMAGE, "count": "1", "port": "80"}}],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def test_bad_ecs_image_fails_apply_fast_and_destroys_clean(tmp_path, monkeypatch):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        apply_start = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        apply_elapsed = time.monotonic() - apply_start
        print(f"\n[finding#3] bad-image apply-full took {apply_elapsed:.1f}s")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Bounded + honest FAILURE, not a silent success and not an 8-min hang.
        assert body["status"] == "applied_tf_failed", body
        assert body["tf"]["status"] == "failed", body
        assert apply_elapsed < 150, f"apply took {apply_elapsed:.1f}s -- not bounded"
        tail = " ".join(body["tf"].get("tail", [])).lower()
        assert "timeout" in tail or "steady state" in tail or "tfstable" in tail, body["tf"]

        # The model surfaces the honest reason (a FAILED deployment), not the
        # old hardcoded COMPLETED.
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        ecs = boto3.client(
            "ecs", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
            config=Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 0}),
        )
        described = ecs.describe_services(cluster="odin", services=["web"])["services"]
        if described:  # the service was created before the wait failed
            (deployment,) = described[0]["deployments"]
            assert deployment["rolloutState"] == "FAILED", described[0]
            assert described[0]["runningCount"] == 0

        # Destroy (empty canvas) completes promptly, not a drain hang.
        destroy_start = time.monotonic()
        resp2 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        destroy_elapsed = time.monotonic() - destroy_start
        print(f"[finding#3] teardown apply-full took {destroy_elapsed:.1f}s")
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["tf"] == {"status": "ok", "exit_code": 0}, resp2.json()
        assert destroy_elapsed < 90, f"destroy took {destroy_elapsed:.1f}s -- drain hang"

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"ECS task containers survived: {leftover.stdout}"
