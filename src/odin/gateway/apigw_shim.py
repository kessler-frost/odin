"""The HTTP <-> Lambda invoke-envelope shim -- the piece `alb -> lambda` was
DECLINED for (`iac/hcl.py::_ALB_NO_LAMBDA`), built once, here.

THE PROBLEM IT SOLVES. odin's routers are nginx containers that dial `host:port`
upstreams (`compute/proxy.py`, `compute/apigw.py`). A Lambda is not a
`host:port` that speaks HTTP: it is a RIE container whose ONLY route is
`POST /2015-03-31/functions/function/invocations`, which takes an EVENT
DOCUMENT and returns whatever the handler returned. So an HTTP API route that
targets a function needs a translator on both legs -- request to event, return
value to response -- and that translator has to live somewhere an nginx
container can dial.

WHERE IT LIVES, AND WHY THAT IS NOT A SECOND LISTENER. This is a Starlette
route mounted on the gateway app AHEAD of its SigV4 catch-all
(`gateway/app.py`). Three reasons, in order of weight:

1. **The gateway already has a real bound port that containers can reach.**
   Every workload's `AWS_ENDPOINT_URL` points at it, and `gateway_port()` is
   already threaded down to every model. A second uvicorn listener would need
   its own port plumbed through `synth.pure_answer` into `apigwctl` for no gain.
2. **It does not touch `catch_all`.** Starlette matches routes in order, so a
   dedicated `Route` in front of the catch-all is a NEW door, not a hole punched
   in the middle of the verify -> classify -> evaluate pipeline. That pipeline's
   invariant -- every request through it is SigV4-signed -- is untouched and
   still testable in one place.
3. **The boundary is then a named thing that can be drawn.** `docs/architecture`
   draws it as UNGATED, because it is.

IT IS UNAUTHENTICATED BY DESIGN, AND BOUNDED ON PURPOSE. An HTTP API route with
`authorization_type = "NONE"` -- the only kind odin emits -- is a public
endpoint; that is what an API Gateway IS. So the shim cannot ask for a
signature. What it does instead is refuse to be MORE powerful than the API's own
published port:

- It never takes a function name. It takes `(env, api_id, integration_id)` and
  resolves the target through the STORED integration record, so it can only ever
  invoke something a route already points at.
- It requires the API's own `route_token` -- a 32-hex secret minted at
  `CreateApi`, stored in the 0600 `apigwctl.json` sidecar, and injected by nginx
  as `X-Odin-Api-Token`. Without it the shim answers 403 and never touches the
  function. This is what stops a container in env A from naming env B in the URL
  and invoking a function it could not otherwise reach: dialing env B's nginx
  port is bounded by env B's routes, and now so is this.

The token is NOT a claim that the API endpoint is protected. Anything that can
reach the published nginx port can call the function, exactly as on real AWS.
The token bounds the SHIM to the same authority, no more.

PAYLOAD FORMAT 2.0, and only 2.0. `iac/hcl.py` emits
`payload_format_version = "2.0"` on every AWS_PROXY integration odin generates,
which is also the default for HTTP APIs, so there is one event shape and one
response shape rather than a version switch nothing would exercise. An
integration that arrives asking for `1.0` is REFUSED at CreateIntegration by
`apigwctl` rather than quietly served a 2.0 event -- a 1.0 handler reads
`event["httpMethod"]`, which a 2.0 event does not have, so serving the wrong
one produces a `KeyError` inside the user's function and blames their code.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass

import httpx
from starlette.requests import Request
from starlette.responses import Response

from odin.aws.backings import ACCOUNT

log = logging.getLogger("odin.gateway.apigw")

# The URL prefix the shim owns on the gateway app. `_odin` is a RESERVED path
# segment: no AWS API uses it, and a signed request that used it would reach
# this route instead of the catch-all.
SHIM_PREFIX = "/_odin/apigw"
# nginx carries the route's context in these; `compute/apigw.py::ApiRoute.headers`
# is the only writer and this module is the only reader.
TOKEN_HEADER = "x-odin-api-token"
RAW_PATH_HEADER = "x-odin-raw-path"
ROUTE_KEY_HEADER = "x-odin-route-key"
STAGE_HEADER = "x-odin-stage"

PAYLOAD_FORMAT = "2.0"
# The stage every odin API serves under. `$default` is the only stage
# `iac/hcl.py` emits, and it is the one whose `invoke_url` has no stage
# segment in the path -- which is what makes the nginx prefix and the route key
# agree about what `/orders` means.
DEFAULT_STAGE = "$default"

# Headers a handler's proxy response may set that would corrupt the framing of
# the response THIS process writes. `content-length` is recomputed by Starlette
# from the real body; the rest are hop-by-hop.
_UNSAFE_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "keep-alive"}

# Bodies with these content types travel as text; everything else is base64'd
# into the event, which is what `isBase64Encoded` means.
_TEXTUAL_TYPES = ("text/", "application/json", "application/javascript", "application/xml", "+json", "+xml")


def mint_route_token() -> str:
    """The per-API secret nginx replays and the shim checks. `secrets`, not
    `random`: it is the only thing standing between a caller that knows an
    `(env, api_id, integration_id)` triple and an invoke."""
    return secrets.token_hex(16)


def _is_textual(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(marker in lowered for marker in _TEXTUAL_TYPES)


@dataclass(frozen=True)
class ShimTarget:
    """Everything the shim needs about one integration, resolved by the caller
    from the stored records so this module never reads a store itself.

    Exactly one of three states, and `unavailable` is the one worth having:
    `function_name` for an AWS_PROXY integration, `http_upstream` for an
    HTTP_PROXY one whose backend really has an address right now, and
    `unavailable` -- a REASON, in words -- for one that does not. An ECS
    service with no running task is the case that produces it, and it is
    genuinely common: `tofu apply` creates the API and the service in the same
    run, and a caller can arrive before a task does. Answering 503 with "service
    `orders` has no running task" beats a bare 502 from a router pointed at an
    address that does not exist."""

    api_id: str
    route_key: str
    stage: str
    function_name: str | None = None
    http_upstream: str | None = None
    unavailable: str | None = None


def build_event(
    request: Request, target: ShimTarget, body: bytes, raw_path: str, now: float,
) -> dict:
    """An HTTP request as an API Gateway **payload format 2.0** event.

    Every member here is one a real 2.0 event carries; nothing is invented and
    nothing real is silently omitted except the members that describe things
    odin has no analogue for (`authorizer`, `cookies` beyond the raw header,
    `domainPrefix`). `rawPath`/`rawQueryString` come from the header nginx set,
    NOT from this request's own path -- the nginx `proxy_pass` rewrote the path
    onto the shim's route, so `request.url.path` is odin's internal URL and
    using it would hand the handler a path the caller never sent.
    """
    path, _, query = raw_path.partition("?")
    headers = {name.lower(): value for name, value in request.headers.items()
               if not name.lower().startswith("x-odin-")}
    query_params = dict(_pairs(query))
    textual = _is_textual(headers.get("content-type", "text/plain"))
    event: dict = {
        "version": PAYLOAD_FORMAT,
        "routeKey": target.route_key,
        "rawPath": path or "/",
        "rawQueryString": query,
        "headers": headers,
        "requestContext": {
            "accountId": ACCOUNT,
            "apiId": target.api_id,
            "domainName": headers.get("host", ""),
            "http": {
                "method": request.method,
                "path": path or "/",
                "protocol": "HTTP/1.1",
                "sourceIp": request.client.host if request.client else "127.0.0.1",
                "userAgent": headers.get("user-agent", ""),
            },
            "requestId": str(uuid.uuid4()),
            "routeKey": target.route_key,
            "stage": target.stage,
            "time": time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime(now)),
            "timeEpoch": int(now * 1000),
        },
        "isBase64Encoded": not textual,
    }
    if query_params:
        event["queryStringParameters"] = query_params
    if body:
        event["body"] = body.decode("utf-8") if textual else base64.b64encode(body).decode("ascii")
    cookies = headers.get("cookie")
    if cookies:
        event["cookies"] = [part.strip() for part in cookies.split(";") if part.strip()]
    return event


def _pairs(query: str) -> list[tuple[str, str]]:
    """`a=1&b=2` -> pairs, with a bare `?flag` keeping an empty value rather
    than vanishing (the same `keep_blank_values` reasoning
    `tests/gateway/conftest.py::split_url` documents for S3 subresources)."""
    from urllib.parse import parse_qsl

    return parse_qsl(query, keep_blank_values=True)


def response_from_return_value(payload: bytes) -> Response:
    """A handler's return value as an HTTP response, by payload-format-2.0's
    own two rules -- and this is the half where getting it wrong is invisible.

    MEASURED against a real `public.ecr.aws/lambda/python:3.12` container. Three
    handlers, one event, raw bytes quoted rather than paraphrased:

        proxy handler (returns statusCode/headers/body/cookies)
          {"statusCode": 201, "headers": {...}, "body": "...",
           "cookies": ["a=1; Path=/", "b=2; Path=/"]}
          function_error: None

        bare handler (no statusCode at all)
          {"ok": true, "echo": "/hello/probe"}
          function_error: None

        raising handler
          {"errorMessage": "boom from the handler", "errorType": "ValueError",
           "requestId": "07f79599-...", "stackTrace": [...]}
          function_error: 'Unhandled'   <- from the BODY; RIE sends no header

    So RIE returns the handler's return value VERBATIM as JSON, `cookies`
    included; a handler with no `statusCode` really does need rule 2 below; and
    a RAISED handler arrives as an ordinary SUCCESSFUL invoke carrying an error
    document, which is why `_from_invocation` checks `function_error` first.

    The first run of that probe answered `Unable to import module 'app'` for all
    three, which reads exactly like an RIE finding. It was the HARNESS: Colima
    only mounts paths under `$HOME`, and the probe rooted its code directory in
    `tempfile.mkdtemp()` under `/private/var/folders/...`, so the bind mount
    silently resolved to an EMPTY directory. Check your own harness before
    believing a measurement -- `tests/simulate/test_apigateway_e2e.py`'s
    `store_root` fixture exists for exactly this.

    Rule 1: a JSON OBJECT carrying `statusCode` is a proxy response. Its
    `statusCode`, `headers`, `cookies`, `body` and `isBase64Encoded` are used.
    Rule 2 (2.0-only, and the one people forget): ANYTHING ELSE -- an object
    with no `statusCode`, a list, a string, a number -- is the RESPONSE BODY,
    served as `200 application/json`. That is why a 2.0 handler can `return
    {"ok": True}` and get a working JSON API, and a shim that implemented only
    rule 1 would answer 500 for the most common handler anyone writes.

    A non-JSON body cannot be either, so it is passed through as
    `text/plain` -- a handler that returned raw bytes is not an error, and
    `_payload_object`'s precedent in `compute/functions.py` is to treat
    unparseable output as "not a document", never as a failure.
    """
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(payload, status_code=200, media_type="text/plain")
    if not isinstance(parsed, dict) or "statusCode" not in parsed:
        return Response(json.dumps(parsed), status_code=200, media_type="application/json")
    return _proxy_response(parsed)


def _proxy_response(parsed: dict) -> Response:
    headers = {
        str(name): str(value) for name, value in (parsed.get("headers") or {}).items()
        if str(name).lower() not in _UNSAFE_RESPONSE_HEADERS
    }
    body = parsed.get("body") or ""
    content = base64.b64decode(body) if parsed.get("isBase64Encoded") else str(body).encode("utf-8")
    response = Response(content, status_code=int(parsed["statusCode"]), headers=headers)
    # `cookies` is 2.0's replacement for 1.0's `multiValueHeaders`: a list of
    # whole Set-Cookie values. `headers` is a plain dict, so it CANNOT carry two
    # of them -- appending is the only way a handler can set more than one, and
    # a dict-based shim silently drops all but the last.
    for cookie in parsed.get("cookies") or []:
        response.raw_headers.append((b"set-cookie", str(cookie).encode("latin-1")))
    return response


def error_response(status: int, message: str) -> Response:
    """The shim's OWN failures, in the shape a real HTTP API uses for its own
    (`{"message": ...}`) -- never the invoked function's shape, so a caller can
    always tell odin's gateway apart from the handler it fronts."""
    return Response(
        json.dumps({"message": message}), status_code=status, media_type="application/json",
    )


