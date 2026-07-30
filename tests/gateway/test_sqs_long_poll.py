"""SQS long polling through the gateway: the forward's read timeout accommodates
the caller's own `WaitTimeSeconds`, and a wait above AWS's maximum is refused
rather than clamped (`gateway/app.py::_long_poll` / `_forward_timeout`).

WHY THESE USE A REAL SOCKET. Every other proxy test forwards over
`httpx.ASGITransport`, and that transport enforces NO timeouts at all -- it
awaits the mounted app and returns, so a backing that takes ten seconds looks
identical to one that takes ten microseconds (read from httpx 0.28.1's
`ASGITransport.handle_async_request`: there is no `timeout` anywhere in it). A
test built that way would have passed against the BROKEN code. So the backing
here is a real `asyncio` listener on loopback, dialled by a real
`httpx.AsyncClient`, and the timeout that fires is httpx's own. The incoming half
stays on ASGITransport, because nothing about the request side is being timed.

WHAT WAS MEASURED, AND WHERE. The numbers in `app.py`'s own comment came from a
real boto3 consumer against a real gateway listener and a real slow socket, before
and after. Before: `WaitTimeSeconds=5` -> `ServiceUnavailableException` 503 in
10.17s (10s, not 5s, because botocore retries a 503); 10 and 20 the same. After:
5 -> empty answer in 5.01s, 10 -> 10.00s, 20 -> 20.01s, and 25 -> `ClientError
Code='InvalidParameterValue'` HTTP 400 in 0.00s -- so `InvalidParameterValue` is
what botocore really surfaces from the document `errors.synth_error` writes,
measured rather than assumed. The one thing NO unit test here can prove is that
REAL goaws holds an empty-queue receive open for the whole wait; that is
`tests/gateway/test_gateway_e2e.py::test_sqs_long_poll_on_an_empty_queue_is_not_a_503`,
which needs a container. goaws's side of it was read from its source at the
pinned `v0.5.4` (`loops := waitTimeSeconds * 10`, one 100ms timer per loop) rather
than guessed.

The base read timeout is deliberately SHRUNK (`_BASE_READ`) in the socket tests
rather than left at production's 5s: it makes the test harder, not easier -- a
smaller base times out sooner -- and it keeps a 20-second poll out of the unit
suite. The production numbers are pinned separately and exactly, by
`test_the_production_forward_client_extends_only_its_read_timeout`, which reads
the real `httpx.AsyncClient()`.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import boto3
import httpx
import pytest

from odin.gateway.app import (
    SQS_MAX_WAIT_TIME_SECONDS,
    GatewayState,
    _forward_timeout,
    _long_poll,
    create_gateway_app,
)
from odin.gateway.keys import KeyStore
from odin.gateway.policy import Statement
from odin.gateway.stores import SynthStores
from odin.reconcile import dispatch

from .harness import CaptureSink

# Small enough to keep the suite fast, large enough that the round trip itself
# never trips it. The long-poll waits below are whole seconds, so the fix has
# room to be visible and the bug has room to bite.
_BASE_READ = 0.5
# What the slow backing waits before answering -- comfortably past `_BASE_READ`,
# so an unextended read timeout fails and an extended one does not.
_BACKING_DELAY = 1.5

_EMPTY_RECEIVE = b'{"Messages": []}'
_OK_HEAD = (
    b"HTTP/1.1 200 OK\r\ncontent-type: application/x-amz-json-1.0\r\n"
    b"content-length: " + str(len(_EMPTY_RECEIVE)).encode() + b"\r\n"
    b"connection: close\r\n\r\n"
)

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _read_request(reader: asyncio.StreamReader) -> list[bytes]:
    """The request's header lines, with its body consumed -- leaving unread body
    bytes in the socket makes the close look like a broken response."""
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    length = int(next((line.split(b":", 1)[1] for line in lines
                       if line.lower().startswith(b"content-length:")), b"0"))
    await reader.readexactly(length)
    return lines


def _target(lines: list[bytes]) -> str:
    return next((line.split(b":", 1)[1].strip().decode() for line in lines
                 if line.lower().startswith(b"x-amz-target:")), "")


@asynccontextmanager
async def backing(handler: Handler) -> AsyncIterator[int]:
    """A real listener on loopback for the duration of the block, yielding its
    port. `asyncio.start_server`, so no thread is involved and the gateway's own
    forward is timed by httpx exactly as in production."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1]


