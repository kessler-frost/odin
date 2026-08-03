"""The HTTP <-> Lambda invoke-envelope shim (`gateway/apigw_shim.py`).

WHAT THESE TESTS PROVE, STATED FIRST BECAUSE THE LIMIT IS THE POINT. Every case
here drives the REAL converter with a STUB invoke. That proves the translation
in both directions; it does NOT prove that a real RIE returns a handler's return
value in the shape the converter expects. A unit test that fabricates the
upstream signal proves the parser, not the integration -- this repo has four
guards that passed their own tests and never fired for exactly that reason. The
integration half is `tests/simulate/test_apigateway_e2e.py`, which invokes a
real `public.ecr.aws/lambda/python:3.12` container and prints what came back.

The one upstream fact these tests DO encode was measured elsewhere and is
already load-bearing in `compute/functions.py`: RIE answers a RAISED handler
with **HTTP 200** and an `{"errorMessage", "errorType", ...}` body, sending no
`X-Amz-Function-Error` header at all. `lambdactl.invoke` turns that into
`Invocation.function_error`, and `test_a_crashed_handler_is_a_502_not_a_200`
below is what stops the shim serving a stack trace as a success.
"""
from __future__ import annotations

import base64
import json

import pytest
from starlette.requests import Request

from odin.gateway import apigw_shim
from odin.gateway.apigw_shim import ShimTarget

TARGET = ShimTarget(api_id="api1", route_key="ANY /orders", stage="$default", function_name="orders")


class _Invocation:
    """`lambdactl.Invocation`'s shape (a NamedTuple there); a class here so a
    test can build one without importing the model."""

    def __init__(self, outcome: str, payload: bytes = b"", function_error=None, detail: str = "") -> None:
        self.outcome, self.payload, self.function_error, self.detail = outcome, payload, function_error, detail


def _request(method: str = "GET", path: str = "/_odin/apigw/e/api1/int1", headers=None, body: bytes = b"") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": raw, "client": ("10.1.2.3", 5000),
        "scheme": "http", "server": ("127.0.0.1", 80), "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def _serve(request: Request, result: _Invocation, target: ShimTarget = TARGET):
    calls: list[tuple] = []

    async def invoke(name, payload):
        calls.append((name, payload))
        return result

    response = await apigw_shim.serve(request, target, invoke)
    return response, calls


# --- request -> event (payload format 2.0) ----------------------------------


def test_the_event_is_built_from_the_RAW_PATH_HEADER_not_the_shim_url():
    """nginx `proxy_pass` REWRITES the path onto the shim's own route, so
    `request.url.path` is odin's internal URL. Building `rawPath` from it would
    hand the handler a path the caller never sent -- and every handler that
    routes on `event["rawPath"]` would see `/_odin/apigw/...`."""
    request = _request(headers={apigw_shim.RAW_PATH_HEADER: "/orders/42?q=1"})
    event = apigw_shim.build_event(request, TARGET, b"", "/orders/42?q=1", 1_700_000_000.0)

    assert event["rawPath"] == "/orders/42"
    assert event["rawQueryString"] == "q=1"
    assert event["queryStringParameters"] == {"q": "1"}
    assert event["requestContext"]["http"]["path"] == "/orders/42"


def test_the_event_declares_format_2_0_and_carries_the_route_key():
    event = apigw_shim.build_event(_request(), TARGET, b"", "/orders", 1_700_000_000.0)

    assert event["version"] == "2.0"
    assert event["routeKey"] == "ANY /orders"
    assert event["requestContext"]["routeKey"] == "ANY /orders"
    assert event["requestContext"]["stage"] == "$default"
    assert event["requestContext"]["apiId"] == "api1"
    # 2.0 has no `httpMethod` at the top level -- it lives under requestContext.
    assert "httpMethod" not in event
    assert event["requestContext"]["http"]["method"] == "GET"


def test_odins_own_routing_headers_never_reach_the_handler():
    """`X-Odin-Api-Token` is a SECRET. A shim that copied every inbound header
    into the event would hand it to arbitrary user code."""
    request = _request(headers={
        apigw_shim.TOKEN_HEADER: "s3cret", apigw_shim.RAW_PATH_HEADER: "/orders",
        "x-custom": "kept",
    })
    event = apigw_shim.build_event(request, TARGET, b"", "/orders", 1.0)

    assert "s3cret" not in json.dumps(event)
    assert not [name for name in event["headers"] if name.startswith("x-odin-")]
    assert event["headers"]["x-custom"] == "kept"


def test_a_textual_body_travels_as_text_and_a_binary_one_as_base64():
    textual = apigw_shim.build_event(
        _request(method="POST", headers={"content-type": "application/json"}),
        TARGET, b'{"a":1}', "/orders", 1.0,
    )
    assert textual["body"] == '{"a":1}'
    assert textual["isBase64Encoded"] is False

    binary = apigw_shim.build_event(
        _request(method="POST", headers={"content-type": "image/png"}),
        TARGET, b"\x89PNG\r\n", "/orders", 1.0,
    )
    assert base64.b64decode(binary["body"]) == b"\x89PNG\r\n"
    assert binary["isBase64Encoded"] is True


def test_cookies_arrive_as_2_0s_own_list():
    event = apigw_shim.build_event(
        _request(headers={"cookie": "a=1; b=2"}), TARGET, b"", "/orders", 1.0,
    )
    assert event["cookies"] == ["a=1", "b=2"]


