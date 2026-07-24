"""V4d -- the FLAGSHIP integration test: a real `tofu apply` creates an
`aws_iam_role` + `aws_lambda_function` (python, inline "return event")
through the real gateway -- proving NORTHSTAR directive 5's whole Lambda
slice end-to-end (CreateFunction -> a REAL RIE container -> the provider's
own Pending->Active waiter -> zero-drift plan -> a REAL Invoke through the
gateway with operator creds, boto3 lambda client, returning the ECHOED
payload -> DeleteFunction -> a real container gone, zero leftovers).

Modeled on test_ec2_tf_e2e.py/test_iamctl_ecr_tf_e2e.py, with lambda's own
substrate shape: unlike EC2's Lima VM (a whole separate host), Lambda's
substrate is a Colima CONTAINER (like ECR's registry:2), so this test needs
Colima/docker on PATH, not limactl. odin materializes the function's zip
itself pre-tofu (agent/hcl.py's own `_lambda` builder does the same thing
for real canvases; this test builds the same TfProject shape by hand for a
raw-HCL round-trip, matching V3d's/V2's own "hand-authored HCL, not
generate_tf()" style).

Container hygiene ABSOLUTE (the brief's own words): the `lambda_cleanup`
fixture force-removes the exact container name in a finalizer, so a test
FAILURE never leaves a stray container even if `tofu destroy` itself never
runs -- same guarantee V3d's `vm_cleanup` gives Lima VMs.

LOAD-BEARING DISCOVERY (this test's own first failed run): the store CANNOT
live under pytest's own `tmp_path` fixture -- that resolves to macOS's
per-user TMPDIR (`/private/var/folders/...`), which is NOT under `$HOME`.
Colima's default VM mount is `$HOME` ONLY (verified live: a bind-mount from
under `/private/var/folders` into an `alpine` container comes up EMPTY,
`cat`: No such file or directory) -- exactly the constraint
`aws/backings.py`'s own module docstring already documents for goaws's
config mount, just never fatal there (goaws tolerates a missing config with
defaults; the RIE base image does NOT -- an empty `/var/task` is a real,
loud `Runtime.ImportModuleError`, not a silent drift). `store_root` below
roots the SpecStore under the repo checkout instead (always under `$HOME`
for a normal checkout), cleaned up unconditionally on teardown.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.agent.hcl import TfProject
from odin.compute.functions import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "lambda-tf-e2e"
FUNCTION_NAME = "v4d-echo"
ROLE_NAME = "v4d-lambda-exec"
ECHO_CODE = "def lambda_handler(event, context):\n    return event\n"

_ROLE = f"""resource "aws_iam_role" "exec" {{
  name = {hcl.quote(ROLE_NAME)}

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = {{ Service = "lambda.amazonaws.com" }}
    }}]
  }})
}}"""

_FUNCTION = f"""resource "aws_lambda_function" "echo" {{
  function_name    = {hcl.quote(FUNCTION_NAME)}
  role             = aws_iam_role.exec.arn
  handler          = {hcl.quote("lambda_function.lambda_handler")}
  runtime          = {hcl.quote("python3.12")}
  filename         = {hcl.quote("echo.zip")}
  source_code_hash = filebase64sha256({hcl.quote("echo.zip")})
}}"""

MAIN_TF = "\n\n".join([hcl.HEADER, hcl.provider_block(), _ROLE, _FUNCTION]) + "\n"


def _echo_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", ECHO_CODE)
    return buf.getvalue()


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


def _tofu(args: list[str], workspace, env_vars: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def _lambdactl_state(root, env: str) -> dict:
    path = root / env / "gateway" / "lambdactl.json"
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def lambda_cleanup():
    """Container hygiene ABSOLUTE: names appended here are force-removed by
    EXACT name on teardown regardless of test outcome -- the guarantee
    `tofu destroy` alone can't give if the test fails before it runs."""
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


