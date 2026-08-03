"""S3 bucket notifications, end to end: a REAL `tofu apply` of
`aws_s3_bucket_notification`, a REAL RustFS write, and a REAL Lambda invoked by
the write.

Three claims that no unit test can reach, and each is the whole point of one
design decision:

  1. **A real `tofu apply` then `plan` is CLEAN.** `s3:PutBucketNotification` /
     `GetBucketNotification` were made PURE (`gateway/models/s3notify.py`)
     precisely so they never reach RustFS -- which was measured rejecting every
     ARN form with `InvalidArgument` while storing the config anyway, giving a
     failed apply, a clean plan and a trigger that never fires. If the provider
     drives PUT `?notification` then GET and the follow-up plan reports no
     changes, that whole three-way contradiction is gone.
  2. **The ETag odin puts in a delivered event is the one RustFS really
     reports.** `synth.postprocess` never sees the backing's response headers,
     so `s3notify` COMPUTES `md5(body)` -- S3's own definition for a
     single-part PUT. Computed is not observed, and a wrong ETag in a delivered
     event is worse than an absent one because a handler will trust it. This
     measures the two against each other.
  3. **The filter actually filters.** A write matching the configured
     prefix+suffix invokes the function; one that does not, does not. A
     notification that fires for everything is the same class of bug as one
     that fires for nothing.
"""
from __future__ import annotations

import hashlib
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
from botocore.config import Config
from fastapi.testclient import TestClient

from odin.compute.functions import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.iac.hcl import TfProject
from odin.simulate import workspace as workspace_mod
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "evdisp-s3-e2e"
BUCKET = "uploads"
FUNCTION = "thumbnailer"
MARKER = "ODIN-S3-FIRED"
PLUGIN_CACHE_DIR = Path.home() / ".terraform.d" / "plugin-cache"

_CODE = (
    "import json\n"
    "def lambda_handler(event, context):\n"
    f"    print('{MARKER} ' + json.dumps(event))\n"
    "    return {'n': len(event.get('Records', []))}\n"
)

MAIN_TF = f"""
terraform {{
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = "~> 5.0" }}
  }}
}}

provider "aws" {{
  region                      = "us-east-1"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true
}}

resource "aws_lambda_function" "thumbnailer" {{
  function_name = "{FUNCTION}"
  role          = "arn:aws:iam::000000000000:role/{FUNCTION}-exec"
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.12"
  filename      = "handler.zip"
}}

resource "aws_s3_bucket_notification" "uploads" {{
  bucket = "{BUCKET}"

  lambda_function {{
    id                  = "thumbs"
    lambda_function_arn = aws_lambda_function.thumbnailer.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "incoming/"
    filter_suffix       = ".jpg"
  }}
}}
"""


def _zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", code)
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


