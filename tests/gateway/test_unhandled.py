"""The gateway's handler of last resort.

This app is what tofu's AWS provider and every workload SDK talk to, and until
now it was a bare `Starlette(routes=[...])` with no exception handler at all --
so anything `catch_all` didn't anticipate reached botocore as uvicorn's
plain-text `Internal Server Error`, with no AWS error document in it.

PROBED BEFORE WRITTEN, and the probe had to be done twice. Driving the app over
`httpx.ASGITransport` cannot answer the question: Starlette's
ServerErrorMiddleware sends the handler's response and then ALWAYS re-raises so
the server can log it, and the in-process transport surfaces that raise instead
of the response. Against a REAL uvicorn with a REAL boto3 client, the fix makes
botocore parse `503 / ServiceUnavailable / retryable=True` where it previously
got unparseable text -- `test_a_real_boto3_client_parses_a_real_gateway_failure`
below is that measurement, not a reconstruction of it.

`TestClient(..., raise_server_exceptions=False)` is what the fast cases use: it
suppresses the deliberate re-raise so the response is observable in-process.
"""
from __future__ import annotations

from pathlib import Path

import boto3
import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.keys import KeyStore
from odin.gateway.policy import Statement
from odin.gateway.stores import SynthStores


class DyingBacking:
    """A backing that fails the way a container removed mid-forward fails:
    httpx raises on the forward, inside `catch_all`, after the policy check has
    already passed. The same race `BackingUnavailable` covers on the control
    app, seen from the gateway's side."""

    async def __call__(self, scope, receive, send):
        raise httpx.ConnectError("All connection attempts failed")


class ExplodingBacking:
    """A total surprise -- something no path anticipated."""

    async def __call__(self, scope, receive, send):
        raise ZeroDivisionError("secret-bearing detail /Users/someone/.odin/keys")


async def _swallow(*_args, **_kwargs) -> None:
    return None


def _app(tmp_path: Path, backing, service: str = "s3"):
    keystore = KeyStore(tmp_path / "keys")
    stores = SynthStores(tmp_path / "synth")
    access_key, secret_key = keystore.issue("default", "api")
    state = GatewayState()
    state.update(
        "default",
        {"api": [Statement(actions=("*",), resources=("*",))]},
        {service: 9000},
    )
    app = create_gateway_app(
        state, keystore, stores, _swallow,
        forward_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=backing)),
    )
    return app, access_key, secret_key


async def _signed(access_key: str, secret_key: str, url: str, service: str = "s3"):
    """A real boto3-signed request, so the gateway identifies a real principal
    and a real service rather than being handed a hand-built header set."""
    client = await boto3.client(
        service, endpoint_url=url, region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      retries={"max_attempts": 1}),
    )
    return client


def test_a_backing_that_dies_mid_forward_answers_service_unavailable(tmp_path):
    """A transport failure IS the "backing isn't there" case, so it reuses the
    word this module already has for it -- and 503 is what an SDK correctly
    treats as retryable, since the next Apply re-creates the backing."""
    app, access_key, secret_key = _app(tmp_path, DyingBacking())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/uploads/a.txt", headers=_sigv4_headers(access_key, secret_key))

    assert response.status_code == 503
    assert "ServiceUnavailable" in response.text
    # The wire shape matters as much as the status: S3 clients parse XML.
    assert response.headers["content-type"].startswith("application/xml")


def _sigv4_headers(access_key: str, secret_key: str) -> dict[str, str]:
    """Sign a GET with botocore itself. Hand-built headers would prove nothing
    about identification, which is where `request.state.service` is set."""
    request = AWSRequest(method="GET", url="http://testserver/uploads/a.txt")
    SigV4Auth(Credentials(access_key, secret_key), "s3", "us-east-1").add_auth(request)
    return dict(request.headers)


