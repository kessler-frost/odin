"""Field-test finding #1 (HIGH): a Lambda invoke must NOT freeze the gateway's
event loop, or the handler's own re-entrant AWS calls back through the gateway
(a boto3 PutItem/PutObject during the invocation) deadlock -- they can't be
accepted while the single loop thread is blocked inside the synchronous invoke,
so they time out and the invoke returns empty.

Reproduced WITHOUT Docker: a fake `FunctionRuntime` whose `invoke` makes a REAL
re-entrant signed call back to the SAME running gateway -- an STS
GetCallerIdentity, answered purely on the loop (verify() is its only gate; it
needs no backing and no policy). The gateway runs on a real uvicorn port
(`serve_in_thread`), so the re-entrant request contends for the loop for real.
With the invoke on the loop it can't be served (times out); offloaded to a
worker thread the loop stays free and it succeeds -- the exact difference the
fix makes.
"""
from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from botocore.config import Config

from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore
from odin.gateway.stores import SynthStores

ENV = "reentrancy"
FUNCTION_NAME = "callback"

# The re-entrant call the fake handler makes back through the gateway while the
# invoke is in flight. A SHORT timeout so the buggy (loop-frozen) path fails
# fast instead of waiting out the invoke's own 30s budget.
_REENTRANT_TIMEOUT = 3.0


class _ReentrantSubstrate:
    """Stands in for `compute/functions.FunctionRuntime`: its `invoke` does what
    a real callback handler does -- a boto3 AWS call back through the gateway --
    then reports whether that succeeded in its returned payload."""

    gateway_port: int = 0
    access_key: str = ""
    secret_key: str = ""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def invoke(self, env: str, function_name: str, payload: bytes, timeout: float = 30.0):
        from odin.compute.functions import InvokeResult

        sts = await boto3.client(
            "sts",
            endpoint_url=f"http://127.0.0.1:{type(self).gateway_port}",
            region_name="us-east-1",
            aws_access_key_id=type(self).access_key,
            aws_secret_access_key=type(self).secret_key,
            config=Config(connect_timeout=_REENTRANT_TIMEOUT, read_timeout=_REENTRANT_TIMEOUT, retries={"max_attempts": 0}),
        )
        reentrant_ok = False
        try:
            sts.get_caller_identity()
            reentrant_ok = True
        except Exception:
            reentrant_ok = False
        return InvokeResult(payload=json.dumps({"reentrant_ok": reentrant_ok}).encode(), function_error=None)

    def logs(self, env: str, function_name: str, tail: int = 20) -> str:
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
    yield port, access_key, secret_key
    stop_in_thread(server, thread)


async def test_invoke_does_not_freeze_the_loop_for_reentrant_calls(gateway):
    port, access_key, secret_key = gateway
    lambda_client = await boto3.client(
        "lambda", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 0}),
    )
    response = await lambda_client.invoke(FunctionName=FUNCTION_NAME, Payload=b"{}")
    payload = json.loads(response["Payload"].read())
    assert payload["reentrant_ok"] is True, (
        "the handler's re-entrant call back through the gateway was not served "
        "while the invoke was in flight -- the event loop was frozen by the invoke"
    )
