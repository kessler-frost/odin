"""S2.5 — the /apply path drives the Reconciler (wiring test, fakes)."""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.simulate.workspace import tf_dir
from odin.spec.store import SpecStore
from tests.volumes import FakeVolumes


class FakeRuntime(FakeVolumes):
    """`FakeVolumes` because `/envs/rm` reclaims this env's named volumes as part
    of its teardown, and it asks the RUNTIME for them -- a fake without the
    volume surface makes every removal answer `remove_unverified` (correctly:
    odin will not call a removal complete on a half it could not read)."""

    def __init__(self):
        self.runs = []

    async def run_container(self, spec):
        self.runs.append(spec.name)
        return RunHandle(id="x", name=spec.name)

    async def stop(self, name):
        pass

    async def facts(self, name, container_port=0):
        return ContainerFacts(phase="pending")

    async def stats(self, name):
        return {"cpu": 0.0, "ram": 0.0}

    async def ensure_host(self):
        return HostFacts()


class FakeRds:
    def __init__(self):
        self.created = []

    async def create_db(self, db_id, user, pw):
        self.created.append(db_id)

    async def delete_db(self, db_id):
        pass

    async def endpoint(self, db_id):
        return None

    def container_name(self, db_id):
        return f"odin-rds-default-{db_id}"


CANVAS = {
    "nodes": [{"type": "rds", "data": {"label": "db"}}],
    "edges": [],
}


def test_apply_stores_the_stack_and_never_creates_a_tf_owned_resource(tmp_path):
    """`/apply` is the reconciler-only half of the pipeline. W2.7 made `rds`
    TF-owned, so this route must commit the desired state and STOP -- creating
    the database here would race the `tofu apply` that `/apply-full` runs (the
    exact class of bug /apply-full's deferred store commit exists to avoid)."""
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, backings=False)
    with TestClient(app) as client:
        resp = client.post("/apply", json=CANVAS)
        assert resp.json()["status"] == "applied" and resp.json()["rev"]
        assert app.state.store.get_stack("default").resources[0].kind == "rds"

        assert rds.created == []               # tofu's CreateDBInstance owns this now
        assert rt.runs == []
        # And nothing entered World: only tf_status.project() (fed by the
        # gateway's own DB-instance record) can put an rds node there.
        assert client.get("/world").json()["resources"] == []


def test_mesh_endpoint_returns_empty_network(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, backings=False)
    with TestClient(app) as client:
        body = client.get("/mesh").json()
        assert body["network"] == "default" and body["hosts"] == []  # no hosts joined yet


def test_destroy_prunes(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, backings=False)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        client.post("/destroy")
        world = client.get("/world").json()
        assert world["resources"] == []


class RecordingAws:
    """A `BackingAws`-shaped stand-in that records the ORDER of the calls
    `/destroy` makes, which is the whole point of finding B6's fix."""

    def __init__(self, log: list) -> None:
        self.log = log

    async def ensure_backing(self, kind: str) -> None:
        self.log.append(("ensure", kind))

    async def gc(self, kinds: set) -> None:
        self.log.append(("gc", tuple(sorted(kinds))))

    async def backing_ports(self) -> dict:
        return {"s3": 9001}

    async def exists(self, kind: str, rid: str) -> bool:
        return True

    async def facts(self, kind: str, rid: str) -> dict:
        return {}

    async def provision(self, kind: str, rid: str, *args) -> None:
        self.log.append(("provision", kind, rid))

    async def deprovision(self, kind: str, rid: str) -> None:
        self.log.append(("deprovision", kind, rid))

    async def subscriptions(self, rid: str) -> tuple:
        return ()

    async def aws_env(self) -> dict:
        return {}


S3_CANVAS = {"nodes": [{"type": "s3", "data": {"label": "uploads"}}], "edges": []}