def test_an_unexpected_exception_answers_internal_failure_without_leaking(tmp_path):
    """500 InternalFailure, naming the exception TYPE only. This response can
    reach an unauthenticated caller -- the gateway binds 0.0.0.0 and a request
    can fail before verification -- so the detail belongs in the log, not here.
    Real AWS's own InternalFailure is opaque for the same reason."""
    app, access_key, secret_key = _app(tmp_path, ExplodingBacking())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/uploads/a.txt", headers=_sigv4_headers(access_key, secret_key))

    assert response.status_code == 500
    assert "InternalFailure" in response.text
    assert "ZeroDivisionError" in response.text          # correlates with the log line
    assert "/Users/someone" not in response.text          # the leak this guards
    assert "secret-bearing" not in response.text


async def test_the_failure_wears_the_wire_format_of_the_service_asked_for(tmp_path, sink):
    """`request.state.service` is why this works: set at identification, the
    earliest point the service is known, so a handler covering paths that never
    reached their own error return still answers in the right dialect. DynamoDB
    clients parse JSON, not XML.

    The request is captured from a REAL boto3 call rather than hand-built --
    a hand-built one failed to classify and was denied 403 before it ever
    reached the forward, which would have tested the deny path instead of this
    handler."""
    app, access_key, secret_key = _app(tmp_path, DyingBacking(), service="dynamodb")
    ddb = await boto3.client(
        "dynamodb", endpoint_url=sink.endpoint, region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )
    captured = sink.call(lambda: ddb.describe_table(TableName="sessions"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(
            captured.method, captured.url, headers=captured.headers, content=captured.body,
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/x-amz-json")
    assert "ServiceUnavailable" in response.text


async def test_a_real_boto3_client_parses_a_real_gateway_failure(tmp_path):
    """The measurement the in-process cases cannot make (see module docstring):
    over a real socket, against real uvicorn, does botocore turn this into a
    ClientError it can reason about? Before the handler existed it received
    unparseable plain text."""
    app, access_key, secret_key = _app(tmp_path, DyingBacking())
    server, thread, port = serve_in_thread(app, host="127.0.0.1", port=0)
    try:
        s3 = await _signed(access_key, secret_key, f"http://127.0.0.1:{port}")
        with pytest.raises(ClientError) as caught:
            s3.get_object(Bucket="uploads", Key="a.txt")
    finally:
        stop_in_thread(server, thread)

    error = caught.value.response
    assert error["ResponseMetadata"]["HTTPStatusCode"] == 503
    assert error["Error"]["Code"] == "ServiceUnavailable"


async def test_an_sts_auth_failure_is_query_xml_so_botocore_can_parse_it(tmp_path):
    """STS is a QUERY-protocol service, like sns/iam/rds — but it was missing
    from `errors._QUERY_XML_SERVICES`, so odin answered an STS auth failure with
    AWS-JSON. Reproduced with a real boto3 client against a real server: botocore
    raised `ResponseParserError: ... invalid XML received` over a body of
    `{"__type": "Invalid...`, instead of the `InvalidClientTokenId` the caller
    can actually act on.

    Reachable whenever a container holds credentials from a revoked env and calls
    GetCallerIdentity — which is exactly when a clear auth error matters most.
    Found by the gateway agent and confirmed on unmodified develop, so it
    predates the loop migration."""
    keystore = KeyStore(tmp_path / "keys")
    stores = SynthStores(tmp_path / "synth")
    app = create_gateway_app(GatewayState(), keystore, stores, _swallow)
    server, thread, port = serve_in_thread(app, host="127.0.0.1", port=0)
    try:
        sts = await boto3.client(
            "sts", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
            aws_access_key_id="AKIAnotreal", aws_secret_access_key="nope",
            config=Config(retries={"max_attempts": 1}),
        )
        with pytest.raises(ClientError) as caught:
            sts.get_caller_identity()
    finally:
        stop_in_thread(server, thread)

    assert caught.value.response["Error"]["Code"] == "InvalidClientTokenId"
    assert caught.value.response["ResponseMetadata"]["HTTPStatusCode"] == 401
