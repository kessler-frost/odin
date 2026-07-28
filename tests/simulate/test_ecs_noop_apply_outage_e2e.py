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


def _post(client, canvas: dict):
    """The raw response, for the case where the apply is REFUSED."""
    return client.post("/apply-full", params={"env": ENV}, json=canvas)


BAD_IMAGE = "odin-nonexistent-image:definitely-not-here"


def _bad_image_canvas() -> dict:
    """A canvas the wiring guard has no objection to, whose tasks still cannot
    start. This is what replaces the broken `${{ghost.ENDPOINT}}` ref as the
    way to reach a zero-task service: the ref is now refused upfront (see the
    companion test), so it can no longer be used to GET to the state the no-op
    claim is about."""
    return {
        "nodes": [{"id": "n1", "type": "ecs",
                   "data": {"label": NODE, "image": BAD_IMAGE, "count": str(COUNT), "port": "80"}}],
        "edges": [],
    }


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

        # --- 2. a no-op apply on a HEALTHY service stays green ------------
        # Re-applying the IDENTICAL canvas: tofu has nothing to diff, so this
        # is the same empty-plan path step 4 exercises, and it must NOT invent
        # a failure.
        body, elapsed = _apply(client, _canvas())
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body
        assert "unhealthy" not in body, body
        assert elapsed < 90, f"a healthy no-op apply must stay prompt, took {elapsed:.1f}s"

        # --- 3. move to an image that cannot start ------------------------
        # This one IS a real update (the task definition changes), so tofu does
        # work and v0.7.1's `wait_for_steady_state` guard is what reports it.
        # It is not the claim under test -- it is how we REACH the state that
        # is, now that the broken `${{ghost.ENDPOINT}}` ref is refused upfront.
        body, _ = _apply(client, _bad_image_canvas())
        assert body["status"] != "applied", f"a service that cannot start is not 'applied': {body}"

        # The healthy OLD-revision tasks are still up: ECS will not tear down
        # working tasks for a revision whose replacements cannot start, which is
        # correct and is why a bad image alone does not reach zero. Field test 3
        # reached zero because the tasks died OUT OF BAND, so that is what this
        # reproduces -- with the bad image supplying the reason they cannot come
        # back, in place of the `${{ghost.ENDPOINT}}` ref that is now refused.
        for container_id in _task_containers():
            _docker("rm", "-f", "-v", container_id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and _task_containers():
            time.sleep(2)
        assert _task_containers() == [], "the out-of-band kill should have emptied the service"

        # --- 4. THE CLAIM: a no-op apply cannot report success at zero ----
        # The canvas is UNCHANGED from step 3, so tofu sees an empty plan and
        # does no work at all -- exactly the path field test 3 found had no
        # guard, where `odin apply` exited 0 with `status: applied / tf: ok`
        # three times running while the service sat at 0 of 3 tasks.
        body, elapsed = _apply(client, _bad_image_canvas())
        assert body["tf"] == {"status": "ok", "exit_code": 0}, f"step 4 must be a genuine no-op: {body}"
        assert body["status"] == "applied_services_unhealthy", body
        short = body["unhealthy"][0]
        assert short["node"] == NODE, short
        assert (short["running"], short["desired"]) == (0, COUNT), short
        assert short["reason"], "a failure with no reason is the bug this file exists for"
        assert elapsed < 180, f"the failure must be bounded, took {elapsed:.1f}s"

        # --- 5. a service that CAN converge is converged, not failed -------
        body, elapsed = _apply(client, _canvas())
        assert body["status"] == "applied", body
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert "unhealthy" not in body, body
        # Asserted on the REAL containers rather than DescribeServices: the
        # service is re-created by this apply, and reading its record straight
        # afterwards raced the re-registration (an empty `services` list, which
        # surfaces as an IndexError rather than as anything informative).
        # Counting the containers the tasks actually run in is both the
        # stronger claim and the stable one.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and len(_task_containers()) < COUNT:
            time.sleep(3)
        assert len(_task_containers()) == COUNT, f"recovery left {len(_task_containers())} of {COUNT} tasks up"

        # --- 6. teardown ---------------------------------------------------
        body, _ = _apply(client, EMPTY_CANVAS)
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"ECS task containers survived: {leftover.stdout}"


def test_a_broken_ref_is_refused_upfront_instead_of_applied(tmp_path, monkeypatch, ecs_cleanup):
    """The OTHER half of the split (owner decision, 2026-07-27).

    The no-op test above used to reach its zero-task state with a broken
    `${{ghost.ENDPOINT}}` ref, because that made the apply a guaranteed tofu
    no-op: the `env` map is injected at container launch and is not in the task
    definition, so adding it changes nothing tofu can diff.

    odin now refuses that apply BEFORE it runs -- which is strictly better
    behaviour, and it makes the old route to the state unreachable by design.
    So the refusal is asserted here as the guarantee it now is, and the no-op
    claim above keeps its own test via a route the guard permits. Neither was
    retired to make a red suite green.
    """
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    with TestClient(create_app(store=SpecStore(tmp_path))) as client:
        resp = _post(client, _canvas({"NEED": BROKEN_REF}))
        assert resp.status_code == 409, f"an unresolvable ref must be refused, got {resp.status_code}: {resp.text}"
        # And it must SAY what is wrong -- naming the ref, not just failing.
        assert "ghost" in resp.text, resp.text
        # Refused BEFORE anything ran: no Terraform, and nothing built.
        assert _task_containers() == [], "a refused apply must not have created containers"
