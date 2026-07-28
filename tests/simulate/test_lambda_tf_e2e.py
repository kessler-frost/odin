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
import socket
import subprocess
import sys
import time
import zipfile
from contextlib import suppress
from pathlib import Path

import attr
import boto3
import httpx
import pytest
from botocore.config import Config
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


# --- Field-test finding #1 (HIGH) re-verify: a Lambda whose handler calls OTHER
# AWS services back through the gateway DURING its invocation. The repo's echo
# lambda never calls back, so it never exercised the re-entrancy deadlock -- this
# does, and it is THE proof the fix lands end-to-end (real RIE container, real
# re-entrant boto3 PutItem/PutObject with the function's OWN injected creds). ---

CALLBACK_ENV = "lambda-callback-e2e"
CALLBACK_FUNCTION_NAME = "callback"
# The handler dials the gateway back AS ITSELF (the four AWS_* vars the RIE
# container gets injected by `_finish_deploy`/`workload_env`), doing a real
# DynamoDB PutItem + S3 PutObject mid-invocation. Endpoint read explicitly from
# the injected env so the test never depends on botocore's AWS_ENDPOINT_URL
# auto-read version; path-style S3 for RustFS.
CALLBACK_CODE = (
    "import os\n"
    "import boto3\n"
    "from botocore.config import Config\n"
    "\n"
    "def lambda_handler(event, context):\n"
    "    endpoint = os.environ['AWS_ENDPOINT_URL']\n"
    "    boto3.client('dynamodb', endpoint_url=endpoint).put_item(\n"
    "        TableName='orders', Item={'id': {'S': 'order-1'}, 'via': {'S': 'callback'}})\n"
    "    boto3.client('s3', endpoint_url=endpoint, config=Config(s3={'addressing_style': 'path'})).put_object(\n"
    "        Bucket='artifacts', Key='receipt.txt', Body=b'paid')\n"
    "    return {'put_item': 'ok', 'put_object': 'ok'}\n"
)
CALLBACK_CANVAS = {
    "nodes": [
        {"id": "fn", "type": "lambda", "data": {
            "label": CALLBACK_FUNCTION_NAME, "runtime": "python3.12", "code": CALLBACK_CODE}},
        {"id": "ddb", "type": "dynamodb", "data": {"label": "orders"}},
        {"id": "bkt", "type": "s3", "data": {"label": "artifacts"}},
    ],
    "edges": [
        {"source": "fn", "target": "ddb",
         "data": {"edgeType": "iam", "permissions": ["dynamodb:PutItem", "dynamodb:GetItem"]}},
        {"source": "fn", "target": "bkt",
         "data": {"edgeType": "iam", "permissions": ["s3:PutObject"]}},
    ],
}


