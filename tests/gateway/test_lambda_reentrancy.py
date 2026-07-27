"""Field-test finding #1 (HIGH): a Lambda invoke must NOT freeze the gateway's
event loop, or the handler's own re-entrant AWS calls back through the gateway
(a boto3 PutItem/PutObject during the invocation) deadlock -- they can't be
accepted while the single loop is blocked inside the invoke, so they time out
and the invoke returns empty.

Reproduced WITHOUT Docker: a fake `FunctionRuntime` whose `invoke` makes a REAL
re-entrant signed call back to the SAME running gateway -- an STS
GetCallerIdentity, answered purely on the loop (verify() is its only gate; it
needs no backing and no policy). The gateway runs on a real uvicorn port, so the
re-entrant request contends for the loop for real.

WHAT THIS FILE CAN AND CANNOT PROVE (v0.7.7 -- read this before trusting it)
---------------------------------------------------------------------------
The substrate is FAKE, so this file does not measure `compute/functions.py::
invoke` at all. What it measures is the gateway's own invoke CHAIN -- route ->
`synth.pure_answer` -> `lambdactl._invoke` -> `await substrate.invoke(...)` --
staying on the loop and remaining re-entrant while an invocation is open. That
is a real property with a real failure mode: any link that blocks, or that runs
the substrate synchronously, starves everything else on that loop.

It is NOT a proof that the real Lambda substrate is non-blocking, and the
previous version of this file could not have been one either: its fake made a
SYNCHRONOUS `boto3` call from inside the `async def` that runs on the gateway's
own loop, so the fake itself froze the loop and the test measured the fake in
both directions -- it would have failed against correct source. The fake now
uses `httpx.AsyncClient` with botocore's own `SigV4Auth`, so the signature on
the wire is still real while the transport genuinely yields.

The real substrate WAS measured separately, with Docker, against a
`public.ecr.aws/lambda/python:3.12` RIE container whose handler calls back into
a server on the same loop that serves the invoke:

    async  (httpx.AsyncClient): callback served in 0.10s, host loop served 1
                                callback; a 2.0s handler let a 50ms ticker
                                advance 39 times
    blocking (httpx.post):      callback timed out after 25.11s, host loop
                                served 0 callbacks; the same 2.0s handler let
                                the ticker advance 1 time

Those numbers are the evidence for the substrate; this file is the evidence for
the gateway around it. Neither substitutes for the other, and this docstring is
here so nobody reads one as the other again.

`serve_in_thread` is deliberate and is not a de-threading miss: it is a
documented test-only helper (production runs the gateway on the control app's
loop via `serve_on_loop`), and it is what makes the synchronous boto3 client in
the first test legal -- the gateway's loop lives in another thread, so blocking
in the test body cannot starve it.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import boto3
import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials

from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore
from odin.gateway.stores import SynthStores

ENV = "reentrancy"
FUNCTION_NAME = "callback"

# The re-entrant call the fake handler makes back through the gateway while the
# invoke is in flight. A SHORT timeout so the buggy (loop-frozen) path fails
# fast instead of waiting out the invoke's own 30s budget.
_REENTRANT_TIMEOUT = 3.0

# How long the fake handler stays inside the invocation after its callback
# returns. Long enough that a loop frozen for the whole invocation could not
# have answered anything during it, short enough not to slow the suite.
_HANDLER_SECONDS = 0.3


def _signed(access_key: str, secret_key: str, method: str, url: str, body: bytes, service: str) -> dict[str, str]:
    """Headers from botocore's OWN SigV4Auth. The gateway verifies the real
    signature, so a hand-built header set would prove nothing -- this keeps the
    wire fidelity the old boto3 call had, without its blocking transport."""
    request = AWSRequest(method=method, url=url, data=body)
    if service == "sts":
        request.headers["Content-Type"] = "application/x-www-form-urlencoded"
    SigV4Auth(Credentials(access_key, secret_key), service, "us-east-1").add_auth(request)
    return dict(request.headers)


class _ReentrantSubstrate:
    """Stands in for `compute/functions.FunctionRuntime`: its `invoke` does what
    a real callback handler does -- an AWS call back through the gateway while
    the invocation is still open -- then reports whether that succeeded in its
    returned payload.

    `invoke` is `async def` because the real one is, and it uses an ASYNC client
    because the real one does. A synchronous client here would block the very
    loop the test is asking about."""

    gateway_port: int = 0
    access_key: str = ""
    secret_key: str = ""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def invoke(self, env: str, function_name: str, payload: bytes, timeout: float = 30.0):
        from odin.compute.functions import InvokeResult

        url = f"http://127.0.0.1:{type(self).gateway_port}/"
        body = b"Action=GetCallerIdentity&Version=2011-06-15"
        headers = _signed(type(self).access_key, type(self).secret_key, "POST", url, body, "sts")
        # Deliberately broad: ANY failure to be served -- timeout, refused, 403
        # -- is the frozen-loop symptom this test exists to catch, and it has to
        # arrive as `reentrant_ok=False` carrying the assertion's own message
        # rather than as a raw exception from inside the substrate.
        try:
            async with httpx.AsyncClient(timeout=_REENTRANT_TIMEOUT) as client:
                response = await client.post(url, headers=headers, content=body)
            reentrant_ok = response.status_code == 200 and "GetCallerIdentityResponse" in response.text
        except Exception:
            reentrant_ok = False
        # ...and stay inside the invocation a while longer, so the concurrency
        # test below has a window it can observe from the outside.
        await asyncio.sleep(_HANDLER_SECONDS)
        return InvokeResult(payload=json.dumps({"reentrant_ok": reentrant_ok}).encode(), function_error=None)

    async def logs(self, env: str, function_name: str, tail: int = 20) -> str:
        # W2.1: every Invoke also ships this tail into `/aws/lambda/{fn}`.
        return "reentrant callback ran\n"


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch):
    stores = SynthStores(tmp_path)
    keystore = KeyStore(tmp_path)
    access_key, secret_key = keystore.issue(ENV, OPERATOR_NODE_ID)

    # Seed an Active function record so `_invoke` reaches the substrate.
    stores.lambdactl.set(ENV, f"fn:{FUNCTION_NAME}", {
        "function_name": FUNCTION_NAME, "state": "Active",
        "function_arn": f"arn:aws:lambda:us-east-1:000000000000:function:{FUNCTION_NAME}",
    })

    monkeypatch.setattr("odin.gateway.models.lambdactl.FunctionRuntime", _ReentrantSubstrate)

    async def on_deny(*_args: object) -> None:
        return None

    state = GatewayState()
    port_holder: dict[str, int] = {}
    app = create_gateway_app(state, keystore, stores, on_deny, gateway_port=lambda: port_holder["port"])
    server, thread, port = serve_in_thread(app, port=0)
    port_holder["port"] = port
    _ReentrantSubstrate.gateway_port = port
    _ReentrantSubstrate.access_key = access_key
    _ReentrantSubstrate.secret_key = secret_key
    # A SECOND principal, so "the gateway still serves everyone else" is
    # literal rather than the invoking caller talking to itself.
    bystander = keystore.issue(ENV, "bystander")
    yield port, access_key, secret_key, bystander
    stop_in_thread(server, thread)


def test_invoke_does_not_freeze_the_loop_for_reentrant_calls(gateway):
    """A REAL boto3 Invoke, synchronous on purpose: the gateway's loop is in
    another thread, so blocking here cannot starve it, and the request on the
    wire is one botocore really signed and framed."""
    port, access_key, secret_key, _bystander = gateway
    lambda_client = boto3.client(
        "lambda", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 0}),
    )
    response = lambda_client.invoke(FunctionName=FUNCTION_NAME, Payload=b"{}")
    payload = json.loads(response["Payload"].read())
    assert payload["reentrant_ok"] is True, (
        "the handler's re-entrant call back through the gateway was not served "
        "while the invoke was in flight -- the event loop was frozen by the invoke"
    )


async def test_the_gateway_answers_other_callers_while_an_invoke_is_in_flight(gateway):
    """The half a re-entrant call alone cannot show, and the reason the fake
    holds the invocation open for `_HANDLER_SECONDS`: an invoke must not stop
    the gateway serving EVERYONE ELSE either. The re-entrant test above could in
    principle be satisfied by a gateway that special-cases its own callbacks;
    this one cannot.

    A DIFFERENT principal -- `bystander`, with its own issued key pair -- makes
    its own real signed STS call while the invocation is still open, and the
    answer has to come back inside the window the handler is holding. If the
    invoke owned the loop, that request would sit unanswered until the
    invocation finished and the elapsed time would exceed the window. It is a
    signed request rather than a bare `/health` probe because the gateway
    authenticates everything: an unsigned `/health` is answered `401
    InvalidClientTokenId`, which would have made this assertion measure the
    reject path instead of the serve path.

    The bound asserted is `_HANDLER_SECONDS` itself -- the window the fake
    genuinely holds -- not a round number chosen to pass. Both requests go
    through `httpx.AsyncClient` so this test needs no thread of its own: the
    concurrency being measured is the GATEWAY's, and borrowing a thread here
    would hide the very serialization the assertion is looking for."""
    port, access_key, secret_key, (other_key, other_secret) = gateway
    url = f"http://127.0.0.1:{port}/2015-03-31/functions/{FUNCTION_NAME}/invocations"
    payload = b"{}"
    headers = _signed(access_key, secret_key, "POST", url, payload, "lambda")

    sts_url = f"http://127.0.0.1:{port}/"
    sts_body = b"Action=GetCallerIdentity&Version=2011-06-15"
    sts_headers = _signed(other_key, other_secret, "POST", sts_url, sts_body, "sts")

    async with httpx.AsyncClient(timeout=10.0) as client:
        invoking = asyncio.create_task(client.post(url, headers=headers, content=payload))
        # Let the invocation actually reach the substrate before probing.
        await asyncio.sleep(_HANDLER_SECONDS / 3)
        started = time.monotonic()
        bystander = await client.post(sts_url, headers=sts_headers, content=sts_body)
        elapsed = time.monotonic() - started
        invoked = await invoking

    assert bystander.status_code == 200, (
        f"the gateway did not serve a second caller during an invoke: {bystander.text}"
    )
    assert "GetCallerIdentityResponse" in bystander.text
    assert elapsed < _HANDLER_SECONDS, (
        f"a second caller waited {elapsed:.3f}s while an invoke was in flight, longer than the "
        f"{_HANDLER_SECONDS}s the handler holds -- it was queued behind the invoke, so the invoke had the loop"
    )
    assert invoked.status_code == 200, invoked.text
    assert json.loads(invoked.content)["reentrant_ok"] is True
