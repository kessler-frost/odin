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
def test_translate_default_posts_no_body_and_prints_main_tf(runner):
    route = respx.post(f"{BASE}/translate", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=TRANSLATED)
    )
    result = runner.invoke(app, ["translate"])
    assert result.exit_code == 0
    assert route.calls.last.request.content == b""  # server uses the stored Stack
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
def test_translate_json_mode_prints_full_result(runner):
    respx.post(f"{BASE}/translate").mock(return_value=httpx.Response(200, json=TRANSLATED))
    result = runner.invoke(app, ["translate", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == TRANSLATED
    assert result.stderr == ""


@respx.mock
def test_translate_server_down(runner):
    respx.post(f"{BASE}/translate").mock(side_effect=httpx.ConnectError("refused"))
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


@respx.mock
def test_import_tf_server_down(runner, tmp_path):
    tf_file = tmp_path / "main.tf"
    tf_file.write_text(MAIN_TF)
    respx.post(f"{BASE}/import-tf").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["import-tf", str(tf_file)])
    assert result.exit_code == 2
    assert "odin start" in result.stderr
