"""Field test 6: the tf workspace is the CURRENT project, not the union of
every project ever applied into it.

The report was a lambda's `other.zip` surviving the failed apply that would
have created lambda `other`, in a directory documented as regenerated on every
apply. Two different severities came out of chasing it, and the tests below keep
them apart on purpose:

* a stale `.zip` is litter. `hcl.generate_tf` derives a lambda block's
  `filename`/`filebase64sha256()` and the zip it writes from the SAME name
  table in the same call, so a zip the current `main.tf` names is always one
  that call just rewrote. `test_a_stale_zip_is_never_the_bytes_a_project_reads`
  pins that reasoning against the real generator rather than asserting it.
* a stale `.tf` is APPLIED. tofu is pointed at a directory and loads every
  `*.tf` in it -- measured on the real tofu (`Plan: 2 to add` with one stale
  file present, `Plan: 1 to add` without it) -- so a leftover file creates a
  resource for a node that is not on the canvas. That is what the prune is for,
  and `.zip` is included in it only because it is the same shape.
"""
from __future__ import annotations

from odin.iac.hcl import TfProject, generate_tf
from odin.simulate import workspace
from odin.spec.models import FieldValue, ResourceDesired, Stack

ENV = "default"


def _tf(root, env=ENV):
    return workspace.tf_dir(root, env)


def _lambda_stack(*names: str) -> Stack:
    return Stack(env=ENV, resources=[
        ResourceDesired(id=name, kind="lambda", fields={"code": FieldValue(value=f"# {name}\n")})
        for name in names
    ])


# --- the serious half: a leftover .tf would be applied -----------------------


def test_a_tf_file_the_project_no_longer_contains_is_removed(tmp_path):
    """The multi-file refinement path, which is how this happens without anyone
    hand-editing the workspace: `translate.EmitTerraformInput.files` is a LIST
    of paths and the guardrail only checks that the resource SET is unchanged,
    so one apply can write `main.tf` + `lambda.tf` and the next (refine off, or
    timed out, or guardrail-rejected back to the single-file skeleton) writes
    only `main.tf`."""
    workspace.materialize(tmp_path, ENV, TfProject(files={
        "main.tf": 'resource "random_pet" "kept" {}\n',
        "lambda.tf": 'resource "random_pet" "gone_from_the_canvas" {}\n',
    }))
    assert (_tf(tmp_path) / "lambda.tf").is_file()

    workspace.materialize(tmp_path, ENV, TfProject(files={"main.tf": 'resource "random_pet" "kept" {}\n'}))

    assert not (_tf(tmp_path) / "lambda.tf").exists()
    assert (_tf(tmp_path) / "main.tf").is_file()


def test_the_prune_reports_exactly_what_it_removed(tmp_path):
    workspace.materialize(tmp_path, ENV, TfProject(
        files={"main.tf": "a", "extra.tf": "b"}, binary_files={"fn.zip": b"PK\x03\x04"},
    ))
    removed = workspace._prune_stale(_tf(tmp_path), {"main.tf", "override.tf"})
    assert removed == ["extra.tf", "fn.zip"]


def test_a_stale_zip_is_removed_too_because_it_is_the_same_shape(tmp_path):
    """The reported symptom. Litter rather than a hazard (see the module
    docstring), but litter carrying the user's own function source at 0600 in a
    directory `odin backup` archives -- and pruning it is what makes the
    workspace's documented contract true rather than nearly true."""
    workspace.materialize(tmp_path, ENV, TfProject(files={"main.tf": "x"}, binary_files={"other.zip": b"PK\x03\x04"}))
    assert (_tf(tmp_path) / "other.zip").is_file()

    workspace.materialize(tmp_path, ENV, TfProject(files={"main.tf": "x"}))

    assert not (_tf(tmp_path) / "other.zip").exists()


# --- what the prune must NEVER touch ----------------------------------------