def test_destroy_boots_the_backings_before_tofu_then_gcs_them_after(tmp_path, monkeypatch):
    """Field test 2, finding B6: `odin destroy` on a restored env ran 8m26s with
    no progress. `/destroy` never booted the backings, so the gateway 503'd every
    AWS call the destroy made and aws-sdk-go retried each ~25x with backoff --
    silently. `tofu destroy` genuinely needs those containers reachable: an s3
    bucket is deleted by a real DeleteBucket forwarded to RustFS.

    Also the gc-versus-ensure hazard, asserted directly: nothing may gc between
    the ensure and tofu finishing, and NOTHING may still be running afterwards
    (`gc(())` -- an empty desired kind set -- is what stops every backing)."""
    log: list = []

    async def fake_destroy(self, env, gateway_port, access_key, secret_key, secrets=frozenset()):
        # Longer than the reconciler's 1.0s poll interval ON PURPOSE: this is
        # the gc-versus-ensure race test. Without hold(), a background tick
        # lands mid-destroy and its gc (the desired Stack is still the s3 one
        # here, but on a fresh/restored env it is empty) stops the very
        # container tofu is talking to.
        await asyncio.sleep(1.3)
        log.append(("tofu-destroy", env))
        from odin.simulate.runner import TfResult
        return TfResult(ok=True, exit_code=0)

    monkeypatch.setattr("odin.simulate.runner.TfRunner.destroy", fake_destroy)
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(),
        aws=RecordingAws(log), backings=False,
    )
    (tmp_path / "default" / "tf").mkdir(parents=True)  # a workspace exists -> tofu runs
    with TestClient(app) as client:
        client.post("/apply", json=S3_CANVAS)
        log.clear()
        resp = client.post("/destroy")

    assert resp.status_code == 200, resp.text
    assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}, resp.json()
    kinds = [entry for entry in log if entry[0] in ("ensure", "gc", "tofu-destroy")]
    assert ("ensure", "s3") in kinds, kinds
    assert kinds.index(("ensure", "s3")) < kinds.index(("tofu-destroy", "default")), kinds
    # Nothing gc'd until tofu is done, and the LAST gc leaves nothing running.
    gcs = [i for i, entry in enumerate(kinds) if entry[0] == "gc"]
    assert all(i > kinds.index(("tofu-destroy", "default")) for i in gcs), kinds
    assert kinds[gcs[-1]] == ("gc", ()), kinds


def test_destroy_with_no_tf_workspace_boots_nothing(tmp_path):
    """The ensure is scoped to the case that needs it: nothing tofu-managed
    means no AWS calls to make reachable, so booting containers to tear down
    nothing would be pure waste."""
    log: list = []
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(),
        aws=RecordingAws(log), backings=False,
    )
    with TestClient(app) as client:
        client.post("/apply", json=S3_CANVAS)
        log.clear()
        assert client.post("/destroy").status_code == 200
    assert not [entry for entry in log if entry[0] == "ensure"], log


def test_destroy_revokes_the_envs_gateway_keys(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, backings=False)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        access_key, _secret_key = app.state.gateway_keys.issue("default", "db")
        assert app.state.gateway_keys.lookup(access_key) is not None

        client.post("/destroy")
        assert app.state.gateway_keys.lookup(access_key) is None


# --- field test 3, P2-5: `/world` must not lose a resource that EXISTS ------
#
# A 22-node canvas whose ECS node never came up failed the apply, and a failed
# apply deliberately does not commit the desired state -- so the reconciler
# never provisioned or observed the 12 s3/sqs/sns/dynamodb nodes and the
# trailing `gc({})` stopped the backings it had just booted. `odin world`
# showed 12 rows; `tofu state` showed 26 resources; `aws s3api list-buckets`
# answered ServiceUnavailable. The nodes had no badge at all.


def _tf_state(root, env, *names):
    directory = tf_dir(root, env)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "terraform.tfstate").write_text(json.dumps({
        "version": 4,
        "resources": [
            {"mode": "managed", "type": "aws_s3_bucket", "name": name,
             "instances": [{"attributes": {"bucket": name, "tags": {"odin:node": name}}}]}
            for name in names
        ],
    }))


def test_world_shows_a_resource_tofu_created_whose_backing_is_down(tmp_path):
    # A non-`default` env deliberately: `default` is the env lifespan always
    # resumes a reconciler for, and that reconciler republishes the gateway's
    # routing table every tick -- which is the very thing the next test sets.
    store = SpecStore(tmp_path)
    app = create_app(runtime=FakeRuntime(), store=store, rds=FakeRds(), backings=False)
    _tf_state(store.root, "e2", "uploads")
    with TestClient(app) as client:
        rows = client.get("/world", params={"env": "e2"}).json()["resources"]

    assert [r["id"] for r in rows] == ["uploads"], "a resource that exists must not vanish"
    assert rows[0]["kind"] == "s3"
    assert rows[0]["phase"] == "crashed"                       # an honest phase, not absence
    assert "ServiceUnavailable" in rows[0]["verdict"]           # ...and the error the user sees


def test_world_says_nothing_extra_once_the_gateway_can_route_to_the_backing(tmp_path):
    """No false alarm during a healthy apply: the backing is up from
    `ensure_backings` well before the reconciler observes the new bucket, and
    the gateway's own routing table is what this reads."""
    store = SpecStore(tmp_path)
    app = create_app(runtime=FakeRuntime(), store=store, rds=FakeRds(), backings=False)
    _tf_state(store.root, "e2", "uploads")
    app.state.gateway.update("e2", {}, {"s3": 9000})  # what ensure_backings registers
    with TestClient(app) as client:
        assert client.get("/world", params={"env": "e2"}).json()["resources"] == []


def test_world_stays_empty_for_an_env_that_never_applied_anything(tmp_path):
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        assert client.get("/world", params={"env": "nothing-here"}).json()["resources"] == []
