"""`sqs -> lambda` against a REAL goaws and a REAL RIE container.

This is the test the unit suite cannot substitute for, and the reason is
specific rather than general. `tests/reconcile/test_dispatch_sqs.py` stubs the
HTTP layer, so every assertion there is about the request odin SENDS. Whether
goaws recognises that request -- whether it identifies a queue from a
`QueueUrl` naming a port odin is not dialling, whether it serves the JSON 1.0
protocol at all when dialled directly rather than through the gateway, whether
it accepts an unsigned call -- is a claim about the other end that only the
other end can settle. .claude/CLAUDE.md honesty rule 1: probe the real
component and print what it returns before coding against it.

Three of those were genuinely open when this was written, and each is now
either confirmed or fixed here rather than assumed.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient

from odin.compute.functions import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "evdisp-sqs-e2e"
FUNCTION = "queue-worker"
QUEUE = "jobs"
MARKER = "ODIN-SQS-FIRED"
_CODE = (
    "import json\n"
    "def lambda_handler(event, context):\n"
    f"    print('{MARKER} ' + json.dumps(event))\n"
    "    return {'n': len(event.get('Records', []))}\n"
)

CANVAS = {
    "nodes": [
        {"id": "sqs-node", "type": "sqs", "data": {"label": QUEUE}},
    ],
    "edges": [],
}

_ACTIVE_TIMEOUT = 240.0
_DELIVER_TIMEOUT = 60.0    # a 1s poll and a 1-tick cadence; this is generous slack


def _zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", code)
    return buf.getvalue()


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture
def cleanup():
    """Scoped to THIS test's own env prefix, never a machine-wide sweep."""
    yield
    ps = _docker("ps", "-aq", "--filter", f"name=odin-aws-goaws-{ENV}",
                 "--filter", f"name={container_name(ENV, FUNCTION)}")
    for cid in ps.stdout.split():
        _docker("rm", "-f", "-v", cid)
    _docker("rm", "-f", "-v", container_name(ENV, FUNCTION))
    _docker("rm", "-f", "-v", f"odin-aws-goaws-{ENV}")


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-dispatch-sqs-test"
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


def _marked(logs, function: str) -> list[dict]:
    group = f"/aws/lambda/{function}"
    if not any(g["logGroupName"] == group
               for g in logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups", [])):
        return []
    return [e for e in logs.filter_log_events(logGroupName=group).get("events", [])
            if MARKER in e["message"]]


def test_a_message_on_a_real_queue_invokes_a_real_lambda_and_is_then_gone(store_root, cleanup):
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as http:
        # A real goaws for this env, booted by the ordinary apply path.
        http.post("/apply", json=CANVAS, params={"env": ENV})
        _await(
            lambda: {r["id"]: r["phase"] for r in http.get("/world", params={"env": ENV}).json()["resources"]}
            .get(QUEUE) == "healthy",
            120.0, "the sqs backing never became healthy",
        )
        gateway_port = http.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)

        def _client(service: str):
            return boto3.client(
                service, endpoint_url=f"http://127.0.0.1:{gateway_port}",
                aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                region_name="us-east-1",
            )

        awslambda, sqs, logs = _client("lambda"), _client("sqs"), _client("logs")
        world = {r["id"]: r for r in http.get("/world", params={"env": ENV}).json()["resources"]}
        queue_url = world[QUEUE]["facts"]["QUEUE_URL"]

        awslambda.create_function(
            FunctionName=FUNCTION, Runtime="python3.12",
            Role=f"arn:aws:iam::000000000000:role/{FUNCTION}-exec",
            Handler="lambda_function.lambda_handler", Code={"ZipFile": _zip(_CODE)},
        )
        _await(
            lambda: awslambda.get_function(FunctionName=FUNCTION)["Configuration"]["State"] == "Active",
            _ACTIVE_TIMEOUT, f"{FUNCTION} never reached Active",
        )

        # The route that did not exist before this change: without it, this call
        # is `unmappable-action` and `tofu apply` fails on the resource.
        mapping = awslambda.create_event_source_mapping(
            EventSourceArn=f"arn:aws:sqs:us-east-1:000000000000:{QUEUE}",
            FunctionName=FUNCTION, BatchSize=10, Enabled=True,
        )
        assert mapping["State"] == "Enabled", mapping

        sqs.send_message(QueueUrl=queue_url, MessageBody="work-item-1")

        found = _await(lambda: _marked(logs, FUNCTION), _DELIVER_TIMEOUT,
                       "the event source mapping never delivered the message")
        event = json.loads(found[0]["message"].split(MARKER, 1)[1].strip())
        record = event["Records"][0]
        assert record["eventSource"] == "aws:sqs"
        assert record["body"] == "work-item-1"
        assert record["eventSourceARN"] == f"arn:aws:sqs:us-east-1:000000000000:{QUEUE}"

        # THE half a stub cannot prove: the DELETE really reached goaws, so the
        # message is gone rather than redelivered forever.
        #
        # SHORT-polled, and that is not laziness. `WaitTimeSeconds >= 5` makes
        # goaws hold the connection until the timeout when the queue is empty,
        # and the gateway's forward client is a plain `httpx.AsyncClient()`
        # whose default read timeout is 5s -- so a long poll against an EMPTY
        # queue reliably 500s with `unhandled ReadTimeout serving POST /`.
        # Measured while writing this test; it is a real pre-existing gateway
        # limit (docs/limits.md), and an empty queue is exactly the state this
        # assertion needs, so long-polling here would test that bug instead.
        # Several short polls over a few seconds cover a slow backing.
        for _ in range(5):
            remaining = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
            assert not remaining.get("Messages"), (
                f"the delivered message was not deleted from the real queue: {remaining['Messages']}")
            time.sleep(1.0)

        # ...and one message produced exactly one invocation.
        assert len(_marked(logs, FUNCTION)) == 1

        awslambda.delete_event_source_mapping(UUID=mapping["UUID"])
        awslambda.delete_function(FunctionName=FUNCTION)
        http.post("/destroy", params={"env": ENV})