def test_the_prune_never_touches_tofus_own_files(tmp_path):
    """State, the state backup, the dependency lock and the provider cache are
    tofu's, and deleting any of them would be a far worse bug than the one this
    closes -- a lost `terraform.tfstate` orphans every real resource it names.
    None of them has an odin-owned suffix, which is why the allow-list is by
    suffix."""
    directory = _tf(tmp_path)
    directory.mkdir(parents=True)
    (directory / "terraform.tfstate").write_text('{"serial": 3}')
    (directory / "terraform.tfstate.backup").write_text('{"serial": 2}')
    (directory / ".terraform.lock.hcl").write_text("provider locks")
    (directory / "tfplan").write_bytes(b"binary plan")
    (directory / ".terraform").mkdir()
    (directory / ".terraform" / "marker").write_text("provider-cache")

    workspace.materialize(tmp_path, ENV, TfProject(files={"main.tf": "x"}))

    assert (directory / "terraform.tfstate").read_text() == '{"serial": 3}'
    assert (directory / "terraform.tfstate.backup").read_text() == '{"serial": 2}'
    assert (directory / ".terraform.lock.hcl").read_text() == "provider locks"
    assert (directory / "tfplan").read_bytes() == b"binary plan"
    assert (directory / ".terraform" / "marker").read_text() == "provider-cache"


def test_a_project_with_no_main_tf_leaves_no_main_tf_behind(tmp_path):
    """The prune's semantic, stated deliberately: the workspace IS the project.
    A project with no `main.tf` therefore leaves none -- which is the right
    direction (a stale main.tf would be re-applied) and is currently
    unreachable anyway, because every producer emits one: `generate_tf` always
    returns `{"main.tf": HEADER + provider_block()}` even for an empty Stack,
    and `translate` carries the skeleton's files on every path including the
    no-supported-resources short-circuit. Pinned so a future reader knows this
    is a decision and not an oversight."""
    workspace.materialize(tmp_path, ENV, TfProject(files={"main.tf": "x"}))
    workspace.materialize(tmp_path, ENV, TfProject(files={}))
    assert not (_tf(tmp_path) / "main.tf").exists()
    assert (_tf(tmp_path) / "override.tf").is_file()  # odin's own is still written


def test_the_prune_keeps_the_files_this_materialize_just_wrote(tmp_path):
    project = TfProject(
        files={"main.tf": "x", "outputs.tf": "y"}, binary_files={"fn1.zip": b"PK\x03\x04"},
    )
    workspace.materialize(tmp_path, ENV, project)
    directory = _tf(tmp_path)
    assert sorted(p.name for p in directory.iterdir()) == [
        "README.md", "fn1.zip", "main.tf", "outputs.tf", "override.tf",
        "terraform.tfstate", "terraform.tfstate.backup",
    ]


# --- the "is a stale zip ever CONSUMED?" question, answered on the generator --


def test_a_stale_zip_is_never_the_bytes_a_project_reads(tmp_path):
    """Why the zip half is (a) litter and not (b) a stale artifact an apply can
    pick up: every zip a generated `main.tf` names is written by the SAME
    `generate_tf` call, off the same HCL-name table. Asserted against the real
    generator, for the exact rename that produced the report: the surviving
    `other.zip` is unreferenced, and the zip the new project DOES reference
    carries the new project's bytes."""
    first = generate_tf(_lambda_stack("other"))
    assert set(first.binary_files) == {"other.zip"}
    workspace.materialize(tmp_path, ENV, first)

    second = generate_tf(_lambda_stack("renamed"))
    workspace.materialize(tmp_path, ENV, second)

    # Every `filename`/`filebase64sha256()` the new main.tf names is a file
    # this very materialize wrote -- so no apply can read stale bytes.
    for name in second.binary_files:
        assert name in second.files["main.tf"]
        assert (_tf(tmp_path) / name).read_bytes() == second.binary_files[name]
    assert "other.zip" not in second.files["main.tf"]
