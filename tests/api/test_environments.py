"""M6 — environments are independent (separate worlds, listed, isolated)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime

TWO_DBS = {"nodes": [{"type": "rds", "data": {"label": "db"}},
                     {"type": "rds", "data": {"label": "db2"}}], "edges": []}
DB_ONLY = {"nodes": [{"type": "rds", "data": {"label": "db"}}], "edges": []}
S3_ONLY = {"nodes": [{"type": "s3", "data": {"label": "uploads"}}], "edges": []}


class FakeAws:
    """Per-env stand-in for BackingAws (same constructor signature)."""

    def __init__(self, runtime, env="default"):
        self.env = env
        self.provisioned = []

    def provision(self, service, name, subscriptions=()):
        self.provisioned.append((service, name, subscriptions))

    def exists(self, service, name):
        return True

    def deprovision(self, service, name):
        pass

    def facts(self, service, name):
        return {"BUCKET": name, "endpoint": "http://host.docker.internal:9000"}

    def gc(self, active_kinds):
        pass


def test_environments_are_isolated_and_listed(tmp_path):
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path),
                     rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        client.post("/apply?env=staging", json=TWO_DBS)     # db + db2
        client.post("/apply?env=production", json=DB_ONLY)  # db only

        staging = {r["id"] for r in client.get("/world?env=staging").json()["resources"]}
        production = {r["id"] for r in client.get("/world?env=production").json()["resources"]}
        assert "db2" in staging and "db2" not in production  # isolated desired state

        envs = client.get("/envs").json()["envs"]
        assert "staging" in envs and "production" in envs


def test_each_env_gets_its_own_aws_backing(tmp_path, monkeypatch):
    # The default wiring hands every env its OWN BackingAws — same node label,
    # zero cross-env collision. FakeAws stands in so no containers run.
    monkeypatch.setattr("odin.server.BackingAws", FakeAws)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds())
    with TestClient(app) as client:
        client.post("/apply?env=staging", json=S3_ONLY)
        client.post("/apply?env=production", json=S3_ONLY)

        staging_aws = app.state.reconcilers["staging"]._aws
        production_aws = app.state.reconcilers["production"]._aws
        assert staging_aws is not production_aws            # per-env aws object
        assert (staging_aws.env, production_aws.env) == ("staging", "production")
        # set-wise: the apply-kicked tick can race the loop's first tick, and
        # provision is idempotent — what matters is no foreign-env resource.
        assert set(staging_aws.provisioned) == {("s3", "uploads", ())}
        assert set(production_aws.provisioned) == {("s3", "uploads", ())}

        # no cross-env World leakage: each world tracks only its own bucket
        for env in ("staging", "production"):
            ids = [r["id"] for r in client.get(f"/world?env={env}").json()["resources"]]
            assert ids == ["uploads"]
