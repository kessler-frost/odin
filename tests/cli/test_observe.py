"""`odin world` / `odin envs` / `odin events` against a respx-mocked server."""
from __future__ import annotations

import inspect
import json

import httpx
import respx

from odin.cli import observe
from odin.cli.app import app
from odin.reconcile.reconciler import LoopHealth
from odin.spec.store import SpecStore
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


def test_a_never_used_store_really_lists_default(tmp_path):
    """The fact `odin envs --help` is allowed to state, measured at the source.

    `GET /envs` is `{"envs": store.list_envs()}` verbatim, and `list_envs`
    floors at `["default"]` -- so a store that has never been touched lists
    ONE env, not none. Confirmed against the real route on a never-used store
    dir: `curl /envs` -> `{"envs":["default"]}`, `odin envs` -> `default`,
    exit 0.
    """
    assert SpecStore(tmp_path / "never-used").list_envs() == ["default"]


def test_a_store_with_a_real_env_stops_listing_default(tmp_path):
    """The help's second sentence, and the reason it is not "always lists
    `default`": the floor is a FALLBACK, not an addition. Once one env has a
    HEAD, the list is exactly the envs that have one."""
    root = tmp_path / "store"
    (root / "clifix1").mkdir(parents=True)
    (root / "clifix1" / "HEAD").write_text("deadbeef")
    assert SpecStore(root).list_envs() == ["clifix1"]


def test_envs_help_states_what_the_store_actually_does(tmp_path):
    """The bug: the help said "a fresh odin lists none — not an error, and exit
    0 either way", and a fresh odin lists `default`. Pinned to the real store
    in the same test, so the sentence cannot drift from the code again -- edit
    either side alone and this fails.
    """
    help_text = " ".join(inspect.getdoc(observe.envs).split())
    fresh, = SpecStore(tmp_path / "never-used").list_envs()

    assert f"A never-used odin lists `{fresh}`" in help_text
    assert "lists none" not in help_text
    # ...and the OTHER two claims the old sentence made and could not back:
    # `default` is not conjured by an apply, and `envs` exits 2 (not 0) when
    # the server is unreachable -- see `test_envs_server_down`.
    assert "comes into existence when a canvas is applied" not in help_text
    assert "exit 0 either way" not in help_text


@respx.mock
def test_envs_invents_no_explanation_for_a_list_it_did_not_get(runner):
    """The removed dead branch. It printed "no environments yet — an env exists
    once something has been applied to it" on `envs == []`, which is (a) a
    state odin's own `/envs` cannot answer with and (b) false about `default`.
    An answer odin did not receive is not an answer it should narrate."""
    respx.get(f"{BASE}/envs").mock(return_value=httpx.Response(200, json={"envs": []}))
    result = runner.invoke(app, ["envs"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no environments yet" not in result.stderr
    assert "applied" not in result.stderr


@respx.mock
def test_envs_json_stays_pure_json(runner):
    respx.get(f"{BASE}/envs").mock(return_value=httpx.Response(200, json={"envs": ["default"]}))
    result = runner.invoke(app, ["envs", "-o", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"envs": ["default"]}


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


# --- a dead reconciler has to reach the terminal too ------------------------

# The block `/world` really publishes -- built by the same `LoopHealth` the
# route serializes, so this cannot drift into a hand-typed shape the server
# never sends. (The real string comes from a real dead loop in
# tests/api/test_reconciler_liveness.py and in the e2e proof.)
DEAD_LOOP = LoopHealth(
    env="prod", ticking=False, ticks=12, last_tick_seconds_ago=94.0,
    verdict="odin's reconciler for env 'prod' is NOT converging: its task was CANCELLED. ...",
).model_dump()


@respx.mock
def test_world_text_says_when_the_reconciler_is_not_converging(runner):
    """Without this the table is a frozen snapshot printed as if it were live."""
    respx.get(f"{BASE}/world", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json={**WORLD, "reconciler": DEAD_LOOP})
    )
    result = runner.invoke(app, ["world", "--env", "prod"])
    assert result.exit_code == 0
    assert "RECONCILER DOWN" in result.stderr
    assert "is NOT converging" in result.stderr
    assert len(result.stdout.splitlines()) == 2  # the table itself is unchanged


@respx.mock
def test_world_says_the_reconciler_is_down_even_when_the_world_is_empty(runner):
    """"world is empty" from a loop that never ticked is the same lie as a
    stale table, so the warning sits ABOVE the empty-world return."""
    respx.get(f"{BASE}/world", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json={"env": "prod", "resources": [], "reconciler": DEAD_LOOP})
    )
    result = runner.invoke(app, ["world", "--env", "prod"])
    assert result.exit_code == 0
    assert "RECONCILER DOWN" in result.stderr
    assert "world is empty" in result.stdout


@respx.mock
def test_world_is_silent_about_a_converging_reconciler(runner):
    respx.get(f"{BASE}/world", params={"env": "prod"}).mock(
        return_value=httpx.Response(200, json={
            **WORLD, "reconciler": LoopHealth(env="prod", ticking=True, ticks=99).model_dump(),
        })
    )
    result = runner.invoke(app, ["world", "--env", "prod"])
    assert result.exit_code == 0
    assert "RECONCILER" not in result.stderr
