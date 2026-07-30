"""`POST /envs/rm` — an environment can be REMOVED, and says whether it is gone.

## What this is for

`odin destroy --env X` tears the resources down and deliberately keeps the
environment: the desired state is what makes a retry possible and the reconciler
is what converges the next apply. odin had only that verb, so an environment was
registered forever once created — seven accumulated during one field-test
session, each with a loop ticking over nothing.

## The contract asserted here

`/envs/rm` performs an action, so it reports whether the END STATE HOLDS
(honesty rule 2). The status is DERIVED once from `_REMOVE_STATUS`, never
initialised optimistically, and every way the removal can fall short leaves
`.odin/<env>/` completely intact — so a failure is retryable, never half-done.

The three guards each read a signal that actually arrives (honesty rule 1):
the reconciler's own task `done()`, the machine's real container list, and the
directory's own `exists()` after the delete. Each has a test below that makes it
FIRE, which is what mutation-testing them is checking.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from odin.server import _REMOVE_FAILED, _REMOVE_OK, _REMOVE_STATUS, create_app
from odin.simulate.runner import TfResult
from odin.spec.store import SpecStore
from tests.api.test_apply import CANVAS, FakeRds, FakeRuntime
from tests.volumes import IN_USE

S3_CANVAS = {"nodes": [{"type": "s3", "data": {"label": "uploads"}}], "edges": []}

DROP = "envrm-drop"
KEEP = "envrm-keep"


class CleanRuntime(FakeRuntime):
    """A machine that really has no container left for any env — `container_names`
    is what `/envs/rm`'s third guard reads, and `FakeRuntime` does not have it
    (which is itself a case: see `test_a_machine_odin_cannot_ask_...`).

    Its volume half comes from `FakeRuntime`/`tests.volumes.FakeVolumes`, which
    models docker's `odin.env` label filter for real — a fake that ignored the
    filter would let a reclaim that swept the whole machine pass."""

    async def container_names(self):
        return []


class SurvivingRuntime(FakeRuntime):
    """A machine where this env's backing container is still up after the
    teardown claimed success."""

    async def container_names(self):
        return [f"odin-aws-rustfs-{DROP}", "odin-aws-rustfs-someone-else"]


class BlindRuntime(FakeRuntime):
    """A machine odin cannot ask: `docker` will not answer at all."""

    async def container_names(self):
        raise RuntimeError("Cannot connect to the Docker daemon")


class FakeAws:
    """Per-env BackingAws stand-in (the `tests/api/test_environments.py` shape)."""

    def __init__(self, runtime=None, env="default", gateway_port=4266, mesh=None):
        self.env = env

    async def ensure_backing(self, service):
        pass

    async def provision(self, service, name, subscriptions=()):
        pass

    async def exists(self, service, name):
        return True

    async def deprovision(self, service, name):
        pass

    async def facts(self, service, name):
        return {"BUCKET": name}

    async def gc(self, active_kinds):
        pass

    async def backing_ports(self):
        return {}


def _app(tmp_path, runtime=None):
    return create_app(
        runtime=runtime or CleanRuntime(), store=SpecStore(tmp_path),
        rds=FakeRds(), aws=FakeAws(), reap_ec2_vms=False,
    )


def _seed(client, app, env: str, canvas=S3_CANVAS) -> None:
    """An env with something in every per-env store odin keeps."""
    client.post(f"/apply?env={env}", json=canvas)
    app.state.gateway_keys.issue(env, "uploads")
    app.state.gateway_stores.tags.set(env, "s3:uploads", {"owner": env})
    app.state.gateway_stores.sqs_queues.set(env, "jobs", {"attributes": {}})


def _files(root, env: str) -> set[str]:
    return {p.name for p in (root / env).rglob("*") if p.is_file()}


# --- the happy path, and the proof that "gone" means gone -----------------


def test_env_rm_removes_the_env_and_says_so(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _seed(client, app, DROP)
        assert (tmp_path / DROP / "HEAD").exists()
        assert (tmp_path / DROP / "keys.json").exists()
        assert (tmp_path / DROP / "gateway" / "tags.json").exists()
        assert DROP in client.get("/envs").json()["envs"]

        # The task OBJECT, captured before the removal -- see below.
        task = app.state.reconcilers[DROP]._task
        assert not task.done()

        resp = client.post(f"/envs/rm?env={DROP}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "removed"
        assert "error" not in body

        # 1. the state directory
        assert not (tmp_path / DROP).exists()
        assert body["state_dir"] == str((tmp_path / DROP).resolve())
        # 2. the env list
        assert DROP not in client.get("/envs").json()["envs"]
        # 3. the reconciler -- the TASK is finished, not merely unreferenced.
        #    `reconcilers.pop` alone proves nothing: an asyncio.Task that
        #    nothing points at goes on running exactly as it did.
        assert DROP not in app.state.reconcilers
        assert task.done()
        # 4. every in-memory cache that was holding something
        forgotten = body["forgotten"]
        assert forgotten["reconciler"] is True
        assert set(forgotten["gateway_stores"]) >= {"tags", "sqs_queues"}
        assert forgotten["gateway_policy"] is True
        # ...and `keys` is EMPTY on this path, which is the correct reading,
        # not a miss: the teardown ran first and `revoke_env` had already
        # dropped them. The field reports what was still cached when the
        # forget step ran, so a non-empty list means one was issued in
        # between -- see the route.
        assert forgotten["keys"] == []
        assert not (tmp_path / DROP / "keys.json").exists()


def test_a_removed_env_takes_its_credentials_and_records_with_it(tmp_path):
    """The in-memory half, which the directory going away does NOT cover:
    `KeyStore._by_key` and `JsonStore._loaded` are process-lifetime caches with
    no invalidation of their own, so a later env of the SAME NAME would be
    served the removed env's records out of memory."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        _seed(client, app, DROP)
        access_key, _secret = app.state.gateway_keys.issue(DROP, "uploads")
        assert app.state.gateway_keys.lookup(access_key) is not None
        assert app.state.gateway_stores.tags.get(DROP, "s3:uploads") == {"owner": DROP}

        assert client.post(f"/envs/rm?env={DROP}").json()["status"] == "removed"

        assert app.state.gateway_keys.lookup(access_key) is None
        assert app.state.gateway_stores.tags.get(DROP, "s3:uploads") is None
        assert app.state.gateway_stores.tags.items(DROP) == {}
        assert app.state.gateway.backing_port(DROP, "s3") is None
        assert app.state.tf_runner.status(DROP)["last"] is None


