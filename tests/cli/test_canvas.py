"""`odin canvas get` / `odin canvas set` against a respx-mocked server."""
from __future__ import annotations

import json

import httpx
import respx

from odin.cli.app import app
from tests.cli.conftest import BASE

GRAPH = {"nodes": [{"id": "uploads", "type": "s3"}], "edges": []}


@respx.mock
def test_canvas_get_json(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    result = runner.invoke(app, ["canvas", "get", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == GRAPH


@respx.mock
def test_canvas_get_text(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    result = runner.invoke(app, ["canvas", "get"])
    assert result.exit_code == 0
    assert "1 nodes" in result.stdout
    assert "0 edges" in result.stdout


@respx.mock
def test_canvas_get_honors_url_option_and_envvar(runner):
    respx.get("http://odin.local:9999/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    assert runner.invoke(app, ["canvas", "get", "--url", "http://odin.local:9999"]).exit_code == 0
    assert runner.invoke(app, ["canvas", "get"], env={"ODIN_URL": "http://odin.local:9999"}).exit_code == 0


@respx.mock
def test_canvas_get_server_down(runner):
    respx.get(f"{BASE}/canvas").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["canvas", "get"])
    assert result.exit_code == 2
    assert "Could not reach odin server at http://localhost:4200" in result.stderr
    assert "odin start" in result.stderr


@respx.mock
def test_canvas_set_from_file(runner, tmp_path):
    route = respx.post(f"{BASE}/canvas").mock(
        return_value=httpx.Response(200, json={"status": "saved"})
    )
    canvas_file = tmp_path / "canvas.json"
    canvas_file.write_text(json.dumps(GRAPH))
    result = runner.invoke(app, ["canvas", "set", str(canvas_file)])
    assert result.exit_code == 0
    assert "saved" in result.stdout
    assert json.loads(route.calls.last.request.content) == GRAPH


@respx.mock
def test_canvas_set_from_stdin_json_mode(runner):
    respx.post(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json={"status": "saved"}))
    result = runner.invoke(app, ["canvas", "set", "-", "-o", "json"], input=json.dumps(GRAPH))
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "saved"}


def test_canvas_set_rejects_malformed_json(runner, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    result = runner.invoke(app, ["canvas", "set", str(bad)])
    assert result.exit_code == 1
    assert "not valid JSON" in result.stderr


@respx.mock
def test_canvas_set_server_down(runner):
    respx.post(f"{BASE}/canvas").mock(side_effect=httpx.ConnectTimeout("slow"))
    result = runner.invoke(app, ["canvas", "set", "-"], input="{}")
    assert result.exit_code == 2
    assert "Could not reach odin server" in result.stderr
