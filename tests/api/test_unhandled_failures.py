"""Field test 6: an exception that reaches the ASGI boundary must still be a
verdict, not a traceback.

The report was `/apply-full` answering `HTTP 500` with the five plain-text words
`Internal Server Error` after a backing container was deleted while the ensure
phase was booting it (reproduced for real with `docker rm -f
odin-aws-rustfs-<env>` racing the route). `BackingUnavailable` escaped the
route, Starlette's default error response is not JSON, and the CLI's own honest
fallback printed

    odin server returned HTTP 500 with a non-JSON body: Internal Server Error

which names neither the container that went missing nor anything to do about it.

These tests are about the SHAPE, which is why they do not stop at
`/apply-full`. `BackingUnavailable` specifically also escapes `/apply` and
`/destroy` -- both reach `ensure_backing` (`/apply` through its trailing
`reconciler.tick()` -> plan -> provision, `/destroy` through its own
`ensure_backings`). `/tf/apply` cannot raise THAT one (it neither ensures
backings nor ticks) but had the identical bare-text 500 for everything else it
calls, so it is covered with an unmapped exception instead of a pretend one.
`ReclaimFailed`/`MeshRefreshFailed` had it too -- and `/destroy`'s own comment
claimed `ReclaimFailed` produced "500 with the VM names" while the VM names only
ever reached the server log.

`raise_server_exceptions=False` throughout: TestClient's default re-raises the
original exception instead of handing back the response the handler produced,
and the response IS the thing under test (same reason
tests/api/test_canvas_validation.py uses it).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from odin.agent.hcl import generate_tf
from odin.agent.translate import TranslateResult
from odin.aws.backings import BackingUnavailable
from odin.gateway.models import ec2compute
from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack

S3_ONLY = {"nodes": [{"type": "s3", "data": {"label": "uploads"}}], "edges": []}


class FakeRuntime:
    def run_container(self, spec):
        return RunHandle(id="x", name=spec.name)

    def stop(self, name):
        pass

    def facts(self, name, container_port=0):
        return ContainerFacts(phase="pending")

    def stats(self, name):
        return {"cpu": 0.0, "ram": 0.0}

    def ensure_host(self):
        return HostFacts()

    def container_names(self):
        return []


class FakeRds:
    def create_db(self, db_id, user, pw):
        pass

    def delete_db(self, db_id):
        pass

    def endpoint(self, db_id):
        return None

    def container_name(self, db_id):
        return f"odin-rds-default-{db_id}"


class FakeAws:
    """A BackingAws stand-in whose `ensure_backing` raises the REAL exception
    the real one raises, carrying the same structured attributes the real
    `_published_port` attaches (aws/backings.py) -- see
    tests/aws/test_backings.py for the half that proves the real code produces
    them, driven through the real ColimaRuntime."""

    def __init__(self, ensure_raises: Exception | None = None):
        self.ensure_raises = ensure_raises

    def ensure_backing(self, service):
        if self.ensure_raises is not None:
            raise self.ensure_raises

    def provision(self, service, name, subscriptions=()):
        # The real `BackingAws.provision` starts with `ensure_backing(service)`
        # and then dials `client(service)`, so BOTH of its first two steps go
        # through `_published_port`. Modelling that is what makes `/apply`'s
        # trailing tick a real second route for this exception, rather than a
        # route that only looks like one.
        self.ensure_backing(service)

    def exists(self, service, name):
        return True

    def deprovision(self, service, name):
        pass

    def facts(self, service, name):
        return {"endpoint": "http://host.docker.internal:9000"}

    def gc(self, active_kinds):
        pass

    def backing_ports(self):
        return {}


def _gone(container: str = "odin-aws-rustfs-applyfix", observed: str = "absent") -> BackingUnavailable:
    return BackingUnavailable(
        f"the s3 backing container {container} is unavailable: it publishes no port 9000, and the "
        f"container runtime reports its state as {observed!r} -- no container by that name exists "
        f"(it was deleted, or was never created)",
        container=container, observed=observed,
    )


def _app(tmp_path, aws=None):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path),
                      rds=FakeRds(), aws=aws or FakeAws(), backings=False)


def _patch_translate(monkeypatch) -> None:
    """The real `translate` spawns claude-agent-sdk. The files it stands in with
    are the REAL deterministic skeleton for this canvas, not `{}`: `/apply-full`
    only enters its ensure/tofu phase when the project has a TF resource in it
    (`resource_set(translated.files)`), so an empty file set would step around
    the very phase the bug is in."""
    skeleton = generate_tf(canvas_to_stack(S3_ONLY, env="applyfix"))

    async def fake_translate(stack, **kwargs):
        return TranslateResult(files=dict(skeleton.files), refined=False)

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)


def _no_tofu(monkeypatch) -> None:
    """For the tests whose exception is raised AFTER the tofu phase: without
    this the real `tofu` on this machine's PATH runs a real init/apply against
    the gateway (minutes, network). `TofuNotInstalled` is handled by the route
    itself, so everything downstream of it still executes -- which is exactly
    where `ensure_instance_mesh` and the trailing tick live."""
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)


# --- the reported bug ---------------------------------------------------------


def test_apply_full_answers_a_deleted_backing_with_a_json_verdict(tmp_path, monkeypatch):
    _patch_translate(monkeypatch)
    app = _app(tmp_path, aws=FakeAws(ensure_raises=_gone()))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/apply-full", params={"env": "applyfix"}, json=S3_ONLY)

    assert resp.status_code == 503
    body = resp.json()  # THE regression: this used to be the text "Internal Server Error"
    assert body["status"] == "backing_unavailable"
    assert body["env"] == "applyfix"
    # Names the container, and the real underlying reason -- not "apply failed".
    assert "odin-aws-rustfs-applyfix" in body["error"]
    assert "publishes no port 9000" in body["error"]
    assert "(it was deleted, or was never created)" in body["error"]
    assert "BackingUnavailable" in body["error"]
    # ...and structurally, so a UI/agent doesn't have to scrape the sentence.
    assert body["backing"] == {"container": "odin-aws-rustfs-applyfix", "observed": "absent"}


def test_the_verdict_never_claims_the_apply_succeeded(tmp_path, monkeypatch):
    """`cli/http.body_or_fail` keys on a truthy `error` for its nonzero exit,
    and `cli/apply.py` treats anything but `status == "applied"` as a failure.
    Both must see a failure here -- three releases of `odin apply`/`destroy`
    exiting 0 over a standing env is why."""
    _patch_translate(monkeypatch)
    app = _app(tmp_path, aws=FakeAws(ensure_raises=_gone()))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/apply-full", params={"env": "applyfix"}, json=S3_ONLY)
    body = resp.json()
    assert body["error"]
    assert body["status"] != "applied"
    assert not resp.is_success
    # ...and the desired state was NOT quietly committed on the way out.
    assert app.state.store.get_stack("applyfix").resources == ()


def test_the_verdict_names_the_recovery_that_actually_works(tmp_path, monkeypatch):
    """A missing or stopped backing is re-created by the next `ensure_backing`,
    so "run Apply again" is a real fix and not a shrug. The verdict must not
    also imply odin knows what the failed apply left behind: this exception can
    escape from the ensure phase (before any store write) or from the trailing
    tick (after it), and nothing at the boundary can tell those apart."""
    _patch_translate(monkeypatch)
    app = _app(tmp_path, aws=FakeAws(ensure_raises=_gone()))
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.post("/apply-full", params={"env": "applyfix"}, json=S3_ONLY).json()
    assert "odin apply --env applyfix" in body["error"]
    assert "odin world --env applyfix" in body["error"]


# --- the sibling sweep: same shape, other routes and other exceptions --------


@pytest.mark.parametrize("path", ["/apply", "/apply-full"])
def test_the_canvas_apply_routes_both_answer_json(tmp_path, monkeypatch, path):
    """`/apply` reaches `ensure_backing` through its own trailing
    `reconciler.tick()` -> plan -> provision, so the identical exception comes
    out of a route that never calls `ensure_backings` at all."""
    _patch_translate(monkeypatch)
    app = _app(tmp_path, aws=FakeAws(ensure_raises=_gone()))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(path, params={"env": "applyfix"}, json=S3_ONLY)
    assert resp.status_code == 503
    assert resp.json()["status"] == "backing_unavailable"


def test_a_malformed_origin_header_is_a_403_not_a_server_error(tmp_path):
    """The cheapest 500 in odin, found by sweeping for siblings of the reported
    bug: `_csrf_guard` runs ahead of every route and called
    `urlparse(...).hostname`, which raises `ValueError: Invalid IPv6 URL` on
    `Origin: http://[::1`. One header, no credentials, no body, no route
    reached. It is a malformed REQUEST, so it belongs with the cross-origin
    refusal (fail closed), not in the failure handler."""
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/apply", params={"env": "applyfix"}, json=S3_ONLY,
                           headers={"Origin": "http://[::1"})
    assert resp.status_code == 403
    assert "cross-origin request rejected" in resp.json()["error"]


def test_a_loopback_origin_is_still_allowed_through(tmp_path, monkeypatch):
    """...and the fix above must not close the door on the browser odin serves."""
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/apply", params={"env": "applyfix"}, json=S3_ONLY,
                           headers={"Origin": "http://localhost:4200"})
    assert resp.status_code == 200


def test_tf_apply_answers_json_for_a_failure_it_never_anticipated(tmp_path, monkeypatch):
    """`/tf/apply` cannot raise `BackingUnavailable` (it neither calls
    `ensure_backings` nor ticks), so this covers it with the class of failure it
    CAN have: one of the several unguarded calls between its rejections and
    tofu -- `wiring.stage`'s disk write here."""
    def boom(stores, env, stack):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("odin.server.wiring.stage", boom)
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/tf/apply", params={"env": "applyfix"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "server_error"
    assert "No space left on device" in body["error"]
    assert body["error"].startswith("POST /tf/apply did not complete for env 'applyfix'")


