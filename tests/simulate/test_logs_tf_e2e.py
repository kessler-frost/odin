"""W2.1 -- the ONE integration test for CloudWatch Logs: a real canvas
(lambda + log-group + an IAM edge between them) through a real `tofu apply`,
a real RIE container invoke, and a real `GetLogEvents` that returns the
handler's own printed line -- fetched WITH THE LAMBDA'S OWN gateway
credentials, and refused (real AccessDenied) for a principal with no edge.

What this proves that no unit test can:
  1. `aws_cloudwatch_log_group` is REAL through the gateway: tofu creates it
     (name + retention_in_days from the canvas node), and apply -> plan is
     ZERO DRIFT (`tofu plan -detailed-exitcode` == 0).
  2. The Lambda substrate SHIPS its output into that group: the RIE
     container's stdout lands in `/aws/lambda/{fn}` after an Invoke -- the log
     group is a real sink, not a metadata record.
  3. IAM edges gate the Logs data plane for real: the drawn
     `lambda -> log-group` edge is what lets the function's own principal
     read its lines back, and a principal with no such edge gets a genuine
     `AccessDeniedException` from the gateway (default-deny, PRD R6).

Shape/hygiene modeled on tests/simulate/test_lambda_tf_e2e.py -- including
its LOAD-BEARING store-root discovery (Colima only mounts `$HOME`, so the
SpecStore must live under the repo checkout, never pytest's `tmp_path`) and
its absolute container-hygiene fixture (the exact container name is
force-removed on teardown even if the test fails before `tofu destroy`).
The translation agent's refine pass is stubbed to the deterministic skeleton
(same as that test's canvas-path case): this test is about the gateway +
substrate + IAM, not about the SDK pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.functions import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.gateway.models import logsctl
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "logs-tf-e2e"
FUNCTION = "logfn"
GROUP = f"/aws/lambda/{FUNCTION}"
MARKER = "w21-log-line-proof"
RETENTION_DAYS = "14"

# The handler PRINTS (stdout -> the RIE container's logs -> the log group) and
# returns, so the proof line exists only because the substrate really shipped
# the container's output. `flush=True` removes the only nondeterminism that
# isn't odin's: python's own stdout buffering inside the RIE process.
CODE = (
    "def lambda_handler(event, context):\n"
    f"    print({MARKER!r}, flush=True)\n"
    "    return {'printed': True}\n"
)

CANVAS = {
    "nodes": [
        {"id": "fn", "type": "lambda", "data": {"label": FUNCTION, "runtime": "python3.12", "code": CODE}},
        {"id": "lg", "type": "logs", "data": {"label": GROUP, "retentionInDays": RETENTION_DAYS}},
    ],
    "edges": [
        {"source": "fn", "target": "lg", "data": {"edgeType": "iam", "permissions": [
            "logs:CreateLogStream", "logs:PutLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams",
        ]}},
    ],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def _tofu(args: list[str], workspace: Path, env_vars: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


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


def _client(service: str, port: int, keys: tuple[str, str]):
    return boto3.client(
        service, endpoint_url=f"http://127.0.0.1:{port}",
        aws_access_key_id=keys[0], aws_secret_access_key=keys[1], region_name="us-east-1",
        config=Config(connect_timeout=45, read_timeout=45, retries={"max_attempts": 0}),
    )


def _logsctl_state(root: Path, env: str) -> dict:
    path = root / env / "gateway" / "logsctl.json"
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def lambda_cleanup():
    """Container hygiene ABSOLUTE: force-removed by EXACT name on teardown
    regardless of outcome -- the guarantee `tofu destroy` alone can't give if
    the test fails before it runs."""
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)
        # ...and, for an rds container, its NAMED data volume: `rm -f -v`
        # deliberately leaves those standing (that is what makes odin's repair
        # non-destructive), so removing only the container leaks a Postgres
        # volume on every run that fails before its real teardown. A no-op --
        # exit 0 -- for every other kind, which has no such volume.
        _docker("volume", "rm", "-f", f"{name}-data")


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-w21-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def skeleton_translate(monkeypatch):
    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)


def _marker_count(logs_client, stream: str) -> int:
    events = logs_client.get_log_events(logGroupName=GROUP, logStreamName=stream)["events"]
    return len([e for e in events if MARKER in e["message"]])


def _shipped(logs_client, stream: str, invoke, invokes: int, deadline_seconds: float = 30.0) -> int:
    """How many marker lines the group holds, invoking again if none have
    shown up yet. Shipping is inline in the Invoke handler and the handler
    flushes, so the FIRST read normally answers -- the bounded loop only
    covers latency between the RIE process writing stdout and `docker logs`
    (the substrate's own read) seeing it. `invokes` is how many invokes have
    already happened; the stored count must equal the total, which IS the
    cursor-dedup invariant."""
    deadline = time.monotonic() + deadline_seconds
    while True:
        count = _marker_count(logs_client, stream)
        if count:
            assert count == invokes, f"{count} stored lines after {invokes} invokes -- lines were duplicated"
            return invokes
        assert time.monotonic() < deadline, f"no {MARKER!r} line reached {GROUP} within {deadline_seconds}s"
        time.sleep(0.5)
        invoke()
        invokes += 1


def test_lambda_ships_its_logs_to_a_real_log_group_iam_edge_gates_the_read(
    store_root, lambda_cleanup, skeleton_translate,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.append(container_name(ENV, FUNCTION))

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=CANVAS, params={"env": ENV})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body["tf"]

        # (1) The log group is REAL: tofu created it through the gateway, with
        # the canvas node's retention, adopted-flag clear (a genuine
        # CreateLogGroup, not substrate auto-creation).
        state = _logsctl_state(store.root, ENV)
        group = state[f"group:{GROUP}"]
        assert group["log_group_name"] == GROUP
        assert group["retention_in_days"] == int(RETENTION_DAYS)
        assert group["auto"] is False

        gateway_port = client.get("/health").json()["gateway"]["port"]
        operator = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)

        # Zero drift: apply -> plan changes NOTHING (the research bar, and the
        # whole reason the tag/retention/describe shapes are modeled).
        workspace = workspace_mod.tf_dir(store.root, ENV)
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, _tf_env(gateway_port, *operator))
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # (2) A real Invoke -> the RIE container prints -> the substrate ships
        # its tail into the group.
        lambda_client = _client("lambda", gateway_port, operator)

        def invoke() -> None:
            response = lambda_client.invoke(FunctionName=FUNCTION, Payload=b"{}")
            assert response.get("FunctionError") is None, response
            assert json.loads(response["Payload"].read()) == {"printed": True}

        invoke()

        # (3) THE proof, with the LAMBDA'S OWN creds (issue() is stable, so
        # this is literally the pair injected into its container) -- reading
        # its lines back is allowed only because the canvas edge grants it.
        own_keys = app.state.gateway_keys.issue(ENV, FUNCTION)
        assert own_keys != operator
        workload_logs = _client("logs", gateway_port, own_keys)
        streams = workload_logs.describe_log_streams(logGroupName=GROUP)["logStreams"]
        # One stream per real container, named after it (logsctl's documented
        # deviation from AWS's date/requestId naming).
        assert [s["logStreamName"] for s in streams] == [container_name(ENV, FUNCTION)]
        stream = streams[0]["logStreamName"]
        invokes = _shipped(workload_logs, stream, invoke, invokes=1)
        print(f"\n[W2.1] '{MARKER}' read back from {GROUP} with the function's own creds "
              f"({invokes} invoke(s), {invokes} stored line(s))")

        # One more Invoke re-reads the SAME container tail plus its own new
        # line: the cursor dedup means each invocation's line is stored exactly
        # ONCE. Shipping is inline in the Invoke handler, so no polling here.
        invoke()
        assert _marker_count(workload_logs, stream) == invokes + 1, "the log-shipping cursor duplicated lines"

        # (4) No edge, no read: a principal the canvas never connected to this
        # log group gets a REAL AccessDenied, not an empty answer.
        stranger = app.state.gateway_keys.issue(ENV, "stranger")
        with pytest.raises(ClientError) as denied:
            _client("logs", gateway_port, stranger).get_log_events(
                logGroupName=GROUP, logStreamName=streams[0]["logStreamName"],
            )
        assert denied.value.response["Error"]["Code"] == "AccessDeniedException", denied.value.response

        # Teardown through the ONLY human surface (empty canvas + Apply).
        resp = client.post("/apply-full", json=EMPTY_CANVAS, params={"env": ENV})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}
        assert not logsctl.group_exists(app.state.gateway_stores, ENV, GROUP)

    ps_after = _docker("ps", "-a", "--filter", f"name={container_name(ENV, FUNCTION)}", "--format", "{{.Names}}")
    assert ps_after.stdout.strip() == "", f"lambda container survived teardown: {ps_after.stdout}"
    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name={ENV}")
    assert leftover.stdout.strip() == ""