async def serve_request(request: Request, stores, invoke_via, gateway_port: int | None) -> Response:
    """The gateway app's route handler: resolve, authorize, serve.

    Resolution and the token check live in `apigwctl.shim_target` -- this module
    never reads a store, so `serve` below can be unit-tested against a stub with
    no containers, no records and no gateway. Imported inside the function
    because `apigwctl` imports THIS module for `ShimTarget`/`SHIM_PREFIX`, and a
    module-level import either way is a cycle. That is the one inline import in
    this file and it is here rather than in `apigwctl` because this direction is
    the request path's, used once per request; the other direction is used on
    every route render.
    """
    from odin.gateway.models import apigwctl

    if gateway_port is None:
        # Only reachable in a test that builds the app with no lifespan: nginx
        # cannot have been pointed here without a port to point at.
        return error_response(503, "odin's gateway has not bound a port yet")
    params = request.path_params
    resolved = apigwctl.shim_target(
        stores, params["env"], params["api_id"], params["integration_id"],
        request.headers.get(TOKEN_HEADER, ""),
    )
    if isinstance(resolved, str):
        # `Forbidden` is the token failure and is deliberately the ONLY one that
        # answers 403 with no detail: telling an unauthorized caller which of
        # the three ids was wrong is a free oracle over another env's records.
        return error_response(403 if resolved == "Forbidden" else 404, resolved)
    route_key = request.headers.get(ROUTE_KEY_HEADER) or resolved.route_key
    stage = request.headers.get(STAGE_HEADER) or resolved.stage
    return await serve(
        request,
        ShimTarget(
            api_id=resolved.api_id, route_key=route_key, stage=stage,
            function_name=resolved.function_name, http_upstream=resolved.http_upstream,
            unavailable=resolved.unavailable,
        ),
        lambda name, payload: invoke_via(stores, params["env"], name, payload),
    )


