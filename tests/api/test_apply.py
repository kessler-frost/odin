"""S2.5 — the new /apply path drives the Reconciler (wiring test, fakes)."""
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
        return f"ministack-rds-{db_id}"


CANVAS = {
    "nodes": [
        {"type": "rds", "data": {"label": "db"}},
        {"type": "service", "data": {
            "label": "api", "image": "app:latest", "port": 8000,
            "env": {"DATABASE_URL": "${{db.DATABASE_URL}}"}}},
    ],
    "edges": [],
}


def test_apply_translates_stores_and_reconciles(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, embed=False, complete=False)
    with TestClient(app) as client:
        resp = client.post("/apply", json=CANVAS)
        assert resp.json()["status"] == "applied" and resp.json()["rev"]

        world = client.get("/world").json()
        phases = {r["id"]: r["phase"] for r in world["resources"]}
        assert rds.created == ["db"]          # rds creation was driven
        assert phases["db"] == "starting"
        assert phases["api"] == "blocked"     # gated on db, not run
        assert "api" not in rt.runs


def test_mesh_endpoint_returns_empty_network(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, embed=False, complete=False)
    with TestClient(app) as client:
        body = client.get("/mesh").json()
        assert body["network"] == "default" and body["hosts"] == []  # no hosts joined yet


def test_preview_returns_diff_structure(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, embed=False, complete=False)
    with TestClient(app) as client:
        resp = client.post("/preview", json=CANVAS)
        body = resp.json()
        assert "diff" in body and body["env"] == "default"  # staged-changeset shape


def test_destroy_prunes(tmp_path):
    rt, rds = FakeRuntime(), FakeRds()
    app = create_app(runtime=rt, store=SpecStore(tmp_path), rds=rds, embed=False, complete=False)
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS)
        client.post("/destroy")
        world = client.get("/world").json()
        assert world["resources"] == []
