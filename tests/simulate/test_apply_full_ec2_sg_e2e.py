"""Field-test finding #2 (HIGH) re-verify: an EC2 instance placed in a NAMED
security group must re-apply idempotently. The field test drew a
vpc+subnet+sg+ec2(in the sg) canvas; the first apply booted a real Lima VM, but
the model ignored the instance's SecurityGroupIds, so every subsequent `tofu
plan` saw a changed group set with no primary network interface to modify and
failed `applied_tf_failed` -- the iterate loop that is the whole point of local
dev was disqualifying.

This boots a REAL Lima VM (needs limactl), then proves: `tofu plan` after apply
is ZERO drift, and a second `/apply-full` of the identical canvas is a clean
`applied` (exit 0), not `applied_tf_failed`.

VM hygiene ABSOLUTE (the V3 brief): `vm_cleanup` force-deletes the exact VM name
in a finalizer, so a FAILURE never leaves a stray VM even if teardown never runs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.instances import vm_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ec2-sg-reapply-e2e"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 600, "height": 420},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 260, "height": 90},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 260}, "size": {"width": 260, "height": 60},
         "data": {"label": "web-sg", "ingressRules": "tcp:22:0.0.0.0/0\ntcp:80:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n4", "type": "ec2", "position": {"x": 120, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web", "subnet": "public", "securityGroups": "web-sg"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


def _ec2compute_state(root, env: str) -> dict:
    path = root / env / "gateway" / "ec2compute.json"
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def vm_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=30)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=30)


def test_ec2_in_a_security_group_re_applies_with_zero_drift(tmp_path, vm_cleanup, monkeypatch):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"

    # Deterministic translate (skip the slow, network-dependent SDK refine pass
    # -- same stub the lambda canvas e2e uses; the deterministic HCL is exactly
    # what carries every scenario in the field test anyway).
    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        # Register the VM for cleanup from disk BEFORE asserting apply succeeded.
        for instance in [v for k, v in _ec2compute_state(store.root, ENV).items() if k.startswith("instance:")]:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["tf"] is not None and body["tf"]["status"] == "ok", body
        assert body["unsupported"] == [], body

        (instance,) = [v for k, v in _ec2compute_state(store.root, ENV).items() if k.startswith("instance:")]
        assert instance["state_name"] == "running", instance
        assert instance["security_group_ids"], "the instance must carry its assigned SG(s)"

        # THE core proof: apply -> plan changes NOTHING (was "2 to change" +
        # failure before the fix).
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        workspace = store.root / ENV / "tf"
        plan = subprocess.run(
            ["tofu", "plan", "-input=false", "-no-color", "-detailed-exitcode"],
            cwd=workspace, env=_tf_env(gateway_port, access_key, secret_key),
            capture_output=True, text=True, timeout=120,
        )
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # And a second /apply-full of the identical canvas is a clean success,
        # never applied_tf_failed.
        resp2 = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        assert body2["status"] == "applied", body2
        assert body2["tf"] == {"status": "ok", "exit_code": 0}, body2

        # Teardown (empty canvas = full destroy): the VM is really gone.
        resp3 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert resp3.status_code == 200, resp3.text
        assert resp3.json()["tf"] == {"status": "ok", "exit_code": 0}, resp3.json()

    for name in vm_cleanup:
        listing = subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=30)
        assert name not in listing.stdout.split(), f"VM survived teardown: {name}"