def test_a_message_survives_when_the_function_cannot_run(store_root, cleanup):
    """The redrive half, against a real queue: a mapping whose function is not
    invocable must LEAVE the message, so goaws's own visibility timeout returns
    it. Deleting here would lose it silently -- the worst outcome a queue has,
    and invisible at every surface."""
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as http:
        http.post("/apply", json=CANVAS, params={"env": ENV})
        _await(
            lambda: {r["id"]: r["phase"] for r in http.get("/world", params={"env": ENV}).json()["resources"]}
            .get(QUEUE) == "healthy",
            120.0, "the sqs backing never became healthy",
        )
        gateway_port = http.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)

        def _client(service: str):
            return boto3.client(
                service, endpoint_url=f"http://127.0.0.1:{gateway_port}",
                aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                region_name="us-east-1",
            )

        awslambda, sqs = _client("lambda"), _client("sqs")
        queue_url = {r["id"]: r for r in http.get("/world", params={"env": ENV}).json()["resources"]}[
            QUEUE]["facts"]["QUEUE_URL"]

        awslambda.create_function(
            FunctionName=FUNCTION, Runtime="python3.12",
            Role=f"arn:aws:iam::000000000000:role/{FUNCTION}-exec",
            Handler="lambda_function.lambda_handler", Code={"ZipFile": _zip(_CODE)},
        )
        _await(
            lambda: awslambda.get_function(FunctionName=FUNCTION)["Configuration"]["State"] == "Active",
            _ACTIVE_TIMEOUT, f"{FUNCTION} never reached Active",
        )
        mapping = awslambda.create_event_source_mapping(
            EventSourceArn=f"arn:aws:sqs:us-east-1:000000000000:{QUEUE}",
            FunctionName=FUNCTION, Enabled=True,
        )

        # Take the execution environment away, exactly as an out-of-band
        # `docker rm` would. The record still says Active until something
        # notices, which is precisely the window a naive drain would delete a
        # message in.
        _docker("rm", "-f", "-v", container_name(ENV, FUNCTION))
        sqs.send_message(QueueUrl=queue_url, MessageBody="must-survive")

        # Give the dispatcher several real passes at it (1s poll, 1-tick
        # cadence): every one of them must fail to deliver and none may delete.
        time.sleep(12)

        # The visibility timeout is >= the function timeout, so wait it out
        # before concluding anything -- otherwise "invisible" is
        # indistinguishable from "deleted" and this test would pass for the very
        # bug it exists to catch.
        #
        # SHORT-polled for the same measured reason as the test above: a
        # `WaitTimeSeconds >= 5` receive against a queue whose message is
        # currently invisible makes goaws hold past the gateway forward client's
        # 5s default read timeout, and the gateway answers 503 (a `ReadTimeout`
        # is an `httpx.HTTPError`, which `_unhandled_failure` maps to
        # ServiceUnavailable). See docs/limits.md.
        got = _await(
            lambda: sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0,
            ).get("Messages"),
            120.0, "the undelivered message was lost rather than redelivered",
        )
        assert any(m["Body"] == "must-survive" for m in got), got

        awslambda.delete_event_source_mapping(UUID=mapping["UUID"])
        awslambda.delete_function(FunctionName=FUNCTION)
        http.post("/destroy", params={"env": ENV})
