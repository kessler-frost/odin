"""TF workspace materialization for the tofu runner (S2).

Writes an env's TF workspace under `.odin/{env}/tf/`: `main.tf` verbatim from
`generate_tf`'s `TfProject` (agent/deterministic-generator-owned, portable --
no endpoints, no `skip_*`, no credentials) and `override.tf` (odin-owned).
`override.tf` is one of Terraform/OpenTofu's recognized *override filenames*
-- its blocks merge into the matching block address from the regular
config, attribute by attribute, rather than needing to be a full copy -- so
it can carry just the four verified `skip_*` args + `s3_use_path_style`
(research §1: `skip_requests_validation` does NOT exist) while main.tf's own
`provider "aws" { region = ... }` block supplies the rest. Real
identity/endpoint routing is env-var only (`AWS_ENDPOINT_URL`,
`AWS_ACCESS_KEY_ID`/`SECRET`, injected by the runner at invoke time) --
never baked into either file, so the workspace itself stays inspectable and
main.tf stays byte-identical to what a plain `generate_tf` call produces.

State stays local under the same directory (`.terraform/`,
`terraform.tfstate`) and is never touched by `materialize()` -- only
`main.tf`/`override.tf` are (re)written on every call, so re-materializing
for a new apply never disturbs an existing state file or downloaded
provider plugin.
"""
from __future__ import annotations

from pathlib import Path

from odin.agent.hcl import TfProject

# Exactly the four verified `skip_*` provider args (research §1 --
# `skip_requests_validation` is NOT a real argument) + `s3_use_path_style`
# (mandatory: no per-bucket DNS locally). No `region`/`access_key`/
# `secret_key`/`endpoints {}` here -- region lives in main.tf's own
# provider block (merged by the override mechanism), creds/endpoints are
# env-var only (research §1's "DX win").
_OVERRIDE_ATTRS = {
    "skip_credentials_validation": "true",
    "skip_metadata_api_check": "true",
    "skip_region_validation": "true",
    "skip_requesting_account_id": "true",
    "s3_use_path_style": "true",
}


def _override_tf() -> str:
    width = max(len(key) for key in _OVERRIDE_ATTRS)
    lines = "\n".join(f"  {key.ljust(width)} = {value}" for key, value in _OVERRIDE_ATTRS.items())
    return f'provider "aws" {{\n{lines}\n}}\n'


OVERRIDE_TF = _override_tf()


def tf_dir(root: Path, env: str) -> Path:
    return root / env / "tf"


def materialize(root: Path, env: str, project: TfProject) -> Path:
    """Write `project.files` (main.tf) + the generated override.tf into the
    env's TF workspace, creating it if needed. Returns the workspace dir."""
    workspace = tf_dir(root, env)
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in project.files.items():
        (workspace / name).write_text(content)
    (workspace / "override.tf").write_text(OVERRIDE_TF)
    return workspace