def _tofu(args: list[str], workspace, env_vars: dict[str, str], timeout: float = 300):
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture
def cleanup():
    """Scoped to THIS env's own names, never a machine-wide sweep."""
    yield
    _docker("rm", "-f", "-v", container_name(ENV, FUNCTION))
    _docker("rm", "-f", "-v", f"odin-aws-rustfs-{ENV}")


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-dispatch-s3-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _await(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(1.0)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def _marked(logs) -> list[dict]:
    group = f"/aws/lambda/{FUNCTION}"
    if not any(g["logGroupName"] == group
               for g in logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups", [])):
        return []
    return [e for e in logs.filter_log_events(logGroupName=group).get("events", [])
            if MARKER in e["message"]]


def test_a_real_apply_is_clean_and_a_real_write_fires_the_function(store_root, cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as http:
        # The BUCKET comes from the canvas, not from `main.tf`, and the reason
        # is mechanical: the gateway forwards `s3:*` to this env's RustFS
        # backing, and nothing boots that backing until an apply asks for an s3
        # kind. Without it every S3 call 503s and `tofu apply` spends its whole
        # budget in the provider's own retry loop -- measured, a 300s timeout
        # with no useful error. This is also the real shape: a bucket is a
        # canvas node, and Simulate authors what sits on top of it.
        http.post("/apply", json={
            "nodes": [{"id": "s3-node", "type": "s3", "data": {"label": BUCKET}}], "edges": [],
        }, params={"env": ENV})
        _await(
            lambda: {r["id"]: r["phase"] for r in
                     http.get("/world", params={"env": ENV}).json()["resources"]}.get(BUCKET) == "healthy",
            180.0, "the s3 backing never became healthy",
        )

        gateway_port = http.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        workspace = workspace_mod.materialize(
            store.root, ENV,
            TfProject(files={"main.tf": MAIN_TF}, binary_files={"handler.zip": _zip(_CODE)}),
        )

        init = _tofu(["init"], workspace, env_vars)
        assert init.returncode == 0, f"init failed:\n{init.stdout}\n{init.stderr}"

        apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=300)
        assert apply.returncode == 0, f"apply failed:\n{apply.stdout}\n{apply.stderr}"

        # CLAIM 1a: apply SUCCEEDS. Before the notification handlers were pure,
        # this call was forwarded to RustFS and came back `InvalidArgument`.
        # CLAIM 1b: the follow-up plan is CLEAN. RustFS used to answer the GET
        # from a config it had rejected, which made a broken trigger look
        # settled; odin owns both sides now, so agreement is real.
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, (
            f"plan reported drift after apply (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}")

        # ...and the config really is odin's, not RustFS's.
        stored = json.loads((store.root / ENV / "gateway" / "s3notify.json").read_text())
        assert f"notify:{BUCKET}" in stored, stored

        def _client(service: str, **extra):
            return boto3.client(
                service, endpoint_url=f"http://127.0.0.1:{gateway_port}",
                aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                region_name="us-east-1", **extra,
            )

        s3 = _client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
        logs = _client("logs")

        # --- CLAIM 2: the ETag odin computes vs the one RustFS reports --------
        body = b"real-object-bytes-for-etag-comparison"
        put = s3.put_object(Bucket=BUCKET, Key="etag-probe.bin", Body=body)
        observed = put["ETag"].strip('"')
        computed = hashlib.md5(body).hexdigest()  # noqa: S324 -- S3's own single-part ETag definition
        print(f"\nMEASURED single-part ETag: RustFS={observed!r} md5(body)={computed!r} "
              f"{'AGREE' if observed == computed else 'DISAGREE'}")
        assert observed == computed, (
            "s3notify COMPUTES the ETag it puts in a delivered event; RustFS reports a different "
            "one, so the computed value is wrong and should be blanked rather than guessed")

        # --- CLAIM 3: the filter filters -------------------------------------
        s3.put_object(Bucket=BUCKET, Key="elsewhere/nope.jpg", Body=b"wrong prefix")
        s3.put_object(Bucket=BUCKET, Key="incoming/nope.png", Body=b"wrong suffix")
        s3.put_object(Bucket=BUCKET, Key="incoming/yes.jpg", Body=b"a real match")

        found = _await(lambda: _marked(logs), 60.0,
                       "the bucket notification never invoked the function")
        event = json.loads(found[0]["message"].split(MARKER, 1)[1].strip())
        record = event["Records"][0]
        assert record["eventSource"] == "aws:s3"
        assert record["s3"]["bucket"]["name"] == BUCKET
        assert record["s3"]["object"]["key"] == "incoming/yes.jpg"
        assert record["eventName"].startswith("ObjectCreated")

        # Give the dispatcher several more real passes; the two non-matching
        # writes must NEVER produce an invocation. Asserted after a wait rather
        # than immediately, because "not yet" and "never" look identical at the
        # instant the matching one lands.
        time.sleep(8)
        keys = [json.loads(e["message"].split(MARKER, 1)[1].strip())["Records"][0]["s3"]["object"]["key"]
                for e in _marked(logs)]
        assert keys == ["incoming/yes.jpg"], f"the filter let something through: {keys}"

        # Nothing may be left owed: a delivered notification is consumed.
        dispatch_state = json.loads((store.root / ENV / "gateway" / "dispatch.json").read_text())
        assert not [k for k in dispatch_state if k.startswith("pending:")], dispatch_state

        destroy = _tofu(["destroy", "-auto-approve"], workspace, env_vars, timeout=180)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"
