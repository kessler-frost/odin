"""`odin world` / `odin envs` / `odin events` against a respx-mocked server."""
from __future__ import annotations

import json

import httpx
import respx

from odin.cli.app import app
from tests.cli.conftest import BASE

WORLD = {
    "env": "prod",
    "resources": [
        {"id": "db", "kind": "rds", "phase": "healthy", "facts": {}, "verdict": "pg_ready", "restarts": 0},
        {"id": "uploads", "kind": "s3", "phase": "starting", "facts": {}, "verdict": None, "restarts": 0},
    ],
}
EVENTS = [
    {"type": "world_delta", "env": "default", "resource_id": "db", "kind": "rds", "phase": "healthy"},
    {"type": "tf_run", "env": "default", "phase": "apply", "ok": True},
]


@respx.mock
def test_world_json(runner):
    respx.get(f"{BASE}/world", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json=WORLD)
    )
    result = runner.invoke(app, ["world", "--env", "prod", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == WORLD


@respx.mock
def test_world_text_renders_phase_table(runner):
    respx.get(f"{BASE}/world", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json=WORLD)
    )
    result = runner.invoke(app, ["world", "--env", "prod"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert "db" in lines[0] and "rds" in lines[0] and "healthy" in lines[0]
    assert "uploads" in lines[1] and "s3" in lines[1] and "starting" in lines[1]


@respx.mock
def test_world_text_empty(runner):
    respx.get(f"{BASE}/world", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json={"env": "default", "resources": []})
    )
    result = runner.invoke(app, ["world"])
    assert result.exit_code == 0
    assert "world is empty" in result.stdout


@respx.mock
def test_world_server_down(runner):
    respx.get(f"{BASE}/world").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["world"])
    assert result.exit_code == 2
    assert "Could not reach odin server" in result.stderr


@respx.mock
def test_envs_text_and_json(runner):
    respx.get(f"{BASE}/envs").mock(
        return_value=httpx.Response(200, json={"envs": ["default", "prod"]})
    )
    text = runner.invoke(app, ["envs"])
    assert text.exit_code == 0
    assert text.stdout.splitlines() == ["default", "prod"]
    as_json = runner.invoke(app, ["envs", "-o", "json"])
    assert json.loads(as_json.stdout) == {"envs": ["default", "prod"]}


@respx.mock
def test_envs_server_down(runner):
    respx.get(f"{BASE}/envs").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["envs"])
    assert result.exit_code == 2
    assert "odin start" in result.stderr


@respx.mock
def test_events_text_one_line_per_event(runner):
    respx.get(f"{BASE}/events", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=EVENTS)
    )
    result = runner.invoke(app, ["events"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert [json.loads(line) for line in lines] == EVENTS


@respx.mock
def test_events_json_mode(runner):
    respx.get(f"{BASE}/events", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json=EVENTS)
    )
    result = runner.invoke(app, ["events", "--env", "prod", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == EVENTS


@respx.mock
def test_events_server_down(runner):
    respx.get(f"{BASE}/events").mock(side_effect=httpx.ConnectTimeout("slow"))
    result = runner.invoke(app, ["events"])
    assert result.exit_code == 2
    assert "Could not reach odin server" in result.stderr
