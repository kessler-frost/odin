"""S2.5 — the /apply path drives the Reconciler (wiring test, fakes)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.spec.store import SpecStore


class FakeRuntime:
    def __init__(self):
        self.runs = []

    def run_container(self, spec):
        self.runs.append(spec.name)
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


def test_destroy_revokes_the_envs_gateway_keys(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, backings=False)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        access_key, _secret_key = app.state.gateway_keys.issue("default", "db")
        assert app.state.gateway_keys.lookup(access_key) is not None

        client.post("/destroy")
        assert app.state.gateway_keys.lookup(access_key) is None