@pytest.fixture
def store_root():
    """A store root under the repo checkout, NOT pytest's `tmp_path` -- see
    the module docstring's "LOAD-BEARING DISCOVERY". Wiped unconditionally
    on both setup and teardown, so a prior failed run never leaves stale
    state a fresh run could accidentally read."""
    root = Path(__file__).resolve().parents[2] / ".odin-v4d-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_tf_apply_boots_a_real_rie_zero_drift_invoke_destroy(store_root, lambda_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.append(container_name(ENV, FUNCTION_NAME))

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        workspace = workspace_mod.materialize(
            store.root, ENV, TfProject(files={"main.tf": MAIN_TF}, binary_files={"echo.zip": _echo_zip()}),
        )

        init = _tofu(["init"], workspace, env_vars)
        assert init.returncode == 0, f"init failed:\n{init.stdout}\n{init.stderr}"

        # tofu's own CreateFunction waiter polls GetFunction until
        # State=="Active" DURING apply -- a returncode 0 here already proves
        # the REAL RIE container came up and answered, not a timer (a cold
        # `public.ecr.aws/lambda/python:3.12` pull is the accepted budget).
        boot_start = time.monotonic()
        apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=300)
        boot_elapsed = time.monotonic() - boot_start
        print(f"\n[V4d] tofu apply (incl. real RIE container boot) took {boot_elapsed:.1f}s")
        assert apply.returncode == 0, f"apply failed:\n{apply.stdout}\n{apply.stderr}"

        state = _lambdactl_state(store.root, ENV)
        (fn,) = [v for k, v in state.items() if k.startswith("fn:")]
        assert fn["function_name"] == FUNCTION_NAME
        assert fn["state"] == "Active"
        assert fn["last_update_status"] == "Successful"
        assert fn["runtime"] == "python3.12"

        # THE proof the container is real, not a model fiction.
        ps = _docker("ps", "--filter", f"name={container_name(ENV, FUNCTION_NAME)}", "--format", "{{.Image}}")
        assert ps.stdout.strip() == "public.ecr.aws/lambda/python:3.12"

        # Zero drift: apply -> plan changes NOTHING (the research bar).
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # THE data-plane proof: a REAL Invoke through the gateway, operator
        # creds, a real boto3 lambda client -- the RIE container echoes the
        # payload straight back (ECHO_CODE == "return event").
        lambda_client = boto3.client(
            "lambda", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        )
        payload_in = {"key1": "value1", "nested": {"n": 42}}
        invoke_start = time.monotonic()
        response = lambda_client.invoke(FunctionName=FUNCTION_NAME, Payload=json.dumps(payload_in).encode())
        invoke_elapsed = time.monotonic() - invoke_start
        print(f"[V4d] first real Invoke round-trip took {invoke_elapsed * 1000:.0f}ms")
        assert response.get("FunctionError") is None, response
        payload_out = json.loads(response["Payload"].read())
        print(f"[V4d] echoed payload: {payload_out}")
        assert payload_out == payload_in

        destroy = _tofu(["destroy", "-auto-approve"], workspace, env_vars, timeout=120)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"

        # The container is actually gone -- not just the model record.
        ps_after = _docker("ps", "-a", "--filter", f"name={container_name(ENV, FUNCTION_NAME)}", "--format", "{{.Names}}")
        assert ps_after.stdout.strip() == "", f"lambda container survived teardown: {ps_after.stdout}"

        final_state = _lambdactl_state(store.root, ENV)
        assert final_state == {}, f"function record orphaned after destroy: {final_state}"
        assert not (store.root / ENV / "gateway" / "lambda" / f"{FUNCTION_NAME}.zip").exists()
        assert not (store.root / ENV / "gateway" / "lambda" / f"{FUNCTION_NAME}-code").exists()

    # Belt-and-braces: no odin-labelled container left for this env.
    # (No anonymous-volume check needed here -- the lambda container uses
    # only a host bind-mount for /var/task, never a docker-managed volume,
    # so there is nothing of that shape for THIS substrate to leak; `stop`'s
    # `-v` flag matters for backings that DO use one, e.g. Postgres.)
    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name={ENV}")
    assert leftover.stdout.strip() == ""


CANVAS_ENV = "lambda-tf-e2e-canvas"
CANVAS_FUNCTION_NAME = "v4d-echo-canvas"
CANVAS_LAMBDA = {
    "nodes": [{"id": "n1", "type": "lambda", "data": {
        "label": CANVAS_FUNCTION_NAME, "runtime": "python3.12", "code": ECHO_CODE,
    }}],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def test_apply_full_canvas_path_threads_binary_files_through_a_real_tofu_apply(store_root, lambda_cleanup, monkeypatch):
    """Release finding #1 (BLOCKER), proven against the REAL canvas path --
    unlike the flagship test above (hand-authored HCL), this one goes
    through canvas -> Stack -> generate_tf -> translate -> /apply-full's
    TfProject reconstruction -> a REAL tofu apply -> a REAL RIE container.
    The SDK refinement pass itself is stubbed out here (cheap, and already
    covered for the guardrail/validate_refinement shape by
    tests/agent/test_translate.py's binary_files tests) -- this test's only
    job is proving the zip survives translate()'s TranslateResult and
    apply_full's TfProject reconstruction against a real apply, not a fake
    tofu script."""
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.append(container_name(CANVAS_ENV, CANVAS_FUNCTION_NAME))

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=CANVAS_LAMBDA, params={"env": CANVAS_ENV})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body["tf"]

        state = _lambdactl_state(store.root, CANVAS_ENV)
        (fn,) = [v for k, v in state.items() if k.startswith("fn:")]
        assert fn["function_name"] == CANVAS_FUNCTION_NAME
        assert fn["state"] == "Active"

        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(CANVAS_ENV, OPERATOR_NODE_ID)
        lambda_client = boto3.client(
            "lambda", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        )
        payload_in = {"proof": "canvas-path"}
        response = lambda_client.invoke(FunctionName=CANVAS_FUNCTION_NAME, Payload=json.dumps(payload_in).encode())
        assert response.get("FunctionError") is None, response
        assert json.loads(response["Payload"].read()) == payload_in

        # Full teardown via the empty-canvas Apply path (the NORTHSTAR "no
        # Destroy button" promise) -- proves the zip's presence doesn't
        # orphan anything either.
        resp = client.post("/apply-full", json=EMPTY_CANVAS, params={"env": CANVAS_ENV})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}

    ps_after = _docker(
        "ps", "-a", "--filter", f"name={container_name(CANVAS_ENV, CANVAS_FUNCTION_NAME)}", "--format", "{{.Names}}",
    )
    assert ps_after.stdout.strip() == "", f"lambda container survived teardown: {ps_after.stdout}"
