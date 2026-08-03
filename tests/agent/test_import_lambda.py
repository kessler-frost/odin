"""Terraform -> canvas for `aws_lambda_function` — the last kind, and the phantom.

## Two different things this closes

**The kind.** A function's CONFIG is all in the HCL; its CODE is in a
single-entry zip beside `main.tf` (`hcl.py::_lambda` references it by filename +
`filebase64sha256`). So directory mode recovers the body and text mode cannot,
and the difference is REPORTED: from HCL text alone the node comes back with
odin's `_DEFAULT_LAMBDA_CODE` placeholder, and letting that pass for someone's
own function silently is the exact substitution this module exists to refuse.

**The phantom role, which was a bug before any of this.** A lambda drawn with no
`role` gets an auto-generated `aws_iam_role` named `<function>-role` (hcl.py's
pass 3). Because `aws_iam_role` maps to a real canvas kind, importing odin's own
generated project produced a node the user never drew. Measured before the fix:
a one-lambda canvas round-tripped into a canvas holding one `iam_role` called
`thumbnailer-role` and NO function at all — the lambda was listed unsupported and
its bookkeeping took its place.

## The compromise, named

Detecting the auto-role reconstructs hcl.py's `<function>-role` naming
convention, which I avoided doing for the ec2 key pair (that one is followed by
reference). Here there is no alternative: the generated HCL carries no marker
separating an auto-role from a drawn one — `_iam_role` and the auto pass emit the
IDENTICAL default Lambda trust policy — so the name is the only signal. A user
who draws a role and names it `<function>-role` gets it folded in; the effect is
that their `role` field comes back empty and odin regenerates the same role, so
the Terraform is unchanged either way.
"""
from __future__ import annotations

import io
import zipfile

from odin.iac.hcl import generate_tf
from odin.iac.import_tf import parse_hcl, parse_hcl_dir, parse_hcl_text
from odin.spec.translate import canvas_to_stack

CODE = "def lambda_handler(event, context):\n    return {'ok': True}\n"

CANVAS = {
    "nodes": [
        {"id": "f1", "type": "lambda", "position": {"x": 0, "y": 0},
         "data": {"label": "thumbnailer", "runtime": "python3.13", "code": CODE}},
    ],
    "edges": [],
}


def _generated(canvas: dict = CANVAS):
    return generate_tf(canvas_to_stack(canvas))


def _node(result, label: str) -> dict:
    return next(n for n in result.nodes if n["id"] == label)


def test_a_function_comes_back_as_a_node():
    result = parse_hcl_text(_generated().files["main.tf"])
    assert [n["type"] for n in result.nodes] == ["lambda"]
    assert "aws_lambda_function" not in {e.type for e in result.unsupported}


def test_the_auto_generated_role_does_NOT_become_a_node():
    """THE phantom. Before this, a one-lambda canvas imported as one `iam_role`
    and no function -- odin's own bookkeeping standing in for the user's work."""
    result = parse_hcl_text(_generated().files["main.tf"])
    assert [n["id"] for n in result.nodes] == ["thumbnailer"]
    assert "thumbnailer-role" not in [n["id"] for n in result.nodes]


def test_a_folded_role_takes_its_warnings_with_it():
    """The per-resource honesty pass runs while the role is still a node, so its
    `assume_role_policy` warning outlived the fold and fired on EVERY lambda
    import. Warning noise is not harmless in a module whose value is that its
    warnings are worth reading."""
    project = _generated()
    result = parse_hcl(project.files, project.binary_files)
    assert result.warnings == [], result.warnings


def test_the_config_round_trips():
    result = parse_hcl_text(_generated().files["main.tf"])
    data = _node(result, "thumbnailer")["data"]
    assert data["runtime"] == "python3.13"
    assert data["handler"] == "lambda_function.lambda_handler"
    assert "role" not in data, "an auto-generated role must leave the field EMPTY, as drawn"


def test_directory_mode_recovers_the_real_code_from_the_zip():
    project = _generated()
    result = parse_hcl(project.files, project.binary_files)
    assert _node(result, "thumbnailer")["data"]["code"] == CODE


def test_text_mode_reports_the_code_it_could_not_read():
    """odin's DEFAULT payload passing for someone's own function, silently, is
    the worst thing this importer could do."""
    result = parse_hcl_text(_generated().files["main.tf"])
    assert "code" not in _node(result, "thumbnailer")["data"]
    (warning,) = [w for w in result.warnings if "CODE" in w]
    assert "thumbnailer.zip" in warning, warning
    assert "NOT your function" in warning, warning


def test_the_whole_thing_regenerates_byte_for_byte_INCLUDING_the_zip():
    """The strongest statement available: config AND body survive, so a project
    can go out to Terraform and come back with nothing lost."""
    project = _generated()
    imported = parse_hcl(project.files, project.binary_files)
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == project.files["main.tf"]
    assert again.binary_files == project.binary_files


def test_a_directory_import_reads_the_zip_off_disk(tmp_path):
    """`parse_hcl_dir` is the real caller (`odin translate import <dir>`), and it
    had to learn to read `*.zip` -- it globbed only `*.tf`."""
    project = _generated()
    for name, text in project.files.items():
        (tmp_path / name).write_text(text)
    for name, blob in project.binary_files.items():
        (tmp_path / name).write_bytes(blob)

    result = parse_hcl_dir(tmp_path)
    assert _node(result, "thumbnailer")["data"]["code"] == CODE
    assert result.warnings == [], result.warnings


