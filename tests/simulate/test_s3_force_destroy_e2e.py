"""Field-test finding #4 (MED) re-verify: a non-empty S3 bucket must tear down
cleanly. The field test's `odin destroy` errored `BucketNotEmpty` because the
generated aws_s3_bucket had no force_destroy -- the backing prune reclaimed the
data but tofu's own state was left inconsistent. force_destroy=true empties the
bucket before deleting it, so the empty-canvas teardown succeeds with an object
still in the bucket.
"""
from __future__ import annotations

import shutil

import boto3
import pytest
from botocore.config import Config
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "s3-force-destroy-e2e"
CANVAS = {"nodes": [{"id": "n1", "type": "s3", "data": {"label": "artifacts"}}], "edges": []}
EMPTY_CANVAS = {"nodes": [], "edges": []}


async def test_non_empty_bucket_destroys_clean(tmp_path, monkeypatch):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}, resp.json()

        # Put an object so the bucket is NON-empty at teardown.
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        s3 = await boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        s3.put_object(Bucket="artifacts", Key="keep.txt", Body=b"data")

        # Empty canvas = full destroy: force_destroy empties + deletes the
        # bucket instead of erroring BucketNotEmpty.
        resp2 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert resp2.status_code == 200, resp2.text
        body = resp2.json()
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        # Belt-and-braces: no BucketNotEmpty anywhere in the tofu output.
        assert "BucketNotEmpty" not in " ".join(body["tf"].get("tail", []) or []), body
