"""G5 — the gateway against reality: real containers, real SigV4, real
denials (task-g5-brief.md; PRD docs/superpowers/specs/2026-07-22-gateway-prd.md
§6). The app-workload layer is parked (NORTHSTAR.md, tag app-layer-parked),
so principals are exercised directly: `app.state.gateway_keys.issue(env,
node_id)` mints creds for a workload identity, and iam edges are authored
against a phantom (unknown-kind) canvas node standing in for that workload
-- edges-as-grants outlive workload kinds (translate.py, see
tests/spec/test_translate.py's survival test for the unit-level proof).

Every test boots a real `create_app()` (real ColimaRuntime, real
BackingAws-provisioned RustFS/goaws/dynalite, real gateway listener on
ODIN_GATEWAY_PORT=0), applies a canvas, waits for /world healthy, drives
real boto3/aws-cli SigV4 traffic through the gateway, destroys, and asserts
zero odin containers survive. Marked `integration`: needs Colima/Docker
with the backing + aws-cli images pulled.
"""
from __future__ import annotations

import statistics
import subprocess
import time

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

# A phantom "worker" node stands in for a workload identity: unknown canvas
# kind (dropped from Stack.resources by translate.py), but its iam edge to
# the s3 node still compiles into a policy (the post-ripout contract).
CANVAS_S3_WITH_WORKER_EDGE = {
    "nodes": [
        {"id": "s3-node", "type": "s3", "data": {"label": "uploads"}},
        {"id": "worker-node", "type": "phantomWorkload", "data": {"label": "worker"}},
    ],
    "edges": [{"source": "worker-node", "target": "s3-node",
               "data": {"edgeType": "iam", "permissions": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]}}],
}

CANVAS_UPLOADS_AND_SECRETS = {
    "nodes": [
        {"id": "n1", "type": "s3", "data": {"label": "uploads"}},
        {"id": "n2", "type": "s3", "data": {"label": "secrets"}},
    ],
    "edges": [],
}

# jobs (sqs) / alerts (sns, subscribed to jobs) / sessions (dynamodb), all
# granted to "worker" -- the multi-service acceptance canvas (test 4).
CANVAS_MULTI_SERVICE = {
    "nodes": [
        {"id": "sqs-node", "type": "sqs", "data": {"label": "jobs"}},
        {"id": "sns-node", "type": "sns", "data": {"label": "alerts"}},
        {"id": "ddb-node", "type": "dynamodb", "data": {"label": "sessions"}},
        {"id": "worker-node", "type": "phantomWorkload", "data": {"label": "worker"}},
    ],
    "edges": [
        {"source": "sns-node", "target": "sqs-node"},  # the alerts->jobs subscription
        {"source": "worker-node", "target": "sqs-node",
         "data": {"edgeType": "iam", "permissions": ["sqs:SendMessage", "sqs:ReceiveMessage"]}},
        {"source": "worker-node", "target": "sns-node",
         "data": {"edgeType": "iam", "permissions": ["sns:Publish"]}},
        {"source": "worker-node", "target": "ddb-node",
         "data": {"edgeType": "iam", "permissions": ["dynamodb:PutItem", "dynamodb:GetItem"]}},
    ],
}


def _world(client, env="default") -> dict:
    resources = client.get("/world", params={"env": env}).json()["resources"]
    return {r["id"]: r for r in resources}


def _phases(client, env="default") -> dict:
    return {rid: r["phase"] for rid, r in _world(client, env).items()}