def test_a_recreated_env_of_the_same_name_starts_empty(tmp_path):
    """The consequence a user actually meets, in the same process."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        _seed(client, app, DROP)
        client.post(f"/envs/rm?env={DROP}")

        client.post(f"/apply?env={DROP}", json={"nodes": [], "edges": []})
        assert app.state.gateway_stores.tags.items(DROP) == {}
        assert client.get(f"/world?env={DROP}").json()["resources"] == []


# --- isolation: removing one env must not touch another ------------------


def test_removing_one_env_leaves_the_other_completely_intact(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _seed(client, app, DROP)
        _seed(client, app, KEEP)
        keep_key, _secret = app.state.gateway_keys.issue(KEEP, "uploads")
        keep_files = _files(tmp_path, KEEP)
        keep_task = app.state.reconcilers[KEEP]._task
        keep_world = client.get(f"/world?env={KEEP}").json()["resources"]
        assert keep_world, "the surviving env must have something to survive WITH"

        assert client.post(f"/envs/rm?env={DROP}").json()["status"] == "removed"

        # state on disk, byte-for-byte the same set of files
        assert _files(tmp_path, KEEP) == keep_files
        assert (tmp_path / KEEP / "HEAD").exists()
        # credentials
        assert app.state.gateway_keys.lookup(keep_key) is not None
        # gateway records
        assert app.state.gateway_stores.tags.get(KEEP, "s3:uploads") == {"owner": KEEP}
        # still listed
        envs = client.get("/envs").json()["envs"]
        assert KEEP in envs and DROP not in envs
        # ...and STILL RECONCILING: the same task, still not done.
        assert app.state.reconcilers[KEEP]._task is keep_task
        assert not keep_task.done()
        health = client.get(f"/world?env={KEEP}").json()["reconciler"]
        assert health["ticking"] is True, health["verdict"]
        assert client.get(f"/world?env={KEEP}").json()["resources"] == keep_world


# --- the guards. each one FIRES here, and none of them deletes anything ---


def test_a_reconciler_that_will_not_stop_blocks_the_removal(tmp_path):
    """THE guard this feature turns on. Deleting an env's state while its loop
    is alive lets the loop re-create `world.json` -- and, through `plan()`, real
    containers -- inside the directory being removed.

    The signal is the loop task's own `done()`, not `_task is None`: `stop()`
    clears that reference whether or not the loop ended, and an unreferenced
    asyncio.Task keeps running. Mutation-test: make
    `Reconciler.loop_finished` return True unconditionally and this fails."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        _seed(client, app, DROP)
        reconciler = app.state.reconcilers[DROP]

        async def _refuse_to_stop():
            return None

        reconciler.stop = _refuse_to_stop
        resp = client.post(f"/envs/rm?env={DROP}")
        del reconciler.stop  # ...so the lifespan can really stop it on the way out

        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["status"] == "remove_failed_loop_running"
        assert "had NOT finished" in body["error"]
        # NOTHING was removed.
        assert (tmp_path / DROP / "HEAD").exists()
        assert DROP in client.get("/envs").json()["envs"]
        assert DROP in app.state.reconcilers