def test_callback_lambda_reaches_other_services_during_invoke(store_root, lambda_cleanup, monkeypatch):
    """The exact scenario the field test flagged: a Lambda whose handler does a
    real boto3 PutItem to a dynamodb node + PutObject to an s3 node DURING its
    invocation. Before the fix the synchronous invoke froze the gateway's event
    loop, so those re-entrant calls could never be served -- they timed out and
    the invoke returned empty. Now the invoke returns the handler's result and
    the item+object actually land."""
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.append(container_name(CALLBACK_ENV, CALLBACK_FUNCTION_NAME))

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=CALLBACK_CANVAS, params={"env": CALLBACK_ENV})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}, resp.json()

        state = _lambdactl_state(store.root, CALLBACK_ENV)
        (fn,) = [v for k, v in state.items() if k.startswith("fn:")]
        assert fn["state"] == "Active", fn

        gateway_port = client.get("/health").json()["gateway"]["port"]
        operator_key, operator_secret = app.state.gateway_keys.issue(CALLBACK_ENV, OPERATOR_NODE_ID)
        lambda_client = boto3.client(
            "lambda", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=operator_key, aws_secret_access_key=operator_secret, region_name="us-east-1",
            config=Config(connect_timeout=45, read_timeout=45, retries={"max_attempts": 0}),
        )
        invoke_start = time.monotonic()
        response = lambda_client.invoke(FunctionName=CALLBACK_FUNCTION_NAME, Payload=b"{}")
        invoke_elapsed = time.monotonic() - invoke_start
        print(f"\n[finding#1] re-entrant Invoke round-trip took {invoke_elapsed:.1f}s")

        # THE proof: the invoke returns the handler's own result (not 0 bytes,
        # not a 30s ServiceException timeout) and did not error.
        assert response.get("FunctionError") is None, response
        payload_out = json.loads(response["Payload"].read())
        assert payload_out == {"put_item": "ok", "put_object": "ok"}, payload_out
        assert invoke_elapsed < 20, f"invoke took {invoke_elapsed:.1f}s -- the re-entrancy deadlock is not fixed"

        # And the writes actually LANDED (operator creds, full-allow).
        ddb = boto3.client(
            "dynamodb", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=operator_key, aws_secret_access_key=operator_secret, region_name="us-east-1",
        )
        item = ddb.get_item(TableName="orders", Key={"id": {"S": "order-1"}}).get("Item")
        assert item and item["via"]["S"] == "callback", item

        s3 = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=operator_key, aws_secret_access_key=operator_secret, region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        assert s3.get_object(Bucket="artifacts", Key="receipt.txt")["Body"].read() == b"paid"

        # Empty the bucket before teardown so THIS test stays scoped to finding
        # #1 -- a non-empty-bucket destroy is finding #4's concern (force_destroy),
        # proven separately in tests/agent/test_hcl.py + its own e2e.
        s3.delete_object(Bucket="artifacts", Key="receipt.txt")
        resp = client.post("/apply-full", json=EMPTY_CANVAS, params={"env": CALLBACK_ENV})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}

    ps_after = _docker(
        "ps", "-a", "--filter", f"name={container_name(CALLBACK_ENV, CALLBACK_FUNCTION_NAME)}", "--format", "{{.Names}}",
    )
    assert ps_after.stdout.strip() == "", f"lambda container survived teardown: {ps_after.stdout}"


# --- v0.8.14: a function that is a whole DIRECTORY -- its own modules, and a
# vendored dependency, both really executing inside a real RIE container. ------

PKG_ENV = "lampkg-multifile-e2e"
PKG_FUNCTION = "lampkg-thumbnailer"

# The tree a user owns. Three things the single-textarea shape could not
# express: a package the handler imports (`thumbs`), a nested module inside it,
# and a DEPENDENCY vendored into the source directory the way `pip install -t .`
# leaves one -- odin's whole dependency story, and the only one it offers (it
# never fetches anything at apply time; docs/limits.md says so).
VENDORED = "attr"  # the `attrs` distribution's import package -- see `_write_tree`
PACKAGE_TREE = {
    "lambda_function.py": (
        "import os\n"
        f"import {VENDORED}\n"
        "from thumbs.resize import describe\n"
        "\n"
        "def lambda_handler(event, context):\n"
        f"    Point = {VENDORED}.make_class('Point', ['x', 'y'])\n"
        "    return {\n"
        "        'described': describe(event['name']),\n"
        f"        'vendored_ran': {VENDORED}.asdict(Point(1, 2)),\n"
        f"        'vendored_version': {VENDORED}.__version__,\n"
        f"        'vendored_from': {VENDORED}.__file__,\n"
        "        'task_files': sorted(os.listdir('/var/task')),\n"
        "    }\n"
    ),
    "thumbs/__init__.py": "",
    "thumbs/resize.py": "def describe(name):\n    return name.upper() + '-128x128'\n",
}


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    """The source directory `sourceDir` points at, INCLUDING a real vendored
    dependency.

    `attrs` is a genuine third-party pure-Python distribution (a package with
    submodules, type stubs, `__pycache__` and all), copied in exactly where
    `pip install -t <dir> attrs` would put it -- copied rather than downloaded,
    because odin's whole dependency claim is that nothing is fetched at apply
    time. A handmade stub package would have proved the import machinery and
    nothing about a real distribution's layout.

    WHY `attrs` AND NOT SOMETHING FROM boto3's OWN DEPENDENCY TREE. The first
    version of this used `jmespath` and the proof was worthless: deleting the
    vendored copy from the archive entirely still returned the right answer,
    because `public.ecr.aws/lambda/python:3.12` bundles boto3 -- and therefore
    jmespath -- in `/var/runtime`. Measured by mutation, not reasoned about.
    `attrs` is in no AWS runtime, and the handler additionally reports
    `__file__` so the assertion can prove the module that ran came out of
    `/var/task` rather than out of the image.

    `tmp_path` is fine HERE, and that is not a contradiction of this module's
    LOAD-BEARING DISCOVERY: nothing bind-mounts this tree. The SERVER reads it
    at translate time and puts its bytes in the zip; what Colima mounts is the
    extracted `.odin/<env>/gateway/lambda/<fn>-code/` directory, which the
    `store_root` fixture already keeps under the repo checkout.
    """
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    site_packages = Path(attr.__file__).parent.parent
    shutil.copytree(site_packages / VENDORED, root / VENDORED, dirs_exist_ok=True)
    # ...and its `.dist-info`, because that is what `pip install -t` leaves and
    # because `attr.__version__` reads it through `importlib.metadata` -- found
    # by the container raising `PackageNotFoundError` when only the import
    # package was copied. Vendoring the code without the metadata is a real
    # mistake a user can make, so the test makes the faithful copy instead.
    dist_info = next(site_packages.glob(f"{VENDORED}s-*.dist-info"))
    shutil.copytree(dist_info, root / dist_info.name, dirs_exist_ok=True)
    return root


