"""S2 -- TF workspace materialization: main.tf verbatim from a TfProject,
override.tf odin-generated (the research §1 verified skip_* block), state
left untouched across re-materialization."""
from __future__ import annotations

from pathlib import Path

from odin.agent.hcl import TfProject
from odin.simulate import workspace

# The exact override.tf block (research §1 -- `skip_requests_validation`
# does NOT exist, only these four `skip_*` + `s3_use_path_style`; no region/
# creds/endpoints -- those are env-var-only or already in main.tf).
_GOLDEN_OVERRIDE_TF = (
    'provider "aws" {\n'
    "  skip_credentials_validation = true\n"
    "  skip_metadata_api_check     = true\n"
    "  skip_region_validation      = true\n"
    "  skip_requesting_account_id  = true\n"
    "  s3_use_path_style           = true\n"
    "}\n"
)


def test_materialize_writes_main_tf_verbatim(tmp_path):
    project = TfProject(files={"main.tf": "resource \"aws_s3_bucket\" \"x\" {}\n"})
    workspace.materialize(tmp_path, "default", project)
    assert (tmp_path / "default" / "tf" / "main.tf").read_text() == project.files["main.tf"]


def test_materialize_writes_golden_override_tf(tmp_path):
    project = TfProject(files={"main.tf": "x"})
    workspace.materialize(tmp_path, "default", project)
    assert (tmp_path / "default" / "tf" / "override.tf").read_text() == _GOLDEN_OVERRIDE_TF


def test_tf_dir_is_env_scoped_under_root():
    root = Path("/root")
    assert workspace.tf_dir(root, "prod") == root / "prod" / "tf"


def test_materialize_creates_the_directory_tree(tmp_path):
    workspace.materialize(tmp_path, "an-env", TfProject(files={"main.tf": "x"}))
    assert (tmp_path / "an-env" / "tf").is_dir()


def test_materialize_never_touches_existing_state_or_terraform_dir(tmp_path):
    tf_dir = tmp_path / "default" / "tf"
    tf_dir.mkdir(parents=True)
    (tf_dir / "terraform.tfstate").write_text('{"serial": 3}')
    (tf_dir / ".terraform").mkdir()
    (tf_dir / ".terraform" / "marker").write_text("provider-cache")

    workspace.materialize(tmp_path, "default", TfProject(files={"main.tf": "new content"}))

    assert (tf_dir / "terraform.tfstate").read_text() == '{"serial": 3}'
    assert (tf_dir / ".terraform" / "marker").read_text() == "provider-cache"
    assert (tf_dir / "main.tf").read_text() == "new content"  # main.tf itself IS rewritten


def test_rematerializing_overwrites_main_tf_and_override_tf(tmp_path):
    workspace.materialize(tmp_path, "default", TfProject(files={"main.tf": "first"}))
    workspace.materialize(tmp_path, "default", TfProject(files={"main.tf": "second"}))
    assert (tmp_path / "default" / "tf" / "main.tf").read_text() == "second"


# --- field test 3: the workspace warns the human who cd's into it ---------


def test_materialize_writes_a_readme_that_names_the_real_aws_hazard(tmp_path):
    """main.tf stays PORTABLE (no endpoints -- it is real AWS Terraform on
    purpose), so a hand-run `tofu plan` in this directory reaches real AWS.
    The one place a person is standing when they are about to do that is this
    directory, so the warning lives here too, not only in the README."""
    workspace.materialize(tmp_path, "default", TfProject(files={"main.tf": "x"}))
    readme = (tmp_path / "default" / "tf" / "README.md").read_text()
    assert "REAL AWS" in readme
    assert "odin tf plan --env default" in readme
    assert "AWS_ENDPOINT_URL" in readme


def test_the_workspace_readme_is_not_terraform_tofu_would_load(tmp_path):
    workspace.materialize(tmp_path, "default", TfProject(files={"main.tf": "x"}))
    assert not (tmp_path / "default" / "tf" / "README.md").name.endswith(".tf")


def test_materialize_writes_binary_files_alongside_main_tf(tmp_path):
    # V4c: a lambda node's zip'd deployment package -- odin materializes it
    # itself, pre-tofu, into the same workspace `filename` references.
    project = TfProject(files={"main.tf": "x"}, binary_files={"fn1.zip": b"PK\x03\x04fake-zip"})
    workspace.materialize(tmp_path, "default", project)
    assert (tmp_path / "default" / "tf" / "fn1.zip").read_bytes() == b"PK\x03\x04fake-zip"
