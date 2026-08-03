"""Release finding #5 -- `/destroy` parity: the route must run the tofu
half too (whatever tofu created -- vpc/subnet/sg have NO reconciler-driven
teardown path at all) before pruning the reconciler's own half, not just
empty the Stack and walk away leaving tofu's state to lie forever.

Unit-level only (fakes for runner.destroy, no real tofu/workspace
materialization) -- the existing integration suites
(test_apply_full_e2e.py, test_gateway_e2e.py, test_backings_e2e.py) already
prove the real tofu round-trip for the sibling /apply-full and /tf/* routes,
so this file asserts the *wiring*: destroy calls runner.destroy exactly
when a workspace exists, honors the same 409/tofu-unavailable semantics
/tf/destroy already has, and the reconciler half always still runs after.
"""
from __future__ import annotations

import json

import httpx
import pytest
import typer
from fastapi.testclient import TestClient

from odin.cli import http
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate.runner import SimulateBusy, TfResult, TofuNotInstalled
from odin.spec.store import SpecStore
from tests.api.test_apply import CANVAS, FakeRds, FakeRuntime
from tests.api.test_apply_full import FakeAws


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def _make_workspace(tmp_path, env: str = "default") -> None:
    (tmp_path / env / "tf").mkdir(parents=True, exist_ok=True)


def test_destroy_skips_tofu_when_no_workspace_ever_existed(tmp_path):
    app = _app(tmp_path)
    calls = []

    async def _destroy(*args, **kwargs):
        calls.append(args)
        return TfResult(ok=True, exit_code=0)

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] is None
    assert calls == []  # never called -- nothing was ever applied through tofu


def test_destroy_runs_tofu_destroy_when_a_workspace_exists(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)
    calls = []
    principals = []

    async def _destroy(env, gateway_port, access_key, secret_key, **kwargs):
        calls.append((env, gateway_port, access_key, secret_key))
        # Captured DURING the call -- /destroy revokes the env's keys
        # (including this operator credential) right after, so the
        # principal must be resolved here, not after the request returns.
        principals.append(app.state.gateway_keys.lookup(access_key))
        return TfResult(ok=True, exit_code=0)

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] == {"status": "ok", "exit_code": 0}
    assert len(calls) == 1
    env, _gateway_port, _access_key, _secret_key = calls[0]
    assert env == "default"
    # operator credentials, the same wiring /tf/destroy uses
    assert principals[0].node_id == OPERATOR_NODE_ID


def test_destroy_reports_tofu_failure_and_keeps_the_desired_state_for_a_retry(tmp_path):
    """Field test 5: a destroy that FAILED must not commit an empty Stack.

    Committing it regardless is what bricked the env -- the next destroy's
    `ensure_backings(last_applied)` got an empty Stack, started no backings, and
    tofu's AWS calls 503-retried to the 300s deadline. The desired state changes
    when the teardown succeeded, never because it was attempted (the same rule
    `/apply-full` already follows in the other direction)."""
    app = _app(tmp_path)
    _make_workspace(tmp_path)

    async def _destroy(*args, **kwargs):
        return TfResult(ok=False, exit_code=1, tail=("boom",))

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 500
    body = resp.json()
    assert body["tf"] == {"status": "failed", "exit_code": 1, "tail": ["boom"]}
    assert app.state.store.get_stack("default").resources != ()
    assert "re-run `odin destroy --env default`" in body["error"]


# --- field test 6 (F2): the failed-destroy NARRATIVE ---
#
# The behaviour above is right and stays. What was wrong is what the user was
# told about it: "The env's desired state was left as it was, so re-running the
# destroy once the cause above is fixed picks up exactly here" -- while the
# reconciler was re-creating the s3/sqs/sns/dynamodb resources the destroy had
# already removed. Measured at the shipped cadence against a real server: a
# deleted queue and bucket were both back in the REAL backings 0.76s later.

class _BlindRuntime(FakeRuntime):
    """A machine odin cannot ask: `docker` will not answer at all."""

    async def container_names(self, env=None):
        raise RuntimeError("Cannot connect to the Docker daemon")


class _CleanRuntime(FakeRuntime):
    """A machine that really has no container left for this env."""

    async def container_names(self, env=None):
        return []