async def serve(
    request: Request, target: ShimTarget, invoke, timeout: float = 30.0,
) -> Response:
    """One API request, answered.

    `invoke` is a callable `(function_name, payload_bytes) -> Invocation` --
    `gateway/models/lambdactl.py::invoke` bound to its stores/env by the caller.
    Passed in rather than imported so this module has no dependency on the
    model layer and can be unit-tested against a stub with no containers.
    """
    body = await request.body()
    raw_path = request.headers.get(RAW_PATH_HEADER) or request.url.path
    if target.unavailable is not None:
        return error_response(503, target.unavailable)
    if target.http_upstream is not None:
        return await _proxy_http(request, target, body, raw_path, timeout)
    result = await invoke(
        target.function_name,
        json.dumps(build_event(request, target, body, raw_path, time.time())).encode("utf-8"),
    )
    return _from_invocation(result, target)


def _from_invocation(result, target: ShimTarget) -> Response:
    """An `lambdactl.Invocation` as an HTTP response.

    The three non-`ran` outcomes are 500/502/503 with the REASON, never a bare
    502: `missing` means the route points at a function that is not there
    (odin's own wiring is wrong), `not_ready` means it exists and is still
    booting (retry helps), `unreachable` means its container is gone. A real
    API Gateway answers 500 `Internal Server Error` for all three and says
    nothing, which is exactly the "name what is still standing" failure this
    repo's honesty rule 2 forbids.

    `function_error` is the OTHER half, and the one a naive shim gets wrong: RIE
    answers a RAISED handler with **HTTP 200** and an `{"errorMessage",
    "errorType", ...}` body (measured, `compute/functions.py`'s `_ERROR_PAYLOAD_KEYS`
    note). Passing that through rule 2 of `response_from_return_value` would
    serve a crashed function as `200 OK` with a stack trace as the payload --
    a green response for a failed request. Real API Gateway answers 502 for
    exactly this, so odin does too, and keeps the function's own error document
    as the body because it is the only thing that says what broke.
    """
    if result.outcome != "ran":
        return error_response(_OUTCOME_STATUS[result.outcome], result.detail)
    if result.function_error:
        log.warning("apigw route %s: function %s raised", target.route_key, target.function_name)
        return Response(
            result.payload or b'{"message":"Internal Server Error"}',
            status_code=502, media_type="application/json",
            headers={"x-amzn-errortype": result.function_error},
        )
    return response_from_return_value(result.payload)