# --- return value -> response -----------------------------------------------


def test_a_proxy_shaped_return_value_becomes_that_response():
    response = apigw_shim.response_from_return_value(json.dumps({
        "statusCode": 201, "headers": {"content-type": "text/plain"}, "body": "made",
    }).encode())

    assert response.status_code == 201
    assert response.body == b"made"
    assert response.headers["content-type"] == "text/plain"


def test_a_return_value_with_no_statusCode_is_the_BODY_at_200():
    """Payload format 2.0's own second rule, and the one people forget: anything
    that is not a proxy response IS the response body, served as 200 JSON. A
    shim implementing only the first rule answers 500 for the most common
    handler anyone writes (`return {"ok": True}`)."""
    response = apigw_shim.response_from_return_value(b'{"ok": true}')

    assert response.status_code == 200
    assert json.loads(response.body) == {"ok": True}
    assert response.headers["content-type"] == "application/json"


def test_a_list_return_value_is_also_the_body():
    response = apigw_shim.response_from_return_value(b"[1, 2, 3]")
    assert response.status_code == 200
    assert json.loads(response.body) == [1, 2, 3]


def test_a_base64_proxy_body_is_decoded():
    response = apigw_shim.response_from_return_value(json.dumps({
        "statusCode": 200, "body": base64.b64encode(b"\x00\x01").decode(), "isBase64Encoded": True,
    }).encode())
    assert response.body == b"\x00\x01"


def test_two_cookies_both_survive():
    """`headers` is a dict and CANNOT carry two `Set-Cookie` values -- appending
    is the only way, which is exactly why 2.0 has a `cookies` list. A dict-based
    shim silently drops all but the last."""
    response = apigw_shim.response_from_return_value(json.dumps({
        "statusCode": 200, "body": "", "cookies": ["a=1; Path=/", "b=2; Path=/"],
    }).encode())

    set_cookies = [value.decode() for name, value in response.raw_headers if name == b"set-cookie"]
    assert set_cookies == ["a=1; Path=/", "b=2; Path=/"]


def test_a_handler_that_sets_content_length_cannot_corrupt_the_framing():
    response = apigw_shim.response_from_return_value(json.dumps({
        "statusCode": 200, "headers": {"content-length": "9999"}, "body": "short",
    }).encode())

    assert response.headers["content-length"] == str(len(b"short"))


def test_a_non_json_return_value_is_passed_through_rather_than_failed():
    response = apigw_shim.response_from_return_value(b"\xff\xfe not json")
    assert response.status_code == 200
    assert response.body == b"\xff\xfe not json"


# --- outcomes ---------------------------------------------------------------


async def test_a_healthy_invoke_serves_the_handlers_response():
    body = json.dumps({"statusCode": 200, "body": "hello"}).encode()
    response, calls = await _serve(_request(), _Invocation("ran", payload=body))

    assert response.status_code == 200
    assert response.body == b"hello"
    assert calls[0][0] == "orders"
    assert json.loads(calls[0][1])["version"] == "2.0"


async def test_a_crashed_handler_is_a_502_not_a_200():
    """THE ONE THAT MATTERS. RIE answers a RAISED handler with HTTP **200** and
    an error document (measured -- `compute/functions.py`'s `_ERROR_PAYLOAD_KEYS`
    note). Passing that through payload-format-2.0's "anything else is the body"
    rule would serve a crashed function as `200 OK` with a stack trace as the
    payload: a green response for a failed request, which is this repo's most
    repeated bug class."""
    crash = json.dumps({"errorMessage": "boom", "errorType": "ValueError"}).encode()
    response, _calls = await _serve(
        _request(), _Invocation("ran", payload=crash, function_error="Unhandled"),
    )

    assert response.status_code == 502
    assert response.headers["x-amzn-errortype"] == "Unhandled"
    # The function's own error document is kept: it is the only thing that says
    # what broke.
    assert json.loads(response.body)["errorMessage"] == "boom"


@pytest.mark.parametrize(
    ("outcome", "status"),
    [("missing", 500), ("not_ready", 503), ("unreachable", 502)],
)
async def test_every_non_ran_outcome_answers_with_its_own_status_and_reason(outcome, status):
    """A real API Gateway answers 500 for all three and says nothing. Naming
    which one it is is the difference between "retry" and "your wiring is
    wrong"."""
    response, _calls = await _serve(
        _request(), _Invocation(outcome, detail=f"the {outcome} reason"),
    )
    assert response.status_code == status
    assert json.loads(response.body)["message"] == f"the {outcome} reason"


async def test_an_unavailable_target_is_a_503_naming_the_service():
    target = ShimTarget(
        api_id="api1", route_key="ANY /checkout", stage="$default",
        unavailable="The ECS service 'checkout' this route targets has no running task",
    )
    response, calls = await _serve(_request(), _Invocation("ran"), target=target)

    assert response.status_code == 503
    assert "checkout" in json.loads(response.body)["message"]
    # And nothing was invoked.
    assert calls == []


async def test_the_shims_own_errors_are_shaped_unlike_a_handlers():
    """A caller must always be able to tell odin's gateway apart from the
    function it fronts. odin's own failures are `{"message": ...}` -- a real
    HTTP API's own shape -- never the handler's."""
    response = apigw_shim.error_response(503, "nothing to forward to")
    assert set(json.loads(response.body)) == {"message"}