_PROVISIONED_CANVAS = {
    "nodes": [
        {"type": "sqs", "data": {"label": "jobs"}},
        {"type": "s3", "data": {"label": "uploads"}},
        {"type": "rds", "data": {"label": "db"}},
    ],
    "edges": [],
}


def test_a_failed_destroy_says_the_reconciler_will_re_create_what_it_removed(tmp_path):
    """The sentence must name the re-creation, the resources it applies to, the
    window, and what to do -- and must NOT claim the retry resumes."""
    app = create_app(
        runtime=_SurvivingRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), aws=FakeAws(),
    )
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _failing_destroy(1, ("Error: deleting S3 Bucket",))
    with TestClient(app) as client:
        client.post("/apply", json=_PROVISIONED_CANVAS)
        resp = client.post("/destroy")

    assert resp.status_code == 500, resp.text
    error = resp.json()["error"]
    assert "did NOT preserve progress" in error
    assert "RE-CREATES" in error
    # Only the PROVISIONED kinds, derived from the still-committed Stack -- the
    # rds node is TF-owned and the loop does not re-create it.
    assert "jobs (sqs)" in error and "uploads (s3)" in error
    assert "db (rds)" not in error
    assert "about one tick" in error
    # What to do, and the honest characterisation of a retry.
    assert "starts over rather than resuming" in error
    assert "`odin stop`" in error
    # ...and the claim the field test caught is gone for good.
    assert "picks up exactly here" not in error
    # `still standing` no longer over-claims either.
    assert "does NOT list a resource the destroy deleted and the loop has since put back" in error


def test_an_env_with_nothing_the_loop_re_creates_is_not_warned_about_it(tmp_path):
    """Derived, not asserted: the warning is built from the desired Stack, so an
    env whose desired state holds no s3/sqs/sns/dynamodb resource is not told
    about a re-creation that cannot happen to it. (`CANVAS` is rds-only.)"""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _failing_destroy(1, ("boom",))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        error = client.post("/destroy").json()["error"]
    assert "RE-CREATES" not in error
    assert "holds no s3/sqs/sns/dynamodb resource" in error
    assert "nothing was resumed either" in error


def test_a_failed_destroy_with_nothing_visible_does_not_read_as_a_success(tmp_path):
    """`still standing: 0 resource(s) [], 0 container(s) []` contradicted the
    verdict it was attached to -- a bare `[]` twice, on a report whose whole job
    is naming what survived. Reachable for real: a timed-out destroy on a machine
    the containers have already left."""
    app = create_app(
        runtime=_CleanRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False,
    )
    _make_workspace(tmp_path)  # a workspace with no state file -> no addresses
    app.state.tf_runner.destroy = _failing_destroy(-9, ("killed",), timed_out=True)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        error = client.post("/destroy").json()["error"]
    assert "still standing: nothing odin can see" in error
    assert "That is not a success" in error
    assert "[]" not in error


def test_a_destroy_that_cannot_list_containers_says_unknown_not_zero(tmp_path):
    """`_surviving_containers` returned `[]` both for a clean machine and for a
    docker daemon that would not answer, with the real reason in the server log
    only -- so "couldn't tell" wore the words of "there is nothing there"."""
    app = create_app(
        runtime=_BlindRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False,
    )
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _failing_destroy(1, ("boom",))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        body = client.post("/destroy").json()
    assert body["still_standing"]["containers"] is None
    assert "could not read the machine's container list" in body["error"]
    assert "UNKNOWN rather than zero" in body["error"]


def test_nothing_visible_and_nothing_knowable_are_not_the_same_sentence(tmp_path):
    """The sharp edge of the same distinction, and the one a falsiness test cannot
    see: tofu's state holds nothing AND docker will not answer. `None` must not be
    folded into the nothing-standing sentence, because "odin can see nothing" is
    then a claim odin has no basis for."""
    app = create_app(
        runtime=_BlindRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False,
    )
    _make_workspace(tmp_path)  # workspace, no state file -> tf_state == []
    app.state.tf_runner.destroy = _failing_destroy(-9, ("killed",), timed_out=True)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        body = client.post("/destroy").json()
    assert body["still_standing"] == {"tf_state": [], "containers": None}
    error = body["error"]
    assert "still standing: nothing odin can see" not in error
    assert "0 resource(s) [] in tofu state" in error
    assert "could not read the machine's container list" in error


