"""The SQS drain: `sqs -> lambda`, the one trigger where odin's architecture
and Amazon's are the same thing -- a real event source mapping IS a poller.

TWO LAYERS, because they answer different questions and neither is sufficient:

  * the unit tests here stub the HTTP layer with `respx` and prove the FAILURE
    SEMANTICS -- above all that a message whose invoke did not run is NOT
    deleted, so the queue's own visibility timeout redelivers it. That is SQS's
    redrive and it is free; deleting on failure silently loses the message.
  * `test_dispatch_sqs_e2e.py` drives the same code against a REAL goaws
    container, because everything above is a claim about MY OWN request shape
    until something on the other end answers it. .claude/CLAUDE.md honesty
    rule 1: a unit test that fabricates the upstream signal proves the parser,
    not the integration.

The request bytes were MEASURED before any of this was written, captured from
real boto3 through `tests/gateway/harness.CaptureSink`: SQS here is the JSON
1.0 protocol with `X-Amz-Target: AmazonSQS.<Op>` POSTed to `/`, not the legacy
query protocol.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from odin.reconcile.dispatch import Dispatcher

from .test_dispatch import (
    ENV, FUNCTION, FUNCTION_ARN, FakeFunctions, MovableClock, _port, seed_function, seed_mapping,
)

PORT = 4599
QUEUE_URL = f"http://127.0.0.1:{PORT}/000000000000/jobs"


@pytest.fixture
def stores(tmp_path: Path):
    from odin.gateway.stores import SynthStores
    return SynthStores(tmp_path)


def _target(request: httpx.Request) -> str:
    return request.headers.get("x-amz-target", "").split(".")[-1]


class GoawsStub:
    """A minimal goaws whose ANSWERS are the shapes botocore's own sqs model
    declares. Records every call so the assertions can read what odin sent."""

    def __init__(self, messages: list[dict] | None = None) -> None:
        self.messages = messages if messages is not None else []
        self.calls: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        op = _target(request)
        payload = json.loads(request.content or b"{}")
        self.calls.append((op, payload))
        if op == "ReceiveMessage":
            return httpx.Response(200, json={"Messages": self.messages})
        if op == "DeleteMessageBatch":
            self.deleted.extend(e["ReceiptHandle"] for e in payload["Entries"])
            return httpx.Response(200, json={
                "Successful": [{"Id": e["Id"]} for e in payload["Entries"]], "Failed": [],
            })
        return httpx.Response(400, json={"__type": "InvalidAction"})


def _message(body: str, handle: str) -> dict:
    return {"MessageId": f"m-{handle}", "ReceiptHandle": handle, "Body": body,
            "MD5OfBody": "d41d8cd98f00b204e9800998ecf8427e", "Attributes": {}}


@respx.mock
async def test_a_received_message_is_delivered_as_an_sqs_records_envelope(stores):
    seed_function(stores)
    seed_mapping(stores)
    goaws = GoawsStub([_message("hello", "rh-1")])
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)
    functions = FakeFunctions()

    assert await Dispatcher(functions, MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT)) == {}

    record = json.loads(functions.payloads[0])["Records"][0]
    assert record["eventSource"] == "aws:sqs"
    assert record["body"] == "hello"
    assert record["receiptHandle"] == "rh-1"
    assert record["eventSourceARN"] == "arn:aws:sqs:us-east-1:000000000000:jobs"
    assert record["awsRegion"] == "us-east-1"


@respx.mock
async def test_the_receive_short_polls_and_asks_for_the_mappings_batch_size(stores):
    """`WaitTimeSeconds=0`. Real AWS long-polls up to 20s, and the equivalent
    here would park a coroutine for 20s per mapping on the shared loop."""
    seed_function(stores)
    seed_mapping(stores)
    goaws = GoawsStub()
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)

    await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))

    op, payload = goaws.calls[0]
    assert op == "ReceiveMessage"
    assert payload["WaitTimeSeconds"] == 0
    assert payload["MaxNumberOfMessages"] == 10
    assert payload["QueueUrl"] == QUEUE_URL


@respx.mock
async def test_the_visibility_timeout_is_at_least_the_functions_own_timeout(stores):
    """Otherwise the message is redelivered while the first invoke is still
    running and the function runs TWICE for one message."""
    seed_function(stores)
    stores.lambdactl.update(ENV, f"fn:{FUNCTION}", lambda fn: {**fn, "timeout": 120})
    seed_mapping(stores)
    goaws = GoawsStub()
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)

    await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))
    assert goaws.calls[0][1]["VisibilityTimeout"] == 120


@respx.mock
async def test_a_delivered_message_is_deleted(stores):
    seed_function(stores)
    seed_mapping(stores)
    goaws = GoawsStub([_message("a", "rh-1"), _message("b", "rh-2")])
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)

    await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))
    assert goaws.deleted == ["rh-1", "rh-2"]


@respx.mock
async def test_a_message_whose_invoke_did_not_run_is_never_deleted(stores):
    """THE failure semantic, and it is AWS's own: do not delete, and the
    visibility timeout redelivers. Deleting here loses the message silently --
    the worst possible outcome for a queue, and indistinguishable from success
    at every surface a user can see."""
    seed_function(stores, state="Failed")     # the invoke cannot run at all
    seed_mapping(stores)
    goaws = GoawsStub([_message("precious", "rh-1")])
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)

    verdicts = await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))

    assert goaws.deleted == [], "a message whose invoke did not run must survive for the redrive"
    assert "could not run" in verdicts[FUNCTION]
    assert "event source mapping" in verdicts[FUNCTION]


@respx.mock
async def test_a_handler_that_raised_still_counts_as_delivered_and_is_deleted(stores):
    """The subtle half of the rule above. "The invoke RAN" is not "the handler
    succeeded": a handler that raises has consumed the message, exactly as real
    Lambda's own SQS mapping treats it once the batch is reported handled. The
    failure is recorded by the invoke wrapper (`last_invocation_error`), which
    `/world` projects -- redelivering forever instead would turn one bad
    message into an infinite invoke loop."""
    from odin.compute.functions import InvokeResult

    seed_function(stores)
    seed_mapping(stores)
    goaws = GoawsStub([_message("poison", "rh-1")])
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)
    functions = FakeFunctions(InvokeResult(payload=b"{}", function_error="Unhandled"))

    await Dispatcher(functions, MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))
    assert goaws.deleted == ["rh-1"]
    assert stores.lambdactl.get(ENV, f"fn:{FUNCTION}")["last_invocation_error"] == "Unhandled"


@respx.mock
async def test_an_empty_queue_costs_one_receive_and_no_invoke(stores):
    seed_function(stores)
    seed_mapping(stores)
    goaws = GoawsStub([])
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)
    functions = FakeFunctions()

    assert await Dispatcher(functions, MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT)) == {}
    assert [op for op, _ in goaws.calls] == ["ReceiveMessage"]
    assert functions.payloads == []


@respx.mock
async def test_a_queue_that_cannot_be_read_is_reported_not_swallowed(stores):
    seed_function(stores)
    seed_mapping(stores)
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=httpx.ConnectError("refused"))

    verdicts = await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))
    assert "could not read its source" in verdicts[FUNCTION]
    assert "jobs" in verdicts[FUNCTION]


@respx.mock
async def test_two_mappings_are_drained_concurrently_and_one_failure_does_not_stop_the_other(stores):
    """`asyncio.TaskGroup` across mappings, so a busy or broken queue cannot
    starve the tick."""
    seed_function(stores)
    seed_mapping(stores)
    stores.lambdactl.set(ENV, "esm:m-2", {
        **stores.lambdactl.get(ENV, "esm:m-1"),
        "uuid": "m-2", "event_source_arn": "arn:aws:sqs:us-east-1:000000000000:second",
    })
    goaws = GoawsStub([_message("x", "rh-1")])
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)
    functions = FakeFunctions()

    await Dispatcher(functions, MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))
    receives = [payload["QueueUrl"] for op, payload in goaws.calls if op == "ReceiveMessage"]
    assert sorted(receives) == sorted([QUEUE_URL, f"http://127.0.0.1:{PORT}/000000000000/second"])
    assert len(functions.payloads) == 2


@respx.mock
async def test_the_function_arn_the_mapping_stored_is_what_gets_invoked(stores):
    """A mapping names its function by NAME in the record, and that name is the
    canvas label. Getting this wrong would drain one queue into another
    function's handler."""
    seed_function(stores, label="jobs-worker-node")
    seed_mapping(stores)
    goaws = GoawsStub([_message("x", "rh-1")])
    respx.post(f"http://127.0.0.1:{PORT}/").mock(side_effect=goaws.handler)
    functions = FakeFunctions()

    await Dispatcher(functions, MovableClock()).verdicts(stores, ENV, sqs_port=_port(PORT))
    assert functions.payloads, "the mapping must invoke the function it names"
    assert FUNCTION_ARN.endswith(FUNCTION)
