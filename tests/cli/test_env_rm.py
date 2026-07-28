"""`odin env rm` — the REAL CLI against a REAL app, not a mocked body.

odin has shipped features that were unreachable from the product because only
the function was tested (`odin apply --env prod` fetched the DEFAULT env's
canvas for a whole release; see `test_env_reaches_the_canvas.py`). So these
drive the actual Typer command — real argument parsing, real `httpx` call, real
rendering, real exit code — into a real `create_app()` over a real filesystem.
The only thing replaced is the socket: `http.request` is pointed at a live
`TestClient`, which is `httpx` over an ASGI transport into the same app uvicorn
serves.

The one thing a socket would add on top -- that `uv run odin env rm` reaches a
`uvicorn` process over TCP -- was measured by hand and is recorded in the commit
message, because a second in-process HTTP server would need a thread the
concurrency directive rules out.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from odin.cli import http
from odin.cli.app import app as cli
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_env_rm import DROP, KEEP, CleanRuntime, FakeAws, S3_CANVAS
from tests.api.test_apply import FakeRds

BASE = "http://localhost:4200"


def _through(client: TestClient):
    """`http.request`'s signature, answered by the real app."""
    def request(method, url, path, params=None, body=None, unreachable_code=2):
        return client.request(method, f"{BASE}{path}", params=params, json=body)
    return request


def _live(tmp_path, monkeypatch, runtime=None):
    app = create_app(
        runtime=runtime or CleanRuntime(), store=SpecStore(tmp_path),
        rds=FakeRds(), aws=FakeAws(), reap_ec2_vms=False,
    )
    client = TestClient(app)
    monkeypatch.setattr(http, "request", _through(client))
    return app, client


def test_odin_env_rm_removes_the_env_and_exits_zero(tmp_path, monkeypatch, runner):
    app, client = _live(tmp_path, monkeypatch)
    with client:
        client.post(f"/apply?env={DROP}", json=S3_CANVAS)
        assert (tmp_path / DROP / "HEAD").exists()

        result = runner.invoke(cli, ["env", "rm", DROP])

        assert result.exit_code == 0, result.output
        assert "status: removed" in result.stdout
        assert str(tmp_path / DROP) in result.stdout
        assert not (tmp_path / DROP).exists()
        assert DROP not in client.get("/envs").json()["envs"]
        assert DROP not in app.state.reconcilers


def test_odin_envs_stops_listing_a_removed_env(tmp_path, monkeypatch, runner):
    """The user-visible surface: `odin envs` is what a script reads."""
    _app, client = _live(tmp_path, monkeypatch)
    with client:
        client.post(f"/apply?env={DROP}", json=S3_CANVAS)
        client.post(f"/apply?env={KEEP}", json=S3_CANVAS)
        assert sorted(runner.invoke(cli, ["envs"]).stdout.split()) == [DROP, KEEP]

        runner.invoke(cli, ["env", "rm", DROP])

        listed = runner.invoke(cli, ["envs"])
        assert listed.exit_code == 0
        assert listed.stdout.split() == [KEEP]


def test_a_removal_that_did_not_happen_exits_one_and_says_what_stands(tmp_path, monkeypatch, runner):
    """The contract this whole feature is held to: a command performing an
    action reports whether the END STATE HOLDS. A reconciler that will not stop
    is a removal that did not happen, and must not exit 0."""
    app, client = _live(tmp_path, monkeypatch)
    with client:
        client.post(f"/apply?env={DROP}", json=S3_CANVAS)
        reconciler = app.state.reconcilers[DROP]

        async def _refuse_to_stop():
            return None

        reconciler.stop = _refuse_to_stop
        result = runner.invoke(cli, ["env", "rm", DROP])
        del reconciler.stop

        assert result.exit_code == 1, result.output
        assert "was NOT removed" in result.stderr
        assert "had NOT finished" in result.stderr
        assert (tmp_path / DROP / "HEAD").exists()
        assert DROP in client.get("/envs").json()["envs"]


def test_json_mode_puts_the_whole_failure_on_stdout(tmp_path, monkeypatch, runner):
    """Field test 6 F7's rule, applied here from the start: `-o json` on a
    failure writes the payload to stdout (the ANSWER) and the one-line reason
    to stderr (the VERDICT). Zero bytes on stdout is what made `odin destroy
    -o json | jq .status` a parse error."""
    _app, client = _live(tmp_path, monkeypatch)
    with client:
        client.post(f"/apply?env={DROP}", json=S3_CANVAS)
        (tmp_path / DROP / "tf").mkdir(parents=True, exist_ok=True)

        async def _failing_destroy(*args, **kwargs):
            from odin.simulate.runner import TfResult
            return TfResult(ok=False, exit_code=1, tail=("Error: deleting S3 Bucket",))

        _app.state.tf_runner.destroy = _failing_destroy
        result = runner.invoke(cli, ["env", "rm", DROP, "-o", "json"])

    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert body["status"] == "remove_failed_teardown"
    assert body["teardown"]["tf"]["tail"] == ["Error: deleting S3 Bucket"]


def test_removing_an_env_that_never_existed_exits_zero_and_creates_nothing(tmp_path, monkeypatch, runner):
    _app, client = _live(tmp_path, monkeypatch)
    with client:
        result = runner.invoke(cli, ["env", "rm", "envrm-nope"])
    assert result.exit_code == 0, result.output
    assert "status: not_found" in result.stdout
    assert not (tmp_path / "envrm-nope").exists()
