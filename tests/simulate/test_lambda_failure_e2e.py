"""Field test 3 (MED): a FAILED Lambda invocation must not look successful.

`aws lambda invoke` on a function whose handler raises came back
`StatusCode: 200` with no `FunctionError` field -- the documented AWS way to
detect a failed invoke -- so a CI job scored a crashing function as a
success. The error payload odin returned was correct and complete; only the
signal was missing.

Root cause, and why a unit test alone could never have caught it: the real
RIE does NOT send `X-Amz-Function-Error`. A raised handler answers `200 OK`
with the error document as the body and no such header (verified against a
real `public.ecr.aws/lambda/python:3.12` container); odin read only that
header, so `function_error` was always None -- and with it
`last_invocation_error`, the field v0.7.1 added for the World verdict, which
is fed from the SAME value. odin's own fake RIE in the unit tests obligingly
sent the header, which real RIE never does.

So the check that matters is this one: a REAL RIE container whose handler
raises, invoked through the REAL gateway, with what BOTO3 surfaces as
`FunctionError` as the assertion. Two functions are deployed -- one that
raises, one that returns -- because "always reports an error" would pass
half this test.

Same substrate constraints as `test_lambda_tf_e2e.py`: the store root must
live under `$HOME` (Colima only mounts that tree, and an empty `/var/task`
is a real `Runtime.ImportModuleError`), and container hygiene is absolute --
every container name is force-removed by exact name on teardown whatever the
outcome. No tofu here: CreateFunction goes straight through the gateway with
boto3, which is exactly the surface the finding is about.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient

from odin.compute.functions import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "lambda-fail-e2e"
RAISER = "crasher"
ECHO = "healthy-echo"
_RAISE_CODE = 'def lambda_handler(event, context):\n    raise ValueError("boom")\n'
_ECHO_CODE = "def lambda_handler(event, context):\n    return event\n"
_ACTIVE_TIMEOUT = 240.0  # a cold public.ecr.aws/lambda/python:3.12 pull is a real fetch


def _zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", code)
    return buf.getvalue()


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def _lambdactl_state(root: Path) -> dict:
    path = root / ENV / "gateway" / "lambdactl.json"
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def lambda_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-lambda-fail-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _deploy(client, name: str, code: str) -> None:
    client.create_function(
        FunctionName=name, Runtime="python3.12",
        Role=f"arn:aws:iam::000000000000:role/{name}-exec",
        Handler="lambda_function.lambda_handler", Code={"ZipFile": _zip(code)},
    )


def _await_active(client, name: str) -> None:
    deadline = time.monotonic() + _ACTIVE_TIMEOUT
    while time.monotonic() < deadline:
        state = client.get_function(FunctionName=name)["Configuration"]["State"]
        assert state != "Failed", client.get_function(FunctionName=name)["Configuration"]
        if state == "Active":
            return
        time.sleep(1.0)
    raise AssertionError(f"{name} never reached Active within {_ACTIVE_TIMEOUT}s")


def test_a_raising_handler_reports_function_error_to_boto3(store_root, lambda_cleanup):
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.extend([container_name(ENV, RAISER), container_name(ENV, ECHO)])

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as http_client:
        gateway_port = http_client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        client = boto3.client(
            "lambda", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        )

        _deploy(client, RAISER, _RAISE_CODE)
        _deploy(client, ECHO, _ECHO_CODE)
        _await_active(client, RAISER)
        _await_active(client, ECHO)

        # THE proof these are real containers, not a model fiction.
        for name in (container_name(ENV, RAISER), container_name(ENV, ECHO)):
            ps = _docker("ps", "--filter", f"name={name}", "--format", "{{.Image}}")
            assert ps.stdout.strip() == "public.ecr.aws/lambda/python:3.12", ps.stdout

        # THE assertion the finding is about: what an SDK client sees.
        failed = client.invoke(FunctionName=RAISER, Payload=b"{}")
        assert failed["StatusCode"] == 200  # real Lambda's own contract: 200 + FunctionError
        assert failed.get("FunctionError") == "Unhandled", failed
        payload = json.loads(failed["Payload"].read())
        assert payload["errorType"] == "ValueError"
        assert payload["errorMessage"] == "boom"

        # ...and a working function is still clean -- the detection must not
        # simply flag everything.
        ok = client.invoke(FunctionName=ECHO, Payload=json.dumps({"n": 1}).encode())
        assert ok.get("FunctionError") is None, ok
        assert json.loads(ok["Payload"].read()) == {"n": 1}

        # ONE source of truth: the same signal is what the World verdict reads
        # (v0.7.1's `last_invocation_error`), so it was silently dead too.
        records = {v["function_name"]: v for v in _lambdactl_state(store_root).values()}
        assert records[RAISER]["last_invocation_error"] == "Unhandled"
        assert records[ECHO]["last_invocation_error"] is None

        client.delete_function(FunctionName=RAISER)
        client.delete_function(FunctionName=ECHO)

    for name in (container_name(ENV, RAISER), container_name(ENV, ECHO)):
        ps = _docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}")
        assert ps.stdout.strip() == "", f"lambda container survived teardown: {ps.stdout}"
