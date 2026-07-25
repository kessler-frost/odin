"""`odin translate` / `odin import-tf` against a respx-mocked server."""
from __future__ import annotations

import json

import httpx
import respx

from odin.cli.app import app
from tests.cli.conftest import BASE

MAIN_TF = 'resource "aws_s3_bucket" "uploads" {\n  bucket = "uploads"\n}\n'
TRANSLATED = {
    "files": {"main.tf": MAIN_TF},
    "notes": [], "unsupported": ["ecs"], "refined": False, "binary_files": {},
}
IMPORTED = {
    "nodes": [{"id": "uploads", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "uploads"}}],
    "edges": [],
    "unsupported": [{"type": "aws_iam_role", "name": "r", "reason": "not supported"}],
}
GRAPH = {"nodes": [{"id": "uploads", "type": "s3"}], "edges": []}


@respx.mock
def test_translate_default_posts_the_saved_canvas(runner):
    """Field-test 2 findings B4/MEDIUM-10: with no `--file` this used to POST
    an empty body, which made the server translate the env's STORED STACK --
    empty until something has been applied -- so `odin canvas set x.json &&
    odin translate` printed only the terraform{}/provider{} blocks, exit 0.
    The default now matches what README promises and what `odin apply` does:
    the canvas saved on the server."""
    canvas = respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    route = respx.post(f"{BASE}/translate", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=TRANSLATED)
    )
    result = runner.invoke(app, ["translate"])
    assert result.exit_code == 0
    assert canvas.called
    assert json.loads(route.calls.last.request.content) == GRAPH
    assert result.stdout == MAIN_TF
    assert "unsupported: ecs" in result.stderr


@respx.mock
def test_translate_with_file_posts_that_canvas(runner, tmp_path):
    canvas_file = tmp_path / "canvas.json"
    canvas_file.write_text(json.dumps(GRAPH))
    route = respx.post(f"{BASE}/translate", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json=TRANSLATED)
    )
    result = runner.invoke(app, ["translate", "--env", "prod", "--file", str(canvas_file)])
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content) == GRAPH


@respx.mock
def test_translate_notes_an_empty_saved_canvas_on_stderr(runner):
    """The other half of B4/U5: nothing is drawn, so there IS no Terraform --
    exit 0 (same as `apply`'s legitimate empty-canvas teardown), but never
    silently, so `odin translate > main.tf` in CI can't look like a success
    that produced a real file."""
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json={"nodes": [], "edges": []}))
    respx.post(f"{BASE}/translate").mock(return_value=httpx.Response(
        200, json={**TRANSLATED, "unsupported": []},
    ))
    result = runner.invoke(app, ["translate"])
    assert result.exit_code == 0
    assert "saved canvas is empty" in result.stderr
    assert "--file" in result.stderr


