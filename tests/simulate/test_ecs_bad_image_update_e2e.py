"""Field-test 2 finding B1 (HIGH) -- the bad-image ECS *UPDATE*, end to end.

The v0.5.4 guard (`wait_for_steady_state` + a bounded `timeouts`) genuinely
covered CREATE (tests/simulate/test_ecs_bad_image_e2e.py) but was inert on
UPDATE: pointing a healthy 3-task service at `nginx:this-tag-does-not-exist-9z9z`
and re-Applying reported `status: applied / tf: ok`, **exit 0 in 2.3 seconds**,
while every healthy task was destroyed and the load balancer served 503. A CI
deploy of a typo'd tag scored a full outage as green -- the worst possible
failure shape.

Mechanism (see `ecsctl._on_current_revision`): terraform-provider-aws's
steady-state waiter keys on `len(deployments) == 1 && desiredCount ==
runningCount`, and a revision-blind `runningCount` still counted the STALE
tasks at the instant UpdateService returned, so the waiter declared the
deployment stable before the reconcile thread had even started.

This test is the real-container proof of the fix: a HEALTHY service, then one
re-Apply whose only change is a bogus image tag, must
 1. exit NON-ZERO (`applied_tf_failed`) within a BOUNDED time, and
 2. name the image problem -- the real `docker` pull error, carried on the
    deployment's `rolloutStateReason`, the service's ECS event, the task's
    `stoppedReason`, and the node's World verdict.

Deliberately no ALB here: `aws_lb` creation burns ~60s of provider pre-poll and
adds nothing to the claim under test.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

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

ENV = "ecs-bad-update-e2e"
NODE = "web"
GOOD_IMAGE = "nginx:alpine"
BAD_IMAGE = "nginx:this-tag-does-not-exist-9z9z"
COUNT = "2"


def _canvas(image: str) -> dict:
    return {
        "nodes": [{"id": "n1", "type": "ecs", "data": {
            "label": NODE, "image": image, "count": COUNT, "port": "80"}}],
        "edges": [],
    }


EMPTY_CANVAS = {"nodes": [], "edges": []}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _task_records(root: Path) -> list[dict]:
    path = root / ENV / "gateway" / "ecsctl.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [task for key, task in state.items() if key.startswith("task:")]


@pytest.fixture
def ecs_cleanup():
    """Container hygiene absolute: whatever the outcome, every task container
    this env ever created is force-removed by EXACT name."""
    yield
    ps = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    for container_id in (line for line in ps.stdout.splitlines() if line):
        _docker("rm", "-f", "-v", container_id)


def _ecs_client(client, app):
    gateway_port = client.get("/health").json()["gateway"]["port"]
    access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
    return boto3.client(
        "ecs", endpoint_url=f"http://127.0.0.1:{gateway_port}",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        config=Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 0}),
    )


def test_a_bad_image_update_fails_apply_and_names_the_image(tmp_path, monkeypatch, ecs_cleanup):
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
        # --- 1. a genuinely healthy service on a good image ------------------
        first = client.post("/apply-full", params={"env": ENV}, json=_canvas(GOOD_IMAGE))
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "applied", first.json()
        assert first.json()["tf"]["status"] == "ok", first.json()

        ecs = _ecs_client(client, app)
        healthy = ecs.describe_services(cluster="odin", services=[NODE])["services"][0]
        assert healthy["runningCount"] == int(COUNT), healthy
        assert healthy["deployments"][0]["rolloutState"] == "COMPLETED", healthy
        good_revision = healthy["taskDefinition"]

        # --- 2. the typo'd tag: one re-Apply, nothing else changed -----------
        update_start = time.monotonic()
        second = client.post("/apply-full", params={"env": ENV}, json=_canvas(BAD_IMAGE))
        update_elapsed = time.monotonic() - update_start
        print(f"\n[B1] bad-image UPDATE apply-full took {update_elapsed:.1f}s")

        assert second.status_code == 200, second.text
        body = second.json()
        # THE regression: this used to be `applied` / `tf: ok` in 2.3s.
        assert body["status"] == "applied_tf_failed", body
        assert body["tf"]["status"] == "failed", body
        assert body["tf"]["exit_code"] != 0, body
        assert update_elapsed < 240, f"update took {update_elapsed:.1f}s -- not bounded"
        tail = " ".join(body["tf"].get("tail", [])).lower()
        assert "timeout" in tail or "steady state" in tail or "stable" in tail, body["tf"]

        # --- 3. the failure NAMES the image problem -------------------------
        failed = ecs.describe_services(cluster="odin", services=[NODE])["services"][0]
        assert failed["taskDefinition"] != good_revision, "the update really was applied"
        assert failed["runningCount"] != failed["desiredCount"], "would read as steady state"
        (deployment,) = failed["deployments"]
        assert deployment["rolloutState"] == "FAILED", failed
        reason = deployment["rolloutStateReason"]
        assert BAD_IMAGE in reason, reason
        assert failed["events"], "a failed deployment posts a real ECS service event"
        assert BAD_IMAGE in failed["events"][0]["message"], failed["events"]

        stopped = [t for t in _task_records(store.root) if t["last_status"] == "STOPPED"]
        assert stopped, "the replacement tasks really did fail to start"
        assert any(BAD_IMAGE in (t.get("stopped_reason") or "") for t in stopped), stopped

        verdict = next(
            (r for r in client.get("/world", params={"env": ENV}).json()["resources"] if r["id"] == NODE), None,
        )
        assert verdict is not None and verdict["phase"] == "crashed", verdict
        assert BAD_IMAGE in (verdict.get("verdict") or ""), verdict

        # --- 4. teardown still completes promptly ---------------------------
        destroy_start = time.monotonic()
        third = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        destroy_elapsed = time.monotonic() - destroy_start
        print(f"[B1] teardown apply-full took {destroy_elapsed:.1f}s")
        assert third.status_code == 200, third.text
        assert third.json()["tf"] == {"status": "ok", "exit_code": 0}, third.json()
        assert destroy_elapsed < 120, f"destroy took {destroy_elapsed:.1f}s -- drain hang"

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"ECS task containers survived: {leftover.stdout}"
