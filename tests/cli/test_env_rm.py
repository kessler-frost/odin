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
from tests.substrates import NoVm

BASE = "http://localhost:4200"


def _through(client: TestClient):
    """`http.request`'s signature, answered by the real app."""
    def request(method, url, path, params=None, body=None, unreachable_code=2):
        return client.request(method, f"{BASE}{path}", params=params, json=body)
    return request


def _live(tmp_path, monkeypatch, runtime=None):
    # `vm=NoVm()`: `odin env rm` sweeps this env's Lima disks -- see
    # `tests/api/test_destroy_tf.py::_app`. Nothing here asserts on one.
    app = create_app(
        runtime=runtime or CleanRuntime(), store=SpecStore(tmp_path),
        rds=FakeRds(), aws=FakeAws(), reap_ec2_vms=False, vm=NoVm(),
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


# --- the DISK, through the real CLI ---------------------------------------


def test_the_cli_names_each_volume_it_reclaimed(tmp_path, monkeypatch, runner):
    """A Postgres data directory is not small and nothing else on the machine
    would ever say it went back. A reclaim the CLI does not print is
    indistinguishable from one that had nothing to do."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DROP}-app-db-data", DROP)
    _app, client = _live(tmp_path, monkeypatch, runtime=runtime)
    with client:
        client.post(f"/apply?env={DROP}", json=S3_CANVAS)
        result = runner.invoke(cli, ["env", "rm", DROP])

    assert result.exit_code == 0, result.output
    assert f"reclaimed volume odin-rds-{DROP}-app-db-data" in result.stdout
    assert runtime.volumes == set()


def test_a_volume_that_would_not_go_exits_one_and_names_it(tmp_path, monkeypatch, runner):
    """Exit code, not just a body: `odin env rm` performs an action, so it
    reports whether the end state HOLDS. A volume odin could not reclaim means it
    does not — the disk leak is still there and the state directory was kept so a
    retry is clean."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DROP}-app-db-data", DROP)
    runtime.refuse_volume(f"odin-rds-{DROP}-app-db-data")
    _app, client = _live(tmp_path, monkeypatch, runtime=runtime)
    with client:
        client.post(f"/apply?env={DROP}", json=S3_CANVAS)
        result = runner.invoke(cli, ["env", "rm", DROP])

        assert result.exit_code == 1, result.output
        assert "was NOT removed" in result.stderr
        assert "could not reclaim the Docker volume" in result.stderr
        assert (tmp_path / DROP / "HEAD").exists()
        assert DROP in client.get("/envs").json()["envs"]
    assert runtime.volumes == {f"odin-rds-{DROP}-app-db-data"}


def test_odin_volumes_lists_orphans_and_the_command_that_reclaims_them(tmp_path, monkeypatch, runner):
    """`odin volumes` is the discovery surface for the disk an rds instance's
    named volume holds — the thing that previously required a hand-run
    `docker volume ls`."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{KEEP}-app-db-data", KEEP)
    runtime.seed_volume("odin-rds-envrm-ghost-app-db-data", "envrm-ghost")
    _app, client = _live(tmp_path, monkeypatch, runtime=runtime)
    with client:
        client.post(f"/apply?env={KEEP}", json=S3_CANVAS)
        listed = runner.invoke(cli, ["volumes"])

        assert listed.exit_code == 0, listed.output
        assert "env envrm-ghost" in listed.stdout
        assert "ORPHAN — reclaim with: odin env rm envrm-ghost" in listed.stdout
        # The live env's row says in-use and offers no command at all.
        assert f"odin-rds-{KEEP}-app-db-data" in listed.stdout
        assert f"reclaim with: odin env rm {KEEP}" not in listed.stdout
        assert "1 of 2 volume(s) belong to no live environment." in listed.stdout

        # ...and the offered command really works, end to end through the CLI.
        assert runner.invoke(cli, ["env", "rm", "envrm-ghost"]).exit_code == 0
        assert "0 of 1 volume(s)" in runner.invoke(cli, ["volumes"]).stdout

    assert runtime.volumes == {f"odin-rds-{KEEP}-app-db-data"}


def test_odin_volumes_prints_the_zero_rather_than_nothing(tmp_path, monkeypatch, runner):
    """A command that prints nothing when there is nothing to reclaim leaves a
    user unable to tell it from a command they misread. The count is the answer."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{KEEP}-app-db-data", KEEP)
    _app, client = _live(tmp_path, monkeypatch, runtime=runtime)
    with client:
        client.post(f"/apply?env={KEEP}", json=S3_CANVAS)
        listed = runner.invoke(cli, ["volumes"])

    assert listed.exit_code == 0, listed.output
    assert "0 of 1 volume(s) belong to no live environment." in listed.stdout


def test_odin_volumes_on_an_empty_machine_says_so(tmp_path, monkeypatch, runner):
    _app, client = _live(tmp_path, monkeypatch)
    with client:
        listed = runner.invoke(cli, ["volumes"])
    assert listed.exit_code == 0, listed.output
    assert "odin holds no named Docker volumes." in listed.stdout


def test_odin_volumes_reports_a_docker_it_cannot_read_on_stderr(tmp_path, monkeypatch, runner):
    """`{"volumes": []}` from a docker that would not answer would print "odin
    holds no named Docker volumes" — the single most misleading sentence this
    command could produce. It exits 1 with the reason instead."""
    class BlindRuntime(CleanRuntime):
        async def volume_names(self, env=None):
            raise RuntimeError("Cannot connect to the Docker daemon")

    _app, client = _live(tmp_path, monkeypatch, runtime=BlindRuntime())
    with client:
        listed = runner.invoke(cli, ["volumes"])

    assert listed.exit_code == 1, listed.output
    assert "could not list this machine's Docker volumes" in listed.stderr
    assert "holds no named Docker volumes" not in listed.stdout