def _assert_ran_the_package(payload: dict) -> None:
    """Every claim the multi-file feature makes, read off one real invocation."""
    assert payload["described"] == "HERO-128x128", payload  # the node's own module ran
    assert payload["vendored_ran"] == {"x": 1, "y": 2}, payload  # the vendored dep really executed
    assert payload["vendored_version"] == attr.__version__, payload  # ...and its metadata shipped too
    # ...and it was the VENDORED copy, not one the base image happened to ship.
    assert payload["vendored_from"].startswith("/var/task/"), payload
    assert {"lambda_function.py", "thumbs", VENDORED} <= set(payload["task_files"]), payload


def _archive_names(root: Path, env: str) -> list[str]:
    """Every member name of the deployment zip odin actually shipped, read back
    off the one the GATEWAY stored -- so what is checked is the archive that
    reached the substrate, not the generator's own output."""
    with zipfile.ZipFile(root / env / "gateway" / "lambda" / f"{PKG_FUNCTION}.zip") as archive:
        return archive.namelist()


def _package_canvas(source_dir: Path, function_name: str = PKG_FUNCTION) -> dict:
    return {
        "nodes": [{"id": "fn", "type": "lambda", "data": {
            "label": function_name, "runtime": "python3.12", "sourceDir": str(source_dir),
        }}],
        "edges": [],
    }


def test_a_multi_file_source_directory_really_runs_in_the_rie_container(store_root, lambda_cleanup, tmp_path):
    """The whole v0.8.14 claim, end to end and through the product's own path:
    canvas -> Stack -> generate_tf (which WALKS the directory) -> a real
    `tofu apply` -> a real RIE container -> a real Invoke whose answer could
    only have been produced by code in THREE different files of that tree.

    Plus the determinism claim, measured rather than asserted about: `/tf/plan`
    re-runs `generate_tf` from scratch, so it re-walks the directory and rebuilds
    the archive. `no_changes` means the second archive hashed identically to the
    one in tofu's state -- if member order or a timestamp leaked in, this is
    `changes` and every Apply would redeploy the function.
    """
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.append(container_name(PKG_ENV, PKG_FUNCTION))
    source_dir = _write_tree(tmp_path / "thumbnailer", PACKAGE_TREE)

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=_package_canvas(source_dir), params={"env": PKG_ENV})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body["tf"]
        assert body["status"] == "applied", body

        state = _lambdactl_state(store.root, PKG_ENV)
        (fn,) = [v for k, v in state.items() if k.startswith("fn:")]
        assert fn["state"] == "Active", fn

        # THE data-plane proof: every one of the three files ran.
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(PKG_ENV, OPERATOR_NODE_ID)
        lambda_client = boto3.client(
            "lambda", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        )
        response = lambda_client.invoke(
            FunctionName=PKG_FUNCTION, Payload=json.dumps({"name": "hero"}).encode(),
        )
        assert response.get("FunctionError") is None, response
        payload_out = json.loads(response["Payload"].read())
        print(f"\n[v0.8.14] multi-file invoke answered: {payload_out}")
        _assert_ran_the_package(payload_out)

        # The archive the GATEWAY stored, read back: the real vendored
        # distribution shipped, and no bytecode did.
        #
        # The `.pyc` half of that is deliberately NOT the proof of the exclusion
        # rules, and saying so is the point: `__pycache__` is caught twice over
        # (once as a skipped directory, once by the `.pyc` suffix), so deleting
        # EITHER rule leaves this assertion green -- measured, by doing exactly
        # that against this test. Each rule is killed individually by
        # `tests/agent/test_lambda_package.py`, using the inputs that
        # distinguish them. What this line is worth end-to-end is the other
        # direction: an exclusion that grew too greedy and swallowed a vendored
        # dependency would fail it.
        shipped = set(_archive_names(store.root, PKG_ENV))
        assert {f"{VENDORED}/__init__.py", f"{VENDORED}/_make.py"} <= shipped, sorted(shipped)
        assert not [name for name in shipped if name.endswith(".pyc")], sorted(shipped)

        # ZERO DRIFT: a second translate of an unchanged directory must produce
        # a byte-identical archive, or `source_code_hash` churns forever.
        plan = client.post("/tf/plan", params={"env": PKG_ENV})
        assert plan.status_code == 200, plan.text
        assert plan.json()["status"] == "no_changes", plan.json()

        resp = client.post("/apply-full", json=EMPTY_CANVAS, params={"env": PKG_ENV})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}

    ps_after = _docker(
        "ps", "-a", "--filter", f"name={container_name(PKG_ENV, PKG_FUNCTION)}", "--format", "{{.Names}}",
    )
    assert ps_after.stdout.strip() == "", f"lambda container survived teardown: {ps_after.stdout}"


