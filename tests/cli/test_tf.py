"""`odin tf status` / `odin tf destroy` against a respx-mocked server."""
from __future__ import annotations

import json

import httpx
import respx

from odin.cli.app import app
from tests.cli.conftest import BASE

STATUS_IDLE = {"env": "default", "running": False, "workspace_exists": False, "last": None}
STATUS_LAST_FAILED = {
    "env": "prod", "running": True, "workspace_exists": True,
    "last": {"ok": False, "exit_code": 1, "tail": ["Error: something exploded"]},
}


@respx.mock
def test_tf_status_json(runner):
    respx.get(f"{BASE}/tf/status", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=STATUS_IDLE)
    )
    result = runner.invoke(app, ["tf", "status", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == STATUS_IDLE


@respx.mock
def test_tf_status_text_idle(runner):
    respx.get(f"{BASE}/tf/status", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=STATUS_IDLE)
    )
    result = runner.invoke(app, ["tf", "status"])
    assert result.exit_code == 0
    assert "running: False" in result.stdout
    assert "workspace: absent" in result.stdout
    assert "last run: none" in result.stdout


@respx.mock
def test_tf_status_text_with_failed_last_run(runner):
    respx.get(f"{BASE}/tf/status", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json=STATUS_LAST_FAILED)
    )
    result = runner.invoke(app, ["tf", "status", "--env", "prod"])
    assert result.exit_code == 0
    assert "running: True" in result.stdout
    assert "workspace: exists" in result.stdout
    assert "last run: failed (exit code 1)" in result.stdout
    assert "Error: something exploded" in result.stdout


@respx.mock
def test_tf_status_server_down(runner):
    respx.get(f"{BASE}/tf/status").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["tf", "status"])
    assert result.exit_code == 2
    assert "Could not reach odin server" in result.stderr


# --- field test 3: `odin tf plan`, the safe drift check --------------------
#
# Exit codes mirror `tofu plan -detailed-exitcode` so a CI drift gate can be
# `odin tf plan --env dev` and nothing else: 0 no changes, 2 changes present,
# 1 a real error. The server being unreachable is 3 -- deliberately NOT the 2
# every other command uses, because for THIS command 2 already means drift.

PLAN_NO_CHANGES = {
    "status": "no_changes", "env": "default", "exit_code": 0,
    "tail": ["No changes. Your infrastructure matches the configuration."], "unsupported": [],
}
PLAN_CHANGES = {
    "status": "changes", "env": "default", "exit_code": 2,
    "tail": ["Plan: 1 to add, 0 to change, 0 to destroy."], "unsupported": [],
}
PLAN_FAILED = {
    "status": "failed", "env": "default", "exit_code": 1,
    "tail": ["Error: Invalid provider configuration"], "unsupported": [],
}


@respx.mock
def test_tf_plan_no_changes_exits_zero(runner):
    respx.post(f"{BASE}/tf/plan", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=PLAN_NO_CHANGES)
    )
    result = runner.invoke(app, ["tf", "plan"])
    assert result.exit_code == 0
    assert "status: no_changes" in result.stdout
    assert "No changes." in result.stdout


@respx.mock
def test_tf_plan_with_changes_exits_two(runner):
    respx.post(f"{BASE}/tf/plan", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json=PLAN_CHANGES)
    )
    result = runner.invoke(app, ["tf", "plan"])
    assert result.exit_code == 2
    assert "status: changes" in result.stdout
    assert "Plan: 1 to add" in result.stdout


@respx.mock
def test_tf_plan_error_exits_one(runner):
    respx.post(f"{BASE}/tf/plan").mock(return_value=httpx.Response(500, json=PLAN_FAILED))
    result = runner.invoke(app, ["tf", "plan"])
    assert result.exit_code == 1
    assert "Error: Invalid provider configuration" in result.stdout


