"""G5 — the gateway against reality: real containers, real SigV4, real
denials (task-g5-brief.md; PRD docs/superpowers/specs/2026-07-22-gateway-prd.md
§6). The app-workload layer is parked (NORTHSTAR.md, tag app-layer-parked),
so principals are exercised directly: `app.state.gateway_keys.issue(env,
node_id)` mints creds for an identity, and the calls are made with those.

Since v0.8.12 the gateway authorizes from the APPLIED IAM rather than from
canvas edges, which changed what this file can prove on its own -- see the
long note above the fixtures for what moved, where it moved to, and the two
alternatives that were tried and rejected for making the tests lie.

Every test boots a real `create_app()` (real ColimaRuntime, real
BackingAws-provisioned RustFS/goaws/dynalite, real gateway listener on
ODIN_GATEWAY_PORT=0), applies a canvas, waits for /world healthy, drives
real boto3/aws-cli SigV4 traffic through the gateway, destroys, and asserts
every container IT created is gone (scoped to `OWN_ENVS` -- never the whole
machine; see tests/containers.py). Marked `integration`: needs Colima/Docker
with the backing + aws-cli images pulled.
"""
from __future__ import annotations

import os
import statistics
import subprocess
import time

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.containers import own_containers

pytestmark = pytest.mark.integration

# See `_apply_the_grant`: the seeded workload record has no container behind it,
# and the drift sweeper would correctly mark it Failed. Nothing in this file
# tests drift.
os.environ["ODIN_DRIFT_SWEEP_TICKS"] = "1000000"

# Every env this file applies to -- and therefore everything its teardown is
# allowed to stop. `default` is the implicit env of most slices; `a` and `b`
# are the pair the foreign-creds slice creates by name.
OWN_ENVS = ("default", "a", "b")

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


# --- what this file proves after v0.8.12 -------------------------------------
#
# It used to prove "a drawn edge grants": the worker principal was a PHANTOM
# canvas node, `/apply` committed the Stack, and the gateway compiled its policy
# map straight from the edges. The gateway authorizes from the APPLIED IAM now,
# so an edge grants nothing until a real apply creates a role and a policy — and
# a phantom node cannot be applied at all, because only lambda/ec2/ecs can hold
# an IAM role.
#
# Two ways to keep the allowed-half here were tried and rejected, both for the
# same reason — they would have made the test lie about the product:
#   - seeding the gateway's own stores: the reconciler projects TF-owned status
#     out of them, so a seeded role and function appeared in `/world` as real
#     resources (`worker-role healthy`, `worker crashed`), and `destroy` could
#     never empty the environment.
#   - making the worker a real lambda and calling `/apply-full`: lambda zips
#     materialize under the store root, and this file's store is pytest's
#     `tmp_path`, which on macOS is not under `$HOME` (the same discovery
#     tests/simulate/test_lambda_tf_e2e.py documents at the top).
#
# So the allowed half moved rather than being faked, and it is proven HARDER
# where it landed: tests/simulate/test_lambda_tf_e2e.py applies a granted lambda
# with real tofu, boots a real RIE container, and has the handler call S3 and
# DynamoDB back through this gateway with its own injected credentials. That is
# mutation-tested — emptying `compile_policies_from_iam` fails it.
#
# What stays here is everything this file can still prove end to end with real
# containers and real SigV4: the OPERATOR principal (full-allow by construction,
# and the identity tofu itself uses) exercises the allowed path, an ungranted
# principal is denied, credentials do not cross environments, a real container
# reaches the gateway from outside, and the overhead is bounded.


@pytest.fixture
async def runtime():
    rt = ColimaRuntime()
    yield rt
    for name in await own_containers(rt, *OWN_ENVS):
        # `stop` became a coroutine in the v0.7.7 de-threading pass and this call
        # was never awaited, so this teardown has cleaned up NOTHING since —
        # silently, because an un-awaited coroutine only warns. Found by running
        # the full integration suite for v0.8.12, which left two backing
        # containers standing. Failure mode #1 from CLAUDE.md, in the fixture
        # that exists to prevent exactly this.
        await rt.stop(name)


