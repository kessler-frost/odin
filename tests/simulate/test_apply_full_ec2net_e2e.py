"""V1 final integration pass — the cross-layer seam the three V1 tasks (V1a
gateway model, V1b Nebula compile, V1c UI/HCL) must agree on end to end:
a canvas dict with a VPC + a contained Subnet + a contained SG (2 ingress
rules) -> POST /apply-full -> tofu ok -> re-apply is zero drift -> GET
/mesh?env= shows the VPC's Nebula network + the SG's compiled firewall ->
an EMPTY-canvas Apply tears everything down (the NORTHSTAR "no Destroy
button" promise).

No Colima/backing containers needed: vpc/subnet/sg are pure gateway-model
(EC2 has no backing), so `create_app(store=...)` alone is enough -- same
minimal harness `test_ec2net_tf_e2e.py` already uses.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "v1-cross-layer-e2e"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 560, "height": 380},
         "data": {"label": "main-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 100}, "size": {"width": 200, "height": 80},
         "data": {"label": "main-subnet", "cidr": "10.0.1.0/24", "vpc": "main-vpc"}},
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 220}, "size": {"width": 220, "height": 60},
         "data": {"label": "web-sg", "ingressRules": "tcp:443:0.0.0.0/0\ntcp:22:10.0.0.0/16", "vpc": "main-vpc"}},
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


def test_canvas_to_mesh_to_teardown(tmp_path):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["tf"] is not None and body["tf"]["status"] == "ok", body
        assert body["unsupported"] == [], body

        # GET /mesh?env=: the VPC's Nebula network + the SG's compiled firewall.
        mesh = client.get("/mesh", params={"env": ENV}).json()
        assert len(mesh["vpcs"]) == 1 and mesh["vpcs"][0]["cidr_block"] == "10.0.0.0/16"
        (web_sg,) = [sg for sg in mesh["security_groups"] if sg["group_name"] == "web-sg"]
        inbound = {(r["port"], r["proto"], r.get("cidr")) for r in web_sg["firewall"]["inbound"]}
        assert inbound == {("443", "tcp", "0.0.0.0/0"), ("22", "tcp", "10.0.0.0/16")}

        # Re-apply the identical canvas: zero drift (the research bar).
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        workspace = store.root / ENV / "tf"
        plan = subprocess.run(
            ["tofu", "plan", "-input=false", "-no-color", "-detailed-exitcode"],
            cwd=workspace, env=_tf_env(gateway_port, access_key, secret_key),
            capture_output=True, text=True, timeout=120,
        )
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # Empty canvas: full teardown (NORTHSTAR "no Destroy button" promise).
        # LOAD-BEARING for V1: vpc/subnet/sg have NO reconciler-driven teardown
        # path (plan.py NoOps them forever, so they're never even entered into
        # World -- confirmed by reading reconcile/plan.py + reconciler.py) --
        # tofu is the ONLY thing that can ever remove them.
        resp2 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        assert body2["status"] == "applied", body2
        assert body2["tf"] is not None and body2["tf"]["status"] == "ok", body2

        ec2net_path = store.root / ENV / "gateway" / "ec2net.json"
        state = ec2net_path.read_text() if ec2net_path.exists() else "{}"
        assert state.strip() in ("{}", ""), f"vpc/subnet/sg orphaned after empty-canvas apply: {state}"

        mesh_after = client.get("/mesh", params={"env": ENV}).json()
        assert mesh_after["vpcs"] == [] and mesh_after["security_groups"] == []