# --- field test 4 (HIGH): a destroy that did not destroy may not say it did --


class _SurvivingRuntime(FakeRuntime):
    """A machine that still has containers after the failed destroy -- two for
    this env and one for a DIFFERENT env, so the residue report is proven to be
    env-scoped rather than a blanket listing."""

    _OWNER = {"odin-rds-default-db": "default", "odin-aws-s3-default": "default",
              "odin-rds-other-db": "other"}

    async def container_names(self, env=None):
        # Honours `env` as `docker ps --filter label=odin-env=` does. odin used
        # to filter these by NAME; since v0.8.21 the substrate filters, so a
        # fake that ignored `env` would be testing a filter nothing runs.
        return [n for n, e in self._OWNER.items() if env is None or e == env]


def _surviving_app(tmp_path):
    return create_app(runtime=_SurvivingRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def _write_state(tmp_path, env: str = "default") -> None:
    """tofu's own state, as it really looks after a destroy that gave up
    partway: resources it still owns and a retry would have to delete."""
    _make_workspace(tmp_path, env)
    (tmp_path / env / "tf" / "terraform.tfstate").write_text(json.dumps({
        "version": 4,
        "resources": [
            {"mode": "managed", "type": "aws_db_instance", "name": "app_db", "instances": [{}]},
            {"mode": "managed", "type": "aws_s3_bucket", "name": "uploads", "instances": [{}]},
            {"mode": "data", "type": "aws_caller_identity", "name": "current", "instances": [{}]},
        ],
    }))


def _failing_destroy(exit_code: int, tail: tuple[str, ...], timed_out: bool = False):
    async def _destroy(*args, **kwargs):
        return TfResult(ok=False, exit_code=exit_code, tail=tail, timed_out=timed_out)

    return _destroy


def test_a_timed_out_destroy_is_not_reported_as_destroyed(tmp_path):
    """THE bug: `body["status"]` was set to "destroyed" at the top of the route
    and never revised, so a destroy killed at its 300s deadline answered
    `status: destroyed` with `tf: failed` nested inside it, and `odin destroy`
    exited 0 while everything was still standing."""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _failing_destroy(
        -9, ("tofu destroy timed out after 300s -- process killed",), timed_out=True,
    )
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "destroy_timed_out", body
    # A deadline expiry is a distinct outcome from tofu erroring: nothing was diagnosed.
    assert "whole-call deadline" in body["error"]
    assert "ODIN_TOFU_DESTROY_TIMEOUT" in body["error"]


def test_an_externally_killed_destroy_does_not_blame_the_deadline(tmp_path):
    """Field test 5 (MED): the route used to read `result.exit_code < 0` as
    "odin's own killpg fired", on the belief that nothing else produces a
    negative code. Any kill gives -9 -- an external `kill -9` 0.87s into a
    destroy was reported as a 300-SECOND deadline expiry, sending the operator
    to tune `ODIN_TOFU_DESTROY_TIMEOUT` for something that had nothing to do
    with it. `TfResult.timed_out` is the real signal, carried out of the one
    frame that knows, and it is False here."""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    # Exactly what an external SIGKILL looks like: -9, and the runner's own
    # timeout branch never ran, so it appended nothing to the tail.
    app.state.tf_runner.destroy = _failing_destroy(-9, ())
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "destroy_failed", body
    assert "deadline" not in body["error"]
    assert "ODIN_TOFU_DESTROY_TIMEOUT" not in body["error"]
    assert "tofu exited -9" in body["error"]


def test_a_failed_destroy_names_what_is_still_standing(tmp_path):
    """"destroy failed" with no inventory is nearly as unhelpful as the false
    success: the report names tofu's surviving state AND the real containers,
    which is exactly what the field test had to go find by hand."""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _failing_destroy(1, ("Error: deleting RDS DB Instance",))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "destroy_failed", body
    # Managed resources only -- a `data` block is not something a retry deletes.
    assert body["still_standing"]["tf_state"] == ["aws_db_instance.app_db", "aws_s3_bucket.uploads"]
    assert body["still_standing"]["containers"] == ["odin-aws-s3-default", "odin-rds-default-db"]
    assert "aws_db_instance.app_db" in body["error"]
    assert "odin-rds-default-db" in body["error"]
    assert "odin-rds-other-db" not in body["error"], "another env's container is not this env's residue"


def test_a_failed_destroy_exits_nonzero_in_the_cli(tmp_path):
    """The whole point of the status: `cli/http.body_or_fail` keys on a truthy
    `error`, so `odin destroy` can no longer exit 0 on a destroy that left the
    env standing -- the same convention `odin apply` follows."""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _failing_destroy(1, ("boom",))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        body = client.post("/destroy").json()
    with pytest.raises(typer.Exit):
        http.body_or_fail(httpx.Response(500, json=body))


def test_a_destroy_that_really_destroyed_still_reports_destroyed(tmp_path):
    """The guardrail: this must not start failing honest teardowns. A clean
    tofu destroy is still a 200 with no `error` and no residue report -- and
    costs no state read or container listing at all."""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)

    async def _destroy(*args, **kwargs):
        return TfResult(ok=True, exit_code=0)

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "destroyed", body
    assert "error" not in body
    assert "still_standing" not in body


