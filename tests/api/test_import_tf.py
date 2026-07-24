"""S4 -- the `/import-tf` route. `source="hcl"` is fully deterministic (no
tofu, no SDK) so it's exercised for real; `source="live"` needs a real
gateway+backings round-trip (tests/simulate/test_import_tf_e2e.py, marked
integration) so here only the route<->import_tf wiring + operator-credential
issuance is verified with a monkeypatched `import_live`, mirroring test_tf.py."""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.agent.import_tf import ImportResult
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.runtime.colima import ContainerFacts, HostFacts, RunHandle
from odin.server import create_app
from odin.spec.store import SpecStore


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
    def create_db(self, db_id, user, pw):
        pass

    def delete_db(self, db_id):
        pass

    def endpoint(self, db_id):
        return None

    def container_name(self, db_id):
        return f"odin-rds-default-{db_id}"


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def test_import_tf_hcl_source_parses_for_real(tmp_path):
    app = _app(tmp_path)
    body = {"source": "hcl", "hcl": 'resource "aws_s3_bucket" "uploads" {\n  bucket = "uploads"\n}\n'}
    with TestClient(app) as client:
        resp = client.post("/import-tf", params={"env": "default"}, json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["nodes"] == [
        {"id": "uploads", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "uploads"}},
    ]
    assert payload["edges"] == []
    assert payload["unsupported"] == []


def test_import_tf_hcl_source_lists_unsupported_types(tmp_path):
    app = _app(tmp_path)
    body = {"source": "hcl", "hcl": 'resource "aws_cloudwatch_log_group" "logs" {\n  name = "logs"\n}\n'}
    with TestClient(app) as client:
        resp = client.post("/import-tf", params={"env": "default"}, json=body)
    assert resp.status_code == 200
    unsupported = resp.json()["unsupported"]
    assert unsupported == [{"type": "aws_cloudwatch_log_group", "name": "logs", "reason": unsupported[0]["reason"]}]
    assert "not supported" in unsupported[0]["reason"]


def test_import_tf_live_source_issues_operator_credentials_and_forwards_to_import_live(tmp_path, monkeypatch):
    app = _app(tmp_path)
    seen = {}

    async def fake_import_live(resources, gateway_port, access_key, secret_key):
        seen["resources"] = resources
        seen["gateway_port"] = gateway_port
        seen["access_key"] = access_key
        seen["secret_key"] = secret_key
        return ImportResult(nodes=[{"id": "uploads", "type": "s3", "position": {"x": 0, "y": 0}, "data": {}}])

    monkeypatch.setattr("odin.server.import_tf_mod.import_live", fake_import_live)
    body = {"source": "live", "resources": [{"type": "s3", "id": "uploads"}]}
    with TestClient(app) as client:
        resp = client.post("/import-tf", params={"env": "default"}, json=body)
    assert resp.status_code == 200
    assert resp.json()["nodes"][0]["id"] == "uploads"

    assert len(seen["resources"]) == 1
    assert seen["resources"][0].type == "s3"
    assert seen["resources"][0].id == "uploads"

    access_key, _secret = app.state.gateway_keys.issue("default", OPERATOR_NODE_ID)
    assert seen["access_key"] == access_key
    assert app.state.gateway_keys.lookup(access_key) is not None
