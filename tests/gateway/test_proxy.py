"""G3 -- the gateway proxy app: verify -> scope -> classify -> evaluate ->
forward, with protocol-shaped denials (task-g3-brief.md).

Most cases drive `create_gateway_app` with REAL boto3-signed requests
(KeyStore-issued creds captured via `tests/gateway/harness.py`'s
CaptureSink, same pattern G2 established) replayed through the app over
httpx.ASGITransport, forwarding to a tiny recording ASGI "echo" standing
in for a real backing container -- no containers in this task (G5 does
real acceptance). A small final section proves the whole thing over REAL
sockets with a REAL boto3 client, since ASGITransport alone can't
demonstrate that boto3 actually raises `ClientError`.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import boto3
import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from odin.aws.backings import ACCESS_KEY as BACKING_ACCESS_KEY
from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.keys import KeyStore
from odin.gateway.policy import Statement
from odin.server import create_app
from odin.spec.store import SpecStore

from .harness import CaptureSink

FAKE_PORT = 19999  # placeholder -- ASGITransport dispatches to the mounted app, never actually dials this


@dataclass
class RecordedForward:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes


class EchoBacking:
    """Stands in for a real backing container: records every forwarded
    request and returns a canned response, so tests can assert exactly
    what the gateway sent onward (re-signed headers, byte-identical body)
    without a real container. HEAD responses carry the body's real
    Content-Length but no body bytes, matching real server HEAD semantics
    (the case research flagged as easy to get wrong)."""

    def __init__(self, body: bytes = b"", status_code: int = 200) -> None:
        self.requests: list[RecordedForward] = []
        self._body = body
        self._status_code = status_code

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        self.requests.append(RecordedForward(
            method=scope["method"], path=scope["raw_path"].decode("latin-1"),
            query=scope["query_string"].decode("latin-1"), headers=headers, body=body,
        ))
        response_body = b"" if scope["method"] == "HEAD" else self._body
        await send({
            "type": "http.response.start", "status": self._status_code,
            "headers": [(b"content-length", str(len(self._body)).encode())],
        })
        await send({"type": "http.response.body", "body": response_body})


@pytest.fixture
def sink() -> Iterator[CaptureSink]:
    capture = CaptureSink()
    yield capture
    capture.close()


@pytest.fixture
def keystore(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path)


def _issued_client(sink: CaptureSink, keystore: KeyStore, env: str, node_id: str, service: str, **extra):
    access_key, secret_key = keystore.issue(env, node_id)
    return boto3.client(
        service, endpoint_url=sink.endpoint, region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key, **extra,
    )


def _recording_on_deny():
    events: list[tuple] = []

    async def on_deny(principal, action, resource, reason) -> None:
        events.append((principal, action, resource, reason))

    return on_deny, events


async def _drive(app, req) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        return await client.request(req.method, req.url, headers=req.headers, content=req.body)


# --- allowed: forward + s3 re-sign + pass-through for others --------------


async def test_allowed_s3_get_object_forwards_and_resigns(sink, keystore):
    s3 = _issued_client(sink, keystore, "default", "api", "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))

    state = GatewayState()
    state.update("default", {"api": [Statement(actions=("s3:GetObject",), resources=("uploads",))]}, {"s3": FAKE_PORT})
    echo = EchoBacking(body=b"payload")
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny, forward_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=echo)))

    resp = await _drive(app, req)

    assert resp.status_code == 200
    assert events == []
    forwarded = echo.requests[0]
    assert forwarded.headers["authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={BACKING_ACCESS_KEY}/")
    assert forwarded.headers["authorization"] != req.headers["Authorization"]


async def test_allowed_s3_put_object_forwards_body_byte_identical(sink, keystore):
    s3 = _issued_client(sink, keystore, "default", "api", "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="a.txt", Body=b"payload-bytes-123"))

    state = GatewayState()
    state.update("default", {"api": [Statement(actions=("s3:PutObject",), resources=("uploads",))]}, {"s3": FAKE_PORT})
    echo = EchoBacking()
    on_deny, _events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny, forward_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=echo)))

    resp = await _drive(app, req)

    assert resp.status_code == 200
    assert echo.requests[0].body == b"payload-bytes-123"


async def test_s3_head_object_preserves_upstream_content_length(sink, keystore):
    s3 = _issued_client(sink, keystore, "default", "api", "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    req = sink.call(lambda: s3.head_object(Bucket="uploads", Key="a.txt"))

    state = GatewayState()
    state.update("default", {"api": [Statement(actions=("s3:GetObject",), resources=("uploads",))]}, {"s3": FAKE_PORT})
    echo = EchoBacking(body=b"seventeen-bytes!!")  # 17 bytes, deliberately not len(response body) since HEAD sends none
    on_deny, _events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny, forward_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=echo)))

    resp = await _drive(app, req)

    assert resp.content == b""
    assert resp.headers["content-length"] == str(len(b"seventeen-bytes!!"))


async def test_allowed_sqs_send_message_forwards_creds_pass_through(sink, keystore):
    sqs = _issued_client(sink, keystore, "default", "worker", "sqs")
    req = sink.call(lambda: sqs.send_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi"))

    state = GatewayState()
    state.update("default", {"worker": [Statement(actions=("sqs:SendMessage",), resources=("jobs",))]}, {"sqs": FAKE_PORT})
    echo = EchoBacking()
    on_deny, _events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny, forward_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=echo)))

    resp = await _drive(app, req)

    assert resp.status_code == 200
    # sqs/goaws ignores auth -- the ORIGINAL caller-signed Authorization is forwarded untouched.
    assert echo.requests[0].headers["authorization"] == req.headers["Authorization"]
    assert echo.requests[0].body == req.body


# --- denies: protocol-correct shapes + on_deny fired -----------------------


async def test_s3_denied_action_returns_bare_error_xml(sink, keystore):
    s3 = _issued_client(sink, keystore, "default", "api", "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))

    state = GatewayState()
    state.update("default", {}, {"s3": FAKE_PORT})  # no statements -> default-deny
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 403
    assert "<Error><Code>AccessDenied</Code>" in resp.text
    assert "<ErrorResponse>" not in resp.text  # bare <Error>, not sns's wrapped shape
    ((principal, action, resource, reason),) = events
    assert principal.node_id == "api" and action == "s3:GetObject" and resource == "uploads" and reason == "denied"


async def test_dynamodb_denied_action_returns_json_access_denied_exception(sink, keystore):
    dynamodb = _issued_client(sink, keystore, "default", "worker", "dynamodb")
    req = sink.call(lambda: dynamodb.get_item(TableName="orders", Key={"id": {"S": "1"}}))

    state = GatewayState()
    state.update("default", {}, {"dynamodb": FAKE_PORT})
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 403
    body = resp.json()
    assert body["__type"] == "com.amazonaws.dynamodb.v20120810#AccessDeniedException"
    ((principal, action, resource, reason),) = events
    assert action == "dynamodb:GetItem" and resource == "orders" and reason == "denied"


async def test_sqs_denied_action_returns_json_access_denied_exception(sink, keystore):
    sqs = _issued_client(sink, keystore, "default", "worker", "sqs")
    req = sink.call(lambda: sqs.send_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi"))

    state = GatewayState()
    state.update("default", {}, {"sqs": FAKE_PORT})
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 403
    assert resp.json()["__type"] == "com.amazonaws.sqs#AccessDeniedException"
    assert events[0][3] == "denied"


async def test_sns_denied_action_returns_wrapped_error_response_xml(sink, keystore):
    sns = _issued_client(sink, keystore, "default", "worker", "sns")
    req = sink.call(lambda: sns.publish(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts", Message="hi"))

    state = GatewayState()
    state.update("default", {}, {"sns": FAKE_PORT})
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 403
    assert "<ErrorResponse><Error>" in resp.text and "<Code>AccessDenied</Code>" in resp.text
    assert events[0][1] == "sns:Publish" and events[0][2] == "alerts"


async def test_sqs_get_queue_url_unmappable_denies_cleanly(sink, keystore):
    sqs = _issued_client(sink, keystore, "default", "worker", "sqs")
    req = sink.call(lambda: sqs.get_queue_url(QueueName="jobs"))

    state = GatewayState()
    state.update("default", {"worker": [Statement(actions=("*",), resources=("*",))]}, {"sqs": FAKE_PORT})
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 403  # broad allow doesn't matter -- classify() itself returns None
    assert events[0][3] == "unmappable-action"


async def test_sns_create_topic_unmappable_denies_cleanly(sink, keystore):
    sns = _issued_client(sink, keystore, "default", "worker", "sns")
    req = sink.call(lambda: sns.create_topic(Name="alerts"))

    state = GatewayState()
    state.update("default", {"worker": [Statement(actions=("*",), resources=("*",))]}, {"sns": FAKE_PORT})
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 403
    assert events[0][3] == "unmappable-action"


async def test_unknown_access_key_denies_401_invalid_client_token_id(sink, keystore, tmp_path):
    other = KeyStore(tmp_path / "other-keystore")  # a key this gateway's keystore never issued
    stray_key, stray_secret = other.issue("default", "ghost")
    s3 = boto3.client(
        "s3", endpoint_url=sink.endpoint, region_name="us-east-1",
        aws_access_key_id=stray_key, aws_secret_access_key=stray_secret,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))

    state = GatewayState()
    state.update("default", {}, {"s3": FAKE_PORT})
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 401
    assert "<Code>InvalidClientTokenId</Code>" in resp.text
    ((principal, action, resource, reason),) = events
    assert principal is None and reason == "unknown-key"


async def test_bad_signature_denies_401_signature_does_not_match(sink, keystore):
    s3 = _issued_client(sink, keystore, "default", "api", "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    tampered_headers = dict(req.headers)
    tampered_headers["Authorization"] = tampered_headers["Authorization"][:-4] + "0000"  # corrupt the signature

    state = GatewayState()
    state.update("default", {"api": [Statement(actions=("s3:GetObject",), resources=("uploads",))]}, {"s3": FAKE_PORT})
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        resp = await client.request(req.method, req.url, headers=tampered_headers, content=req.body)

    assert resp.status_code == 401
    assert "<Code>SignatureDoesNotMatch</Code>" in resp.text
    ((principal, action, resource, reason),) = events
    assert principal is not None and principal.node_id == "api" and reason == "bad-signature"


async def test_backing_unavailable_returns_503(sink, keystore):
    s3 = _issued_client(sink, keystore, "default", "api", "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))

    state = GatewayState()
    state.update("default", {"api": [Statement(actions=("s3:GetObject",), resources=("uploads",))]}, {})  # no s3 backing registered
    on_deny, events = _recording_on_deny()
    app = create_gateway_app(state, keystore, on_deny)

    resp = await _drive(app, req)

    assert resp.status_code == 503
    assert events[0][3] == "backing-unavailable"


# --- real sockets: prove boto3 actually raises ClientError -----------------


def test_real_boto3_client_raises_access_denied_client_error(tmp_path):
    store = KeyStore(tmp_path)
    access_key, secret_key = store.issue("default", "worker")
    state = GatewayState()
    state.update("default", {}, {})  # default-deny: no statements at all

    async def on_deny(principal, action, resource, reason) -> None:
        pass

    app = create_gateway_app(state, store, on_deny)
    server, thread, port = serve_in_thread(app, port=0)
    try:
        client = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        with pytest.raises(ClientError) as exc_info:
            client.get_object(Bucket="uploads", Key="a.txt")
        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"
    finally:
        stop_in_thread(server, thread)


def test_real_boto3_client_raises_invalid_client_token_id_for_unknown_key(tmp_path):
    store = KeyStore(tmp_path)
    state = GatewayState()
    state.update("default", {}, {})

    async def on_deny(principal, action, resource, reason) -> None:
        pass

    app = create_gateway_app(state, store, on_deny)
    server, thread, port = serve_in_thread(app, port=0)
    try:
        client = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
            aws_access_key_id="AKODINneverissued00000", aws_secret_access_key="not-a-real-secret",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        with pytest.raises(ClientError) as exc_info:
            client.get_object(Bucket="uploads", Key="a.txt")
        assert exc_info.value.response["Error"]["Code"] == "InvalidClientTokenId"
    finally:
        stop_in_thread(server, thread)


# --- server.py wiring: the gateway listener starts/stops with the app ------


def test_create_app_boots_gateway_thread_and_health_still_works(tmp_path):
    app = create_app(store=SpecStore(tmp_path), backings=False, gateway_port=0)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}
    assert app.state.gateway is not None
    assert app.state.gateway_keys is not None