def test_a_destroy_with_no_workspace_is_still_a_clean_destroyed(tmp_path):
    """Nothing was ever applied through tofu, so there is nothing for tofu to
    fail at -- the optimistic status is correct here and stays."""
    app = _surviving_app(tmp_path)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 200
    assert resp.json()["status"] == "destroyed"


# --- field test 5 (HIGH): the FOURTH form of the exit-0 destroy ---


def _no_tofu():
    async def _destroy(*args, **kwargs):
        raise TofuNotInstalled()

    return _destroy


def test_tofu_unavailable_over_a_live_state_is_a_failed_destroy(tmp_path):
    """v0.7.4 reported `status: destroyed`, exit 0, with every
    Terraform-managed resource still in state, because the `TofuNotInstalled`
    branch set `body["tf"]` and left the optimistically-initialised `status`
    alone. Keying on the exit code -- the fix that closed the previous form --
    cannot reach this one: tofu never ran, so there is no exit code. The trigger
    is mundane: a server launched outside a login shell has no
    /opt/homebrew/bin."""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _no_tofu()
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "destroy_unavailable", body
    assert body["tf"]["status"] == "unavailable"
    assert "not on this server's PATH" in body["error"]
    # ...and it says what is still standing, like every other honest failure here.
    assert body["still_standing"]["tf_state"] == ["aws_db_instance.app_db", "aws_s3_bucket.uploads"]


def test_tofu_unavailable_does_not_brick_the_env(tmp_path):
    """The second half of the same bug: the route committed an empty Stack on
    its way out even though nothing was destroyed, so the NEXT destroy's
    `ensure_backings(last_applied)` got an empty Stack, started no backing
    containers, and tofu's AWS calls 503-retried to the 300s deadline (measured:
    5:00.38). Recovery meant re-applying the original canvas -- documented
    nowhere. Here: the desired state survives, and a second destroy with tofu
    back on PATH still works."""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    app.state.tf_runner.destroy = _no_tofu()
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        applied = app.state.store.get_stack("default")
        assert client.post("/destroy").status_code == 500
        assert app.state.store.get_stack("default") == applied, "the env was bricked"

        ensured = []

        async def _destroy(*args, **kwargs):
            return TfResult(ok=True, exit_code=0)

        app.state.tf_runner.destroy = _destroy
        reconciler = app.state.reconcilers["default"]
        original_ensure = reconciler.ensure_backings

        async def _ensure(stack):
            ensured.append(tuple(r.id for r in stack.resources))
            return await original_ensure(stack)

        reconciler.ensure_backings = _ensure
        resp = client.post("/destroy")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "destroyed"
    # The retry booted the backings the surviving Stack names -- the exact step
    # an empty Stack silently skipped.
    assert ensured == [("db",)]
    assert app.state.store.get_stack("default").resources == ()


