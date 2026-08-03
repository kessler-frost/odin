"""An S3 removal notification, through the REAL gateway to a REAL RustFS: does
odin fire for the key it actually removed, and stay silent for the key that was
never there?

WHY THIS FILE EXISTS AT ALL. Until it did, every S3 delete-notification test in
the repo called `synth.postprocess` DIRECTLY, and the one real-gateway S3 e2e
(`test_dispatch_s3_e2e.py`) is PUT-only -- so the delete path had no coverage
through `app.py` whatsoever. That matters more here than for a PUT, because the
fix being measured lives in `app.py` and NOT in `postprocess`: the existence
answer has to be taken BEFORE the forward, and a unit test that hands
`postprocess` an `absent` set of its own construction proves the filter and
says nothing about whether anybody ever fills it in. This drives real boto3
through the real gateway to a real container, so the wiring is the subject.

WHAT WAS MEASURED FIRST, against `rustfs/rustfs:latest` on 2026-08-03, because
the fix would be worthless if the discriminator it rests on were imagined:

    HEAD  a key that exists          -> 200 (content-length, etag, last-modified)
    HEAD  a key that never existed   -> 404
    DELETE a key that exists         -> 204
    DELETE a key that never existed  -> 204          <- identical
    DeleteObjects, one of each       -> 200, BOTH under <Deleted>, zero <Error>

-- so 200-vs-404 on HEAD is the only signal that separates them, and it has to
be read before the delete destroys it.

Both delete SHAPES are exercised, because they reach the key by different
routes (`DELETE /{b}/{k}` from the path, `POST /{b}?delete` from the request
body) and a fix for one is not a fix for the other.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "s3del-e2e"
BUCKET = "removals"
LAMBDA_ARN = "arn:aws:lambda:us-east-1:000000000000:function:on-remove"

LIVE_KEY = "incoming/real.jpg"
GHOST_KEY = "incoming/never-existed.jpg"
LIVE_BATCH_KEY = "incoming/real-batch.jpg"
GHOST_BATCH_KEY = "incoming/never-existed-batch.jpg"


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture
def cleanup():
    """Scoped to THIS env's own container name, never a machine-wide sweep."""
    yield
    _docker("rm", "-f", "-v", f"odin-aws-rustfs-{ENV}")


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-s3-delete-notification-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _await(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(1.0)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def _fired(store_root: Path) -> list[str]:
    """The keys odin ENQUEUED, read off the file the dispatcher drains.

    Deliberately not the dispatcher's own output: this test is about which
    events were RAISED, and reading them here needs no Lambda, no tofu and no
    tick to have run."""
    records = store_root / ENV / "gateway" / "dispatch.json"
    stored = json.loads(records.read_text()) if records.exists() else {}
    return sorted(v["key"] for k, v in stored.items() if k.startswith("pending:"))


def test_a_removal_fires_for_the_key_that_was_there_and_not_for_the_one_that_never_was(store_root, cleanup):
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as http:
        # The bucket is a canvas node, which is what boots this env's RustFS --
        # nothing else does, and without it every S3 call 503s.
        http.post("/apply", json={
            "nodes": [{"id": "s3-node", "type": "s3", "data": {"label": BUCKET}}], "edges": [],
        }, params={"env": ENV})
        _await(
            lambda: {r["id"]: r["phase"] for r in
                     http.get("/world", params={"env": ENV}).json()["resources"]}.get(BUCKET) == "healthy",
            180.0, "the s3 backing never became healthy",
        )

        gateway_port = http.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        s3 = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

        s3.put_bucket_notification_configuration(
            Bucket=BUCKET,
            NotificationConfiguration={"LambdaFunctionConfigurations": [
                {"Id": "on-remove", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
            ]},
        )
        for key in (LIVE_KEY, LIVE_BATCH_KEY):
            s3.put_object(Bucket=BUCKET, Key=key, Body=b"bytes")

        # The two PUTs above match no configuration (this bucket only asked for
        # removals), so the store is empty before the deletes -- stated as an
        # assertion rather than assumed, since a stray Created record would
        # make the removal count below read wrong.
        assert _fired(store_root) == [], "only removals are configured on this bucket"

        # SHAPE 1: single-object DELETE, key in the path. Both answer 204.
        s3.delete_object(Bucket=BUCKET, Key=LIVE_KEY)
        s3.delete_object(Bucket=BUCKET, Key=GHOST_KEY)

        # SHAPE 2: DeleteObjects, keys in the request body. RustFS reports both
        # under <Deleted> with zero <Error>, so the response cannot tell them
        # apart -- only the pre-forward probe can.
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [
            {"Key": LIVE_BATCH_KEY}, {"Key": GHOST_BATCH_KEY},
        ]})

    # The expected list is written out in full rather than derived from the
    # keys above: a comprehension over the same tuples would pass for a gateway
    # that had stopped probing and started firing for everything.
    assert _fired(store_root) == ["incoming/real-batch.jpg", "incoming/real.jpg"]
