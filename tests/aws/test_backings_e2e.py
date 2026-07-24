"""M5 — the sqs/sns/dynamodb backings are real, isolated per env, and recover.

Five slices against real containers (goaws, RustFS, dynalite via node:alpine):
- sqs: a queue node roundtrips a message through the host-side client.
- sns→sqs: a canvas edge (ReactFlow node ids — exercises the id→label edge
  translation) becomes a raw-delivery subscription; a LIVE EDIT re-apply that
  adds a second queue + edge to the already-healthy topic must retrofit the
  new subscription via the reconciler's observe pass (plan NoOps healthy
  resources) and deliver to BOTH queues.
- dynamodb: put_item/get_item roundtrip.
- env isolation: same node label in envs a+b → two backing containers,
  destroy of a gc's only a's.
- crash recovery: killing the backing demotes the node, the reconciler
  re-provisions it back to healthy.

Marked `integration`: needs Colima/Docker. Run with `-m integration`.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from odin.aws.backings import ACCOUNT, BackingAws
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

CANVAS_SQS = {"nodes": [{"id": "n1", "type": "sqs", "data": {"label": "jobs"}}], "edges": []}
CANVAS_DDB = {"nodes": [{"id": "n1", "type": "dynamodb", "data": {"label": "sessions"}}],
              "edges": []}
CANVAS_S3 = {"nodes": [{"id": "n1", "type": "s3", "data": {"label": "uploads"}}], "edges": []}
# ReactFlow-shaped: edge endpoints are node IDs, not labels — the subscription
# only happens if translate maps n1→alerts / n2→jobs.
CANVAS_SNS = {
    "nodes": [
        {"id": "n1", "type": "sns", "data": {"label": "alerts"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
    ],
    "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
}
# The live-edit shape: CANVAS_SNS plus a second queue and a second sns→sqs
# edge — applied while `alerts` is already healthy, so only the reconciler's
# observe pass (plan() NoOps healthy resources) can retrofit the subscription.
CANVAS_SNS_LIVE_EDIT = {
    "nodes": CANVAS_SNS["nodes"] + [{"id": "n3", "type": "sqs", "data": {"label": "jobs2"}}],
    "edges": CANVAS_SNS["edges"] + [{"id": "e2", "source": "n1", "target": "n3"}],
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


def _receive(sqs, queue_url: str, timeout: float = 15.0) -> str:
    """Poll a queue until one message lands; delete it (so a later receive
    can't get a visibility-timeout replay) and return its body."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1).get("Messages", [])
        if msgs:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msgs[0]["ReceiptHandle"])
            return msgs[0]["Body"]
        time.sleep(0.5)
    raise AssertionError(f"no message on {queue_url} within {timeout}s")


def _destroy(client, env="default"):
    client.post("/destroy", params={"env": env})
    _wait(client, lambda p: not p, env=env)


@pytest.fixture
def runtime():
    rt = ColimaRuntime()
    yield rt
    for cid in rt.list_odin():
        rt.stop(cid)



def _aws_for(client, runtime, env="default"):
    """A BackingAws matched to the app's ACTUAL resolved gateway port.

    The suite runs with ODIN_GATEWAY_PORT=0, so the app's gateway binds an
    ephemeral port and goaws publishes THAT inside-port — a standalone
    BackingAws built with the 4266 default would query a mapping that does
    not exist (the /health gateway.port is the one source of truth).
    """
    port = client.get("/health").json()["gateway"]["port"]
    return BackingAws(runtime, env, gateway_port=port)

def test_sqs_roundtrip(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_SQS)
        _wait(client, lambda p: p.get("jobs") == "healthy")
        assert f"/{ACCOUNT}/jobs" in _world(client)["jobs"]["facts"]["QUEUE_URL"]

        sqs = _aws_for(client, runtime).client("sqs")
        url = sqs.get_queue_url(QueueName="jobs")["QueueUrl"]
        sqs.send_message(QueueUrl=url, MessageBody="hello-roundtrip")
        assert _receive(sqs, url) == "hello-roundtrip"

        _destroy(client)
    assert runtime.list_odin() == []