def test_a_surviving_container_blocks_the_removal(tmp_path):
    """A teardown that reported success while a container of this env's is
    still up. Removing the state would leave it running with nothing left on
    disk that names it."""
    app = _app(tmp_path, runtime=SurvivingRuntime())
    with TestClient(app) as client:
        _seed(client, app, DROP)
        resp = client.post(f"/envs/rm?env={DROP}")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "remove_failed_containers_standing"
    assert body["still_standing"]["containers"] == [f"odin-aws-rustfs-{DROP}"]
    assert "docker rm -f" in body["error"]
    assert (tmp_path / DROP / "HEAD").exists()


def test_a_machine_odin_cannot_ask_about_containers_blocks_the_removal(tmp_path):
    """"odin could not tell" must not wear the same words as "there is nothing
    there" (field test 6, F4's sibling). Unknown goes the failure way, and
    nothing is deleted, so a retry once docker answers is clean."""
    app = _app(tmp_path, runtime=BlindRuntime())
    with TestClient(app) as client:
        _seed(client, app, DROP)
        resp = client.post(f"/envs/rm?env={DROP}")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "remove_unverified"
    assert "could not list this machine's containers" in body["error"]
    assert "NOTHING was deleted" in body["error"]
    assert (tmp_path / DROP / "HEAD").exists()


def test_a_failed_teardown_removes_nothing_and_carries_the_whole_report(tmp_path):
    app = _app(tmp_path)
    (tmp_path / DROP / "tf").mkdir(parents=True)  # a workspace, so tofu really runs

    async def _failing_destroy(*args, **kwargs):
        return TfResult(ok=False, exit_code=1, tail=("Error: deleting S3 Bucket",))

    app.state.tf_runner.destroy = _failing_destroy
    with TestClient(app) as client:
        _seed(client, app, DROP)
        resp = client.post(f"/envs/rm?env={DROP}")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "remove_failed_teardown"
    # The teardown's OWN report rides along -- the tofu tail is the diagnosis.
    assert body["teardown"]["tf"]["tail"] == ["Error: deleting S3 Bucket"]
    assert "nothing was forgotten" in body["error"]
    assert (tmp_path / DROP / "HEAD").exists()
    assert app.state.store.get_stack(DROP).resources != ()


def test_a_tofu_run_in_progress_refuses_before_anything_is_touched(tmp_path):
    app = _app(tmp_path)
    real_status = None

    def _busy(env: str) -> dict:
        return {**real_status(env), "running": True}

    with TestClient(app) as client:
        _seed(client, app, DROP)
        # `runner.status(env)["running"]` is the signal `/destroy`'s busy guard
        # really reads -- stubbed at that seam rather than by acquiring the
        # runner's asyncio.Lock, which this (sync) test has no loop to take.
        real_status = app.state.tf_runner.status
        app.state.tf_runner.status = _busy
        resp = client.post(f"/envs/rm?env={DROP}")
        app.state.tf_runner.status = real_status

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["status"] == "remove_refused"
    assert "a tofu run is in progress" in body["error"]
    assert (tmp_path / DROP / "HEAD").exists()


# --- the two answers that are not failures --------------------------------