def slow_receive(seen: list[str], delay: float = _BACKING_DELAY) -> Handler:
    """goaws's own shape: only a `ReceiveMessage` polls; everything else answers
    at once. That is what lets one test drive a long poll and an ordinary call
    through the SAME routing table."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target = _target(await _read_request(reader))
        seen.append(target)
        if target.endswith("ReceiveMessage"):
            await asyncio.sleep(delay)
        writer.write(_OK_HEAD + _EMPTY_RECEIVE)
        await writer.drain()
        writer.close()

    return handler


def never_answers(seen: list[str], released: asyncio.Event) -> Handler:
    """A backing that accepts the request and then says nothing until the test
    lets go -- the shape a read timeout is supposed to catch.

    It waits on an EVENT rather than sleeping a fixed time, and that is not
    tidiness: a handler that closes the socket after N seconds gives httpx a
    `RemoteProtocolError`, which is also an `httpx.HTTPError` and also becomes a
    503 -- so the test would pass without the read timeout ever firing, which is
    exactly how a mutation of the derivation survived the first round of this
    file. Holding the connection open means the only thing that can end the wait
    is the timeout under test."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(_target(await _read_request(reader)))
        await released.wait()
        writer.close()

    return handler


@pytest.fixture
def sink() -> Iterator[CaptureSink]:
    capture = CaptureSink()
    yield capture
    capture.close()


@pytest.fixture
def keystore(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path)


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


async def _no_deny(principal, action, resource, reason) -> None:
    return None


def _sqs_client(sink: CaptureSink, keystore: KeyStore, env: str = "sqslp", node_id: str = "worker"):
    access_key, secret_key = keystore.issue(env, node_id)
    return boto3.client(
        "sqs", endpoint_url=sink.endpoint, region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )


def _granted(backing_port: int, *actions: str) -> GatewayState:
    state = GatewayState()
    state.update("sqslp", {"worker": [Statement(actions=actions, resources=("jobs",))]},
                 {"sqs": backing_port})
    return state


def _app(state: GatewayState, keystore: KeyStore, stores: SynthStores):
    """The gateway with a REAL forward client (not an ASGI stand-in), so the
    timeout under test is httpx's own."""
    return create_gateway_app(
        state, keystore, stores, _no_deny,
        forward_client=httpx.AsyncClient(timeout=httpx.Timeout(_BASE_READ)),
    )


