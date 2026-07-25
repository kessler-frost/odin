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
from odin.server import _TOFU_NOT_INSTALLED, create_app
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
        return f"odin-rds-default-{db_id}"


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


# --- owner directive B1: pre-apply admission control -----------------------


class _LowMemRuntime(FakeRuntime):
    def ensure_host(self):
        return HostFacts(total_mem_mib=1000.0)  # far too small for 50 t3.medium EC2 nodes


BIG_EC2 = {
    "nodes": [
        {"type": "ec2", "data": {"label": f"web{i}", "instanceType": "t3.medium"}}
        for i in range(50)
    ],
    "edges": [],
}


def test_apply_full_rejects_before_touching_anything_when_over_the_memory_budget(tmp_path, monkeypatch):
    calls = _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = create_app(runtime=_LowMemRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), aws=FakeAws(), backings=False)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": "default"}, json=BIG_EC2)
    assert resp.status_code == 409
    body = resp.json()
    assert "GiB" in body["error"] and "reduce instance sizes or apply fewer nodes" in body["error"]
    assert body["estimated_mib"] > body["budget_mib"]
    assert calls == []  # translate() never even ran -- rejected before anything else touched
    assert app.state.store.get_stack("default").resources == ()  # never became the desired state either


def test_apply_full_admits_a_small_stack_that_fits_the_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    app = _app(tmp_path)  # default FakeRuntime -- HostFacts() (unknown) -- never rejects on memory
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
    assert resp.status_code == 200  # unaffected: nothing regressed on the ordinary path


def test_rds_only_canvas_now_goes_THROUGH_tofu(tmp_path, monkeypatch):
    """W2.7 inverted this test. An rds-only canvas used to be the one shape
    with ZERO TF-supported resources -- translate() short-circuited, tofu was
    never invoked, and the reconciler created the database itself. `rds` is an
    `aws_db_instance` now, so the SAME canvas must reach tofu (and the
    reconciler must create nothing). `which -> None` makes that visible as a
    real "unavailable" verdict rather than a silent pass."""
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    rds, aws = FakeRds(), FakeAws()
    app = _app(tmp_path, rds=rds, aws=aws)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=RDS_ONLY)
    assert resp.status_code == 200
    body = resp.json()
    # `applied_tf_failed`, not `applied`: with rds inside Terraform, tofu being
    # missing is now a real failure for this canvas rather than irrelevant to it.
    assert body["status"] == "applied_tf_failed"
    assert body["tf"] == {"status": "unavailable", "exit_code": None, **_TOFU_NOT_INSTALLED}
    # tofu being ABSENT still commits the desired state (unlike a tofu run that
    # actually failed) -- the pre-existing rule, unchanged by W2.7.
    assert body["rev"]
    assert body["env"] == "default"
    assert body["skipped"] == []
    assert body["unsupported"] == []  # no longer the documented exception
    assert rds.created == []  # the reconciler no longer creates the database
    # rds needs no shared BACKING container (its substrate is its own Postgres),
    # so ensure_backings still has nothing to boot for this canvas.
    assert aws.ensured == []


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


def test_apply_full_passes_a_shared_translate_cache_across_requests(tmp_path, monkeypatch):
    # Release finding #5: apply-full must thread the SAME cache dict into
    # every call of translate() -- not a fresh one per request -- or an
    # unchanged canvas would never skip the SDK pass on a re-apply.
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    seen_caches = []

    async def fake_translate(stack, cache=None, **kwargs):
        seen_caches.append(cache)
        return TranslateResult(files=_skeleton_files(), refined=True)

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/apply-full", json=S3_SQS)
        client.post("/apply-full", json=S3_SQS)
    assert len(seen_caches) == 2
    assert seen_caches[0] is not None
    assert seen_caches[0] is seen_caches[1] is app.state.translate_cache


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


# --- release finding #4: a per-env epoch guards against a stale request ------
# undoing a newer teardown/apply once its own store.apply() finally runs
# (a client disconnect does NOT cancel the in-flight server-side request).

STALE_BODY = {"error": "superseded by a newer teardown/apply"}


def test_destroy_bumps_the_env_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    app = _app(tmp_path)
    with TestClient(app) as client:
        before = app.state.env_epoch.get("default", 0)
        client.post("/destroy")
        after = app.state.env_epoch.get("default", 0)
    assert after == before + 1


def test_empty_canvas_apply_full_bumps_the_env_epoch(tmp_path, monkeypatch):
    # An empty-canvas Apply IS a teardown (the "no Destroy button" NORTHSTAR
    # promise -- see create_apply_full_router's own docstring) -- it must
    # invalidate an older in-flight apply-full the same way /destroy does.
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = _app(tmp_path)
    with TestClient(app) as client:
        before = app.state.env_epoch.get("default", 0)
        resp = client.post("/apply-full", json={"nodes": [], "edges": []})
        after = app.state.env_epoch.get("default", 0)
    assert resp.status_code == 200
    assert after == before + 1


def test_non_empty_apply_full_does_not_bump_the_epoch(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    app = _app(tmp_path)
    with TestClient(app) as client:
        before = app.state.env_epoch.get("default", 0)
        resp = client.post("/apply-full", json=S3_SQS)
        after = app.state.env_epoch.get("default", 0)
    assert resp.status_code == 200
    assert after == before


def test_stale_request_is_superseded_before_tofu_ever_starts(tmp_path, monkeypatch):
    """An epoch bump that lands before this request reaches tofu (e.g. a
    concurrent /destroy that raced ahead) must abort BEFORE tofu ever runs --
    the first of the finding's two checkpoints."""
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    app = _app(tmp_path)

    async def fake_translate(stack, **kwargs):
        app.state.env_epoch["default"] = app.state.env_epoch.get("default", 0) + 1
        return TranslateResult(files=_skeleton_files(), refined=True)

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    apply_calls: list = []
    orig_runner_apply = app.state.tf_runner.apply

    async def recording_runner_apply(*args, **kwargs):
        apply_calls.append(1)
        return await orig_runner_apply(*args, **kwargs)

    app.state.tf_runner.apply = recording_runner_apply
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
    assert resp.status_code == 409
    assert resp.json() == STALE_BODY
    assert apply_calls == []  # tofu never even started
    assert app.state.store.head("default") is None


def test_stale_request_is_superseded_right_before_the_final_commit(tmp_path, monkeypatch):
    """The second checkpoint: the epoch can also change WHILE tofu itself is
    running (a slow apply racing a fast concurrent /destroy). Even though
    tofu succeeded, the stale request must not go on to commit."""
    _write_fake_tofu(tmp_path, _APPLY_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    _patch_translate(monkeypatch, TranslateResult(files=_skeleton_files(), refined=True))
    app = _app(tmp_path)

    orig_runner_apply = app.state.tf_runner.apply

    async def apply_then_bump(*args, **kwargs):
        result = await orig_runner_apply(*args, **kwargs)
        app.state.env_epoch["default"] = app.state.env_epoch.get("default", 0) + 1
        return result

    app.state.tf_runner.apply = apply_then_bump
    calls: list[str] = []
    orig_store_apply = app.state.store.apply

    def recording_store_apply(stack):
        calls.append("store.apply")
        return orig_store_apply(stack)

    app.state.store.apply = recording_store_apply
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_SQS)
    assert resp.status_code == 409
    assert resp.json() == STALE_BODY
    assert calls == []
    assert app.state.store.head("default") is None
