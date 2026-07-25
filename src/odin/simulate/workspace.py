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
from odin.util import (
    SECRET_FILE_MODE,
    atomic_write_bytes,
    atomic_write_text,
    ensure_private_file,
    private_mkdir,
)

# tofu's own state files. Neither is ever written by odin -- they are
# pre-CREATED 0600 (see `ensure_private_file`) so tofu's in-place rewrite
# inherits the mode on every apply, instead of a one-time chmod that the very
# next apply would undo.
_TOFU_STATE_FILES = ("terraform.tfstate", "terraform.tfstate.backup")

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
    """Write `project.files` (main.tf) + `project.binary_files` (V4c: a
    lambda node's zip'd deployment package, referenced by `filename` from
    its own aws_lambda_function block) + the generated override.tf into the
    env's TF workspace, creating it if needed. Returns the workspace dir."""
    workspace = private_mkdir(tf_dir(root, env))
    for name, content in project.files.items():
        atomic_write_text(workspace / name, content, mode=SECRET_FILE_MODE)
    for name, content in project.binary_files.items():
        atomic_write_bytes(workspace / name, content, mode=SECRET_FILE_MODE)
    atomic_write_text(workspace / "override.tf", OVERRIDE_TF, mode=SECRET_FILE_MODE)
    _lock_down(workspace)
    return workspace


def _lock_down(workspace: Path) -> None:
    """0600 for everything in the workspace, on every materialize.

    `main.tf` carries every canvas secret in cleartext (SECURITY.md: the
    generated Terraform is a legitimate home for the plaintext, precisely
    because tofu has to send it) and `terraform.tfstate` carries the same
    values back from the provider -- both were 0644 in v0.7.0, which voided
    the file-mode argument for exactly the files that matter.

    Two moves, because two different processes write here. odin's own files get
    their mode at write time above. tofu's files get the state pair
    pre-created 0600 (its rewrites are in-place, so that mode persists), plus a
    sweep of whatever else it may have dropped in the workspace this run (a
    `tfplan`, a `generated.tf`, a crash log). `.terraform/`'s provider cache is
    deliberately skipped: hundreds of downloaded plugin files, no secrets, and
    re-chmod'ing them on every apply would be pure waste.
    """
    for name in _TOFU_STATE_FILES:
        ensure_private_file(workspace / name)
    for path in (p for p in workspace.iterdir() if p.is_file()):
        path.chmod(SECRET_FILE_MODE)
