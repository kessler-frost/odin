"""Field test 3 (HIGH) -- the hole in v0.7.1's own flagship fix, end to end.

v0.7.1 made a bad-image ECS *UPDATE* fail the apply (`tf` times out on
`wait_for_steady_state`; `test_ecs_bad_image_update_e2e.py`). Field test 3
confirmed that holds -- and then found the way past it: `wait_for_steady_state`
is only ever evaluated when tofu actually **updates** the resource, so any apply
tofu sees as a **no-op** never checks anything. `odin apply` exited **0** with
`status: applied / tf: ok` while the service sat at **0 of 3 tasks** with every
task failing, three times consecutively.

This test reproduces the field's exact scenario with real containers, using the
field-verified trigger that makes the no-op DETERMINISTIC: a broken `${{...}}`
ref in the node's `env`. That map is injected at container launch and is
deliberately NOT in the task definition (`gateway/wiring.py`), so adding it
changes nothing tofu can diff -- every apply below step 1 is a guaranteed
`tf: ok` empty plan, which is precisely the path that had no guard at all.

  1. a healthy 3-task service                    -> applied,  exit 0, prompt
  2. + the broken ref, service still healthy     -> applied,  exit 0  (no false
                                                    positive on a no-op)
  3. the tasks die out of band; re-apply         -> FAILS, names `web`, 0/3,
                                                    and the broken ref itself
  4. drop the ref, re-apply                      -> applied,  exit 0, recovered

Step 3 is the bug. Step 2 and step 4 are the guardrails: a no-op apply on a
healthy service must stay green, and a service that CAN converge must be
converged by the Apply rather than failed.
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

ENV = "ecs-noop-outage-e2e"
NODE = "web"
IMAGE = "nginx:alpine"
COUNT = 3
BROKEN_REF = "${{ghost.ENDPOINT}}"

EMPTY_CANVAS: dict = {"nodes": [], "edges": []}


def _canvas(env_map: dict[str, str] | None = None) -> dict:
    data = {"label": NODE, "image": IMAGE, "count": str(COUNT), "port": "80"}
    return {
        "nodes": [{"id": "n1", "type": "ecs", "data": {**data, **({"env": env_map} if env_map else {})}}],
        "edges": [],
    }


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def _task_containers() -> list[str]:
    ps = _docker("ps", "-q", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    return [line for line in ps.stdout.splitlines() if line]


def _task_records(root: Path) -> list[dict]:
    path = root / ENV / "gateway" / "ecsctl.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [task for key, task in state.items() if key.startswith("task:")]


@pytest.fixture
def ecs_cleanup():
    """Container hygiene absolute, and scoped to THIS env's own name prefix --
    never a blanket `label=odin=1` sweep, which would rm containers this test
    did not create."""
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


def _apply(client, canvas: dict) -> tuple[dict, float]:
    started = time.monotonic()
    resp = client.post("/apply-full", params={"env": ENV}, json=canvas)
    assert resp.status_code == 200, resp.text
    return resp.json(), time.monotonic() - started


def test_a_noop_apply_cannot_report_success_while_the_service_is_at_zero(tmp_path, monkeypatch, ecs_cleanup):
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
        # --- 1. a genuinely healthy 3-task service --------------------------
        body, elapsed = _apply(client, _canvas())
        print(f"\n[FT3] fresh apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert body["tf"]["status"] == "ok", body
        assert "unhealthy" not in body, body

        ecs = _ecs_client(client, app)
        healthy = ecs.describe_services(cluster="odin", services=[NODE])["services"][0]
        assert healthy["runningCount"] == COUNT, healthy
        assert len(_task_containers()) == COUNT

        # --- 2. add the broken ref: a no-op apply on a HEALTHY service ------
        # The `env` map is not in the task definition, so tofu's plan is empty
        # and the running tasks are untouched. This must stay green.
        body, elapsed = _apply(client, _canvas({"NEED": BROKEN_REF}))
        print(f"[FT3] no-op apply on a healthy service took {elapsed:.1f}s")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body
        assert "unhealthy" not in body, body
        assert elapsed < 90, f"a healthy no-op apply must stay prompt, took {elapsed:.1f}s"

        # --- 3. the tasks die out of band, then THE BUG ---------------------
        # `docker stop` (not rm) is what a crashed container looks like to the
        # sweep. Every relaunch now fails on the unresolvable ref, so the
        # service cannot converge -- while tofu still has nothing to do.
        for container_id in _task_containers():
            _docker("stop", "-t", "1", container_id)

        body, elapsed = _apply(client, _canvas({"NEED": BROKEN_REF}))
        print(f"[FT3] no-op apply on a BROKEN service took {elapsed:.1f}s -> {body['status']}")
        # tofu genuinely had nothing to do -- which is exactly why it could
        # never have caught this, and why odin must.
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        # THE regression: this used to be `applied`, exit 0, at 0 of 3 tasks.
        assert body["status"] == "applied_services_unhealthy", body
        (short,) = body["unhealthy"]
        assert short["node"] == NODE, short
        assert (short["running"], short["desired"]) == (0, COUNT), short
        # ...and it names the real underlying reason, in the APPLY's own output
        # (field test 3: the cause was in /world and events but never here).
        assert "ghost" in (short["reason"] or ""), short
        assert elapsed < 180, f"the failure must be bounded, took {elapsed:.1f}s"

        stopped = [t for t in _task_records(store.root) if t["last_status"] == "STOPPED"]
        assert any("ghost" in (t.get("stopped_reason") or "") for t in stopped), stopped

        # --- 4. drop the ref: the Apply is the recovery, and it is quick -----
        body, elapsed = _apply(client, _canvas())
        print(f"[FT3] recovery apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert "unhealthy" not in body, body
        assert elapsed < 120, f"recovery must not crawl, took {elapsed:.1f}s"
        recovered = ecs.describe_services(cluster="odin", services=[NODE])["services"][0]
        assert recovered["runningCount"] == COUNT, recovered

        # --- 5. teardown still completes promptly ---------------------------
        body, elapsed = _apply(client, EMPTY_CANVAS)
        print(f"[FT3] teardown apply took {elapsed:.1f}s")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"ECS task containers survived: {leftover.stdout}"
