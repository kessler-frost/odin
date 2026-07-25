"""W2.2 -- ONE real-container proof of drift detection, end to end.

Apply an ecs canvas through the real Apply route (`/apply-full`, real tofu,
real gateway, a real Colima container), then `docker rm -f` the task container
OUT OF BAND -- the exact thing odin used to report `healthy` forever, because
every TF-owned kind was observed by reading odin's own synth stores rather
than reality (the audit finding W2.2 exists to close).

Three claims, in order:
 1. /world flips the node to `crashed` with a verdict that NAMES the drift
    ("container odin-ecs-... removed outside odin — re-Apply to recreate")
    within the sweep window -- and the durable event log carries it too, so
    the UI badge/Logs tab and `odin world` all have something honest to show.
 2. Nothing auto-healed it: odin reported, tofu's state was never fought.
 3. re-Apply (the same canvas, the same button) converges it back to
    `healthy` with a REAL new container -- which is the whole reason
    `ecsctl.converge_services` exists (an ECS task is not a TF resource, so
    tofu's plan for an unchanged service is empty forever).

ECS on purpose: no Lima VM needed (the ec2 half of the sweep is unit-tested
against a fake `limactl list`), and it's the kind whose sweep also has to
write reality back into its own store. `translate` is monkeypatched to the
deterministic skeleton (no agent call), matching test_ecs_bad_image_e2e.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.tasks import container_name
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ecs-drift-e2e"
NODE = "web"
CANVAS = {
    "nodes": [{"id": "n1", "type": "ecs", "data": {
        "label": NODE, "image": "nginx:alpine", "count": "1", "port": "80"}}],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}
DRIFT_WINDOW = 90.0  # generous: one sweep is ~1 tick here, the rest is container work


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def _task_containers(root: Path) -> dict[str, str]:
    """`container name -> task lastStatus`, discovered from the persisted
    ecsctl state -- a task's container name embeds a random id minted at
    launch, so it can't be predicted (test_ecs_tf_e2e.py's own technique)."""
    path = root / ENV / "gateway" / "ecsctl.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return {
        container_name(ENV, task["task_id"], task["container_name"]): task["last_status"]
        for key, task in state.items() if key.startswith("task:")
    }


def _running_tasks(root: Path) -> list[str]:
    return [name for name, status in _task_containers(root).items() if status == "RUNNING"]


def _env_containers() -> list[str]:
    ps = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    return [line for line in ps.stdout.splitlines() if line]


@pytest.fixture
def ecs_cleanup():
    """Container hygiene absolute: every name seen during the test is
    force-removed by EXACT name on teardown, whatever the outcome."""
    names: set[str] = set()
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


def _observed(client, timeout: float, want_phase: str) -> dict | None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        world = client.get("/world", params={"env": ENV}).json()
        last = next((r for r in world["resources"] if r["id"] == NODE), None)
        if last is not None and last["phase"] == want_phase:
            return last
        time.sleep(1)
    return last


def test_removed_task_container_is_reported_as_drift_and_re_apply_converges(tmp_path, monkeypatch, ecs_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    # Sweep every tick (~1s) so this test isn't 10 ticks of waiting; the
    # default cadence itself is covered by tests/reconcile/test_drift.py.
    monkeypatch.setenv("ODIN_DRIFT_SWEEP_TICKS", "1")

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}, resp.json()
        ecs_cleanup.update(_task_containers(store.root))

        # The generated service carries wait_for_steady_state, so a successful
        # apply already means one REAL running container.
        healthy = _observed(client, 60.0, "healthy")
        assert healthy is not None and healthy["phase"] == "healthy", f"never healthy: {healthy}"
        (victim,) = _running_tasks(store.root)
        assert _docker("inspect", "-f", "{{.State.Status}}", victim).stdout.strip() == "running"

        # --- the drift: someone (or something) removes the container ---
        assert _docker("rm", "-f", "-v", victim).returncode == 0

        drifted = _observed(client, DRIFT_WINDOW, "crashed")
        assert drifted is not None and drifted["phase"] == "crashed", (
            f"a removed container must not keep reading healthy (last seen: {drifted})"
        )
        assert drifted["verdict"], "drift without a verdict is just a red badge with no answer"
        assert "removed outside odin" in drifted["verdict"], drifted["verdict"]
        assert "re-Apply" in drifted["verdict"], drifted["verdict"]
        assert victim in drifted["verdict"], "the verdict must name the container that vanished"

        # The durable event log carries the same transition (what the UI's WS
        # push and `odin events` project).
        events = client.get("/events", params={"env": ENV}).json()
        crashes = [
            e for e in events
            if e.get("type") == "world_delta" and e.get("resource_id") == NODE and e.get("phase") == "crashed"
        ]
        assert crashes, f"no crashed world_delta in the event log: {events[-5:]}"
        assert "removed outside odin" in (crashes[-1].get("verdict") or "")

        # Report, don't auto-heal: odin did NOT quietly recreate a container
        # behind tofu's back while we watched it report the drift. (The task
        # RECORD legitimately survives -- marked STOPPED with the drift reason,
        # which is exactly what makes the report honest and the re-Apply below
        # able to see the service is short a task.)
        assert _env_containers() == [], "nothing may be recreated behind tofu's back"
        assert _running_tasks(store.root) == []
        assert _task_containers(store.root)[victim] == "STOPPED"

        # --- the recovery: the same canvas, the same Apply button ---
        reapply = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert reapply.status_code == 200, reapply.text
        assert reapply.json()["tf"] == {"status": "ok", "exit_code": 0}, reapply.json()

        recovered = _observed(client, DRIFT_WINDOW, "healthy")
        ecs_cleanup.update(_task_containers(store.root))
        assert recovered is not None and recovered["phase"] == "healthy", f"re-Apply did not converge: {recovered}"
        assert recovered["verdict"] is None
        (replacement,) = _running_tasks(store.root)
        assert replacement != victim, "a genuinely NEW task, not the resurrected record"
        assert _docker("inspect", "-f", "{{.State.Status}}", replacement).stdout.strip() == "running"

        # Teardown through the real path (empty canvas == full teardown).
        destroy = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert destroy.status_code == 200, destroy.text
        assert destroy.json()["tf"] == {"status": "ok", "exit_code": 0}, destroy.json()

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"ECS task containers survived: {leftover.stdout}"