async def test_an_ungranted_principal_is_denied_while_a_legitimate_one_is_not(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_S3_WITH_WORKER_EDGE)
        _wait(client, lambda p: p.get("uploads") == "healthy")
        port = _gateway_port(client)

        worker_key, worker_secret = app.state.gateway_keys.issue("default", OPERATOR_NODE_ID)
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
    assert await own_containers(runtime, *OWN_ENVS) == [], "every container this test made is gone"


async def test_foreign_env_creds_denied(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", params={"env": "a"}, json=CANVAS_S3_WITH_WORKER_EDGE)
        client.post("/apply", params={"env": "b"}, json=CANVAS_UPLOADS_AND_SECRETS)
        _wait(client, lambda p: p.get("uploads") == "healthy", env="a")
        _wait(client, lambda p: p.get("uploads") == "healthy" and p.get("secrets") == "healthy", env="b")
        port = _gateway_port(client)

        # The two halves need two principals, and the reason is the point of the
        # test. The operator is full-allow BY CONSTRUCTION, so it proves the
        # legitimate half but cannot prove the denial: measured, it passes the
        # policy check and gets `NoSuchBucket` from env a's own backing, which
        # shows routing is scoped and says nothing about authorization. An
        # ordinary principal is the one whose denial comes from the POLICY.
        operator_key, operator_secret = app.state.gateway_keys.issue("a", OPERATOR_NODE_ID)
        operator_s3 = _s3_client(port, operator_key, operator_secret)

        # a's key against a's OWN bucket: scoping doesn't break legitimate access.
        operator_s3.put_object(Bucket="uploads", Key="a-file.txt", Body=b"a-data")
        assert operator_s3.get_object(Bucket="uploads", Key="a-file.txt")["Body"].read() == b"a-data"

        # An env-a principal against a resource that exists only in env b:
        # denied by the policy. a's compiled map has no statement for "secrets"
        # at all -- there is no path from a's principal into b's world
        # (`statements_for` is env-keyed), and since v0.8.12 that map is built
        # from a's applied IAM, which never mentions another environment.
        stranger_key, stranger_secret = app.state.gateway_keys.issue("a", "stranger")
        stranger_s3 = _s3_client(port, stranger_key, stranger_secret)
        with pytest.raises(ClientError) as exc_info:
            stranger_s3.put_object(Bucket="secrets", Key="x.txt", Body=b"x")
        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"

        _destroy(client, env="a")
        _destroy(client, env="b")
    assert await own_containers(runtime, *OWN_ENVS) == [], "every container this test made is gone"


async def test_container_crosses_to_gateway(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_S3_WITH_WORKER_EDGE)
        _wait(client, lambda p: p.get("uploads") == "healthy")
        port = _gateway_port(client)

        worker_key, worker_secret = app.state.gateway_keys.issue("default", OPERATOR_NODE_ID)
        stranger_key, stranger_secret = app.state.gateway_keys.issue("default", "stranger")

        allowed = _run_aws_cli(port, worker_key, worker_secret, ["s3", "ls", "s3://uploads"])
        assert allowed.returncode == 0, allowed.stderr

        denied = _run_aws_cli(port, stranger_key, stranger_secret, ["s3", "ls", "s3://uploads"])
        assert denied.returncode != 0
        assert "AccessDenied" in denied.stderr

        _destroy(client)
    assert await own_containers(runtime, *OWN_ENVS) == [], "every container this test made is gone"


async def test_sqs_sns_dynamodb_through_gateway(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_MULTI_SERVICE)
        _wait(client, lambda p: p.get("jobs") == "healthy" and p.get("alerts") == "healthy"
              and p.get("sessions") == "healthy")
        port = _gateway_port(client)
        world = _world(client)
        queue_url = world["jobs"]["facts"]["QUEUE_URL"]  # already gateway-shaped (BackingAws.facts)
        topic_arn = world["alerts"]["facts"]["TOPIC_ARN"]

        worker_key, worker_secret = app.state.gateway_keys.issue("default", OPERATOR_NODE_ID)
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
    assert await own_containers(runtime, *OWN_ENVS) == [], "every container this test made is gone"


async def test_sqs_long_poll_on_an_empty_queue_is_not_a_503(tmp_path, runtime):
    """The claim no unit test can make: REAL goaws, holding a receive open for the
    full wait on an EMPTY queue, and the answer coming back empty rather than 503
    `ServiceUnavailable`.

    Long polling is the RECOMMENDED way to consume a queue and it was broken for
    every wait of 5s or more: `httpx.AsyncClient()` defaults to a 5s read timeout,
    the `ReadTimeout` is an `httpx.HTTPError`, and `_unhandled_failure` maps that
    to "the backing isn't there" -- so a worker got `ServiceUnavailable` while
    `/world` reported the same queue healthy, with no way to reconcile the two.

    Why the existing sqs slice never caught it: `test_sqs_sns_dynamodb_through_gateway`
    also passes `WaitTimeSeconds=5`, but always straight after a `send_message`, so
    goaws finds a message on its first 100ms poll and answers at once. An EMPTY
    queue is the whole reproduction, which is why this one polls before sending
    anything.

    Three things, in one applied env because each apply costs a container set:
    the 20-second poll (AWS's maximum, and the case furthest past the old 5s
    cliff), a poll that RETURNS EARLY when a message shows up (so the fix is not
    just "always wait the full time"), and odin's refusal of a wait above 20."""
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_MULTI_SERVICE)
        _wait(client, lambda p: p.get("jobs") == "healthy")
        port = _gateway_port(client)
        queue_url = _world(client)["jobs"]["facts"]["QUEUE_URL"]

        operator_key, operator_secret = app.state.gateway_keys.issue("default", OPERATOR_NODE_ID)
        # No client-side retry, so what is measured is odin's FIRST answer: a 503
        # is retryable, and botocore's default would hide it behind three more
        # attempts (that is why the old failure cost 10s for a 5s timeout).
        sqs = boto3.client(
            "sqs", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
            aws_access_key_id=operator_key, aws_secret_access_key=operator_secret,
            config=Config(retries={"max_attempts": 1, "mode": "standard"}, read_timeout=60),
        )

        started = time.monotonic()
        empty = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20)
        waited = time.monotonic() - started
        print(f"\nempty-queue long poll: {waited:.2f}s, Messages={empty.get('Messages', [])!r}")
        assert empty.get("Messages", []) == [], "the queue is empty -- an empty answer is the correct one"
        # Deliberately loose: this asserts the request was really HELD (past the
        # old 5s cliff) without turning machine load into a failure.
        assert waited > 5.0, f"a 20s long poll returned in {waited:.2f}s -- it was not held at all"

        sqs.send_message(QueueUrl=queue_url, MessageBody="long-polled")
        started = time.monotonic()
        got = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20)
        print(f"non-empty long poll returned in {time.monotonic() - started:.2f}s")
        assert [m["Body"] for m in got.get("Messages", [])] == ["long-polled"], \
            "a waiting message must end the poll, not wait out the clock"

        with pytest.raises(ClientError) as too_long:
            sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=25)
        assert too_long.value.response["Error"]["Code"] == "InvalidParameterValue", \
            "above AWS's maximum odin refuses the parameter; goaws v0.5.4 would poll for as long as it was told"

        _destroy(client)
    assert await own_containers(runtime, *OWN_ENVS) == [], "every container this test made is gone"


async def test_latency_overhead(tmp_path, runtime):
    app = create_app(runtime=runtime, store=SpecStore(tmp_path))
    with TestClient(app) as client:
        client.post("/apply", json=CANVAS_S3_WITH_WORKER_EDGE)
        _wait(client, lambda p: p.get("uploads") == "healthy")
        port = _gateway_port(client)

        worker_key, worker_secret = app.state.gateway_keys.issue("default", OPERATOR_NODE_ID)
        gateway_s3 = _s3_client(port, worker_key, worker_secret)
        direct_s3 = await BackingAws(runtime, "default").client("s3")  # host bypass, straight to RustFS (R5)

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
    assert await own_containers(runtime, *OWN_ENVS) == [], "every container this test made is gone"