def test_tofu_unavailable_over_an_empty_state_is_still_a_clean_destroy(tmp_path):
    """The other direction, and why the witness is READ rather than assumed:
    tofu owning nothing for this env means there is nothing an install of tofu
    would change, so demanding one would make an env tofu never touched
    un-destroyable for no reason. tofu's own state is what decides."""
    app = _app(tmp_path)
    _make_workspace(tmp_path)  # a workspace, but no state file at all
    app.state.tf_runner.destroy = _no_tofu()
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        access_key, _secret = app.state.gateway_keys.issue("default", "db")
        resp = client.post("/destroy")
    assert resp.status_code == 200  # not a request-level error
    body = resp.json()
    assert body["status"] == "destroyed"
    assert body["tf"]["status"] == "unavailable"
    assert client.get("/world").json()["resources"] == []  # reconciler half still ran
    assert app.state.gateway_keys.lookup(access_key) is None  # keys still revoked
    assert app.state.store.get_stack("default").resources == ()


def test_an_outcome_the_route_cannot_map_fails_loudly(tmp_path, monkeypatch):
    """The SHAPE, not the instance -- and asserted THROUGH THE ROUTE, not
    against the map. Three fixes each taught one more branch to revise an
    optimistic status, and a fourth branch that hadn't been taught kept
    inheriting the success. So the question this has to answer is "what does
    the route do with an outcome nobody taught it", and the only honest way to
    ask it is to make an outcome unmappable: `_DESTROY_STATUS` is emptied, a
    destroy that genuinely SUCCEEDS runs, and the route must still refuse to
    call it destroyed. (Checking `_DESTROY_STATUS.get(..., "destroy_failed")`
    in the test instead re-derives the default here rather than reading the
    route's -- it passes just as happily when the route's default is flipped
    back to "destroyed", which is the bug.)"""
    app = _surviving_app(tmp_path)
    _write_state(tmp_path)
    monkeypatch.setattr("odin.server._DESTROY_STATUS", {})

    async def _destroy(*args, **kwargs):
        return TfResult(ok=True, exit_code=0)

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "destroy_failed", body
    assert "odin bug" in body["error"], body["error"]
    # ...and it did NOT quietly wipe the desired state on the way out.
    assert app.state.store.get_stack("default").resources != ()


def test_only_outcomes_added_on_purpose_score_as_a_success(tmp_path):
    """The companion to the test above: the success set is small, closed and
    explicit, so a new outcome cannot join it by accident."""
    from odin.server import _DESTROY_STATUS

    assert {k for k, v in _DESTROY_STATUS.items() if v == "destroyed"} == {"ok", "nothing_to_destroy"}


# --- field test 5 (LOW): destroying nothing must create nothing ---


def test_destroy_on_an_env_that_never_existed_mints_nothing(tmp_path):
    """`odin destroy --env typo` used to CREATE `.odin/<env>/` with a HEAD, an
    empty Stack revision and real `keys.json` gateway credentials, after which
    the env appeared in `odin envs` forever -- a typo'd destroy issuing
    credentials for an environment that has never existed."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/destroy", params={"env": "neverexisted"})
        envs = client.get("/envs").json()["envs"]
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "nothing_to_destroy"
    assert not (tmp_path / "neverexisted").exists(), "destroying nothing created an env directory"
    assert "neverexisted" not in envs


def test_destroy_409_when_a_tofu_run_is_already_in_progress(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)
    app.state.tf_runner.status = lambda env: {"env": env, "running": True, "workspace_exists": True, "last": None}

    async def _destroy(*args, **kwargs):  # must never be called -- the guard fires first
        raise AssertionError("runner.destroy should not run while busy")

    app.state.tf_runner.destroy = _destroy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["error"]
    # Nothing was torn down -- the busy guard runs before ANY mutation. Asserted
    # against the desired state rather than World: since W2.7 the rds node in
    # CANVAS is TF-owned, so /apply alone never puts it in World to begin with.
    assert app.state.store.get_stack("default").resources != ()


def test_destroy_409_when_a_run_races_in_after_the_guard(tmp_path):
    app = _app(tmp_path)
    _make_workspace(tmp_path)

    async def _busy(*args, **kwargs):
        raise SimulateBusy("default")

    app.state.tf_runner.destroy = _busy
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        resp = client.post("/destroy")
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["error"]