def test_a_source_directory_missing_its_handler_module_fails_the_apply(store_root, lambda_cleanup, tmp_path):
    """The guard, exercised where it matters. A package with no
    `lambda_function.py` deploys perfectly happily -- RIE answers a TCP connect
    whether or not the module exists -- and only fails when somebody invokes it.
    So the apply must REFUSE, name the missing file, and leave no container."""
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    env = f"{PKG_ENV}-nohandler"
    lambda_cleanup.append(container_name(env, PKG_FUNCTION))
    source_dir = _write_tree(tmp_path / "broken", {"thumbs/resize.py": "def describe(n):\n    return n\n"})

    app = create_app(store=SpecStore(store_root))
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=_package_canvas(source_dir), params={"env": env})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        (reason,) = [u for u in body["not_covered"] if "handler" in u]
        assert "needs lambda_function.py" in reason, reason
        assert _lambdactl_state(SpecStore(store_root).root, env) == {}, "a declined function was deployed anyway"

    ps_after = _docker("ps", "-a", "--filter", f"name={container_name(env, PKG_FUNCTION)}", "--format", "{{.Names}}")
    assert ps_after.stdout.strip() == "", ps_after.stdout


# --- the REAL `odin` CLI, against a REAL server -----------------------------
#
# Verify through the product's own path. The last two import defects in this
# area were invisible to unit tests for one reason: the CLI never sent what the
# tests sent. `sourceDir` is deliberately shaped so it cannot repeat that -- the
# CLI posts the canvas and the SERVER builds the archive, so there is no zip to
# leave behind on the client -- and this test is what makes that a measurement
# rather than an argument. A real uvicorn process, the real `odin apply` binary
# in its own process, and a real invoke of what it built.

CLI_ENV = "lampkg-cli-e2e"
CLI_FUNCTION = "lampkg-cli-thumbnailer"