def test_removing_an_env_that_never_existed_creates_nothing(tmp_path):
    """`odin env rm typo` must not mint `.odin/typo/` on its way to saying no --
    the same rule `/destroy` follows (field test 5, LOW)."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/envs/rm?env=envrm-typo")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "not_found"
    assert "error" not in resp.json()
    assert not (tmp_path / "envrm-typo").exists()


@pytest.mark.parametrize("env", ["..", "../..", "a/b", "", "."])
def test_an_env_name_that_is_not_a_child_of_the_store_is_refused(tmp_path, env):
    """`/envs/rm` ends in `rmtree`. A name that does not resolve to a child of
    the store root is refused before anything is read, written or deleted."""
    app = _app(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    with TestClient(app) as client:
        resp = client.post("/envs/rm", params={"env": env})

    assert resp.status_code == 400, resp.text
    assert resp.json()["status"] == "remove_refused"
    assert "does not name an environment" in resp.json()["error"]
    assert tmp_path.exists() and sentinel.exists()


# --- the SHAPE, not another branch ----------------------------------------


def test_an_outcome_nobody_mapped_is_a_failure():
    """The rule `/destroy` needed four rounds to arrive at, adopted here from
    the start: the status is looked up, and anything the map does not name --
    a new branch, a typo, `None` -- is a failure rather than an inherited
    success."""
    assert _REMOVE_STATUS.get("a_branch_added_next_year", _REMOVE_FAILED) == _REMOVE_FAILED
    assert _REMOVE_STATUS.get(None, _REMOVE_FAILED) == _REMOVE_FAILED
    assert _REMOVE_FAILED not in _REMOVE_OK
    # ...and every success this map CAN produce is one of the two that mean
    # the env is gone. A new entry that is neither fails here.
    assert _REMOVE_OK <= set(_REMOVE_STATUS.values())
    assert {s for s in _REMOVE_STATUS.values() if s in _REMOVE_OK} == _REMOVE_OK


def test_the_route_is_reachable_and_returns_json(tmp_path):
    """The `curl` half of NORTHSTAR directive 8 -- no CLI involved."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        _seed(client, app, DROP)
        resp = client.post(f"/envs/rm?env={DROP}")
    assert resp.headers["content-type"].startswith("application/json")
    assert json.loads(resp.content)["env"] == DROP


def test_an_env_whose_name_suffixes_another_refuses_rather_than_over_deletes(tmp_path):
    """The residual `docs/limits.md` documents, pinned so the doc stays true.

    The container witness matches odin's container NAMING, not a label, so
    `odin-aws-rustfs-b-a` reads as env `a`'s. That is a refusal, never an
    over-delete: nothing is removed, and `b-a` is untouched."""
    class TwoEnvRuntime(FakeRuntime):
        async def container_names(self):
            return ["odin-aws-rustfs-envrm-b-envrm-a"]

    app = _app(tmp_path, runtime=TwoEnvRuntime())
    with TestClient(app) as client:
        _seed(client, app, "envrm-a")
        _seed(client, app, "envrm-b-envrm-a")
        resp = client.post("/envs/rm?env=envrm-a")

    assert resp.json()["status"] == "remove_failed_containers_standing"
    assert resp.json()["still_standing"]["containers"] == ["odin-aws-rustfs-envrm-b-envrm-a"]
    assert (tmp_path / "envrm-a" / "HEAD").exists()
    assert (tmp_path / "envrm-b-envrm-a" / "HEAD").exists()


