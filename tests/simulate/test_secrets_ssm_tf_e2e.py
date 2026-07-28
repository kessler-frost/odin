"""W2.4 -- the ONE integration test for Secrets Manager + SSM Parameter Store:
a real canvas (lambda + secret + ssm parameter, with an IAM edge to the SECRET
ONLY) through a real `tofu apply`, then a real RIE container whose handler
fetches the secret WITH ITS OWN gateway credentials and succeeds -- and gets a
genuine AccessDeniedException on the parameter nobody drew an edge to.

What this proves that no unit test can:
  1. `aws_secretsmanager_secret` (+ `_version`) and `aws_ssm_parameter` are REAL
     through the gateway: tofu creates them, the values land in the per-env
     0600 sidecars, and apply -> plan is ZERO DRIFT
     (`tofu plan -detailed-exitcode` == 0).
  2. **An IAM edge is what grants access to a secret.** One canvas, one
     workload, two secret-bearing resources, ONE edge -- and the outcome
     differs for real: the edged secret reads back, the un-edged parameter
     denies. Nothing in the policy layer knows what a secret is; the edge's DST
     LABEL and classify.py's resource are the same string, and that's the whole
     mechanism.
  3. The plaintext never rides out on a diagnostic channel: `events.jsonl` for
     this env (every `tf` line, every WorldDelta, every access_denied) contains
     neither value.

The handler deliberately returns a SHA-256 DIGEST of the fetched secret rather
than the secret -- proof it really read the right bytes, without moving the
plaintext through the RIE container's stdout (which the Lambda substrate ships
into a log group) or through the invoke response.

Shape/hygiene modeled on tests/simulate/test_logs_tf_e2e.py and
test_lambda_tf_e2e.py -- including their LOAD-BEARING store-root discovery
(Colima only mounts `$HOME`, so the SpecStore must live under the repo
checkout, never pytest's `tmp_path`) and the absolute container-hygiene
fixture (the exact container name is force-removed on teardown even if the test
fails before `tofu destroy`). The translation agent's refine pass is stubbed to
the deterministic skeleton: this test is about the gateway + substrate + IAM.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
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
from odin.gateway.models import secretsctl, ssmctl
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "secrets-tf-e2e"
FUNCTION = "secretfn"
SECRET = "db-password"
PARAM = "/odin/api-key"
SECRET_VALUE = "w24-secret-hunter2-and-then-some"
PARAM_VALUE = "w24-param-s3cr3t-99"
SECRET_DIGEST = hashlib.sha256(SECRET_VALUE.encode()).hexdigest()

# The handler dials the gateway back AS ITSELF (the four AWS_* vars the RIE
# container gets injected by `workload_env`), fetching the secret it has an edge
# to and attempting the parameter it does NOT. It returns a DIGEST, never the
# value -- see the module docstring.
CODE = (
    "import hashlib\n"
    "import os\n"
    "import boto3\n"
    "from botocore.exceptions import ClientError\n"
    "\n"
    "def lambda_handler(event, context):\n"
    "    endpoint = os.environ['AWS_ENDPOINT_URL']\n"
    f"    value = boto3.client('secretsmanager', endpoint_url=endpoint).get_secret_value(SecretId={SECRET!r})['SecretString']\n"
    "    out = {'digest': hashlib.sha256(value.encode()).hexdigest(), 'length': len(value)}\n"
    "    try:\n"
    f"        boto3.client('ssm', endpoint_url=endpoint).get_parameter(Name={PARAM!r}, WithDecryption=True)\n"
    "        out['ssm'] = 'ALLOWED'\n"
    "    except ClientError as exc:\n"
    "        out['ssm'] = exc.response['Error']['Code']\n"
    "    return out\n"
)

CANVAS = {
    "nodes": [
        {"id": "fn", "type": "lambda", "data": {"label": FUNCTION, "runtime": "python3.12", "code": CODE}},
        {"id": "sec", "type": "secret", "data": {
            "label": SECRET, "description": "the db password", "secretString": SECRET_VALUE,
        }},
        {"id": "par", "type": "ssm", "data": {
            "label": PARAM, "paramType": "SecureString", "paramValue": PARAM_VALUE,
        }},
    ],
    # ONE edge, to the secret only. The parameter is drawn but unconnected --
    # which is the whole point of the test.
    "edges": [
        {"source": "fn", "target": "sec", "data": {
            "edgeType": "iam", "permissions": ["secretsmanager:GetSecretValue"],
        }},
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


def _denied_code(call) -> str:
    with pytest.raises(ClientError) as denied:
        call()
    return denied.value.response["Error"]["Code"]


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
    root = Path(__file__).resolve().parents[2] / ".odin-w24-test"
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


def test_an_iam_edge_is_what_lets_a_lambda_read_a_secret(store_root, lambda_cleanup, skeleton_translate):
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
        assert body["skipped"] == []  # neither kind is drawable-only any more

        stores = app.state.gateway_stores
        # (1) Both resources are REAL: tofu created them through the gateway,
        # values and all, in sidecars that are owner-only on disk.
        assert secretsctl.secret_exists(stores, ENV, SECRET)
        assert secretsctl.current_value(stores, ENV, SECRET) == SECRET_VALUE
        assert ssmctl.parameter_value(stores, ENV, PARAM) == PARAM_VALUE
        for sidecar in ("secretsctl.json", "ssmctl.json"):
            path = store.root / ENV / "gateway" / sidecar
            assert oct(path.stat().st_mode & 0o777) == "0o600", f"{sidecar} is not owner-only"

        gateway_port = client.get("/health").json()["gateway"]["port"]
        operator = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)

        # Zero drift: apply -> plan changes NOTHING (the research bar, and the
        # reason DescribeSecret/DescribeParameters/ListTagsForResource/
        # GetResourcePolicy are all modeled at all).
        workspace = workspace_mod.tf_dir(store.root, ENV)
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, _tf_env(gateway_port, *operator))
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # (2) THE proof, from inside the real RIE container with the function's
        # OWN injected credentials: the edged secret reads back (digest match),
        # the un-edged parameter is refused for real.
        response = _client("lambda", gateway_port, operator).invoke(FunctionName=FUNCTION, Payload=b"{}")
        assert response.get("FunctionError") is None, response
        payload = json.loads(response["Payload"].read())
        assert payload["digest"] == SECRET_DIGEST, payload
        assert payload["length"] == len(SECRET_VALUE), payload
        assert payload["ssm"] == "AccessDeniedException", payload
        print(f"\n[W2.4] the lambda read {SECRET!r} with its own creds (digest match) "
              f"and got {payload['ssm']} on the un-edged {PARAM!r}")

        # ...and the same asymmetry holds for that principal from the host
        # (issue() is stable, so these are literally the container's keys).
        own_keys = app.state.gateway_keys.issue(ENV, FUNCTION)
        assert own_keys != operator
        got = _client("secretsmanager", gateway_port, own_keys).get_secret_value(SecretId=SECRET)
        assert got["SecretString"] == SECRET_VALUE
        assert _denied_code(
            lambda: _client("ssm", gateway_port, own_keys).get_parameter(Name=PARAM)
        ) == "AccessDeniedException"
        # An edge grants exactly the ticked verbs: reading is allowed, writing
        # over the secret is not.
        assert _denied_code(
            lambda: _client("secretsmanager", gateway_port, own_keys).put_secret_value(
                SecretId=SECRET, SecretString="overwritten-by-a-workload",
            )
        ) == "AccessDeniedException"

        # (3) No edge at all -> a principal the canvas never connected gets a
        # real AccessDenied, not an empty answer.
        stranger = app.state.gateway_keys.issue(ENV, "stranger")
        assert _denied_code(
            lambda: _client("secretsmanager", gateway_port, stranger).get_secret_value(SecretId=SECRET)
        ) == "AccessDeniedException"

        # (4) Neither plaintext reached the diagnostic channel: events.jsonl
        # holds every streamed `tofu` line, every WorldDelta and every
        # access_denied event for this env.
        events = (store.root / ENV / "events.jsonl").read_text()
        assert SECRET_VALUE not in events, "the secret value leaked into events.jsonl"
        assert PARAM_VALUE not in events, "the parameter value leaked into events.jsonl"
        # TWO independent reasons that holds, and odin only owns the second: the
        # AWS provider marks `secret_string`/`value` sensitive itself, so real
        # tofu prints "(sensitive value)" of its own accord -- which is exactly
        # why this test does NOT assert `[REDACTED]` appears (nothing needed
        # redacting). odin's own scrub set is armed regardless, never relying on
        # the provider's discretion; a tofu that DOES print the values is what
        # tests/simulate/test_secret_no_leak.py runs against.
        assert store.get_stack(ENV).sensitive_values() == frozenset({SECRET_VALUE, PARAM_VALUE})

        # Teardown through the ONLY human surface (empty canvas + Apply).
        resp = client.post("/apply-full", json=EMPTY_CANVAS, params={"env": ENV})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}
        assert not secretsctl.secret_exists(stores, ENV, SECRET)
        assert not ssmctl.parameter_exists(stores, ENV, PARAM)
        assert stores.secretsctl.items(ENV) == {}

    ps_after = _docker("ps", "-a", "--filter", f"name={container_name(ENV, FUNCTION)}", "--format", "{{.Names}}")
    assert ps_after.stdout.strip() == "", f"lambda container survived teardown: {ps_after.stdout}"
    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name={ENV}")
    assert leftover.stdout.strip() == ""
