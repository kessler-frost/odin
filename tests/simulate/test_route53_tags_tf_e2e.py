"""Hosted-zone tags through a REAL `tofu apply`: does removing one from the
config actually remove it from odin, or does it linger?

WHY IT NEEDED A REAL PROVIDER. `route53ctl._change_tags_for_resource` merges
by UNION -- a key in neither `AddTags` nor `RemoveTagKeys` is kept -- which is
correct if and only if the provider announces removals explicitly. If it
instead re-sent the whole desired set with the dropped key merely absent, the
union would keep it forever and `ListTagsForResource` would keep replaying it.
`RemoveTagKeys` had unit coverage, but every request in it was built by BOTO3,
and a fabricated upstream signal proves the parser rather than the
integration. `docs/limits.md` carried the gap in those words until this file
answered it.

MEASURED, OpenTofu 1.12.3 / hashicorp/aws 5.100.0, 2026-08-03. The provider's
second `ChangeTagsForResource` was, byte for byte:

    <ChangeTagsForResourceRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
      <RemoveTagKeys><Key>tier</Key></RemoveTagKeys>
    </ChangeTagsForResourceRequest>

-- a removal and nothing else. No docker: Route 53 is an all-synth model, so
this needs `tofu` on PATH and no backing container at all.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID
from odin.gateway.models import route53ctl
from odin.server import create_app
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "r53tags-e2e"
ZONE = "tagprobe.internal"


def _main_tf(tags: dict[str, str]) -> str:
    body = "\n".join(f'    "{key}" = "{value}"' for key, value in tags.items())
    return f"""\
terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
  }}
}}

provider "aws" {{
  region                      = "us-east-1"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}}

resource "aws_route53_zone" "probe" {{
  name = "{ZONE}"

  tags = {{
{body}
  }}
}}
"""


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "tf"


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-route53-tags-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


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


def _tofu(args, workspace, env_vars, timeout=300):
    return subprocess.run(["tofu", *args, "-input=false", "-no-color"],
                          cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout)


def _stored_tags(store_root: Path) -> dict:
    path = store_root / ENV / "gateway" / "tags.json"
    return json.loads(path.read_text()).get(f"route53:{ZONE}", {}) if path.exists() else {}


def test_a_tag_removed_from_the_config_is_removed_from_odin_not_left_behind(
    store_root, workspace, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    workspace.mkdir(parents=True)

    # Record the RAW bodies the provider sends, so the claim is about the wire
    # and not about what odin chose to store. Wrapping the module attribute is
    # enough: `synth` resolves `route53ctl.pure_answer` at call time.
    sent: list[bytes] = []
    real = route53ctl.pure_answer

    async def recording(action, resource, env, body, stores, now):
        if action == "route53:ChangeTagsForResource":
            sent.append(body)
        return await real(action, resource, env, body, stores, now)

    monkeypatch.setattr(route53ctl, "pure_answer", recording)

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as http:
        gateway_port = http.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)

        (workspace / "main.tf").write_text(_main_tf({"team": "platform", "tier": "prod"}))
        init = _tofu(["init"], workspace, env_vars)
        assert init.returncode == 0, f"init failed:\n{init.stdout}\n{init.stderr}"

        first = _tofu(["apply", "-auto-approve"], workspace, env_vars)
        assert first.returncode == 0, f"first apply failed:\n{first.stdout}\n{first.stderr}"
        assert _stored_tags(store_root) == {"team": "platform", "tier": "prod"}

        # --- the whole question: delete ONE tag and re-apply ------------------
        (workspace / "main.tf").write_text(_main_tf({"team": "platform"}))
        sent.clear()
        second = _tofu(["apply", "-auto-approve"], workspace, env_vars)
        assert second.returncode == 0, f"second apply failed:\n{second.stdout}\n{second.stderr}"

        # CLAIM 1, about the WIRE: the provider announces the removal. If it had
        # sent a full replacement instead, this would be an `AddTags` carrying
        # only `team`, and the union in `_change_tags_for_resource` would be the
        # wrong shape. The expected substrings are literals, not built from the
        # tag dicts above.
        assert len(sent) == 1, f"expected exactly one tag call on the re-apply, got {sent}"
        body = sent[0].decode()
        assert "<RemoveTagKeys><Key>tier</Key></RemoveTagKeys>" in body, body
        assert "<AddTags>" not in body, f"a full replacement, not a removal: {body}"

        # CLAIM 2, about the STORE: the tag is really gone, not merely unsent.
        assert _stored_tags(store_root) == {"team": "platform"}

        # CLAIM 3, the independent half: the provider reads the tags back and
        # AGREES. A lingering tag shows up here as drift even if claims 1 and 2
        # somehow passed, because `ListTagsForResource` is what it compares to.
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, (
            f"drift after the tag removal (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}")
