"""`odin apply` / `odin destroy` against a respx-mocked server."""
from __future__ import annotations

import json

import httpx
import respx

from odin.cli.app import app
from tests.cli.conftest import BASE

GRAPH = {"nodes": [{"id": "uploads", "type": "s3"}], "edges": []}
# Every body here carries `not_covered` because the SERVER does (v0.7.4,
# `server.not_covered`): the CLI no longer computes it, it passes the field
# through, so a mocked body that omitted it would be mocking a server that
# doesn't exist. The API-level proof that the union is right lives in
# tests/api/test_apply_full.py.
APPLIED = {
    "status": "applied", "rev": "abc123", "env": "default",
    "skipped": [], "refined": True, "unsupported": [], "not_covered": [],
    "tf": {"status": "ok", "exit_code": 0},
}
TF_FAILED = {
    "status": "applied_tf_failed", "rev": None, "env": "default",
    "skipped": ["note"], "refined": False, "unsupported": ["ecs"],
    "not_covered": ["note", "ecs"],
    "tf": {"status": "failed", "exit_code": 1, "tail": ["Error: BucketAlreadyExists", "apply failed"]},
    "note": "desired state not committed; fix and re-apply",
}
# Field test 3 (HIGH): tofu had nothing to do, so `tf: ok` -- and the service
# was at 0 of 3 tasks the whole time. Exit 0 here was the bug.
SERVICES_UNHEALTHY = {
    "status": "applied_services_unhealthy", "rev": "abc123", "env": "default",
    "skipped": [], "refined": False, "unsupported": [], "not_covered": [],
    "tf": {"status": "ok", "exit_code": 0},
    "unhealthy": [{
        "node": "web", "running": 0, "desired": 3,
        "reason": "pull access denied for nginx:this-tag-does-not-exist-9z9z",
    }],
    "note": "desired state committed, but the service(s) above are not running "
            "their desired task count — fix and re-apply",
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
def test_apply_names_the_short_service_and_exits_nonzero(runner):
    """`tf: ok` must not be enough to exit 0: the output has to name WHICH
    service, WHAT it observed (running vs desired) and the real reason -- which
    field test 3 could only find in /world and events, never in apply itself."""
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=SERVICES_UNHEALTHY))
    result = runner.invoke(app, ["apply"])
    assert result.exit_code == 1
    assert "status: applied_services_unhealthy" in result.stdout
    assert "tf: ok" in result.stdout
    assert "unhealthy: web — 0/3 tasks running" in result.stdout
    assert "nginx:this-tag-does-not-exist-9z9z" in result.stdout


@respx.mock
def test_apply_unhealthy_json_mode_still_exits_nonzero(runner):
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=SERVICES_UNHEALTHY))
    result = runner.invoke(app, ["apply", "-o", "json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == SERVICES_UNHEALTHY


@respx.mock
def test_the_documented_ci_gate_reads_the_servers_own_field(runner):
    """MISLEAD-1: the README told CI to gate on `.unsupported`, but a node
    whose KIND odin doesn't model lands in `.skipped` -- so
    `jq -e '.unsupported | length == 0'` was TRUE, exit 0, while two drawn
    nodes were silently dropped. One field now carries both, and v0.7.4 moved
    it into the API response: this command PASSES IT THROUGH rather than
    re-deriving it, so `curl /apply-full` and `odin apply -o json` cannot
    disagree about what a pipeline is gating on."""
    dropped = {
        **APPLIED, "skipped": ["kinesis", "notarealservice"],
        "not_covered": ["kinesis", "notarealservice"],
    }
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=dropped))
    body = json.loads(runner.invoke(app, ["apply", "-o", "json"]).stdout)
    assert body["unsupported"] == []                       # the old gate: still empty
    assert body["not_covered"] == ["kinesis", "notarealservice"]   # the gate that works
    assert len(body["not_covered"]) != 0


@respx.mock
def test_the_cli_never_invents_a_not_covered_of_its_own(runner):
    """The regression that would re-open the `curl` hole: if this command
    recomputed the union, a server that got it wrong (or an older one that
    published nothing) would be silently papered over here and the API's own
    consumers would still be trapped. Whatever the server said is what a gate
    sees -- even when it disagrees with the two arrays beside it."""
    contradictory = {
        **APPLIED, "skipped": ["kinesis"], "unsupported": ["db1 (rds): mysql not supported"],
        "not_covered": ["only what the server said"],
    }
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json=GRAPH))
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=contradictory))
    body = json.loads(runner.invoke(app, ["apply", "-o", "json"]).stdout)
    assert body["skipped"] == ["kinesis"]
    assert body["unsupported"] == ["db1 (rds): mysql not supported"]
    assert body["not_covered"] == ["only what the server said"]


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


