"""v0.8.14 -- a Lambda may be a whole DIRECTORY, not one pasted file.

Three sources (`sourceDir` > `files` > `code`), one archive builder, and the
property every one of them has to keep: BYTE-DETERMINISM. `source_code_hash =
filebase64sha256(<zip>)` hashes the archive, so an archive that differs between
two translates of an unchanged canvas is `Plan: 0 to add, 1 to change` forever
and a function redeploy on every Apply (field-test 2, finding HIGH-4 -- the
reason `_deterministic_zip` exists at all).

Multi-file packaging puts a SECOND host-dependent input in reach that the
single-file shape never had: member ORDER, which a directory walk takes from
the filesystem. So the determinism proof here zips a real tree twice and
compares BYTES, and a second test reverses the on-disk creation order to prove
the walk order is not what the archive records.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from odin.iac import hcl
from odin.spec.translate import canvas_to_stack

ECHO = "def lambda_handler(event, context):\n    return event\n"


def _canvas(**data) -> dict:
    return {
        "nodes": [{"id": "n1", "type": "lambda", "data": {"label": "fn", **data}}],
        "edges": [],
    }


def _project(**data) -> hcl.TfProject:
    return hcl.generate_tf(canvas_to_stack(_canvas(**data)))


def _members(project: hcl.TfProject, name: str = "fn.zip") -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(project.binary_files[name])) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


PACKAGE = {
    "lambda_function.py": "from helpers import shout\n\n"
                          "def lambda_handler(event, context):\n    return shout(event['name'])\n",
    "helpers.py": "def shout(name):\n    return {'said': name.upper()}\n",
    "vendor/thirdparty/__init__.py": "VERSION = '1.0'\n",
}


# --- the whole point: a function that imports its own modules ---------------


def test_a_source_directory_packages_the_whole_tree(tmp_path):
    project = _project(sourceDir=str(_tree(tmp_path / "src", PACKAGE)))
    assert set(_members(project)) == set(PACKAGE)
    assert _members(project)["helpers.py"].decode() == PACKAGE["helpers.py"]
    # ...and the HCL still points at exactly this archive, unchanged.
    assert 'filename         = "fn.zip"' in project.files["main.tf"]
    assert 'source_code_hash = filebase64sha256("fn.zip")' in project.files["main.tf"]
    assert project.unsupported == []


def test_a_nested_directory_keeps_its_relative_path(tmp_path):
    """`vendor/thirdparty/__init__.py`, not `__init__.py` -- the archive member
    name IS the path the RIE container's `/var/task` gets, so a flattened member
    would break the very import the directory exists to allow."""
    assert "vendor/thirdparty/__init__.py" in _members(_project(sourceDir=str(_tree(tmp_path / "s", PACKAGE))))


def test_source_dir_expands_a_home_relative_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _tree(tmp_path / "fns" / "echo", {"lambda_function.py": ECHO})
    assert set(_members(_project(sourceDir="~/fns/echo"))) == {"lambda_function.py"}


# --- DETERMINISM, proven by bytes ------------------------------------------


def test_zipping_the_same_tree_twice_is_byte_identical(tmp_path):
    root = _tree(tmp_path / "src", PACKAGE)
    first = _project(sourceDir=str(root)).binary_files["fn.zip"]
    second = _project(sourceDir=str(root)).binary_files["fn.zip"]
    assert first == second


def test_the_archive_ignores_the_order_its_members_arrive_in():
    """The mutation-provable half of the determinism claim. `_package_paths`
    already sorts, so a filesystem-order test cannot fail on a machine whose
    walk happens to come back sorted anyway -- this drives `_deterministic_zip`
    directly with the SAME members inserted in opposite orders, which fails the
    moment its own `sorted()` goes."""
    members = {name: text.encode() for name, text in PACKAGE.items()}
    forward = hcl._deterministic_zip(dict(sorted(members.items())))
    backward = hcl._deterministic_zip(dict(sorted(members.items(), reverse=True)))
    assert forward == backward


def test_member_order_is_sorted_not_filesystem_order(tmp_path):
    """The same three files, created in OPPOSITE orders in two directories, must
    produce the same archive bytes -- otherwise the archive records the order the
    walk happened to return and `source_code_hash` becomes a property of the
    filesystem rather than of the code."""
    forward = _tree(tmp_path / "a", dict(sorted(PACKAGE.items())))
    backward = _tree(tmp_path / "b", dict(sorted(PACKAGE.items(), reverse=True)))
    assert _project(sourceDir=str(forward)).binary_files == _project(sourceDir=str(backward)).binary_files


def test_a_pyc_never_reaches_the_archive(tmp_path):
    """A `.pyc` embeds the source's mtime and size, so a tree that had merely
    been imported once would produce different bytes on every translate -- and
    CPython writes one into any source directory it imports from. This is the
    SUFFIX half of that exclusion (a stray `.pyc` beside the source, which is
    where a copied tree or an old build leaves them)."""
    root = _tree(tmp_path / "src", {**PACKAGE, "helpers.pyc": "not really bytecode"})
    clean = _tree(tmp_path / "clean", PACKAGE)
    assert set(_members(_project(sourceDir=str(root)))) == set(PACKAGE)
    assert _project(sourceDir=str(root)).binary_files == _project(sourceDir=str(clean)).binary_files


def test_a_skipped_directory_is_skipped_WHATEVER_it_holds(tmp_path):
    """The DIRECTORY half, and it needs a member the suffix rule would not have
    caught anyway -- the first version of this test put a `.pyc` inside
    `__pycache__` and so proved only the suffix rule twice (it survived deleting
    the entire skip-directory set). `__pycache__` and `.venv` both routinely hold
    plain `.py` files, which is exactly what must not ship: a virtualenv is
    host-specific by construction and its size dwarfs the function."""
    root = _tree(tmp_path / "src", {
        **PACKAGE,
        "__pycache__/leftover.py": "print('stale')\n",
        ".venv/lib/python3.13/site-packages/wrong.py": "HOST_SPECIFIC = True\n",
    })
    clean = _tree(tmp_path / "clean", PACKAGE)
    assert set(_members(_project(sourceDir=str(root)))) == set(PACKAGE)
    assert _project(sourceDir=str(root)).binary_files == _project(sourceDir=str(clean)).binary_files


def test_a_symlink_is_not_followed_out_of_the_tree(tmp_path):
    root = _tree(tmp_path / "src", {"lambda_function.py": ECHO})
    (tmp_path / "outside.txt").write_text("a host file the user never put in their package")
    (root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    assert set(_members(_project(sourceDir=str(root)))) == {"lambda_function.py"}


def test_the_single_file_archive_is_unchanged_by_multi_file_support():
    """The v1 shape must still produce the archive it always did -- a byte
    change here is a redeploy of every existing lambda on every canvas."""
    members = _members(_project(code=ECHO))
    assert members == {"lambda_function.py": ECHO.encode()}
    (info,) = zipfile.ZipFile(io.BytesIO(_project(code=ECHO).binary_files["fn.zip"])).infolist()
    assert info.date_time == (1980, 1, 1, 0, 0, 0)
    assert info.external_attr >> 16 == 0o100644
    assert info.create_system == 3


# --- refusals: every one of them names what is wrong ------------------------


def test_a_source_dir_that_is_not_a_directory_is_refused(tmp_path):
    project = _project(sourceDir=str(tmp_path / "nope"))
    assert project.binary_files == {}
    assert "aws_lambda_function" not in project.files["main.tf"]
    (reason,) = project.unsupported
    assert "is not a directory on the machine running odin" in reason and "nope" in reason


def test_a_file_passed_as_a_source_dir_is_refused(tmp_path):
    path = tmp_path / "lambda_function.py"
    path.write_text(ECHO)
    (reason,) = _project(sourceDir=str(path)).unsupported
    assert "is not a directory" in reason


def test_an_empty_source_dir_is_refused_not_silently_defaulted(tmp_path):
    """The trap this closes: falling back to `_DEFAULT_LAMBDA_CODE` would deploy
    odin's echo placeholder under the user's function name and call it applied."""
    (tmp_path / "empty").mkdir()
    (reason,) = _project(sourceDir=str(tmp_path / "empty")).unsupported
    assert "holds no files to package" in reason


def test_a_package_without_the_handler_module_is_refused(tmp_path):
    root = _tree(tmp_path / "src", {"helpers.py": "x = 1\n"})
    (reason,) = _project(sourceDir=str(root)).unsupported
    assert "handler 'lambda_function.lambda_handler' needs lambda_function.py" in reason
    assert "helpers.py" in reason


def test_a_custom_handler_names_the_module_it_needs(tmp_path):
    root = _tree(tmp_path / "src", {"app.py": ECHO})
    assert _project(sourceDir=str(root), handler="app.lambda_handler").unsupported == []
    (reason,) = _project(sourceDir=str(root), handler="main.lambda_handler").unsupported
    assert "needs main.py" in reason


def test_a_nested_handler_module_resolves_through_its_path(tmp_path):
    root = _tree(tmp_path / "src", {"src/app.py": ECHO})
    assert _project(sourceDir=str(root), handler="src/app.lambda_handler").unsupported == []


def test_a_node_handler_accepts_any_of_its_module_suffixes(tmp_path):
    for suffix in (".js", ".mjs", ".cjs"):
        root = _tree(tmp_path / suffix.lstrip("."), {f"index{suffix}": "exports.handler = async (e) => e;\n"})
        assert _project(sourceDir=str(root), runtime="nodejs20.x").unsupported == [], suffix


def test_an_oversized_source_dir_is_refused_with_the_measured_size(tmp_path, monkeypatch):
    monkeypatch.setattr(hcl, "_MAX_PACKAGE_BYTES", 1024)
    root = _tree(tmp_path / "src", {"lambda_function.py": ECHO, "big.bin": "x" * 4096})
    (reason,) = _project(sourceDir=str(root)).unsupported
    assert "MiB unzipped, over the" in reason


def test_a_declined_function_stops_at_its_own_node(tmp_path):
    """A refusal must not take the canvas with it: the OTHER nodes still build,
    and no `fn.zip` is written for a block that was never emitted (a
    `filebase64sha256()` on a missing file fails `tofu plan` for the whole
    project, not just that resource)."""
    canvas = {
        "nodes": [
            {"id": "n1", "type": "lambda", "data": {"label": "fn", "sourceDir": str(tmp_path / "gone")}},
            {"id": "n2", "type": "s3", "data": {"label": "uploads"}},
        ],
        "edges": [],
    }
    project = hcl.generate_tf(canvas_to_stack(canvas))
    assert project.binary_files == {}
    assert 'resource "aws_s3_bucket" "uploads"' in project.files["main.tf"]
    assert "aws_lambda_function" not in project.files["main.tf"]
    assert len(project.unsupported) == 1


# --- `files`: the inline multi-file map an import writes --------------------


def test_an_inline_files_map_packages_every_entry():
    project = _project(files=PACKAGE)
    assert {name: text.encode() for name, text in PACKAGE.items()} == _members(project)


def test_files_and_a_source_dir_agreeing_produce_the_same_bytes(tmp_path):
    """The round-trip property: a package read out of an archive and re-packaged
    from `files` is the SAME archive, so an import followed by an apply is a
    zero-drift no-op rather than a redeploy."""
    root = _tree(tmp_path / "src", PACKAGE)
    assert _project(sourceDir=str(root)).binary_files == _project(files=PACKAGE).binary_files


def test_source_dir_wins_over_both_files_and_code(tmp_path):
    root = _tree(tmp_path / "src", {"lambda_function.py": ECHO})
    project = _project(sourceDir=str(root), files=PACKAGE, code="def lambda_handler(e, c): return 'inline'\n")
    assert set(_members(project)) == {"lambda_function.py"}
    assert _members(project)["lambda_function.py"].decode() == ECHO


def test_files_wins_over_code():
    project = _project(files={"lambda_function.py": ECHO}, code="def lambda_handler(e, c): return 'inline'\n")
    assert _members(project)["lambda_function.py"].decode() == ECHO


def test_an_escaping_files_key_is_refused():
    for name in ("../escape.py", "/etc/passwd", ""):
        (reason,) = _project(files={"lambda_function.py": ECHO, name: "x = 1\n"}).unsupported
        assert "must map a RELATIVE path" in reason, name


def test_a_non_text_files_value_is_refused():
    (reason,) = _project(files={"lambda_function.py": ECHO, "data.bin": 42}).unsupported
    assert "must map a RELATIVE path" in reason and "data.bin" in reason


def test_an_empty_files_map_falls_through_to_code():
    """`files: {}` is not a package, it is an absent field -- an importer that
    recovered nothing must not turn into an empty archive."""
    assert _members(_project(files={}, code=ECHO)) == {"lambda_function.py": ECHO.encode()}
