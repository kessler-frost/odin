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
`terraform.tfstate`) and is never touched by `materialize()` -- only the
odin-owned files are, so re-materializing for a new apply never disturbs an
existing state file or downloaded provider plugin.

"Regenerated on every apply" means the odin-owned file SET, not just the
bytes of the files that happen to be in the new project (field test 6). See
`_prune_stale`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from odin.agent.hcl import TfProject
from odin.util import (
    SECRET_FILE_MODE,
    atomic_write_bytes,
    atomic_write_text,
    ensure_private_file,
    private_mkdir,
)

log = logging.getLogger("odin")

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


# Field test 3: a field engineer opened this directory to check for drift,
# ran `tofu plan` by hand without the injected endpoint, and tofu talked to
# REAL AWS (it came back with a genuine `UnrecognizedClientException` from
# Amazon; on a machine with real credentials in the environment it would have
# planned against the real account). main.tf staying portable is deliberate --
# odin emits real AWS Terraform, and the translation guardrail forbids
# `endpoints`/`localhost` in it -- so the warning goes where the person is
# standing when they're about to make that mistake: in the workspace itself.
# Not a `.tf` file, so tofu never loads it.
_README_NAME = "README.md"
_README = """\
# odin's OpenTofu workspace for env `{env}`

**Running tofu by hand in this directory talks to REAL AWS.**

`main.tf` is portable, real-AWS Terraform on purpose: no `endpoints` block,
no `127.0.0.1`, no credentials. odin injects all of that at run time
(`AWS_ENDPOINT_URL` pointing at odin's own gateway, plus this env's operator
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) every time it runs tofu for you.
A hand-run `tofu plan`/`apply` here has none of it, so it goes to Amazon --
and with real credentials in your environment, to your real account.

Let odin run it instead; the endpoint cannot be gotten wrong that way:

    odin tf plan --env {env}       # drift check -- exit 0 none, 2 changes, 1 error
    odin apply --env {env}         # the apply
    odin tf destroy --env {env}    # tofu's own teardown

`main.tf`, `override.tf`, and this file are regenerated from the canvas on
every apply and every plan -- edits here are overwritten, not applied. A `.tf`
or `.zip` file odin did not just write is DELETED for the same reason: tofu
loads every `*.tf` in a directory, so a leftover one would be applied.
"""


# The suffixes odin itself writes into this directory: `project.files` (`.tf`)
# and `project.binary_files` (a lambda's `.zip`). Anything with one of these
# suffixes that THIS materialize did not write is stale by definition and is
# deleted -- see `_prune_stale`. Deliberately a suffix allow-list rather than
# "everything that isn't state": tofu's own artifacts must survive, and they
# do, because none of them ends in `.tf` or `.zip` (`terraform.tfstate`,
# `terraform.tfstate.backup`, `.terraform.lock.hcl`, `tfplan`, `.terraform/`).
_ODIN_OWNED_SUFFIXES = (".tf", ".zip")


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
    its own aws_lambda_function block) + the generated override.tf + the
    README that warns a human off hand-running tofu here (field test 3)
    into the env's TF workspace, creating it if needed. Returns the
    workspace dir."""
    workspace = private_mkdir(tf_dir(root, env))
    for name, content in project.files.items():
        atomic_write_text(workspace / name, content, mode=SECRET_FILE_MODE)
    for name, content in project.binary_files.items():
        atomic_write_bytes(workspace / name, content, mode=SECRET_FILE_MODE)
    atomic_write_text(workspace / "override.tf", OVERRIDE_TF, mode=SECRET_FILE_MODE)
    atomic_write_text(workspace / _README_NAME, _README.format(env=env), mode=SECRET_FILE_MODE)
    _prune_stale(workspace, {*project.files, *project.binary_files, "override.tf"})
    _lock_down(workspace)
    return workspace


def _prune_stale(workspace: Path, written: set[str]) -> list[str]:
    """Delete every odin-owned file this materialize did NOT write, so the
    workspace is the project rather than the union of every project ever
    applied here. Returns the names removed (for the log line and the tests).

    Field test 6 reported a lambda's `other.zip` surviving the failed apply
    that would have created lambda `other`, against a workspace documented as
    regenerated every apply. A stale ZIP turns out to be litter and nothing
    worse -- `hcl.generate_tf` writes `<hcl_name>.zip` for every lambda in the
    Stack and derives the `filename`/`filebase64sha256()` in the HCL block from
    the SAME name table, so a zip the current main.tf references is always one
    this same call just rewrote; a zip it doesn't reference is unreachable.

    The same staleness in a `.tf` file is NOT litter, and that is why this
    prunes by suffix rather than fixing the zip: tofu is pointed at a
    DIRECTORY and loads every `*.tf` in it. Measured on the real tofu, one
    stale file in an otherwise one-resource workspace:

        with leftover.tf     Plan: 2 to add, 0 to change, 0 to destroy
        without it           Plan: 1 to add, 0 to change, 0 to destroy

    -- a resource created for a node that is not on the canvas. It is reachable
    without anyone hand-editing anything: the agent refine pass emits a FILE
    SET (`translate.EmitTerraformInput.files` is a list of paths), the
    guardrail only requires the resource set to be unchanged, so one apply can
    legitimately write `main.tf` + `lambda.tf` and the next -- refine off,
    refine timing out, or the guardrail rejecting and falling back to the
    single-file skeleton -- writes only `main.tf` and used to leave `lambda.tf`
    behind to be applied forever.

    `README.md` is deliberately absent from `written` and survives anyway: its
    suffix is not an odin-owned one, so it is never a prune candidate in the
    first place (same as every tofu artifact).
    """
    stale = sorted(
        path for path in workspace.iterdir()
        if path.is_file() and path.suffix in _ODIN_OWNED_SUFFIXES and path.name not in written
    )
    for path in stale:
        path.unlink()
    if stale:
        log.warning(
            "removed %d stale file(s) from %s that this apply no longer generates: %s",
            len(stale), workspace, [path.name for path in stale],
        )
    return [path.name for path in stale]


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