# --- a role the user actually drew --------------------------------------------

DRAWN_ROLE = {
    "nodes": [
        {"id": "r1", "type": "iam_role", "position": {"x": 0, "y": 0},
         "data": {"label": "shared-exec"}},
        {"id": "f1", "type": "lambda", "position": {"x": 0, "y": 0},
         "data": {"label": "thumbnailer", "code": CODE, "role": "shared-exec"}},
    ],
    "edges": [],
}


def test_a_drawn_role_stays_a_node_and_stays_referenced():
    """The fold must not eat a role the user authored -- its name is not
    `<function>-role`, which is the whole signal."""
    project = _generated(DRAWN_ROLE)
    result = parse_hcl(project.files, project.binary_files)
    assert {n["id"] for n in result.nodes} == {"shared-exec", "thumbnailer"}
    assert _node(result, "thumbnailer")["data"]["role"] == "shared-exec"


def test_a_drawn_role_canvas_regenerates_byte_for_byte():
    project = _generated(DRAWN_ROLE)
    imported = parse_hcl(project.files, project.binary_files)
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == project.files["main.tf"]


def test_a_function_whose_role_is_not_in_the_file_says_so():
    tf = '''
resource "aws_lambda_function" "fn" {
  function_name    = "orphan"
  role             = aws_iam_role.gone.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  filename         = "orphan.zip"
  source_code_hash = filebase64sha256("orphan.zip")
}
'''
    result = parse_hcl_text(tf)
    (warning,) = [w for w in result.warnings if "`role`" in w]
    assert "auto-generate an execution role" in warning, warning


def test_a_corrupt_zip_is_reported_rather_than_crashing_the_import():
    """A .tf directory is an untrusted input (SECURITY.md), so a truncated or
    non-zip file must not take the whole import down."""
    project = _generated()
    result = parse_hcl(project.files, {"thumbnailer.zip": b"not a zip at all"})
    assert "code" not in _node(result, "thumbnailer")["data"]
    assert any("CODE" in w for w in result.warnings), result.warnings


# --- v0.8.14: a MULTI-FILE package, which is where `namelist()[0]` broke -----

HELPERS = "def shout(name):\n    return {'said': name.upper()}\n"
BODY = "from helpers import shout\n\ndef lambda_handler(event, context):\n    return shout(event['name'])\n"
MULTI = {"lambda_function.py": BODY, "helpers.py": HELPERS, "vendor/dep.py": "VERSION = '1.0'\n"}

MULTI_CANVAS = {
    "nodes": [
        {"id": "f1", "type": "lambda", "position": {"x": 0, "y": 0},
         "data": {"label": "thumbnailer", "runtime": "python3.13", "files": MULTI}},
    ],
    "edges": [],
}


def test_every_member_of_a_multi_file_package_reaches_the_canvas():
    """Before v0.8.14 the importer read `namelist()[0]` and called it the
    function's whole body. Sorted, that is `helpers.py` here -- so a helper
    module would have become the function, silently, and been re-applied as it."""
    project = _generated(MULTI_CANVAS)
    node = _node(parse_hcl(project.files, project.binary_files), "thumbnailer")
    assert node["data"]["files"] == MULTI
    # ...and NOT smuggled into `code`, where a single-file textarea would then
    # show one member of a three-file package as the whole function.
    assert "code" not in node["data"]


def test_a_multi_file_package_regenerates_byte_for_byte():
    """The same statement the single-file round trip makes, for a tree: import
    then re-translate is a zero-drift no-op, not a redeploy."""
    project = _generated(MULTI_CANVAS)
    imported = parse_hcl(project.files, project.binary_files)
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == project.files["main.tf"]
    assert again.binary_files == project.binary_files


def test_a_single_file_package_still_comes_back_as_code_not_files():
    """The v1 shape must keep landing in the field the config panel edits."""
    project = _generated()
    node = _node(parse_hcl(project.files, project.binary_files), "thumbnailer")
    assert node["data"]["code"] == CODE and "files" not in node["data"]


def _archive(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, blob in members.items():
            archive.writestr(name, blob)
    return buf.getvalue()


def test_a_binary_member_is_named_rather_than_dropped_in_silence():
    """A canvas carries a package as TEXT, so a vendored `.so` cannot ride
    along. Saying which files were left behind is the whole difference between
    a known limit and a function that quietly stops working."""
    project = _generated(MULTI_CANVAS)
    result = parse_hcl(project.files, {"thumbnailer.zip": _archive({
        "lambda_function.py": BODY.encode(),
        "helpers.py": HELPERS.encode(),
        "_speedups.so": b"\x7fELF\x02\x01\x01\x00\xff\xfe",
    })})
    assert _node(result, "thumbnailer")["data"]["files"] == {"lambda_function.py": BODY, "helpers.py": HELPERS}
    (warning,) = [w for w in result.warnings if "not text" in w]
    assert "_speedups.so" in warning and "sourceDir" in warning, warning