def test_sns_to_sqs_delivery(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_SNS)
        _wait(client, lambda p: p.get("alerts") == "healthy" and p.get("jobs") == "healthy")

        # The goaws-config canary: the ARN carries the configured AccountId,
        # proving the mounted yaml (not goaws defaults) shaped the topic.
        topic_arn = _world(client)["alerts"]["facts"]["TOPIC_ARN"]
        assert f":{ACCOUNT}:" in topic_arn

        aws = _aws_for(client, runtime)
        sns, sqs = aws.client("sns"), aws.client("sqs")
        sns.publish(TopicArn=topic_arn, Message="ping")
        jobs_url = sqs.get_queue_url(QueueName="jobs")["QueueUrl"]
        assert _receive(sqs, jobs_url) == "ping"  # raw delivery: body verbatim

        # LIVE EDIT through the real apply path: add jobs2 + its edge while
        # alerts is already healthy. plan() NoOps the healthy topic, so the
        # retrofit is tick-driven (the observe pass diffs desired vs actual
        # subscriptions) — poll until the subscription really lands before
        # publishing the fanout probe. Both queues must then deliver.
        client.post("/apply", json=CANVAS_SNS_LIVE_EDIT)
        _wait(client, lambda p: p.get("jobs2") == "healthy")

        def _jobs2_subscribed() -> bool:
            subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
            return any(s["Endpoint"].endswith(":jobs2") for s in subs)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not _jobs2_subscribed():
            time.sleep(0.5)
        assert _jobs2_subscribed(), "the live-edited jobs2 subscription never appeared"

        sns.publish(TopicArn=topic_arn, Message="fanout")
        jobs2_url = sqs.get_queue_url(QueueName="jobs2")["QueueUrl"]
        assert _receive(sqs, jobs_url) == "fanout"
        assert _receive(sqs, jobs2_url) == "fanout"

        _destroy(client)
    assert runtime.list_odin() == []


def test_dynamodb_put_get(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_DDB)
        _wait(client, lambda p: p.get("sessions") == "healthy")  # npx cold start is slow
        assert _world(client)["sessions"]["facts"]["TABLE"] == "sessions"

        ddb = _aws_for(client, runtime).client("dynamodb")
        ddb.put_item(TableName="sessions", Item={"id": {"S": "u1"}, "val": {"S": "hello"}})
        item = ddb.get_item(TableName="sessions", Key={"id": {"S": "u1"}})["Item"]
        assert item["val"]["S"] == "hello"

        _destroy(client)
    assert runtime.list_odin() == []


def test_env_isolation(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", params={"env": "a"}, json=CANVAS_S3)
        client.post("/apply", params={"env": "b"}, json=CANVAS_S3)
        _wait(client, lambda p: p.get("uploads") == "healthy", env="a")
        _wait(client, lambda p: p.get("uploads") == "healthy", env="b")

        # One backing container per env, same node label in both.
        assert runtime.status("odin-aws-rustfs-a") == "running"
        assert runtime.status("odin-aws-rustfs-b") == "running"
        for env in ("a", "b"):
            buckets = _aws_for(client, runtime, env).client("s3").list_buckets()["Buckets"]
            assert "uploads" in [b["Name"] for b in buckets]

        # Destroying a gc's ONLY a's backing; b keeps serving.
        _destroy(client, env="a")
        assert runtime.status("odin-aws-rustfs-a") == "absent"
        assert runtime.status("odin-aws-rustfs-b") == "running"
        assert _phases(client, env="b").get("uploads") == "healthy"
        buckets = BackingAws(runtime, "b").client("s3").list_buckets()["Buckets"]
        assert "uploads" in [b["Name"] for b in buckets]

        _destroy(client, env="b")
    assert runtime.list_odin() == []


def test_backing_crash_recovers(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_S3)
        _wait(client, lambda p: p.get("uploads") == "healthy")

        runtime.stop("odin-aws-rustfs-default")  # kill the backing out from under it

        # The node must leave healthy within ~10s (exists() sees the dead
        # backing) — poll fast: the crashed→starting window is about one tick.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and _phases(client).get("uploads") == "healthy":
            time.sleep(0.2)
        assert _phases(client).get("uploads") != "healthy"

        # ...and come back: the reconciler re-provisions, rebooting RustFS.
        _wait(client, lambda p: p.get("uploads") == "healthy", timeout=90)
        buckets = BackingAws(runtime, "default").client("s3").list_buckets()["Buckets"]
        assert "uploads" in [b["Name"] for b in buckets]

        _destroy(client)
    assert runtime.list_odin() == []