def _wait(client, predicate, timeout=120.0, env="default", step=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(_phases(client, env)):
            return
        time.sleep(step)
    raise AssertionError(f"not met within {timeout}s (last={_phases(client, env)})")


def _destroy(client, env="default"):
    client.post("/destroy", params={"env": env})
    _wait(client, lambda p: not p, env=env)


def _gateway_port(client) -> int:
    return client.get("/health").json()["gateway"]["port"]


def _s3_client(port: int, access_key: str, secret_key: str):
    return boto3.client(
        "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _client(service: str, port: int, access_key: str, secret_key: str):
    return boto3.client(
        service, endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )


def _run_aws_cli(port: int, access_key: str, secret_key: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
         "-e", f"AWS_ACCESS_KEY_ID={access_key}", "-e", f"AWS_SECRET_ACCESS_KEY={secret_key}",
         "-e", "AWS_DEFAULT_REGION=us-east-1",
         "-e", f"AWS_ENDPOINT_URL=http://host.docker.internal:{port}",
         "amazon/aws-cli", *args],
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for cid in rt.list_odin():
        rt.stop(cid)


def test_edge_grants_and_absence_denies(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_S3_WITH_WORKER_EDGE)
        _wait(client, lambda p: p.get("uploads") == "healthy")
        port = _gateway_port(client)

        worker_key, worker_secret = app.state.gateway_keys.issue("default", "worker")
        worker_s3 = _s3_client(port, worker_key, worker_secret)
        worker_s3.put_object(Bucket="uploads", Key="hello.txt", Body=b"payload-bytes")
        assert worker_s3.get_object(Bucket="uploads", Key="hello.txt")["Body"].read() == b"payload-bytes"

        stranger_key, stranger_secret = app.state.gateway_keys.issue("default", "stranger")
        stranger_s3 = _s3_client(port, stranger_key, stranger_secret)
        with pytest.raises(ClientError) as exc_info:
            stranger_s3.put_object(Bucket="uploads", Key="nope.txt", Body=b"x")
        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"

        events = client.get("/events").json()
        denied = [e for e in events if e.get("type") == "access_denied"]
        assert any(
            e.get("resource_id") == "stranger" and e.get("action") == "s3:PutObject" and e.get("target") == "uploads"
            for e in denied
        ), denied

        _destroy(client)
    assert runtime.list_odin() == []


def test_foreign_env_creds_denied(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", params={"env": "a"}, json=CANVAS_S3_WITH_WORKER_EDGE)
        client.post("/apply", params={"env": "b"}, json=CANVAS_UPLOADS_AND_SECRETS)
        _wait(client, lambda p: p.get("uploads") == "healthy", env="a")
        _wait(client, lambda p: p.get("uploads") == "healthy" and p.get("secrets") == "healthy", env="b")
        port = _gateway_port(client)

        worker_key, worker_secret = app.state.gateway_keys.issue("a", "worker")
        s3 = _s3_client(port, worker_key, worker_secret)

        # a's key against a's OWN bucket: scoping doesn't break legitimate access.
        s3.put_object(Bucket="uploads", Key="a-file.txt", Body=b"a-data")
        assert s3.get_object(Bucket="uploads", Key="a-file.txt")["Body"].read() == b"a-data"

        # the SAME key against a resource that exists only in env b: denied.
        # a's compiled policy has no statement for "secrets" at all -- there is
        # no path from a's principal into b's world (statements_for is env-keyed).
        with pytest.raises(ClientError) as exc_info:
            s3.put_object(Bucket="secrets", Key="x.txt", Body=b"x")
        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"

        _destroy(client, env="a")
        _destroy(client, env="b")
    assert runtime.list_odin() == []


def test_container_crosses_to_gateway(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_S3_WITH_WORKER_EDGE)
        _wait(client, lambda p: p.get("uploads") == "healthy")
        port = _gateway_port(client)

        worker_key, worker_secret = app.state.gateway_keys.issue("default", "worker")
        stranger_key, stranger_secret = app.state.gateway_keys.issue("default", "stranger")

        allowed = _run_aws_cli(port, worker_key, worker_secret, ["s3", "ls", "s3://uploads"])
        assert allowed.returncode == 0, allowed.stderr

        denied = _run_aws_cli(port, stranger_key, stranger_secret, ["s3", "ls", "s3://uploads"])
        assert denied.returncode != 0
        assert "AccessDenied" in denied.stderr

        _destroy(client)
    assert runtime.list_odin() == []


def test_sqs_sns_dynamodb_through_gateway(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_MULTI_SERVICE)
        _wait(client, lambda p: p.get("jobs") == "healthy" and p.get("alerts") == "healthy"
              and p.get("sessions") == "healthy")
        port = _gateway_port(client)
        world = _world(client)
        queue_url = world["jobs"]["facts"]["QUEUE_URL"]  # already gateway-shaped (BackingAws.facts)
        topic_arn = world["alerts"]["facts"]["TOPIC_ARN"]

        worker_key, worker_secret = app.state.gateway_keys.issue("default", "worker")
        sqs = _client("sqs", port, worker_key, worker_secret)
        sns = _client("sns", port, worker_key, worker_secret)
        ddb = _client("dynamodb", port, worker_key, worker_secret)

        # worker's edge grants only SendMessage+ReceiveMessage (no DeleteMessage
        # -- matches the brief's exact grant list), so messages are left in
        # place rather than deleted; receives are matched by body, not order.
        sqs.send_message(QueueUrl=queue_url, MessageBody="sqs-direct")
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=5).get("Messages", [])
        assert msgs and msgs[0]["Body"] == "sqs-direct"

        sns.publish(TopicArn=topic_arn, Message="sns-fanout")
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=2, WaitTimeSeconds=5).get("Messages", [])
        assert any(m["Body"] == "sns-fanout" for m in msgs)  # raw delivery: body verbatim

        ddb.put_item(TableName="sessions", Item={"id": {"S": "u1"}, "val": {"S": "hello"}})
        item = ddb.get_item(TableName="sessions", Key={"id": {"S": "u1"}})["Item"]
        assert item["val"]["S"] == "hello"

        stranger_key, stranger_secret = app.state.gateway_keys.issue("default", "stranger")
        sqs_stranger = _client("sqs", port, stranger_key, stranger_secret)
        sns_stranger = _client("sns", port, stranger_key, stranger_secret)
        ddb_stranger = _client("dynamodb", port, stranger_key, stranger_secret)

        with pytest.raises(ClientError) as sqs_exc:
            sqs_stranger.send_message(QueueUrl=queue_url, MessageBody="nope")
        assert sqs_exc.value.response["Error"]["Code"] == "AccessDeniedException"

        with pytest.raises(ClientError) as sns_exc:
            sns_stranger.publish(TopicArn=topic_arn, Message="nope")
        assert sns_exc.value.response["Error"]["Code"] == "AccessDenied"

        with pytest.raises(ClientError) as ddb_exc:
            ddb_stranger.get_item(TableName="sessions", Key={"id": {"S": "u1"}})
        assert ddb_exc.value.response["Error"]["Code"] == "AccessDeniedException"

        _destroy(client)
    assert runtime.list_odin() == []


def test_latency_overhead(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_S3_WITH_WORKER_EDGE)
        _wait(client, lambda p: p.get("uploads") == "healthy")
        port = _gateway_port(client)

        worker_key, worker_secret = app.state.gateway_keys.issue("default", "worker")
        gateway_s3 = _s3_client(port, worker_key, worker_secret)
        direct_s3 = BackingAws(runtime, "default").client("s3")  # host bypass, straight to RustFS (R5)

        def median_put_latency(s3_client) -> float:
            samples = []
            for i in range(20):
                start = time.perf_counter()
                s3_client.put_object(Bucket="uploads", Key=f"lat-{i}.bin", Body=b"x" * 1024)
                samples.append(time.perf_counter() - start)
            return statistics.median(samples)

        direct_s3.put_object(Bucket="uploads", Key="warm.bin", Body=b"x")  # warm up both paths first
        gateway_s3.put_object(Bucket="uploads", Key="warm.bin", Body=b"x")

        direct_median = median_put_latency(direct_s3)
        gateway_median = median_put_latency(gateway_s3)
        added_ms = (gateway_median - direct_median) * 1000
        print(
            f"\nlatency: direct={direct_median * 1000:.2f}ms gateway={gateway_median * 1000:.2f}ms "
            f"added={added_ms:.2f}ms (budget 10ms)"
        )

        assert added_ms < 10.0, (
            f"gateway added {added_ms:.2f}ms "
            f"(direct={direct_median * 1000:.2f}ms, gateway={gateway_median * 1000:.2f}ms)"
        )

        _destroy(client)
    assert runtime.list_odin() == []