# --- field test 6 F7: `-o json` wrote ZERO BYTES on the failure path ---------
#
# `POST /destroy`'s 500 body, in the SHAPE server.py really builds it (the
# `still_standing` block and the `error` sentence are assembled together on the
# failure path there, and `error` is the field `body_or_fail` keys on) -- with
# the tf tail carrying the real diagnosis the field test captured.
DESTROY_FAILED_500 = {
    "status": "destroy_failed",
    "env": "f6dest",
    "tf": {
        "status": "failed", "exit_code": -9,
        "tail": [
            "the usual cause is that this env's AWS backing containers are not running, so "
            "every AWS call gets a real ServiceUnavailable and the provider retries it ~25 "
            "times with backoff",
        ],
    },
    "still_standing": {
        "tf_state": ["aws_s3_bucket.dbucket", "aws_sqs_queue.dqueue"],
        "containers": ["odin-aws-rustfs-f6dest", "odin-aws-goaws-f6dest"],
    },
    "error": "destroy did not finish for env 'f6dest': tofu was killed at its whole-call "
             "deadline. still standing: 2 resource(s) in tofu state, 2 container(s).",
}


@respx.mock
def test_destroy_json_emits_the_failure_body_on_stdout(runner):
    """The finding: `odin destroy -o json > dest.json` produced `0 dest.json`.
    A script gating on the payload got an empty string, `jq .status` got a parse
    error, and the best diagnosis odin produced -- which the server really does
    put in this body -- reached nobody."""
    respx.post(f"{BASE}/destroy", params={"env": "f6dest"}).mock(
        return_value=httpx.Response(500, json=DESTROY_FAILED_500)
    )

    result = runner.invoke(app, ["destroy", "--env", "f6dest", "-o", "json"])

    assert result.exit_code == 1, "a failed destroy is still a failure"
    assert result.stdout != "", "the whole finding: zero bytes on stdout"
    body = json.loads(result.stdout)
    assert body == DESTROY_FAILED_500
    # the three things a human or a script actually needs out of it
    assert body["status"] == "destroy_failed"
    assert body["still_standing"]["containers"] == ["odin-aws-rustfs-f6dest", "odin-aws-goaws-f6dest"]
    assert "ServiceUnavailable" in body["tf"]["tail"][0]
    # ...and the one-line verdict still goes to stderr, not into the JSON stream
    assert "destroy did not finish" in result.stderr


@respx.mock
def test_destroy_text_mode_failure_is_unchanged(runner):
    """The other half of the guard: text mode must NOT gain a JSON blob. The
    payload on stdout is the answer to `-o json`; the verdict on stderr is the
    answer to a human."""
    respx.post(f"{BASE}/destroy").mock(return_value=httpx.Response(500, json=DESTROY_FAILED_500))
    result = runner.invoke(app, ["destroy", "--env", "f6dest"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "destroy did not finish" in result.stderr


@respx.mock
def test_apply_json_emits_an_error_body_on_stdout_too(runner):
    """The SHAPE, not the instance: every command that renders a server payload
    had this hole, so `body_or_fail` was fixed rather than `destroy`. `/apply-full`
    answers 409 with `error` when a run is already in progress -- previously
    zero bytes here as well."""
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json={"nodes": [], "edges": []}))
    busy = {"error": "a tofu run is already in progress for env 'default'"}
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(409, json=busy))

    result = runner.invoke(app, ["apply", "-o", "json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == busy
    assert "already in progress" in result.stderr


@respx.mock
def test_apply_json_emits_an_error_body_from_the_canvas_fetch_too(runner):
    """...including the step BEFORE the apply. `odin apply` GETs `/canvas` first,
    and a refusal there is just as invisible to `| jq` as one from the apply."""
    refused = {"error": "no canvas has been saved yet", "fix": "draw one, or pass --file"}
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(409, json=refused))

    result = runner.invoke(app, ["apply", "-o", "json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == refused


@respx.mock
def test_apply_shows_the_recorded_reason_when_tofu_could_only_say_unexpected_state(runner):
    """Field test 6 F4's other half. tofu describes a failed RDS as an unexpected
    state (`last error: %!s(<nil>)` — the provider's own output, not odin's), while
    odin's records hold the real reason. The server publishes it in
    `unhealthy_resources`; text mode rendered nothing at all, so the reason reached
    JSON readers only — and a CI log is text."""
    body = {
        "status": "applied_tf_failed", "env": "default", "rev": "abc123",
        "unhealthy_resources": [{
            "kind": "rds", "node": "app-db", "observed": "failed",
            "reason": "Postgres never became ready: connection refused",
        }],
    }
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=body))
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json={"nodes": [], "edges": []}))

    result = runner.invoke(app, ["apply"])

    assert "unhealthy: rds app-db is failed" in result.stdout
    assert "Postgres never became ready: connection refused" in result.stdout


@respx.mock
def test_apply_says_so_when_a_resource_carries_no_recorded_reason(runner):
    """A verdict must never render as an empty tail. `reason` is `str | None`, and
    dropping it silently made "unhealthy: rds app-db is failed" the whole answer
    with no hint that the reason was missing rather than empty."""
    body = {
        "status": "applied_tf_failed", "env": "default", "rev": "abc123",
        "unhealthy_resources": [{"kind": "rds", "node": "app-db", "observed": "failed", "reason": None}],
    }
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(200, json=body))
    respx.get(f"{BASE}/canvas").mock(return_value=httpx.Response(200, json={"nodes": [], "edges": []}))

    result = runner.invoke(app, ["apply"])

    assert "no reason recorded" in result.stdout
    assert "None" not in result.stdout
