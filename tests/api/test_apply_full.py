"""S5 -- POST /apply-full: the one-shot pipeline (canvas -> Stack -> reconciler
tick -> translate -> tofu apply through the gateway). Unit-level throughout:
FakeRuntime/FakeRds/FakeAws (test_apply.py / test_environments.py style),
`odin.server.translate_mod.translate` monkeypatched with an async fake (the
real one spawns claude-agent-sdk), and a fake `tofu` shell script driving the
runner (test_runner.py style) for the tofu-outcome paths. The real end-to-end
lives with the integration suite, not here."""
from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from odin.agent.hcl import generate_tf
from odin.agent.translate import TranslateResult
from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.simulate.runner import SimulateBusy
from odin.simulate.workspace import tf_dir
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack


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


class FakeRds:
    def __init__(self):
        self.created = []

    def create_db(self, db_id, user, pw):
        self.created.append(db_id)

    def delete_db(self, db_id):
        pass

    def endpoint(self, db_id):
        return None

    def container_name(self, db_id):
        return f"allfather-rds-default-{db_id}"


class FakeAws:
    """BackingAws stand-in (test_environments.py shape) so s3/sqs canvases
    reconcile without real containers."""

    def __init__(self):
        self.ensured = []

    def ensure_backing(self, service):
        self.ensured.append(service)

    def provision(self, service, name, subscriptions=()):
        pass

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


RDS_ONLY = {"nodes": [{"type": "rds", "data": {"label": "db"}}], "edges": []}
S3_SQS = {"nodes": [{"type": "s3", "data": {"label": "uploads"}},
                    {"type": "sqs", "data": {"label": "jobs"}}], "edges": []}

BUSY_BODY = {"error": "a tofu run is already in progress for env 'default'"}


def _app(tmp_path, rds=None, aws=None):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path),
                      rds=rds or FakeRds(), aws=aws or FakeAws(), backings=False)


def _write_fake_tofu(path: Path, script: str) -> None:
    tofu = path / "tofu"
    tofu.write_text(f"#!/bin/sh\n{script}\n")
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC)


_INIT_OK = 'if [ "$1" = "init" ]; then echo "Initializing..."; exit 0; fi'
_APPLY_OK = _INIT_OK + '\nif [ "$1" = "apply" ]; then echo "applied"; exit 0; fi'
_APPLY_FAILS = _INIT_OK + '\nif [ "$1" = "apply" ]; then echo "planning"; echo "boom: invalid resource"; exit 1; fi'


def _skeleton_files() -> dict[str, str]:
    return dict(generate_tf(canvas_to_stack(S3_SQS, env="default")).files)


def _patch_translate(monkeypatch, result: TranslateResult) -> list:
    calls: list = []

    async def fake_translate(stack, **kwargs):
        calls.append(stack)
        return result

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    return calls


def test_rds_only_canvas_skips_tofu_cleanly(tmp_path, monkeypatch):
    # The REAL translate is used: an rds-only stack has zero TF-supported
    # resources, so translate() short-circuits before ever touching the SDK.
    # which -> None makes any wrongful trip into the tofu path show up as
    # "unavailable" instead of silently passing.
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    rds, aws = FakeRds(), FakeAws()
    app = _app(tmp_path, rds=rds, aws=aws)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=RDS_ONLY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["tf"] is None
    assert body["rev"]
    assert body["env"] == "default"
    assert body["skipped"] == []
    assert body["refined"] is False
    assert body["unsupported"] == ["db (rds): Simulate v1 — stays on the reconciler path"]
    assert rds.created == ["db"]  # the reconciler half really ran
    assert aws.ensured == []  # nothing TF-supported -- ensure_backings is never called


