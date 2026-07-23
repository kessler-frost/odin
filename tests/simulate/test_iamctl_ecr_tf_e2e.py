"""V2 final integration pass -- the cross-layer seam V2a (IAM control-plane
model), V2b (ECR + registry:2 backing), and V2c (canvas/HCL) must agree on
end to end: a canvas with ONE iam_role (+ inline policy) and ONE ecr repo
-> POST /apply-full -> tofu ok -> zero-drift re-plan -> a REAL `docker push`
to the created repositoryUri, verified via the registry's OWN `/v2/.../
tags/list` -> an EMPTY-canvas Apply tears everything down (the NORTHSTAR
"no Destroy button" promise), including gc'ing the registry:2 container.

Needs Colima: unlike V1a's ec2net (pure gateway-model, no backing at all),
ecr's DATA plane is a real registry:2 container `create_app`'s default
`ColimaRuntime()` must boot for real (V2b's `ENSURE_KINDS`/`ensure_backing`
wiring). iam_role stays pure gateway-model, same as V1a.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "v2-iamctl-ecr-e2e"
ROLE_NAME = "lambda-exec-role"
REPO_NAME = "v2-app-image"
INLINE_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
})

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "iam_role", "position": {"x": 40, "y": 40}, "size": {"width": 220, "height": 80},
         "data": {"label": ROLE_NAME, "inlinePolicy": INLINE_POLICY}},
        {"id": "n2", "type": "ecr", "position": {"x": 320, "y": 40}, "size": {"width": 200, "height": 40},
         "data": {"label": REPO_NAME}},
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


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def test_role_and_repo_apply_push_zero_drift_teardown(tmp_path):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["tf"] is not None and body["tf"]["status"] == "ok", body
        assert body["unsupported"] == [], body

        # V2a: the role is pure gateway-model state -- inline policy landed,
        # AssumeRolePolicyDocument is the Lambda trust doc (hcl.py's default).
        iamctl_state = json.loads((store.root / ENV / "gateway" / "iamctl.json").read_text())
        (role,) = [v for k, v in iamctl_state.items() if k.startswith("role:")]
        assert role["role_name"] == ROLE_NAME
        assert "lambda.amazonaws.com" in role["assume_role_policy_document"]
        assert json.loads(role["inline_policies"][f"{ROLE_NAME}-inline"]) == json.loads(INLINE_POLICY)

        # V2b: the repo's control-plane record + a REAL registry:2 behind it.
        ecr_state = json.loads((store.root / ENV / "gateway" / "ecr.json").read_text())
        (repo,) = [v for k, v in ecr_state.items() if k.startswith("repo:")]
        assert repo["repository_name"] == REPO_NAME
        repository_uri = repo["repository_uri"]
        assert repository_uri.startswith("127.0.0.1:")

        # A REAL docker push to the created repositoryUri (research §2c's own
        # verified flow) -- tag whatever's already cached locally, no network
        # dependency for THIS test.
        image_ref = f"{repository_uri}:e2e-test"
        tag = _docker("tag", "alpine:3.20", image_ref)
        assert tag.returncode == 0, tag.stderr
        push = _docker("push", image_ref)
        assert push.returncode == 0, push.stdout + push.stderr

        registry_port = repository_uri.split(":")[1].split("/")[0]
        tags = httpx.get(f"http://127.0.0.1:{registry_port}/v2/{REPO_NAME}/tags/list", timeout=10.0)
        assert tags.status_code == 200
        assert "e2e-test" in tags.json()["tags"]
        _docker("rmi", image_ref)  # local tag hygiene -- the pushed layers live in registry:2, not here

        # Zero drift: apply -> plan changes NOTHING (the research bar).
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
        # iam_role/ecr have NO reconciler-driven teardown path (plan.py NoOps
        # them, matching V1a's vpc/subnet/sg) -- tofu is the only thing that
        # can ever remove them; the trailing tick() then gc's the registry
        # container (no "ecr" resource left to keep it alive).
        resp2 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        assert body2["status"] == "applied", body2
        assert body2["tf"] is not None and body2["tf"]["status"] == "ok", body2

        iamctl_after = (store.root / ENV / "gateway" / "iamctl.json").read_text()
        assert json.loads(iamctl_after) == {}, f"iam role orphaned after empty-canvas apply: {iamctl_after}"
        ecr_after = (store.root / ENV / "gateway" / "ecr.json").read_text()
        assert json.loads(ecr_after) == {}, f"ecr repo orphaned after empty-canvas apply: {ecr_after}"

        ps = _docker("ps", "-a", "--filter", f"name=allfather-aws-registry-{ENV}", "--format", "{{.Names}}")
        assert ps.stdout.strip() == "", f"registry container survived teardown: {ps.stdout}"

    # Belt-and-braces: no allfather-labelled container left for this env.
    leftover = _docker("ps", "-aq", "--filter", "label=allfather=1", "--filter", f"name={ENV}")
    assert leftover.stdout.strip() == ""
