"""W2.2's honesty fix -- the ONE real proof that "re-Apply to recreate" is TRUE
for ec2, with a real Lima VM.

Apply a vpc+subnet+ec2 canvas through the real Apply route (`/apply-full`, real
tofu, real gateway, a real VM), then `limactl delete --force` the VM OUT OF
BAND. Before the fix odin reported the drift honestly and then failed the user:
the sweep only changed the projected phase, so odin's gateway kept answering
DescribeInstances with `running` for a VM that no longer existed, tofu's plan
stayed empty, and re-Apply -- the very fix the verdict told the user to run --
did nothing at all. That is precisely the promise NORTHSTAR directive 5
forbids.

Four claims, in order:
 1. /world flips the node to `crashed` with a verdict NAMING the drift
    ("VM odin-ec2-... deleted outside odin — re-Apply to recreate"), and the
    durable event log carries it too.
 2. Nothing auto-healed it: no VM was recreated behind tofu's back while odin
    reported the drift.
 3. re-Apply (the same canvas, the same button) brings a GENUINELY NEW VM back,
    healthy -- the thing that was broken. This works because the sweep also
    corrected the record (`terminated` + a real StateReason), which is what
    makes terraform-provider-aws drop the instance from state and plan a
    create.
 4. An empty-canvas Apply tears it all down: zero VMs left.

VM hygiene ABSOLUTE (the V3 brief): `vm_cleanup` force-deletes VMs by EXACT
name in a finalizer, and every name it ever holds comes from `vm_name(env,
instance_id)` -- the same function that created them. A user's own Lima VM is
never a candidate. `translate` is monkeypatched to the deterministic skeleton
(no agent call), matching test_apply_full_ec2_sg_e2e.py.
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
from odin.compute.instances import vm_name
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ec2-drift-e2e"
NODE = "web"
CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 600, "height": 420},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 260, "height": 90},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        {"id": "n3", "type": "ec2", "position": {"x": 120, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": NODE, "subnet": "public"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}
# Generous: the sweep itself is one tick here, the rest is a real Lima boot.
DRIFT_WINDOW = 120.0


def _limactl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", *args], capture_output=True, text=True, timeout=120)


def _vm_names() -> list[str]:
    return _limactl("list", "-q").stdout.split()


def _instances(root: Path) -> list[dict]:
    path = root / ENV / "gateway" / "ec2compute.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [v for k, v in state.items() if k.startswith("instance:")]


def _live_instance(root: Path) -> dict:
    (instance,) = [i for i in _instances(root) if i["state_name"] != "terminated"]
    return instance


@pytest.fixture
def vm_cleanup():
    """Every VM name this test ever sees, force-deleted by EXACT name whatever
    the outcome -- a FAILURE must never leave a stray VM behind."""
    names: set[str] = set()
    yield names
    for name in names:
        _limactl("stop", "--force", name)
        _limactl("delete", "--force", name)


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


def test_a_vm_deleted_outside_odin_is_reported_and_re_apply_really_recreates_it(tmp_path, monkeypatch, vm_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"

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
        # Register for cleanup from disk BEFORE asserting the apply succeeded.
        vm_cleanup.update(vm_name(ENV, i["instance_id"]) for i in _instances(store.root))
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}, resp.json()

        victim = vm_name(ENV, _live_instance(store.root)["instance_id"])
        healthy = _observed(client, 60.0, "healthy")
        assert healthy is not None and healthy["phase"] == "healthy", f"never healthy: {healthy}"
        assert victim in _vm_names(), "the apply must have left a REAL Lima VM running"

        # --- the drift: someone (or something) deletes the VM ---
        _limactl("delete", "--force", victim)
        assert victim not in _vm_names(), "the premise: the VM is really gone"

        drifted = _observed(client, DRIFT_WINDOW, "crashed")
        assert drifted is not None and drifted["phase"] == "crashed", (
            f"a deleted VM must not keep reading healthy (last seen: {drifted})"
        )
        assert drifted["verdict"], "drift without a verdict is just a red badge with no answer"
        assert "deleted outside odin" in drifted["verdict"], drifted["verdict"]
        assert "re-Apply" in drifted["verdict"], drifted["verdict"]
        assert victim in drifted["verdict"], "the verdict must name the VM that vanished"

        events = client.get("/events", params={"env": ENV}).json()
        crashes = [
            e for e in events
            if e.get("type") == "world_delta" and e.get("resource_id") == NODE and e.get("phase") == "crashed"
        ]
        assert crashes, f"no crashed world_delta in the event log: {events[-5:]}"
        assert "deleted outside odin" in (crashes[-1].get("verdict") or "")

        # Report, don't auto-heal: odin corrected its RECORD (which is what
        # makes the advice true), but never booted a replacement VM behind
        # tofu's back.
        assert [n for n in _vm_names() if n.startswith(f"odin-ec2-{ENV}-")] == []
        (record,) = _instances(store.root)
        assert record["state_name"] == "terminated", "the record tofu refreshes must tell the truth"
        assert "deleted outside odin" in record["state_reason"]["message"]

        # --- the recovery: the same canvas, the same Apply button ---
        reapply = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        vm_cleanup.update(vm_name(ENV, i["instance_id"]) for i in _instances(store.root))
        assert reapply.status_code == 200, reapply.text
        assert reapply.json()["tf"] == {"status": "ok", "exit_code": 0}, reapply.json()

        replacement = vm_name(ENV, _live_instance(store.root)["instance_id"])
        assert replacement != victim, "a genuinely NEW VM, not a resurrected record"
        assert replacement in _vm_names(), f"re-Apply did not recreate the VM: {_vm_names()}"
        recovered = _observed(client, DRIFT_WINDOW, "healthy")
        assert recovered is not None and recovered["phase"] == "healthy", f"re-Apply did not converge: {recovered}"
        assert recovered["verdict"] is None

        # Teardown through the real path (empty canvas == full teardown).
        destroy = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert destroy.status_code == 200, destroy.text
        assert destroy.json()["tf"] == {"status": "ok", "exit_code": 0}, destroy.json()

    assert [n for n in _vm_names() if n.startswith(f"odin-ec2-{ENV}-")] == [], "VMs survived teardown"
