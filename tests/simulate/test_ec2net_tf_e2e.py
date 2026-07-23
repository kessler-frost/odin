"""V1a integration gate -- a real `tofu apply` -> zero-drift `plan` ->
`destroy` for 1 aws_vpc + 2 aws_subnet + 1 aws_security_group (2 ingress
rules) through the REAL gateway, answered entirely by the EC2-network model
(gateway/models/ec2net.py). Modeled on S2's test_tf_runner_e2e.py, with two
deliberate differences:

- No canvas/`generate_tf`: the container-node -> HCL mapping is V1c's layer
  (landing in parallel); the brief's gate is "workspace + operator
  principal, no UI needed", so main.tf is hand-authored to research
  §2a's exact resource shapes via agent/hcl's own HEADER/provider_block/
  quote helpers.
- No backing containers and NO `app.state.gateway.update(ENV, ...)` at all:
  EC2 is all-synth (never forwarded), and the OPERATOR principal is
  special-cased to full-allow in `GatewayState.statements_for` regardless
  of whether the env was ever registered -- this test proves empirically
  that a pure-EC2 env needs no registration. Everything (spec store, TF
  workspace, gateway sidecar stores) lives under pytest's tmp_path, so
  nothing lands in the repo's `.odin/` and cleanup is automatic -- no
  Colima/Lima involved.

Also the empirical proof for ec2net's delete-confirm choice (hard delete,
immediate NotFound): `tofu destroy` exits 0 with no grace window.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent.hcl import TfProject
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ec2net-tf-e2e"

_VPC = f"""resource "aws_vpc" "main" {{
  cidr_block = {hcl.quote("10.0.0.0/16")}
}}"""

_SUBNET_A = f"""resource "aws_subnet" "a" {{
  vpc_id     = aws_vpc.main.id
  cidr_block = {hcl.quote("10.0.1.0/24")}
}}"""

_SUBNET_B = f"""resource "aws_subnet" "b" {{
  vpc_id     = aws_vpc.main.id
  cidr_block = {hcl.quote("10.0.2.0/24")}
}}"""

_SG = f"""resource "aws_security_group" "web" {{
  name        = {hcl.quote("web")}
  description = {hcl.quote("web sg")}
  vpc_id      = aws_vpc.main.id

  ingress {{
    from_port   = 443
    to_port     = 443
    protocol    = {hcl.quote("tcp")}
    cidr_blocks = [{hcl.quote("10.0.0.0/16")}]
  }}

  ingress {{
    from_port   = 22
    to_port     = 22
    protocol    = {hcl.quote("tcp")}
    cidr_blocks = [{hcl.quote("192.168.0.0/24")}]
  }}
}}"""

MAIN_TF = "\n\n".join([hcl.HEADER, hcl.provider_block(), _VPC, _SUBNET_A, _SUBNET_B, _SG]) + "\n"


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


def _tofu(args: list[str], workspace, env_vars: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=300,
    )


def _ec2net_state(root, env: str) -> dict:
    path = root / env / "gateway" / "ec2net.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _kinds(state: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in state:
        kind = key.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def test_tf_apply_zero_drift_destroy_vpc_subnets_sg(tmp_path):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        workspace = workspace_mod.materialize(store.root, ENV, TfProject(files={"main.tf": MAIN_TF}))

        init = _tofu(["init"], workspace, env_vars)
        assert init.returncode == 0, f"init failed:\n{init.stdout}\n{init.stderr}"

        apply = _tofu(["apply", "-auto-approve"], workspace, env_vars)
        assert apply.returncode == 0, f"apply failed:\n{apply.stdout}\n{apply.stderr}"

        # The model store holds exactly the applied resources: 1 vpc, its 2
        # subnets, and 2 SGs (the vpc's auto-created default + "web").
        state = _ec2net_state(store.root, ENV)
        assert _kinds(state) == {"vpc": 1, "subnet": 2, "sg": 2}
        (web_sg,) = [v for k, v in state.items() if k.startswith("sg:") and not v["is_default"]]
        rules = list(web_sg["rules"].values())
        ingress = [r for r in rules if not r["is_egress"]]
        assert {(r["from_port"], r["cidr_ipv4"]) for r in ingress} == {(443, "10.0.0.0/16"), (22, "192.168.0.0/24")}
        # the provider revoked the seeded default egress (config declares none)
        assert [r for r in rules if r["is_egress"]] == []

        # zero drift: the research bar -- apply -> plan changes NOTHING
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        destroy = _tofu(["destroy", "-auto-approve"], workspace, env_vars)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"
        assert _kinds(_ec2net_state(store.root, ENV)) == {}
