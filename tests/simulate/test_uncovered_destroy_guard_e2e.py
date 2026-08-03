"""Field test 5 (HIGHEST): the typo'd apply that deleted a real bucket, its
object and the backing -- proved against the real substrate.

The unit suite (tests/api/test_uncovered_destroy.py) pins the guard's logic.
This proves the thing that actually matters, with real tofu, a real rustfs
backing and a real object written through the gateway:

  1. apply two buckets for real, and put an object in one of them;
  2. change that node's `type` from "s3" to "s3 " -- one character, the exact
     field-test mutation -- and the apply must REFUSE, name the node, and exit
     nonzero through the CLI's own convention;
  3. the bucket and the object are STILL THERE afterwards. This is the whole
     point: the field test's canvas came back `world is empty`, the bucket gone
     from tofu's state, and boto3 answering ServiceUnavailable because the
     rustfs backing had been gc'd too -- all behind `status: applied`, `tf: ok`,
     exit 0;
  4. and the teardown story is UNBROKEN: deleting the other node from the
     canvas still destroys it, and an empty canvas still destroys everything.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import boto3
import pytest
import typer
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.agent import translate as translate_mod
from odin.cli import http
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "uncovered-destroy-e2e"
KEEP, GOING = "keepme", "goingaway"


def _canvas(keep_type: str = "s3", nodes: tuple[str, ...] = (KEEP, GOING)) -> dict:
    types = {KEEP: keep_type, GOING: "s3"}
    return {
        "nodes": [{"id": n, "type": types[n], "data": {"label": n}} for n in nodes],
        "edges": [],
    }


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-uncovered-destroy-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _state_buckets(root: Path) -> list[str]:
    state = root / ENV / "tf" / "terraform.tfstate"
    parsed = json.loads(state.read_text()) if state.exists() else {}
    return sorted(r["name"] for r in parsed.get("resources", []) if r["type"] == "aws_s3_bucket")


def test_a_typoed_type_cannot_delete_a_live_bucket(store_root, monkeypatch):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    app = create_app(store=SpecStore(store_root))
    with TestClient(app) as client:
        # 1 -- two real buckets, and a real object in one of them.
        applied = client.post("/apply-full", params={"env": ENV}, json=_canvas())
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied", applied.json()
        assert _state_buckets(store_root) == [GOING, KEEP]

        port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        s3 = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        s3.put_object(Bucket=KEEP, Key="receipt.txt", Body=b"the object the field test lost")
        assert s3.get_object(Bucket=KEEP, Key="receipt.txt")["Body"].read() == b"the object the field test lost"

        # 2 -- ONE character: "s3" -> "s3 ". The apply must refuse.
        typo = client.post("/apply-full", params={"env": ENV}, json=_canvas(keep_type="s3 "))
        assert typo.status_code == 409, typo.text
        body = typo.json()
        assert [item["node"] for item in body["would_destroy"]] == [KEEP], body
        assert KEEP in body["error"] and "DESTROY" in body["error"], body["error"]
        assert "'s3 '" in body["error"], body["error"]
        # `cli/http.body_or_fail` is what turns this into a nonzero `odin apply`.
        with pytest.raises(typer.Exit):
            http.body_or_fail(typo)

        # 3 -- and the claim is true: the bucket, the object and the backing
        # are all exactly where they were. In the field test all three were
        # gone and the apply said `applied / tf: ok`.
        assert _state_buckets(store_root) == [GOING, KEEP]
        assert s3.get_object(Bucket=KEEP, Key="receipt.txt")["Body"].read() == b"the object the field test lost"
        assert {r["id"] for r in client.get("/world", params={"env": ENV}).json()["resources"]} >= {KEEP, GOING}

        # 4a -- the teardown story: DELETING a node still destroys it.
        removed = client.post("/apply-full", params={"env": ENV}, json=_canvas(nodes=(KEEP,)))
        assert removed.status_code == 200, removed.text
        assert removed.json()["status"] == "applied", removed.json()
        assert _state_buckets(store_root) == [KEEP]
        with pytest.raises(ClientError) as gone:
            s3.head_bucket(Bucket=GOING)
        assert gone.value.response["ResponseMetadata"]["HTTPStatusCode"] in (403, 404)
        # ...and the node that was only ever protected is untouched.
        assert s3.get_object(Bucket=KEEP, Key="receipt.txt")["Body"].read() == b"the object the field test lost"

        # 4b -- and an empty canvas is still a full destroy.
        emptied = client.post("/apply-full", params={"env": ENV}, json={"nodes": [], "edges": []})
        assert emptied.status_code == 200, emptied.text
        assert emptied.json()["status"] == "applied", emptied.json()
        assert _state_buckets(store_root) == []

        client.post("/destroy", params={"env": ENV})