async def _drive(app, req, tolerate_failure: bool = False) -> httpx.Response:
    """Replay a real boto3-signed request through the app.

    `tolerate_failure` is needed for the timeout paths and is not a fudge:
    Starlette's `ServerErrorMiddleware` sends the handler's response and then
    RE-RAISES so the server can log it (`middleware/errors.py`: `raise exc`), which
    a real uvicorn turns into "answer the client, log the traceback" -- probed, and
    that is where the measured 503 above came from. `raise_app_exceptions=False` is
    what makes ASGITransport behave like that server instead of propagating."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=not tolerate_failure)
    async with httpx.AsyncClient(transport=transport) as client:
        return await client.request(req.method, req.url, headers=req.headers, content=req.body)


# --- the derivation itself ---------------------------------------------------


def test_the_production_forward_client_extends_only_its_read_timeout():
    """The exact numbers a deployed odin gets, read off the real client
    `create_gateway_app` builds when no forward client is injected.

    This is the test that rejects a "fix" that simply raises every timeout: read
    stays at the client's OWN 5s when there is no poll, and connect/write/pool
    stay at 5s always."""
    client = httpx.AsyncClient()
    assert client.timeout.read == 5.0, "the httpx default this bug was made of"

    assert _forward_timeout(client, 0).read == 5.0
    assert _forward_timeout(client, 5).read == 10.0
    assert _forward_timeout(client, SQS_MAX_WAIT_TIME_SECONDS).read == 25.0

    stretched = _forward_timeout(client, SQS_MAX_WAIT_TIME_SECONDS)
    assert (stretched.connect, stretched.write, stretched.pool) == (5.0, 5.0, 5.0), \
        "only READ may move -- reaching a loopback container is not slower for a long poll"


def test_the_derivation_reads_the_request_then_the_queue_and_nothing_else(stores):
    """`_long_poll` as a table, asserted STRUCTURALLY rather than through a
    timing-shaped test.

    This exists because of a measured hole: the first version of this file proved
    "a send keeps the base timeout" only by getting a 503, and removing the
    receive-only gate made that same test pass twenty seconds slower instead of
    failing. Timing tests cannot tell "correct" from "slow", and this repo does
    not put wall-clock bounds in asserts. So the contract is pinned by the numbers
    the derivation returns."""
    url = "http://127.0.0.1:1/000000000000/jobs"
    stores.sqs_queues.set("sqslp", "jobs",
                          {"attributes": {"ReceiveMessageWaitTimeSeconds": "20"}, "deleted_at": None})
    stores.sqs_queues.set("sqslp", "wild",
                          {"attributes": {"ReceiveMessageWaitTimeSeconds": "3600"}, "deleted_at": None})
    receive = json.dumps({"QueueUrl": url, "WaitTimeSeconds": 7}).encode()
    bare = json.dumps({"QueueUrl": url}).encode()
    send = json.dumps({"QueueUrl": url, "MessageBody": "hi"}).encode()

    # The request's own wait wins outright.
    assert _long_poll("sqs:ReceiveMessage", "jobs", "sqslp", receive, stores) == (7, 7)
    # No wait in the request -> the queue's own attribute, which is what goaws
    # itself would fall back to.
    assert _long_poll("sqs:ReceiveMessage", "jobs", "sqslp", bare, stores) == (0, 20)
    # A queue odin has never seen created has no attribute: a short poll.
    assert _long_poll("sqs:ReceiveMessage", "unknown", "sqslp", bare, stores) == (0, 0)
    # A queue configured past AWS's maximum is BOUNDED, not obeyed -- the caller
    # never sent that number, so it cannot be refused for it either.
    assert _long_poll("sqs:ReceiveMessage", "wild", "sqslp", bare, stores) == (0, 20)
    # An out-of-range REQUEST value is reported raw, so the refusal can name it,
    # and is still bounded for the timeout it would otherwise buy.
    out_of_range = json.dumps({"QueueUrl": url, "WaitTimeSeconds": 3600}).encode()
    assert _long_poll("sqs:ReceiveMessage", "jobs", "sqslp", out_of_range, stores) == (3600, 20)
    # Nothing but a receive consults any of this -- not even for a queue that IS
    # configured to long-poll.
    assert _long_poll("sqs:SendMessage", "jobs", "sqslp", send, stores) == (0, 0)
    assert _long_poll("sqs:DeleteMessage", "jobs", "sqslp", send, stores) == (0, 0)
    assert _long_poll("s3:GetObject", "uploads", "sqslp", b"", stores) == (0, 0)
    # A value that is not a whole number of seconds is nobody's long poll; the
    # backing owns the rest of the parameter validation.
    for junk in ("soon", 2.5, True, None, [20]):
        body = json.dumps({"QueueUrl": url, "WaitTimeSeconds": junk}).encode()
        assert _long_poll("sqs:ReceiveMessage", "jobs", "sqslp", body, stores) == (0, 20), junk


def test_a_client_with_no_read_timeout_is_left_unbounded():
    """`read=None` already accommodates any poll; turning it into a number would
    take a deliberately unbounded client and bound it."""
    client = httpx.AsyncClient(timeout=None)
    assert client.timeout.read is None
    assert _forward_timeout(client, SQS_MAX_WAIT_TIME_SECONDS).read is None


# --- against a real socket ---------------------------------------------------


async def test_a_long_poll_outlasts_the_base_read_timeout_and_answers_empty(sink, keystore, stores):
    """THE REPRODUCTION. A receive whose wait exceeds the forward client's own
    read timeout used to raise `httpx.ReadTimeout` inside `catch_all`, which
    `_unhandled_failure` answers as 503 `ServiceUnavailable` -- "the backing isn't
    there", for a queue that was healthy. Now the wait is what the timeout is
    derived from, so the empty answer arrives instead."""
    sqs = _sqs_client(sink, keystore)
    req = sink.call(lambda: sqs.receive_message(
        QueueUrl=f"{sink.endpoint}/000000000000/jobs", WaitTimeSeconds=2, MaxNumberOfMessages=1,
    ))
    seen: list[str] = []

    async with backing(slow_receive(seen)) as port:
        resp = await _drive(_app(_granted(port, "sqs:ReceiveMessage"), keystore, stores), req)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"Messages": []}
    assert seen == ["AmazonSQS.ReceiveMessage"]


async def test_a_forward_that_is_not_a_long_poll_keeps_the_base_read_timeout(sink, keystore, stores):
    """The other half of "derived, not raised": a `SendMessage` to the very same
    queue still gives up at the client's own read timeout.

    The queue is deliberately configured to long-poll (the stored
    `ReceiveMessageWaitTimeSeconds` below), so this also pins that the queue
    attribute is consulted for a RECEIVE and nothing else -- dropping that gate
    would hand every send a 25-second read timeout, and a dead backing would then
    take 25s to report on a call that never waits at all."""
    stores.sqs_queues.set("sqslp", "jobs",
                          {"attributes": {"ReceiveMessageWaitTimeSeconds": "20"}, "deleted_at": None})
    sqs = _sqs_client(sink, keystore)
    req = sink.call(lambda: sqs.send_message(
        QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi",
    ))
    seen: list[str] = []
    released = asyncio.Event()

    async with backing(never_answers(seen, released)) as port:
        resp = await _drive(_app(_granted(port, "sqs:SendMessage"), keystore, stores), req,
                            tolerate_failure=True)
        released.set()

    assert resp.status_code == 503, "a send is not a long poll -- it must still fail fast"
    assert resp.json()["__type"] == "com.amazonaws.sqs#ServiceUnavailableException"
    assert seen == ["AmazonSQS.SendMessage"], "the request did reach the backing; the answer did not come back"


async def test_the_queue_attribute_goaws_falls_back_to_is_accommodated_too(sink, keystore, stores):
    """The sibling door. goaws uses the QUEUE's own `ReceiveMessageWaitTimeSeconds`
    when the request's `WaitTimeSeconds` is 0, so a request naming no wait at all
    can still be a 20-second poll -- and a timeout derived from the request alone
    would have left the same 503 alive there.

    The attribute is not written by hand: the queue is CREATED through the app,
    exactly as terraform's `receive_wait_time_seconds` would, and the store
    assertion is what proves the signal odin reads actually arrives."""
    sqs = _sqs_client(sink, keystore)
    create = sink.call(lambda: sqs.create_queue(
        QueueName="jobs", Attributes={"ReceiveMessageWaitTimeSeconds": "5"},
    ))
    receive = sink.call(lambda: sqs.receive_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    assert b"WaitTimeSeconds" not in receive.body, "this receive names no wait of its own"
    seen: list[str] = []

    async def create_then_poll(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target = _target(await _read_request(reader))
        seen.append(target)
        # CreateQueue must answer a QueueUrl, since synth's postprocess rewrites it.
        body = b'{"QueueUrl": "http://us-east-1.goaws.com:4100/000000000000/jobs"}'
        if target.endswith("ReceiveMessage"):
            await asyncio.sleep(_BACKING_DELAY)
            body = _EMPTY_RECEIVE
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: application/x-amz-json-1.0\r\n"
            b"content-length: " + str(len(body)).encode() + b"\r\n"
            b"connection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

    async with backing(create_then_poll) as port:
        app = _app(_granted(port, "sqs:CreateQueue", "sqs:ReceiveMessage"), keystore, stores)

        created = await _drive(app, create)
        assert created.status_code == 200, created.text
        stored = stores.sqs_queues.get("sqslp", "jobs")["attributes"]
        assert stored["ReceiveMessageWaitTimeSeconds"] == "5", \
            "the attribute odin derives the timeout from really is stored by the CreateQueue path"

        resp = await _drive(app, receive)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"Messages": []}
    assert seen == ["AmazonSQS.CreateQueue", "AmazonSQS.ReceiveMessage"]


async def test_a_long_poll_does_not_block_the_event_loop(sink, keystore, stores):
    """A long poll holds a CONNECTION, never the loop. The send must finish while
    the receive is still waiting.

    An ORDER assertion, not a wall-clock bound: an `elapsed < x` assert measures
    the machine's load as much as the code (this repo has been burned by one), and
    order is the property that actually matters -- if the loop were blocked, the
    send could not complete until the receive did."""
    sqs = _sqs_client(sink, keystore)
    long_poll = sink.call(lambda: sqs.receive_message(
        QueueUrl=f"{sink.endpoint}/000000000000/jobs", WaitTimeSeconds=2, MaxNumberOfMessages=1,
    ))
    send = sink.call(lambda: sqs.send_message(
        QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi",
    ))
    seen: list[str] = []
    finished: list[str] = []

    async with backing(slow_receive(seen)) as port:
        app = _app(_granted(port, "sqs:ReceiveMessage", "sqs:SendMessage"), keystore, stores)

        async def drive(label: str, req) -> httpx.Response:
            response = await _drive(app, req)
            finished.append(label)
            return response

        receive_resp, send_resp = await asyncio.gather(drive("receive", long_poll), drive("send", send))

    assert finished == ["send", "receive"], "the send was served while the long poll was still waiting"
    assert (receive_resp.status_code, send_resp.status_code) == (200, 200)
    assert sorted(seen) == ["AmazonSQS.ReceiveMessage", "AmazonSQS.SendMessage"]


# --- the refusal -------------------------------------------------------------


@pytest.mark.parametrize("wait", [21, 25, 3600, -1])
async def test_a_wait_outside_aws_range_is_refused_not_clamped(sink, keystore, stores, wait):
    """Real SQS rejects a `WaitTimeSeconds` outside 0..20; goaws v0.5.4 does not
    validate it at all (`loops := waitTimeSeconds * 10` -- 3600 really would poll
    for an hour), so odin is the only place that can. Clamping was the
    alternative and it is worse: an answer that arrives sooner than the caller
    asked for is indistinguishable from an empty queue.

    Measured through a real boto3 client over a real socket: `Code` comes out as
    `InvalidParameterValue`, HTTP 400, in 0.00s."""
    sqs = _sqs_client(sink, keystore)
    req = sink.call(lambda: sqs.receive_message(
        QueueUrl=f"{sink.endpoint}/000000000000/jobs", WaitTimeSeconds=wait, MaxNumberOfMessages=1,
    ))
    seen: list[str] = []

    async with backing(slow_receive(seen)) as port:
        resp = await _drive(_app(_granted(port, "sqs:ReceiveMessage"), keystore, stores), req)

    assert resp.status_code == 400
    body = resp.json()
    assert body["__type"] == "com.amazonaws.sqs#InvalidParameterValue"
    assert body["message"] == (
        f"Value {wait} for parameter WaitTimeSeconds is invalid. "
        "Reason: Must be >= 0 and <= 20, if provided."
    )
    assert seen == [], "an invalid parameter never reaches the backing"


async def test_the_maximum_wait_itself_is_allowed(sink, keystore, stores):
    """20 is legal in real AWS, so the boundary must not be off by one. The
    backing answers at once here -- what is under test is the refusal NOT
    firing."""
    sqs = _sqs_client(sink, keystore)
    req = sink.call(lambda: sqs.receive_message(
        QueueUrl=f"{sink.endpoint}/000000000000/jobs",
        WaitTimeSeconds=SQS_MAX_WAIT_TIME_SECONDS, MaxNumberOfMessages=1,
    ))
    seen: list[str] = []

    async with backing(slow_receive(seen, delay=0.0)) as port:
        resp = await _drive(_app(_granted(port, "sqs:ReceiveMessage"), keystore, stores), req)

    assert resp.status_code == 200, resp.text
    assert seen == ["AmazonSQS.ReceiveMessage"]


async def test_an_unauthorized_long_poll_is_denied_before_its_parameter_is_judged(sink, keystore, stores):
    """Order matters: a caller with no grant learns it has no grant, not that its
    parameter was out of range. The same reason synth never answers ahead of the
    policy gate."""
    sqs = _sqs_client(sink, keystore)
    req = sink.call(lambda: sqs.receive_message(
        QueueUrl=f"{sink.endpoint}/000000000000/jobs", WaitTimeSeconds=3600, MaxNumberOfMessages=1,
    ))
    seen: list[str] = []

    async with backing(slow_receive(seen)) as port:
        state = GatewayState()
        state.update("sqslp", {}, {"sqs": port})  # no statements -> default deny
        resp = await _drive(_app(state, keystore, stores), req)

    assert resp.status_code == 403
    assert resp.json()["__type"] == "com.amazonaws.sqs#AccessDeniedException"
    assert seen == []


async def test_a_receive_with_no_wait_field_at_all_short_polls(sink, keystore, stores):
    """`WaitTimeSeconds` is optional on the wire and boto3 omits it entirely when
    the caller does. Absent reads as 0 -- a short poll, no extension, no raise."""
    sqs = _sqs_client(sink, keystore)
    req = sink.call(lambda: sqs.receive_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    assert json.loads(req.body) == {"QueueUrl": f"{sink.endpoint}/000000000000/jobs"}
    seen: list[str] = []
    released = asyncio.Event()

    async with backing(never_answers(seen, released)) as port:
        resp = await _drive(_app(_granted(port, "sqs:ReceiveMessage"), keystore, stores), req,
                            tolerate_failure=True)
        released.set()

    assert resp.status_code == 503, "no wait asked for, no wait granted -- the base timeout still rules"
    assert seen == ["AmazonSQS.ReceiveMessage"]


# --- the dispatcher must NOT start long-polling ------------------------------


def test_the_event_dispatcher_still_short_polls():
    """A ratchet on a decision, not a restatement of it: now that long polling
    works through the gateway, `reconcile/dispatch.py` is exactly where someone
    would "optimise" a short poll into a long one -- and a 20-second poll inside a
    reconciler tick stalls every other thing that tick does. The reason lives in a
    comment beside the constant; this is the part that can fail a build.

    (The dispatcher does not go through the gateway at all -- it dials goaws's own
    published port -- so nothing in this file changed its behaviour. What changed
    is the temptation.)"""
    assert dispatch._WAIT_TIME_SECONDS == 0
    assert dispatch._SQS_TIMEOUT == 5.0, \
        "a short poll needs no more than the local round trip; a longer wait here would be the tick's"
    assert 0 <= dispatch._WAIT_TIME_SECONDS <= SQS_MAX_WAIT_TIME_SECONDS, \
        "checked against the gateway's own range, so a local edit cannot look fine in isolation"