@respx.mock
def test_tf_plan_json_mode_keeps_the_exit_code(runner):
    respx.post(f"{BASE}/tf/plan").mock(return_value=httpx.Response(200, json=PLAN_CHANGES))
    result = runner.invoke(app, ["tf", "plan", "-o", "json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout) == PLAN_CHANGES


@respx.mock
def test_tf_plan_names_nodes_the_plan_could_not_cover(runner):
    """`no_changes` only means "no drift in what odin can generate" -- an
    unsupported node is not in the plan at all, so the check says so."""
    body = {**PLAN_NO_CHANGES, "unsupported": ["cache1 (elasticache)"]}
    respx.post(f"{BASE}/tf/plan").mock(return_value=httpx.Response(200, json=body))
    result = runner.invoke(app, ["tf", "plan"])
    assert result.exit_code == 0
    assert "cache1 (elasticache)" in result.stdout


@respx.mock
def test_tf_plan_busy_409_exits_one(runner):
    respx.post(f"{BASE}/tf/plan").mock(
        return_value=httpx.Response(409, json={"error": "a tofu run is already in progress for env 'default'"})
    )
    result = runner.invoke(app, ["tf", "plan"])
    assert result.exit_code == 1
    assert "already in progress" in result.stderr


@respx.mock
def test_tf_plan_server_down_exits_three_not_two(runner):
    """2 is reserved for "changes present" here, so an unreachable server must
    not be able to masquerade as drift in a CI gate."""
    respx.post(f"{BASE}/tf/plan").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["tf", "plan"])
    assert result.exit_code == 3
    assert "Could not reach odin server" in result.stderr


@respx.mock
def test_tf_destroy_ok(runner):
    respx.post(f"{BASE}/tf/destroy", params={"env": "default"}).mock(
        return_value=httpx.Response(200, json={"status": "destroyed", "env": "default", "exit_code": 0})
    )
    result = runner.invoke(app, ["tf", "destroy"])
    assert result.exit_code == 0
    assert "status: destroyed" in result.stdout


@respx.mock
def test_tf_destroy_failed_prints_tail_and_exits_nonzero(runner):
    body = {"status": "failed", "env": "default", "exit_code": 1, "tail": ["Error: kaboom"]}
    respx.post(f"{BASE}/tf/destroy").mock(return_value=httpx.Response(500, json=body))
    result = runner.invoke(app, ["tf", "destroy"])
    assert result.exit_code == 1
    assert "status: failed" in result.stdout
    assert "Error: kaboom" in result.stdout


@respx.mock
def test_tf_destroy_failed_json_mode(runner):
    body = {"status": "failed", "env": "default", "exit_code": 1, "tail": ["Error: kaboom"]}
    respx.post(f"{BASE}/tf/destroy").mock(return_value=httpx.Response(500, json=body))
    result = runner.invoke(app, ["tf", "destroy", "-o", "json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == body


@respx.mock
def test_tf_destroy_tofu_not_installed_409(runner):
    respx.post(f"{BASE}/tf/destroy").mock(
        return_value=httpx.Response(409, json={"error": "tofu not installed", "fix": "brew install opentofu"})
    )
    result = runner.invoke(app, ["tf", "destroy"])
    assert result.exit_code == 1
    assert "tofu not installed" in result.stderr
    assert "brew install opentofu" in result.stderr


@respx.mock
def test_tf_destroy_busy_409(runner):
    respx.post(f"{BASE}/tf/destroy").mock(
        return_value=httpx.Response(409, json={"error": "a tofu run is already in progress for env 'default'"})
    )
    result = runner.invoke(app, ["tf", "destroy"])
    assert result.exit_code == 1
    assert "already in progress" in result.stderr


@respx.mock
def test_tf_destroy_server_down(runner):
    respx.post(f"{BASE}/tf/destroy").mock(side_effect=httpx.ConnectTimeout("slow"))
    result = runner.invoke(app, ["tf", "destroy"])
    assert result.exit_code == 2
    assert "odin start" in result.stderr
