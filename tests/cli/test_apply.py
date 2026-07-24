"""`odin apply` / `odin destroy` against a respx-mocked server."""
from __future__ import annotations

import json

import httpx
import respx

from odin.cli.app import app
from tests.cli.conftest import BASE

GRAPH = {"nodes": [{"id": "uploads", "type": "s3"}], "edges": []}
APPLIED = {
    "status": "applied", "rev": "abc123", "env": "default",
    "skipped": [], "refined": True, "unsupported": [],
    "tf": {"status": "ok", "exit_code": 0},
}
TF_FAILED = {
    "status": "applied_tf_failed", "rev": None, "env": "default",
    "skipped": ["note"], "refined": False, "unsupported": ["ecs"],
    "tf": {"status": "failed", "exit_code": 1, "tail": ["Error: BucketAlreadyExists", "apply failed"]},
    "note": "desired state not committed; fix and re-apply",
}


@respx.mock
def test_apply_fetches_saved_canvas_when_no_file(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    route = respx.post(f"{BASE}/apply-full", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=APPLIED)
    )
    result = runner.invoke(app, ["apply"])
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content) == GRAPH
    assert "status: applied" in result.stdout
    assert "rev: abc123" in result.stdout
    assert "tf: ok" in result.stdout


@respx.mock
def test_apply_with_file_posts_that_canvas(runner, tmp_path):
    canvas_file = tmp_path / "canvas.json"
    canvas_file.write_text(json.dumps(GRAPH))
    route = respx.post(f"{BASE}/apply-full", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json={**APPLIED, "env": "prod"})
    )
    result = runner.invoke(app, ["apply", "--env", "prod", "--file", str(canvas_file)])
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content) == GRAPH


@respx.mock
def test_apply_json_mode_prints_full_body(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=APPLIED))
    result = runner.invoke(app, ["apply", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == APPLIED


@respx.mock
def test_apply_tf_failed_streams_tail_and_exits_nonzero(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=TF_FAILED))
    result = runner.invoke(app, ["apply"])
    assert result.exit_code == 1
    assert "status: applied_tf_failed" in result.stdout
    assert "skipped: note" in result.stdout
    assert "unsupported: ecs" in result.stdout
    assert "Error: BucketAlreadyExists" in result.stdout
    assert "note: desired state not committed" in result.stdout


@respx.mock
def test_apply_tf_failed_json_mode_still_exits_nonzero(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=TF_FAILED))
    result = runner.invoke(app, ["apply", "-o", "json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == TF_FAILED


@respx.mock
def test_apply_busy_409_exits_nonzero(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(
        return_value=httpx.Response(
            409, json={"error": "a tofu run is already in progress for env 'default'"}
        )
    )
    result = runner.invoke(app, ["apply"])
    assert result.exit_code == 1
    assert "already in progress" in result.stderr
    assert result.stdout == ""


@respx.mock
def test_apply_server_down(runner):
    respx.get(f"{BASE}/canvas").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["apply"])
    assert result.exit_code == 2
    assert "Could not reach odin server" in result.stderr


@respx.mock
def test_destroy_text(runner):
    respx.post(f"{BASE}/destroy", params={"env": "default"}).mock(
        return_value=httpx.Response(
            200, json={"status": "destroyed", "env": "default", "tf": {"status": "ok", "exit_code": 0}}
        )
    )
    result = runner.invoke(app, ["destroy"])
    assert result.exit_code == 0
    assert "status: destroyed" in result.stdout
    assert "tf: ok" in result.stdout


@respx.mock
def test_destroy_json_without_tf_half(runner):
    body = {"status": "destroyed", "env": "prod", "tf": None}
    respx.post(f"{BASE}/destroy", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json=body)
    )
    result = runner.invoke(app, ["destroy", "--env", "prod", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == body


@respx.mock
def test_destroy_busy_409_exits_nonzero(runner):
    respx.post(f"{BASE}/destroy").mock(
        return_value=httpx.Response(
            409, json={"error": "a tofu run is already in progress for env 'default'"}
        )
    )
    result = runner.invoke(app, ["destroy"])
    assert result.exit_code == 1
    assert "already in progress" in result.stderr


@respx.mock
def test_destroy_server_down(runner):
    respx.post(f"{BASE}/destroy").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["destroy"])
    assert result.exit_code == 2
    assert "odin start" in result.stderr
