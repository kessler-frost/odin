"""M6 — environments are independent (separate worlds, listed, isolated)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.aws.embed import account_for_env
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import CANVAS, FakeRds, FakeRuntime

DB_ONLY = {"nodes": [{"type": "rds", "data": {"label": "db"}}], "edges": []}


def test_environments_are_isolated_and_listed(tmp_path):
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path),
                     rds=FakeRds(), embed=False, complete=False)
    with TestClient(app) as client:
        client.post("/apply?env=staging", json=CANVAS)     # db + api
        client.post("/apply?env=production", json=DB_ONLY)  # db only

        staging = {r["id"] for r in client.get("/world?env=staging").json()["resources"]}
        production = {r["id"] for r in client.get("/world?env=production").json()["resources"]}
        assert "api" in staging and "api" not in production  # isolated desired state

        envs = client.get("/envs").json()["envs"]
        assert "staging" in envs and "production" in envs


def test_each_env_gets_a_distinct_account():
    assert account_for_env("default") == "000000000000"
    assert account_for_env("staging") != account_for_env("production")
    assert len(account_for_env("staging")) == 12 and account_for_env("staging").isdigit()