def test_destroy_reports_a_failed_vm_reclaim_with_the_vm_names_in_the_body(tmp_path, monkeypatch):
    """The claim `/destroy` already made in a comment, now true. `ReclaimFailed`
    used to escape unhandled, so the VM names -- the entire point of the
    exception -- reached the server log and never the caller."""
    def boom(stores, env):
        raise ec2compute.ReclaimFailed(
            f"env {env!r} is NOT destroyed: 1 EC2 VM(s) are still running and could not be "
            "deleted -- odin-ec2-applyfix-i-123 (limactl: timed out)."
        )

    _no_tofu(monkeypatch)
    monkeypatch.setattr("odin.server.ec2compute.reclaim_env_instances", boom)
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/apply", params={"env": "applyfix"}, json=S3_ONLY)  # make the env exist
        resp = client.post("/destroy", params={"env": "applyfix"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "reclaim_failed"
    assert "odin-ec2-applyfix-i-123" in body["error"]
    assert "still running" in body["error"]
    assert body["status"] != "destroyed"


def test_apply_full_reports_a_mesh_refresh_failure_by_name(tmp_path, monkeypatch):
    def boom(stores, env):
        raise ec2compute.MeshRefreshFailed("web1 did not take its new security groups")

    _patch_translate(monkeypatch)
    _no_tofu(monkeypatch)
    monkeypatch.setattr("odin.server.ec2compute.ensure_instance_mesh", boom)
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/apply-full", params={"env": "applyfix"}, json=S3_ONLY)
    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "mesh_refresh_failed"
    assert "web1 did not take its new security groups" in body["error"]


def test_an_exception_type_nobody_mapped_still_fails_loudly_in_json(tmp_path, monkeypatch):
    """The `_DESTROY_STATUS` lesson one level up: the verdict is DERIVED from
    the exception type through a map, and an unmapped type falls through to a
    failure. A future way for a route to blow up therefore reports a failure, in
    JSON, naming the real exception -- it cannot inherit a success or a bare
    traceback by being forgotten."""
    def boom(stores, env):
        raise ZeroDivisionError("a bug nobody has met yet")

    _patch_translate(monkeypatch)
    _no_tofu(monkeypatch)
    monkeypatch.setattr("odin.server.ec2compute.ensure_instance_mesh", boom)
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/apply-full", params={"env": "applyfix"}, json=S3_ONLY)
    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "server_error"
    assert "ZeroDivisionError: a bug nobody has met yet" in body["error"]
    # ...and it does not diagnose a cause odin has not established. The biggest
    # real population of unmapped failures is a backing that never became ready
    # (a plain RuntimeError from `_await_ready`), which is the environment's
    # fault, not odin's.
    assert "odin has no specific verdict for that failure" in body["error"]
    assert "odin bug" not in body["error"]
    assert body["env"] == "applyfix"


def test_a_get_route_that_blows_up_is_json_too(tmp_path, monkeypatch):
    """The handler is registered on the app, not on the apply routes, so it
    covers the read surface as well -- there is no route left that can answer
    with a body the CLI has to describe as "non-JSON"."""
    monkeypatch.setattr(
        "odin.server.stranded_in_tf_state",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("state file is a directory")),
    )
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/world", params={"env": "applyfix"})
    assert resp.status_code == 500
    assert resp.json()["error"].startswith("GET /world did not complete for env 'applyfix'")


def test_the_verdict_body_is_the_shape_the_cli_can_render(tmp_path, monkeypatch):
    """`cli/http._renderable` accepts an error body with `error` or `status`;
    anything else is reported as "the server refused this request" with
    FastAPI's own document. Every verdict here carries both."""
    _patch_translate(monkeypatch)
    app = _app(tmp_path, aws=FakeAws(ensure_raises=_gone()))
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.post("/apply-full", params={"env": "applyfix"}, json=S3_ONLY).json()
    assert set(body) >= {"status", "env", "error"}
    assert isinstance(body["error"], str)


def test_the_env_in_the_verdict_is_the_one_the_request_named(tmp_path, monkeypatch):
    _patch_translate(monkeypatch)
    app = _app(tmp_path, aws=FakeAws(ensure_raises=_gone()))
    with TestClient(app, raise_server_exceptions=False) as client:
        default = client.post("/apply-full", json=S3_ONLY).json()
    assert default["env"] == "default"  # no ?env= -> odin's own default, never ""