def test_rds_canvas_env_removes_too(tmp_path):
    """A TF-owned kind, not just the reconciler-owned ones."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        _seed(client, app, DROP, canvas=CANVAS)
        assert client.post(f"/envs/rm?env={DROP}").json()["status"] == "removed"
        assert not (tmp_path / DROP).exists()


# --- the DISK a removed env leaves behind ---------------------------------
#
# An rds instance's PGDATA is a NAMED docker volume since v0.8.14, precisely so
# that replacing the container does not replace the database. The consequence
# nobody closed: `docker rm -f -v` never takes one, and nothing else did either.
# Four orphans from two long-dead environments were measured on the development
# machine. `/envs/rm` is where that closes, and every test below is about the
# reclaim being SCOPED and HONEST rather than merely happening.


def test_removing_an_env_reclaims_its_data_volumes_and_names_them(tmp_path):
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DROP}-app-db-data", DROP)
    runtime.seed_volume(f"odin-rds-{DROP}-other-db-data", DROP)
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        _seed(client, app, DROP, canvas=CANVAS)
        body = client.post(f"/envs/rm?env={DROP}").json()

    assert body["status"] == "removed", body
    # NAMED, not counted: nothing else on the machine would ever say the disk
    # went back, and a reclaim that reports nothing is indistinguishable from one
    # that had nothing to do.
    assert body["reclaimed"]["volumes"] == [
        f"odin-rds-{DROP}-app-db-data", f"odin-rds-{DROP}-other-db-data",
    ]
    assert runtime.volumes == set()


def test_removing_one_env_leaves_another_envs_database_on_the_disk(tmp_path):
    """The blast radius. `env rm` is scoped to the `odin.env` LABEL, so the
    surviving env's data volume is still there afterwards.

    Mutation-test: make `volume_names(env=...)` in `tests/volumes.py` ignore its
    argument (docker's filter dropped) and this fails."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DROP}-db-data", DROP)
    runtime.seed_volume(f"odin-rds-{KEEP}-db-data", KEEP)
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        _seed(client, app, DROP, canvas=CANVAS)
        _seed(client, app, KEEP, canvas=CANVAS)
        assert client.post(f"/envs/rm?env={DROP}").json()["status"] == "removed"

    assert runtime.volumes == {f"odin-rds-{KEEP}-db-data"}


def test_an_env_whose_name_prefixes_this_one_keeps_its_database(tmp_path):
    """The collision a name-shaped filter cannot survive, at the route level.
    `envrm-drop` and `envrm-drop-two` share every character of the shorter one's
    prefix, so `--filter name=odin-rds-envrm-drop-` deletes both."""
    other = f"{DROP}-two"
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DROP}-db-data", DROP)
    runtime.seed_volume(f"odin-rds-{other}-db-data", other)
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        _seed(client, app, DROP, canvas=CANVAS)
        _seed(client, app, other, canvas=CANVAS)
        # The container witness matches on NAMING and would read `other`'s
        # container as this env's, so there must be none for either.
        assert client.post(f"/envs/rm?env={DROP}").json()["status"] == "removed"

    assert runtime.volumes == {f"odin-rds-{other}-db-data"}


def test_a_volume_that_will_not_go_blocks_the_removal_and_is_named(tmp_path):
    """The guard, FIRING. Docker refuses to remove a volume a container still
    references, and deleting `.odin/<env>/` over that refusal would leave a
    Postgres data directory with nothing left on the machine that names it —
    exactly the leak this step exists to close.

    Mutation-test: drop the `if volumes.standing` branch in `_remove_env` and
    this fails."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DROP}-db-data", DROP)
    runtime.refuse_volume(f"odin-rds-{DROP}-db-data")
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        _seed(client, app, DROP, canvas=CANVAS)
        resp = client.post(f"/envs/rm?env={DROP}")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "remove_failed_volumes_standing"
    assert body["still_standing"]["volumes"] == [{
        "volume": f"odin-rds-{DROP}-db-data",
        "reason": IN_USE.format(name=f"odin-rds-{DROP}-db-data"),
    }]
    # Docker's OWN sentence reaches the user (asserted verbatim above), and the
    # error text points AT it and says what to do about it.
    assert "still_standing.volumes` names each one" in body["error"]
    assert "volume is in use" in body["error"]
    assert "docker rm -f <container>" in body["error"]
    # ...and NOTHING was deleted, so a retry after removing the container is clean.
    assert (tmp_path / DROP / "HEAD").exists()
    assert DROP in client.get("/envs").json()["envs"]
    assert runtime.volumes == {f"odin-rds-{DROP}-db-data"}


def test_a_machine_odin_cannot_ask_about_volumes_blocks_the_removal(tmp_path):
    """"odin could not tell" must not wear the words of "there was nothing
    there" (field test 6's F4, applied to the volume half). Unknown goes the
    failure way and deletes nothing."""
    class VolumeBlindRuntime(CleanRuntime):
        async def volume_names(self, env=None):
            raise RuntimeError("Cannot connect to the Docker daemon")

    app = _app(tmp_path, runtime=VolumeBlindRuntime())
    with TestClient(app) as client:
        _seed(client, app, DROP, canvas=CANVAS)
        resp = client.post(f"/envs/rm?env={DROP}")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "remove_unverified"
    assert "could not list this env's Docker volumes" in body["error"]
    assert "NOTHING was deleted" in body["error"]
    assert (tmp_path / DROP / "HEAD").exists()


# --- the reported bug: an env that is gone everywhere but the disk --------


def test_an_env_with_no_directory_still_has_its_volumes_reclaimed(tmp_path):
    """THE reported bug. All four measured orphans had exactly this shape: no
    container, no `.odin/<env>/`, absent from `GET /envs` — and gigabytes on the
    disk. This branch used to return a flat `never_existed` and reclaim nothing,
    which is how they accumulated with no way to find them but `docker volume ls`.

    Mutation-test: restore `return "never_existed", {}` and this fails."""
    runtime = CleanRuntime()
    runtime.seed_volume("odin-rds-envrm-orphan-app-db-data", "envrm-orphan")
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        assert "envrm-orphan" not in client.get("/envs").json()["envs"]
        resp = client.post("/envs/rm?env=envrm-orphan")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # "removed", not "not_found": odin just deleted state a user cannot get back,
    # and calling that "there was nothing here" is the same class of lie as an
    # exit 0 over a standing env.
    assert body["status"] == "removed"
    assert body["reclaimed"]["volumes"] == ["odin-rds-envrm-orphan-app-db-data"]
    assert runtime.volumes == set()
    # ...and it minted no state directory on the way (field test 5, LOW).
    assert not (tmp_path / "envrm-orphan").exists()


def test_an_env_with_neither_a_directory_nor_a_volume_is_still_not_found(tmp_path):
    """The other side of the same branch: `odin env rm typo` must still say
    nothing was there. `removed` is reserved for a removal that really removed
    something."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/envs/rm?env=envrm-typo2")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "not_found"
    assert resp.json()["reclaimed"]["volumes"] == []
    assert "error" not in resp.json()