def _free_port() -> int:
    """A port nothing is on right now. NOT a fixed number: several agents and
    several xdist workers share this machine, and a fixed port is the one
    isolation that reliably collides (.claude/CLAUDE.md)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def live_server():
    """A REAL uvicorn process serving the REAL app, rooted in its own directory
    under the repo checkout (`ODIN_DIR` is `.odin` relative to the process's
    CWD, and Colima only mounts $HOME -- this module's own LOAD-BEARING
    DISCOVERY). Yields (root, base_url, env vars for the CLI)."""
    root = Path(__file__).resolve().parents[2] / ".odin-lampkg-cli"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    port = _free_port()
    # ODIN_GATEWAY_PORT=0 -> an ephemeral gateway port. Without it a real server
    # takes the default one, which is exactly how two agents produced a bogus
    # 401 and two phantom bugs.
    env_vars = {**os.environ, "ODIN_GATEWAY_PORT": "0", "ODIN_URL": f"http://127.0.0.1:{port}"}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "odin.server:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--timeout-graceful-shutdown", "5"],
        cwd=root, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        health = _get(f"http://127.0.0.1:{port}/health")
        if health is not None:
            break
        assert server.poll() is None, f"the server exited before serving:\n{server.communicate()[0]}"
        time.sleep(0.25)
    else:
        server.kill()
        raise AssertionError(f"uvicorn never served on {port}:\n{server.communicate()[0]}")
    yield root, f"http://127.0.0.1:{port}", env_vars
    server.terminate()
    server.wait(timeout=30)
    shutil.rmtree(root, ignore_errors=True)


def _get(url: str) -> dict | None:
    with suppress(httpx.HTTPError):
        response = httpx.get(url, timeout=2.0)
        return response.json() if response.status_code == 200 else None
    return None


def _odin(root: Path, env_vars: dict[str, str], *args: str, timeout: float = 420) -> subprocess.CompletedProcess:
    """The REAL CLI, in its own process -- `python -m odin` is the same entry
    point the `odin` console script calls (`odin.__main__:main`)."""
    return subprocess.run(
        [sys.executable, "-m", "odin", *args],
        cwd=root, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def test_the_real_odin_cli_applies_a_multi_file_function(live_server, lambda_cleanup, tmp_path):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    root, base_url, env_vars = live_server
    lambda_cleanup.append(container_name(CLI_ENV, CLI_FUNCTION))
    source_dir = _write_tree(tmp_path / "cli-thumbnailer", PACKAGE_TREE)
    canvas_file = tmp_path / "canvas.json"
    canvas_file.write_text(json.dumps(_package_canvas(source_dir, CLI_FUNCTION)))

    applied = _odin(root, env_vars, "apply", "--file", str(canvas_file), "--env", CLI_ENV)
    assert applied.returncode == 0, f"odin apply failed ({applied.returncode}):\n{applied.stdout}\n{applied.stderr}"
    assert "tf: ok" in applied.stdout, applied.stdout

    # The CLI's own view of the world agrees the function is up...
    world = _odin(root, env_vars, "world", "--env", CLI_ENV, "-o", "json")
    assert world.returncode == 0, world.stderr
    phases = {r["id"]: r["phase"] for r in json.loads(world.stdout)["resources"]}
    assert phases.get(CLI_FUNCTION) == "healthy", phases

    # ...and the RIE container it built really runs all three files. Credentials
    # come from the CLI too (`odin keys issue`, the documented escape hatch),
    # so nothing in this test reaches past the product's own surface except the
    # AWS SDK call any user would make.
    gateway_port = _get(f"{base_url}/health")["gateway"]["port"]
    keys = _odin(root, env_vars, "keys", "issue", OPERATOR_NODE_ID, "--env", CLI_ENV, "-o", "json")
    assert keys.returncode == 0, keys.stderr
    creds = json.loads(keys.stdout)
    lambda_client = boto3.client(
        "lambda", endpoint_url=f"http://127.0.0.1:{gateway_port}",
        aws_access_key_id=creds["access_key"], aws_secret_access_key=creds["secret_key"],
        region_name="us-east-1",
    )
    response = lambda_client.invoke(FunctionName=CLI_FUNCTION, Payload=json.dumps({"name": "hero"}).encode())
    assert response.get("FunctionError") is None, response
    payload_out = json.loads(response["Payload"].read())
    print(f"\n[v0.8.14] real-CLI multi-file invoke answered: {payload_out}")
    _assert_ran_the_package(payload_out)

    # `odin tf plan` is the drift check a CI job runs, and its exit code IS the
    # determinism claim: 0 no changes, 2 changes present. A churning archive
    # would make this 2 on every run.
    plan = _odin(root, env_vars, "tf", "plan", "--env", CLI_ENV)
    assert plan.returncode == 0, f"odin tf plan reported drift ({plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps(EMPTY_CANVAS))
    torn_down = _odin(root, env_vars, "apply", "--file", str(empty), "--env", CLI_ENV)
    assert torn_down.returncode == 0, f"{torn_down.stdout}\n{torn_down.stderr}"
    ps_after = _docker(
        "ps", "-a", "--filter", f"name={container_name(CLI_ENV, CLI_FUNCTION)}", "--format", "{{.Names}}",
    )
    assert ps_after.stdout.strip() == "", f"lambda container survived teardown: {ps_after.stdout}"