@respx.mock
def test_translate_json_mode_prints_full_result(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/translate").mock(return_value=httpx.Response(200, json=TRANSLATED))
    result = runner.invoke(app, ["translate", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == TRANSLATED
    assert result.stderr == ""


@respx.mock
def test_translate_server_down(runner):
    respx.get(f"{BASE}/canvas").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["translate"])
    assert result.exit_code == 2
    assert "Could not reach odin server" in result.stderr


@respx.mock
def test_import_tf_hcl_mode_stdout_is_canvas_shaped(runner, tmp_path):
    tf_file = tmp_path / "main.tf"
    tf_file.write_text(MAIN_TF)
    route = respx.post(f"{BASE}/import-tf").mock(return_value=httpx.Response(200, json=IMPORTED))
    result = runner.invoke(app, ["import-tf", str(tf_file)])
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content) == {"source": "hcl", "hcl": MAIN_TF}
    # stdout is EXACTLY {"nodes":..., "edges":...} — pipeable into `odin canvas set -`
    assert json.loads(result.stdout) == {"nodes": IMPORTED["nodes"], "edges": []}
    assert "aws_iam_role" in result.stderr


@respx.mock
def test_import_tf_json_mode_same_stdout(runner, tmp_path):
    tf_file = tmp_path / "main.tf"
    tf_file.write_text(MAIN_TF)
    respx.post(f"{BASE}/import-tf").mock(return_value=httpx.Response(200, json=IMPORTED))
    result = runner.invoke(app, ["import-tf", str(tf_file), "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"nodes": IMPORTED["nodes"], "edges": []}


@respx.mock
def test_import_tf_live_mode(runner):
    route = respx.post(f"{BASE}/import-tf", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json={**IMPORTED, "unsupported": []})
    )
    result = runner.invoke(
        app, ["import-tf", "--live", "s3=uploads", "--live", "sqs=orders", "--env", "prod"]
    )
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content) == {
        "source": "live",
        "resources": [{"type": "s3", "id": "uploads"}, {"type": "sqs", "id": "orders"}],
    }
    assert result.stderr == ""


def test_import_tf_live_malformed_spec(runner):
    result = runner.invoke(app, ["import-tf", "--live", "s3uploads"])
    assert result.exit_code == 2
    assert "type=id" in result.stderr


def test_import_tf_requires_file_or_live(runner):
    result = runner.invoke(app, ["import-tf"])
    assert result.exit_code == 2
    assert "needs a <file.tf>" in result.stderr


# --- v0.7.1: a real Terraform project IS a directory (field test B7) ----------


@respx.mock
def test_import_tf_accepts_a_directory_and_sends_every_tf_as_one_canvas(runner, tmp_path):
    project = tmp_path / "tfproj"
    project.mkdir()
    (project / "network.tf").write_text('resource "aws_vpc" "net" {}\n')
    (project / "app.tf").write_text(MAIN_TF)
    (project / "README.md").write_text("not terraform\n")
    route = respx.post(f"{BASE}/import-tf").mock(return_value=httpx.Response(200, json=IMPORTED))

    result = runner.invoke(app, ["import-tf", str(project)])

    assert result.exit_code == 0
    sent = json.loads(route.calls.last.request.content)
    assert sent["source"] == "hcl"
    # ONE request, both files, sorted -- and the .md left out.
    assert len(route.calls) == 1
    assert sent["hcl"] == f"# ---- app.tf\n{MAIN_TF}\n# ---- network.tf\nresource \"aws_vpc\" \"net\" {{}}\n"
    assert "as ONE canvas: app.tf, network.tf" in result.stderr
    assert json.loads(result.stdout) == {"nodes": IMPORTED["nodes"], "edges": []}


def test_import_tf_of_a_directory_with_no_tf_files_says_so(runner, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["import-tf", str(empty)])
    assert result.exit_code == 2
    assert "no *.tf files in" in result.stderr and "Traceback" not in result.stderr


def test_import_tf_of_a_missing_path_says_so_instead_of_tracebacking(runner, tmp_path):
    result = runner.invoke(app, ["import-tf", str(tmp_path / "nope.tf")])
    assert result.exit_code == 2
    assert "no such file or directory" in result.stderr and "Traceback" not in result.stderr


@respx.mock
def test_import_tf_malformed_hcl_exits_nonzero(runner, tmp_path):
    # Finding #7: a genuine parse failure is a hard error a CI exit-code check
    # catches -- not a silent exit 0 with an empty canvas.
    tf_file = tmp_path / "bad.tf"
    tf_file.write_text("not { valid hcl")
    respx.post(f"{BASE}/import-tf").mock(return_value=httpx.Response(200, json={
        "nodes": [], "edges": [], "unsupported": [], "warnings": [],
        "parse_error": "HCL failed to parse: something",
    }))
    result = runner.invoke(app, ["import-tf", str(tf_file)])
    assert result.exit_code != 0
    assert "failed to parse" in result.stderr
    assert result.stdout == ""  # never a canvas on a parse failure


@respx.mock
def test_import_tf_only_unsupported_stays_exit_zero(runner, tmp_path):
    # The other side of finding #7: a WELL-FORMED file with only unsupported
    # resources is a success (exit 0), just with an unsupported list on stderr.
    tf_file = tmp_path / "unsupported.tf"
    tf_file.write_text('resource "aws_cloudwatch_log_group" "logs" {\n  name = "logs"\n}\n')
    respx.post(f"{BASE}/import-tf").mock(return_value=httpx.Response(200, json={
        "nodes": [], "edges": [],
        "unsupported": [{"type": "aws_cloudwatch_log_group", "name": "logs", "reason": "not supported"}],
        "warnings": [], "parse_error": None,
    }))
    result = runner.invoke(app, ["import-tf", str(tf_file)])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"nodes": [], "edges": []}
    assert "aws_cloudwatch_log_group" in result.stderr


@respx.mock
def test_import_tf_server_down(runner, tmp_path):
    tf_file = tmp_path / "main.tf"
    tf_file.write_text(MAIN_TF)
    respx.post(f"{BASE}/import-tf").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["import-tf", str(tf_file)])
    assert result.exit_code == 2
    assert "odin start" in result.stderr