def test_a_directoryless_env_with_a_live_container_refuses_before_touching_disk(tmp_path):
    """`env` here is a name a HUMAN typed, not one odin read out of its own
    store — and docker volumes are per-machine while a store root is not. On a
    machine running a second odin (every parallel agent worktree has its own
    `.odin/`), that name may be the other one's LIVE environment, whose database
    has a container. So the container witness runs first and refuses.

    Mutation-test: delete the `_surviving_containers` check from `_reclaim_only`
    and this fails."""
    class OtherOdinsEnvRuntime(CleanRuntime):
        async def container_names(self):
            return ["odin-rds-envrm-elsewhere-app-db"]

    runtime = OtherOdinsEnvRuntime()
    runtime.seed_volume("odin-rds-envrm-elsewhere-app-db-data", "envrm-elsewhere")
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        resp = client.post("/envs/rm?env=envrm-elsewhere")

    assert resp.status_code == 500, resp.text
    assert resp.json()["status"] == "remove_failed_containers_standing"
    assert runtime.volumes == {"odin-rds-envrm-elsewhere-app-db-data"}, (
        "another odin's live database must survive a name collision"
    )


def test_a_directoryless_env_odin_cannot_ask_about_is_unverified_not_not_found(tmp_path):
    """Found by MUTATION TESTING, and it is the more dangerous half of the pair.

    `_reclaim_only` has its own `volumes_unknown` branch, and the test for the
    other one (`test_a_machine_odin_cannot_ask_about_volumes_blocks_the_removal`)
    could not reach it — that env has a directory, so it goes through
    `_remove_env`. With this branch broken, an env whose disk odin could not read
    answers `not_found` and **exits 0**: "there was nothing here" over volumes
    that may well still be holding gigabytes. That is the exact shape of the
    exit-0-over-a-standing-env bug this repo fixed three times.

    Mutation-test: replace `_reclaim_only`'s `if volumes.unknown: return
    "volumes_unknown", detail` with `pass` and this fails."""
    class VolumeBlindRuntime(CleanRuntime):
        async def volume_names(self, env=None):
            raise RuntimeError("Cannot connect to the Docker daemon")

    app = _app(tmp_path, runtime=VolumeBlindRuntime())
    with TestClient(app) as client:
        resp = client.post("/envs/rm?env=envrm-unreadable")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "remove_unverified"
    assert body["status"] not in _REMOVE_OK
    assert "could not list this env's Docker volumes" in body["error"]
    assert not (tmp_path / "envrm-unreadable").exists()


def test_a_directoryless_env_whose_volume_will_not_go_is_named_not_ignored(tmp_path):
    runtime = CleanRuntime()
    runtime.seed_volume("odin-rds-envrm-stuck-db-data", "envrm-stuck")
    runtime.refuse_volume("odin-rds-envrm-stuck-db-data")
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        resp = client.post("/envs/rm?env=envrm-stuck")

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "remove_failed_volumes_standing"
    assert body["still_standing"]["volumes"][0]["volume"] == "odin-rds-envrm-stuck-db-data"
    assert runtime.volumes == {"odin-rds-envrm-stuck-db-data"}
