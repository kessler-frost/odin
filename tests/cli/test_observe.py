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
def test_world_text_renders_a_drift_verdict(runner):
    """W2.2: the reality sweep's verdict is the whole point of `odin world`
    for a drifted resource -- "crashed" alone doesn't tell anyone their VM was
    deleted out of band, or that re-Apply is the fix. It rides
    WorldDelta -> world.json -> /world, so this asserts the CLI actually
    prints it rather than dropping it on the floor."""
    verdict = "VM odin-ec2-prod-i-1 deleted outside odin — re-Apply to recreate"
    respx.get(f"{BASE}/world", params={"env": "prod"}).mock(return_value=httpx.Response(200, json={
        "env": "prod",
        "resources": [
            {"id": "server", "kind": "ec2", "phase": "crashed", "facts": {},
             "verdict": verdict, "restarts": 0},
        ],
    }))
    result = runner.invoke(app, ["world", "--env", "prod"])
    assert result.exit_code == 0
    (line,) = result.stdout.splitlines()
    assert "server" in line and "ec2" in line and "crashed" in line
    assert verdict in line


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


# --- odin logs ---------------------------------------------------------


@respx.mock
def test_logs_prints_lines_and_exits_zero(runner):
    respx.get(f"{BASE}/logs", params={"env": "default", "node": "db", "tail": "100"}).mock(
        return_value=httpx.Response(200, json={
            "env": "default", "node": "db", "kind": "rds", "found": True, "running": True,
            "sources": ["odin-rds-default-db"], "lines": "PostgreSQL init complete", "message": None,
        })
    )
    result = runner.invoke(app, ["logs", "db"])
    assert result.exit_code == 0
    assert "PostgreSQL init complete" in result.stdout


@respx.mock
def test_logs_json_mode(runner):
    body = {
        "env": "default", "node": "db", "kind": "rds", "found": True, "running": True,
        "sources": ["odin-rds-default-db"], "lines": "hello", "message": None,
    }
    respx.get(f"{BASE}/logs", params={"env": "default", "node": "db", "tail": "100"}).mock(
        return_value=httpx.Response(200, json=body)
    )
    result = runner.invoke(app, ["logs", "db", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == body


@respx.mock
def test_logs_not_running_prints_the_honest_message_and_still_exits_zero(runner):
    respx.get(f"{BASE}/logs", params={"env": "default", "node": "db", "tail": "100"}).mock(
        return_value=httpx.Response(200, json={
            "env": "default", "node": "db", "kind": "rds", "found": True, "running": False,
            "sources": ["odin-rds-default-db"], "lines": "", "message": "odin-rds-default-db is not running",
        })
    )
    result = runner.invoke(app, ["logs", "db"])
    assert result.exit_code == 0
    assert "not running" in result.stdout


@respx.mock
def test_logs_unknown_node_exits_one(runner):
    respx.get(f"{BASE}/logs", params={"env": "default", "node": "ghost", "tail": "100"}).mock(
        return_value=httpx.Response(200, json={"env": "default", "node": "ghost", "error": "no such node 'ghost'"})
    )
    result = runner.invoke(app, ["logs", "ghost"])
    assert result.exit_code == 1
    assert "no such node" in result.stderr


@respx.mock
def test_logs_custom_env_and_tail(runner):
    respx.get(f"{BASE}/logs", params={"env": "prod", "node": "app", "tail": "50"}).mock(
        return_value=httpx.Response(200, json={
            "env": "prod", "node": "app", "kind": "ecs", "found": True, "running": True,
            "sources": ["odin-ecs-prod-abc"], "lines": "starting up", "message": None,
        })
    )
    result = runner.invoke(app, ["logs", "app", "--env", "prod", "--tail", "50"])
    assert result.exit_code == 0
    assert "starting up" in result.stdout


@respx.mock
def test_logs_group_reads_a_log_group_with_no_node_argument(runner):
    respx.get(f"{BASE}/logs", params={"env": "default", "group": "/aws/lambda/fn1", "tail": "100"}).mock(
        return_value=httpx.Response(200, json={
            "env": "default", "node": "", "kind": None, "found": True, "running": True,
            "sources": ["odin-lambda-default-fn1"],
            "lines": "2026-07-24T00:00:00.000+00:00 hello from the handler", "message": None,
        })
    )
    result = runner.invoke(app, ["logs", "--group", "/aws/lambda/fn1"])
    assert result.exit_code == 0
    assert "hello from the handler" in result.stdout


@respx.mock
def test_logs_node_and_group_together_pass_both_through(runner):
    respx.get(f"{BASE}/logs", params={"env": "prod", "node": "app", "group": "/ecs/app", "tail": "20"}).mock(
        return_value=httpx.Response(200, json={
            "env": "prod", "node": "app", "kind": None, "found": True, "running": True,
            "sources": ["odin-ecs-prod-abc12345-app"], "lines": "task one up", "message": None,
        })
    )
    result = runner.invoke(app, ["logs", "app", "--group", "/ecs/app", "--env", "prod", "--tail", "20"])
    assert result.exit_code == 0
    assert "task one up" in result.stdout


@respx.mock
def test_logs_with_neither_node_nor_group_exits_one(runner):
    respx.get(f"{BASE}/logs", params={"env": "default", "tail": "100"}).mock(
        return_value=httpx.Response(200, json={
            "env": "default", "node": "", "error": "node or group is required",
        })
    )
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 1
    assert "node or group is required" in result.stderr


@respx.mock
def test_logs_server_down(runner):
    respx.get(f"{BASE}/logs").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["logs", "db"])
    assert result.exit_code == 2
    assert "Could not reach odin server" in result.stderr