# Deliberately NOT `.get()`-defaulted (`lambdactl._INVOKE_ERROR`'s precedent):
# an outcome nobody mapped must raise here rather than inherit some other
# outcome's status code.
_OUTCOME_STATUS = {"missing": 500, "not_ready": 503, "unreachable": 502}


async def _proxy_http(
    request: Request, target: ShimTarget, body: bytes, raw_path: str, timeout: float,
) -> Response:
    """An HTTP_PROXY integration. No envelope -- the request is replayed at the
    target's real address and its answer returned.

    WHY AN ECS ROUTE COMES THROUGH HERE AT ALL, since nginx could dial the task
    directly and that is what `alb -> ecs` does. An ECS task's address is
    `(host.docker.internal, the host port Docker published)`, and that port
    CHANGES every time the task is replaced. nginx would therefore need the
    address baked into its config at converge time plus a push from `ecsctl` on
    every task lifecycle transition -- which is exactly what `elbv2ctl.
    register_target` is, and it is four call sites in `ecsctl` that must all
    stay correct forever. A miss in any one of them is a router pointed at a
    dead port: a 502 with no explanation, which is this repo's worst bug shape.
    Resolving at REQUEST time instead cannot go stale, needs no hook in another
    model, and costs one extra in-process hop on a loopback connection.
    The trade recorded honestly in `docs/limits.md`: odin's own process is in
    the ECS data path for an API route (it is not for an ALB), and nginx's
    request-level `proxy_next_upstream` failover across tasks is not available
    here -- this dials the first running task and reports the failure if it is
    not there.
    """
    path, _, query = raw_path.partition("?")
    url = f"http://{target.http_upstream}{path}" + (f"?{query}" if query else "")
    headers = {name: value for name, value in request.headers.items()
               if not name.lower().startswith("x-odin-") and name.lower() != "host"}
    async with httpx.AsyncClient() as client:
        upstream = await client.request(
            request.method, url, headers=headers, content=body, timeout=timeout,
        )
    return Response(
        upstream.content, status_code=upstream.status_code,
        headers={name: value for name, value in upstream.headers.items()
                 if name.lower() not in _UNSAFE_RESPONSE_HEADERS},
    )