def test_no_tofu_installed_reports_tf_unavailable_but_reconciler_applied(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
        world = client.get("/world").json()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied_tf_failed"
    assert body["tf"] == {"status": "unavailable", "exit_code": None,
                          "error": "tofu not installed", "fix": "brew install opentofu"}
    assert body["rev"]
    # the reconciler half genuinely applied: both resources live in the World
    assert {r["id"] for r in world["resources"]} == {"uploads", "jobs"}


def test_tofu_failure_reports_failed_with_tail(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_FAILS)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied_tf_failed"
    assert body["tf"] == {"status": "failed", "exit_code": 1,
                          "tail": ["planning", "boom: invalid resource"]}
    # Release finding #2: a failed tofu apply must NOT become the env's new
    # desired state -- see test_failed_tofu_apply_does_not_commit below for
    # why (the reconciler would provision the same s3/sqs itself on the next
    # background tick, then a RETRY's tofu apply collides against them:
    # BucketAlreadyExists/ResourceInUseException).
    assert body["rev"] is None
    assert "not committed" in body["note"]


def test_failed_tofu_apply_does_not_commit_the_new_stack(tmp_path, monkeypatch):
    """Root cause: /apply-full used to call store.apply(stack) unconditionally,
    even when tofu failed. The reconciler's OWN background loop then saw a
    new desired state it had never provisioned and created the same s3/sqs
    backings itself (BackingAws.provision) -- so the user's very next retry
    lost the race against tofu's own (non-idempotent) creates: exactly the
    same BucketAlreadyExists/ResourceInUseException class of bug the S5
    store-apply-ordering fix (see the module docstring above) already fixed
    for the FIRST apply. Keeping the prior HEAD on a failed apply means a
    fix-and-retry starts from the same clean slate as the first attempt."""
    _write_fake_tofu(tmp_path, _APPLY_FAILS)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    app = _app(tmp_path)

    calls: list[str] = []
    orig_store_apply = app.state.store.apply

    def recording_store_apply(stack):
        calls.append("store.apply")
        return orig_store_apply(stack)

    app.state.store.apply = recording_store_apply
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
        world = client.get("/world").json()
    assert resp.status_code == 200
    assert calls == []  # store.apply was never reached
    assert app.state.store.head("default") is None  # HEAD stays unset
    assert world["resources"] == []  # the reconciler never provisioned anything


def test_success_path_runs_tofu_and_reports_ok(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    aws = FakeAws()
    app = _app(tmp_path, aws=aws)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["tf"] == {"status": "ok", "exit_code": 0}
    # the backing containers were ensured (gateway-routable) BEFORE tofu ran --
    # the load-bearing ordering fix (a bare bucket/table create isn't
    # idempotent the way SQS/SNS's are, and tofu needs a routable gateway).
    assert set(aws.ensured) == {"s3", "sqs"}
    assert body["refined"] is True
    assert body["skipped"] == []


def test_store_apply_is_deferred_until_after_ensure_backings_and_tofu(tmp_path, monkeypatch):
    """Root cause of the S5 e2e failure: reconciler_for() starts a background
    loop (Reconciler._run, poll_interval=1.0) that ticks INDEPENDENTLY of this
    request. That loop's only signal that there's new work is store.apply()
    making the Stack the env's desired state -- so committing it before
    ensure_backings/tofu run let the background tick provision the SAME
    s3/sqs/sns/dynamodb resources itself, concurrently with (and usually
    faster than) tofu's own init+plan+apply. Tofu's AWS-provider creates
    aren't idempotent, so tofu then lost the race for real against the live
    backings (BucketAlreadyExists / ResourceInUseException / an SQS queue
    stuck waiting forever for attributes the reconciler's bare create never
    set). Verified here by call ORDER, not timing: ensure_backing and
    runner.apply must both fire before store.apply ever does."""
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    aws = FakeAws()
    app = _app(tmp_path, aws=aws)

    calls: list[str] = []
    orig_ensure_backing = aws.ensure_backing
    orig_store_apply = app.state.store.apply
    orig_runner_apply = app.state.tf_runner.apply

    def recording_ensure_backing(service):
        calls.append(f"ensure_backing:{service}")
        return orig_ensure_backing(service)

    def recording_store_apply(stack):
        calls.append("store.apply")
        return orig_store_apply(stack)

    async def recording_runner_apply(*args, **kwargs):
        calls.append("runner.apply")
        return await orig_runner_apply(*args, **kwargs)

    aws.ensure_backing = recording_ensure_backing
    app.state.store.apply = recording_store_apply
    app.state.tf_runner.apply = recording_runner_apply

    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)

    assert resp.status_code == 200
    assert resp.json()["rev"]  # the deferred call still produced a real rev
    assert calls.index("store.apply") > calls.index("runner.apply")
    assert all(c.startswith("ensure_backing") for c in calls[: calls.index("runner.apply")])


def test_refined_false_fallback_still_applies_the_skeleton(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    skeleton = _skeleton_files()
    _patch_translate(monkeypatch, TranslateResult(
        files=skeleton, refined=False,
        notes=["agent proposed no refinement -- using the deterministic skeleton"],
    ))
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
    body = resp.json()
    assert body["refined"] is False
    assert body["status"] == "applied"
    assert body["tf"] == {"status": "ok", "exit_code": 0}
    # tofu really ran against the skeleton files, verbatim
    assert (tf_dir(tmp_path, "default") / "main.tf").read_text() == skeleton["main.tf"]


LAMBDA_CANVAS = {"nodes": [{"type": "lambda", "data": {"label": "fn"}}], "edges": []}


def test_apply_full_materializes_lambda_zip_from_translate_result(tmp_path, monkeypatch):
    # Release finding #1 (BLOCKER): hcl.generate_tf's lambda zip used to be
    # dropped between translate()'s TranslateResult and apply_full's TfProject
    # reconstruction -- tofu then failed EVERY resource with `filebase64sha256:
    # open fn.zip: no such file`. Proven here without spawning the real SDK.
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    skeleton = generate_tf(canvas_to_stack(LAMBDA_CANVAS, env="default"))
    assert skeleton.binary_files  # sanity: the lambda builder really zipped something
    _patch_translate(monkeypatch, TranslateResult(files=skeleton.files, binary_files=skeleton.binary_files))
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=LAMBDA_CANVAS)
    assert resp.status_code == 200
    assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}
    for name, content in skeleton.binary_files.items():
        assert (tf_dir(tmp_path, "default") / name).read_bytes() == content


def test_busy_guard_rejects_before_touching_anything(tmp_path, monkeypatch):
    calls = _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    rds = FakeRds()
    app = _app(tmp_path, rds=rds)
    runner = app.state.tf_runner
    # Hold the env's lock as an in-flight tofu run would (uncontended acquire
    # completes synchronously; the flag survives the throwaway loop).
    asyncio.run(runner._lock("default").acquire())
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
        world = client.get("/world").json()
    assert resp.status_code == 409
    assert resp.json() == BUSY_BODY
    # nothing happened: no store mutation, no reconcile, no translate
    assert world["resources"] == []
    assert rds.created == []
    assert calls == []


def test_busy_race_after_the_guard_returns_409(tmp_path, monkeypatch):
    # The guard saw the lock free, but a second call grabbed it before
    # runner.apply -- the route's own SimulateBusy handler answers.
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    app = _app(tmp_path)

    async def _busy(*args, **kwargs):
        raise SimulateBusy("default")

    app.state.tf_runner.apply = _busy
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
    assert resp.status_code == 409
    assert resp.json() == BUSY_BODY
