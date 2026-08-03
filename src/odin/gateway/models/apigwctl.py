"""API Gateway v2 (HTTP APIs) -- an ALL-SYNTH control plane over a REAL nginx
container per API (`compute/apigw.py`), with `gateway/apigw_shim.py` translating
HTTP into the Lambda invoke envelope on the way through.

Shape mirrors `gateway/models/elbv2ctl.py` exactly: one model module owning
create/get/update/delete over a per-env store, a real container converged in the
background of every mutation, and a `state` the caller can watch. Read that
module first; what follows is only what DIFFERS.

THE WIRE, MEASURED RATHER THAN GUESSED. Every route and body shape below was
recorded from real terraform-provider-aws 5.100.0 driving a recorder over real
HTTP, not read off a doc page. Three things that a reasonable person gets wrong:

1. **The SigV4 credential scope is `apigateway`, for BOTH v1 and v2.**
   Recorded: `Credential=probe/20260803/us-east-1/apigateway/aws4_request`. The
   botocore service model is called `apigatewayv2` and its `endpointPrefix` is
   `apigateway`, so anybody reading the SDK name will "fix" `SERVICE` to
   `apigatewayv2` and silently break every call -- `gateway/app.py` reads the
   service from the credential scope and nothing else.
2. **rest-json with LOWER-camelCase members.** `apiId`, not `ApiId`. A first
   stub answering PascalCase made tofu fail with
   `serialization failed: input member ApiId must not be empty` -- it had parsed
   the create response and found nothing.
3. **`UpdateApi` is `PATCH`**, which odin's gateway did not route at all until
   v0.8.19 (a bare Starlette 405 before any odin code ran; see
   `tests/gateway/test_closed_world_is_method_independent.py`).

The recorded operation set, which is exactly what this module implements:

    POST   /v2/apis                                    CreateApi
    GET    /v2/apis/{apiId}                            GetApi
    PATCH  /v2/apis/{apiId}                            UpdateApi
    DELETE /v2/apis/{apiId}                            DeleteApi
    POST   /v2/apis/{apiId}/integrations               CreateIntegration
    GET    /v2/apis/{apiId}/integrations/{id}          GetIntegration
    PATCH  /v2/apis/{apiId}/integrations/{id}          UpdateIntegration
    DELETE /v2/apis/{apiId}/integrations/{id}          DeleteIntegration
    POST   /v2/apis/{apiId}/routes                     CreateRoute
    GET    /v2/apis/{apiId}/routes/{id}                GetRoute
    PATCH  /v2/apis/{apiId}/routes/{id}                UpdateRoute
    DELETE /v2/apis/{apiId}/routes/{id}                DeleteRoute
    POST   /v2/apis/{apiId}/stages                     CreateStage
    GET    /v2/apis/{apiId}/stages/{stageName}         GetStage
    PATCH  /v2/apis/{apiId}/stages/{stageName}         UpdateStage
    DELETE /v2/apis/{apiId}/stages/{stageName}         DeleteStage

`apiEndpoint` IS THE REAL ENDPOINT -- AND `invoke_url` IS NOT. This started as a
wrong belief and the correction is the useful part.

`GetApi`'s `apiEndpoint` is odin's to answer, and odin answers with its real
published nginx port. So `aws_apigatewayv2_api.<n>.api_endpoint` is a URL a
human can genuinely curl, exactly as `elbv2ctl.endpoint_url` is for an ALB. That
forces the ordering: `CreateApi` converges the container SYNCHRONOUSLY, because
the provider reads `GetApi` immediately and stores the answer.
`compute/apigw.py` is built so this is safe -- one listener on port 80, so the
published host port never changes for the API's life and every later route
change is a config copy plus a SIGHUP.

`aws_apigatewayv2_stage.<n>.invoke_url` is a DIFFERENT thing and odin does not
control it. A first probe concluded the provider derived it from `apiEndpoint`,
because a throwaway stub had answered `apiEndpoint` with exactly the
`https://{apiId}.execute-api.{region}.amazonaws.com` string the provider
constructs -- so the two matched and the match was read as causation. Re-measured
against the real gateway, in one state file:

    aws_apigatewayv2_api.public_api  api_endpoint = "http://127.0.0.1:39999"
    aws_apigatewayv2_stage.default   invoke_url   = "https://api75a2c592.execute-api.us-east-1.amazonaws.com/"

The provider builds `invoke_url` client-side from the API id and the region and
never asks. Nothing odin returns can change it, so a canvas or an output that
wants the reachable address must read `api_endpoint`. Named in docs/limits.md
rather than left for someone to curl.

CONTRAST WITH elbv2ctl, WHICH CONVERGES IN THE BACKGROUND. An ALB has a real
terraform waiter (`LoadBalancerActive`) that polls `DescribeLoadBalancers` until
the state flips, so `CreateLoadBalancer` can answer `provisioning` and finish
later. **apigatewayv2 has no waiter at all** -- measured, `Creation complete
after 0s` -- so an API that answered with a placeholder endpoint would have that
placeholder written into terraform state and drift on the next plan forever.
There is no third option here: either the create blocks on a real container, or
odin lies about the endpoint.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from urllib.parse import unquote

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.compute.apigw import ApiProxy, ApiRoute, target_address
from odin.gateway import apigw_shim, errors
from odin.gateway.errors import exc_text
from odin.gateway.stores import NO_CHANGE, SynthStores

log = logging.getLogger("odin.gateway.apigw")

# The SigV4 CREDENTIAL SCOPE, not the botocore model name. See point 1 of the
# module docstring before changing this to `apigatewayv2`.
SERVICE = "apigateway"

_API_STATE_AVAILABLE = "AVAILABLE"
_API_STATE_FAILED = "FAILED"

# The one protocol odin serves. A `WEBSOCKET` API is refused at CreateApi rather
# than accepted and quietly served over HTTP: nginx would answer a websocket
# handshake with a 200 and the caller would hang.
_PROTOCOL_HTTP = "HTTP"
# The one payload format the shim implements. See `apigw_shim`'s docstring for
# why a 1.0 integration is refused instead of served a 2.0 event.
_PAYLOAD_FORMAT = "2.0"
_AWS_PROXY = "AWS_PROXY"
_HTTP_PROXY = "HTTP_PROXY"

# An HTTP_PROXY integration's URI names an ECS SERVICE through this suffix.
# `aws_apigatewayv2_integration.integration_uri` must be a URI, and an ecs
# service has no URL on real AWS either (an HTTP API reaches a private service
# through a VPC link, which odin does not model) -- so odin uses a hostname only
# odin resolves, and says so on the tile and in docs/limits.md rather than
# implying the generated file would work against Amazon.
ECS_HOST_SUFFIX = ".odin.internal"

_NODE_TAG = "odin:node"


def _mint_id(prefix: str) -> str:
    """Real apigatewayv2 ids are opaque lowercase alphanumerics. `secrets` keeps
    them unguessable, which matters more here than for most odin ids: an id is
    half of what addresses the invoke shim."""
    return f"{prefix}{secrets.token_hex(4)}"


def api_key(api_id: str) -> str:
    return f"api:{api_id}"


def _integration_key(api_id: str, integration_id: str) -> str:
    return f"integration:{api_id}:{integration_id}"


def _route_key_store(api_id: str, route_id: str) -> str:
    return f"route:{api_id}:{route_id}"


def _stage_key(api_id: str, stage_name: str) -> str:
    return f"stage:{api_id}:{stage_name}"


def _api(stores: SynthStores, env: str, api_id: str) -> dict | None:
    return stores.apigwctl.get(env, api_key(api_id))


def apis(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.apigwctl.items(env).items() if k.startswith("api:")]


def _integrations(stores: SynthStores, env: str, api_id: str) -> dict[str, dict]:
    prefix = f"integration:{api_id}:"
    return {k[len(prefix):]: v for k, v in stores.apigwctl.items(env).items() if k.startswith(prefix)}


def _routes(stores: SynthStores, env: str, api_id: str) -> dict[str, dict]:
    prefix = f"route:{api_id}:"
    return {k[len(prefix):]: v for k, v in stores.apigwctl.items(env).items() if k.startswith(prefix)}


def api_arn(api_id: str) -> str:
    return f"arn:aws:apigateway:{REGION}::/apis/{api_id}"


def _tags_for(stores: SynthStores, env: str, api_id: str) -> dict:
    return stores.tags.get(env, f"{SERVICE}:{api_arn(api_id)}", {})


def _set_tags(stores: SynthStores, env: str, api_id: str, tags: dict) -> None:
    stores.tags.set(env, f"{SERVICE}:{api_arn(api_id)}", tags)


# --- route keys ------------------------------------------------------------
#
# A route key is `"<METHOD> <path>"` or the literal `"$default"`. odin emits
# `ANY /<label>` and `ANY /<label>/{proxy+}` per target (see iac/hcl.py), and
# both reduce to the SAME nginx prefix -- which is the point: nginx's
# `location = /orders` + `location /orders/` pair is exactly those two route
# keys, so the pair round-trips as one thing.


def route_prefix(route_key: str) -> str:
    """The path prefix a route key owns: `/` for `$default`, else the path with
    any trailing greedy-proxy segment removed.

    `{proxy+}` is API Gateway's greedy path variable and is only ever the LAST
    segment. Stripping it is what makes `ANY /orders/{proxy+}` and `ANY /orders`
    agree on `/orders`. A non-greedy variable (`/items/{id}`) is NOT stripped --
    nginx would have to match a single segment and odin does not emit such a
    route, so it is left as a literal prefix rather than silently widened into
    something that matches more than the caller wrote."""
    if route_key == "$default":
        return "/"
    _method, _, path = route_key.partition(" ")
    path = (path or "/").rstrip("/")
    if path.endswith("/{proxy+}"):
        path = path[: -len("/{proxy+}")]
    return path or "/"


def _integration_id_of(route: dict) -> str | None:
    """`"integrations/int123"` -> `"int123"`. A route whose target is anything
    else (an authorizer, or nothing yet) has no integration and is skipped by
    the renderer rather than guessed at."""
    target = route.get("target") or ""
    return target.split("/", 1)[1] if target.startswith("integrations/") else None


def ecs_service_of(integration: dict) -> str | None:
    """The ECS service name an HTTP_PROXY integration's URI names, or None.

    The inverse of what `iac/hcl.py` writes (`http://<service>.odin.internal`),
    kept here so the two spellings live one function apart."""
    uri = integration.get("integration_uri") or ""
    host = uri.removeprefix("http://").removeprefix("https://").split("/")[0]
    return host[: -len(ECS_HOST_SUFFIX)] if host.endswith(ECS_HOST_SUFFIX) else None


def function_of(integration: dict) -> str | None:
    """The Lambda function name an AWS_PROXY integration's URI names, or None.

    The URI is `aws_lambda_function.x.invoke_arn`, which the provider computes
    client-side as
    `arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/{function_arn}/invocations`.
    A bare function ARN and a bare NAME are both accepted, because a
    hand-written project may use either and refusing one would be odin being
    stricter than AWS for no benefit."""
    uri = integration.get("integration_uri") or ""
    if "/functions/" in uri:
        uri = uri.split("/functions/", 1)[1].removesuffix("/invocations")
    return uri.rsplit(":function:", 1)[-1].split(":")[0] if uri else None


# --- the nginx route table -------------------------------------------------


def _shim_route(env: str, api: dict, route: dict, integration_id: str, gateway_port: int) -> ApiRoute:
    """One `location` pointed at the invoke shim on odin's own gateway port.

    `upstream_path` REPLACES the matched prefix in nginx, which is why the raw
    path has to travel in a header: after the rewrite the shim's own
    `request.url.path` is odin's internal URL, and building `rawPath` from it
    would hand the handler a path the caller never sent."""
    return ApiRoute(
        prefix=route_prefix(route["route_key"]),
        upstream=target_address(gateway_port),
        upstream_path=f"{apigw_shim.SHIM_PREFIX}/{env}/{api['api_id']}/{integration_id}",
        headers=(
            ("X-Odin-Api-Token", api["route_token"]),
            ("X-Odin-Raw-Path", "$request_uri"),
            ("X-Odin-Route-Key", f'"{route["route_key"]}"'),
        ),
    )


def route_table(stores: SynthStores, env: str, api: dict, gateway_port: int | None) -> tuple[ApiRoute, ...]:
    """Every route of one API as nginx `location` blocks.

    EVERY route goes to the shim, including HTTP_PROXY ones -- see
    `apigw_shim._proxy_http`'s docstring for why an ECS task's address is
    resolved per-request instead of baked in here.

    A route with no resolvable integration is DROPPED rather than rendered
    against a placeholder. That is deliberate and it is the difference between
    a 404 (nginx's catch-all: "this API has no such route") and a 502 (a route
    that exists and points at nothing). The first is true; the second invites
    someone to debug the wrong end.

    `gateway_port` of None means the gateway has not bound its port yet, which
    is only reachable in a test that builds the app without a lifespan. Every
    route needs it, so none is rendered and the API answers its honest 404."""
    if gateway_port is None:
        return ()
    integrations = _integrations(stores, env, api["api_id"])
    routes = []
    for route in _routes(stores, env, api["api_id"]).values():
        integration_id = _integration_id_of(route)
        if integration_id is None or integration_id not in integrations:
            continue
        routes.append(_shim_route(env, api, route, integration_id, gateway_port))
    # Two route keys (`ANY /x` and `ANY /x/{proxy+}`) reduce to one prefix, and
    # rendering both would be a duplicate nginx `location` -- a config nginx
    # refuses outright. First one wins; they are identical by construction.
    seen: dict[str, ApiRoute] = {}
    for route in routes:
        seen.setdefault(route.prefix, route)
    return tuple(seen.values())


def shim_target(
    stores: SynthStores, env: str, api_id: str, integration_id: str, token: str,
) -> apigw_shim.ShimTarget | str:
    """The shim's view of one integration, resolved FRESH on every request -- or
    a string naming why the request must be refused.

    A string rather than an exception because both outcomes are ordinary: a
    caller with the wrong token and a route whose integration was deleted are
    both things that happen, and neither is exceptional enough to unwind."""
    api = _api(stores, env, api_id)
    if api is None:
        return f"No API {api_id} in environment {env}"
    if not secrets.compare_digest(api.get("route_token", ""), token):
        return "Forbidden"
    integration = stores.apigwctl.get(env, _integration_key(api_id, integration_id))
    if integration is None:
        return f"No integration {integration_id} on API {api_id}"
    common = {
        "api_id": api_id,
        "route_key": "$default",
        "stage": apigw_shim.DEFAULT_STAGE,
    }
    if integration["integration_type"] == _AWS_PROXY:
        return apigw_shim.ShimTarget(**common, function_name=function_of(integration))
    service = ecs_service_of(integration)
    address = _ecs_address(stores, env, service)
    if address is None:
        return apigw_shim.ShimTarget(**common, unavailable=(
            f"The ECS service {service!r} this route targets has no running task with a published "
            "port, so there is nothing to forward to"
        ))
    return apigw_shim.ShimTarget(**common, http_upstream=address)


def _ecs_address(stores: SynthStores, env: str, service_name: str | None) -> str | None:
    """`host.docker.internal:{published port}` for the first RUNNING task of
    `service_name`, or None.

    Reads `stores.ecsctl` DIRECTLY rather than importing `ecsctl`, for one
    reason worth stating: `ecsctl` already imports `elbv2ctl`, and a model that
    imports another model is how an import cycle gets built one edge at a time.
    The key prefix and the two field names are ecsctl's own
    (`task:{cluster}:{id}` records with `last_status` and `host_ports`)."""
    if not service_name:
        return None
    for key, task in sorted(stores.ecsctl.items(env).items()):
        if not key.startswith("task:") or task.get("service_name") != service_name:
            continue
        if task.get("last_status") != "RUNNING":
            continue
        ports = [port for port in (task.get("host_ports") or {}).values() if port]
        if ports:
            return target_address(int(sorted(ports)[0]))
    return None


# --- converge --------------------------------------------------------------


async def converge(
    stores: SynthStores, env: str, api_id: str, proxy: ApiProxy, gateway_port: int | None,
) -> None:
    """Render this API's routes into its real nginx container and record the
    outcome. SYNCHRONOUS by design -- see the module docstring's contrast with
    elbv2ctl.

    Every failure lands as `state: FAILED` with the real reason, never as an
    exception escaping into the provider's error path: a raised
    `ProxyNotServing` there would surface to tofu as a 500 with no AWS error
    document, and the API record would keep whatever endpoint it had."""
    api = _api(stores, env, api_id)
    if api is None:
        return
    try:
        port = await proxy.ensure(stores.root, env, api["name"], route_table(stores, env, api, gateway_port))
    except Exception as exc:  # noqa: BLE001 -- deliberately broad; see docstring
        log.warning("apigw converge failed for %s (env %s): %s", api_id, env, exc_text(exc))
        _update_api(stores, env, api_id, state=_API_STATE_FAILED, state_reason=exc_text(exc), host_port=0)
        return
    _update_api(stores, env, api_id, state=_API_STATE_AVAILABLE, state_reason=None, host_port=port)


def _update_api(stores: SynthStores, env: str, api_id: str, **fields: object) -> None:
    def mutate(current: dict | None) -> dict | object:
        if current is None:  # deleted while a converge was mid-flight
            return NO_CHANGE
        return {**current, **fields}

    stores.apigwctl.update(env, api_key(api_id), mutate)


def endpoint_url(record: dict) -> str | None:
    """`http://127.0.0.1:{port}` for the API's real nginx container, or None.

    THE single place all three readers go through (the wire's `apiEndpoint`,
    `reconcile/tf_status.py`'s facts, and `gateway/wiring.py`'s producer facts) --
    `elbv2ctl.endpoint_url`'s rule, for its reason: a host port of 0 is not a
    port, it is "nothing is published", and handing a workload
    `http://127.0.0.1:0` is worse than withholding the fact. Gated on the state
    as well, so a FAILED converge's stale port is never called an address."""
    port = record.get("host_port") or 0
    return f"http://127.0.0.1:{port}" if port and record.get("state") == _API_STATE_AVAILABLE else None


# --- the wire --------------------------------------------------------------


def _json(payload: dict, status: int = 200) -> Response:
    """rest-json out. `None` values are dropped rather than serialized: real
    apigatewayv2 omits an unset member entirely, and a `null` where the SDK
    expects a string is a parse error on the caller's side."""
    return Response(
        json.dumps({k: v for k, v in payload.items() if v is not None}),
        status_code=status, media_type="application/json",
    )


def _not_found(what: str) -> Response:
    return errors.synth_error(SERVICE, "NotFoundException", what, 404)


def _bad_request(message: str) -> Response:
    return errors.synth_error(SERVICE, "BadRequestException", message, 400)


def _api_wire(record: dict, tags: dict) -> dict:
    """The `Api` shape, in the lower-camelCase the wire really uses."""
    return {
        "apiId": record["api_id"],
        "apiEndpoint": endpoint_url(record) or "",
        "apiKeySelectionExpression": record.get("api_key_selection_expression"),
        "createdDate": record["created_date"],
        "description": record.get("description"),
        "disableExecuteApiEndpoint": False,
        "name": record["name"],
        "protocolType": record["protocol_type"],
        "routeSelectionExpression": record.get("route_selection_expression"),
        "tags": tags,
        "version": record.get("version"),
    }


def _integration_wire(integration_id: str, record: dict) -> dict:
    return {
        "integrationId": integration_id,
        "connectionType": record.get("connection_type") or "INTERNET",
        "description": record.get("description"),
        "integrationMethod": record.get("integration_method"),
        "integrationType": record["integration_type"],
        "integrationUri": record.get("integration_uri"),
        "payloadFormatVersion": record.get("payload_format_version"),
        "timeoutInMillis": record.get("timeout_in_millis") or 30000,
    }


def _route_wire(route_id: str, record: dict) -> dict:
    return {
        "routeId": route_id,
        "apiKeyRequired": record.get("api_key_required", False),
        "authorizationType": record.get("authorization_type") or "NONE",
        "operationName": record.get("operation_name"),
        "routeKey": record["route_key"],
        "target": record.get("target"),
    }


def _stage_wire(record: dict) -> dict:
    return {
        "stageName": record["stage_name"],
        "autoDeploy": record.get("auto_deploy", False),
        "createdDate": record["created_date"],
        "lastUpdatedDate": record["created_date"],
        "description": record.get("description"),
        "defaultRouteSettings": {},
        "routeSettings": {},
        "stageVariables": {},
        "tags": {},
    }


def _body(body: bytes) -> dict:
    """The request document, or `{}`. A DELETE carries no body and a malformed
    one is the caller's problem, not a 500 here."""
    text = body.decode("utf-8", "replace").strip()
    return json.loads(text) if text.startswith("{") else {}


# --- handlers --------------------------------------------------------------


async def _create_api(ctx: _Ctx) -> Response:
    payload = ctx.payload
    protocol = payload.get("protocolType") or _PROTOCOL_HTTP
    if protocol != _PROTOCOL_HTTP:
        # Refused rather than served over HTTP: nginx answers a websocket
        # handshake with a 200 and the caller hangs waiting for an upgrade that
        # never comes -- a green create with a dead endpoint.
        return _bad_request(
            f"odin serves HTTP APIs only; protocolType {protocol!r} has no substrate "
            "(a WEBSOCKET API would need a proxy that speaks the upgrade handshake)"
        )
    api_id = _mint_id("api")
    record = {
        "api_id": api_id,
        "name": payload.get("name") or api_id,
        "protocol_type": protocol,
        "api_key_selection_expression": payload.get("apiKeySelectionExpression"),
        "route_selection_expression": payload.get("routeSelectionExpression"),
        "description": payload.get("description"),
        "version": payload.get("version"),
        "created_date": _iso(ctx.now),
        # Minted once, at create, and never rotated: it is what nginx replays to
        # the invoke shim, and rotating it would silently break every already
        # rendered config until the next converge.
        "route_token": apigw_shim.mint_route_token(),
        "state": _API_STATE_FAILED,
        "state_reason": "not converged yet",
        "host_port": 0,
    }
    ctx.stores.apigwctl.set(ctx.env, api_key(api_id), record)
    _set_tags(ctx.stores, ctx.env, api_id, payload.get("tags") or {})
    # SYNCHRONOUS, unlike elbv2ctl's background converge: the provider reads
    # GetApi immediately and stores whatever `apiEndpoint` says, and there is no
    # waiter to correct it later (module docstring).
    await converge(ctx.stores, ctx.env, api_id, ctx.proxy, ctx.gateway_port)
    fresh = _api(ctx.stores, ctx.env, api_id)
    if fresh["state"] != _API_STATE_AVAILABLE:
        # The API record is KEPT, not rolled back: `DeleteApi` must still work,
        # and the reason is what `reconcile/tf_status.py` renders as the node's
        # crash verdict.
        return errors.synth_error(
            SERVICE, "InternalServerErrorException",
            f"the API's proxy did not come up: {fresh['state_reason']}", 500,
        )
    return _json(_api_wire(fresh, _tags_for(ctx.stores, ctx.env, api_id)), status=201)


async def _get_api(ctx: _Ctx) -> Response:
    record = _api(ctx.stores, ctx.env, ctx.api_id)
    if record is None:
        return _not_found(f"No API found with id {ctx.api_id}")
    return _json(_api_wire(record, _tags_for(ctx.stores, ctx.env, ctx.api_id)))


async def _update_api_handler(ctx: _Ctx) -> Response:
    record = _api(ctx.stores, ctx.env, ctx.api_id)
    if record is None:
        return _not_found(f"No API found with id {ctx.api_id}")
    changes = {
        field: ctx.payload[wire] for wire, field in _API_UPDATABLE.items() if wire in ctx.payload
    }
    _update_api(ctx.stores, ctx.env, ctx.api_id, **changes)
    if "name" in changes and changes["name"] != record["name"]:
        # The container is named after the API, so a rename is a NEW container.
        # The old one is removed first or it keeps the host port and the new one
        # cannot bind... which docker would allow (it picks another port) while
        # leaving a stranded container behind forever.
        await ctx.proxy.destroy(ctx.env, record["name"])
    await converge(ctx.stores, ctx.env, ctx.api_id, ctx.proxy, ctx.gateway_port)
    return _json(_api_wire(_api(ctx.stores, ctx.env, ctx.api_id), _tags_for(ctx.stores, ctx.env, ctx.api_id)))


_API_UPDATABLE = {
    "name": "name",
    "description": "description",
    "version": "version",
    "routeSelectionExpression": "route_selection_expression",
    "apiKeySelectionExpression": "api_key_selection_expression",
}


async def _delete_api(ctx: _Ctx) -> Response:
    record = _api(ctx.stores, ctx.env, ctx.api_id)
    if record is None:
        return _not_found(f"No API found with id {ctx.api_id}")
    for key in [k for k in ctx.stores.apigwctl.items(ctx.env) if k.endswith(f":{ctx.api_id}") or f":{ctx.api_id}:" in k]:
        ctx.stores.apigwctl.delete(ctx.env, key)
    ctx.stores.apigwctl.delete(ctx.env, api_key(ctx.api_id))
    await ctx.proxy.destroy(ctx.env, record["name"])
    return Response(status_code=204)


async def _create_integration(ctx: _Ctx) -> Response:
    if _api(ctx.stores, ctx.env, ctx.api_id) is None:
        return _not_found(f"No API found with id {ctx.api_id}")
    payload = ctx.payload
    integration_type = payload.get("integrationType") or ""
    if integration_type not in (_AWS_PROXY, _HTTP_PROXY):
        return _bad_request(
            f"odin models AWS_PROXY (a lambda) and HTTP_PROXY (an ecs service) integrations; "
            f"integrationType {integration_type!r} has no substrate"
        )
    payload_format = payload.get("payloadFormatVersion") or _PAYLOAD_FORMAT
    if integration_type == _AWS_PROXY and payload_format != _PAYLOAD_FORMAT:
        # Refused rather than served a 2.0 event: a 1.0 handler reads
        # `event["httpMethod"]`, which a 2.0 event does not have, so the mismatch
        # surfaces as a KeyError inside the user's own function and blames their
        # code for odin's choice.
        return _bad_request(
            f"odin's invoke shim implements payload format {_PAYLOAD_FORMAT} only; "
            f"{payload_format!r} would hand the handler an event of the wrong shape"
        )
    integration_id = _mint_id("int")
    record = {
        "integration_type": integration_type,
        "integration_uri": payload.get("integrationUri"),
        "integration_method": payload.get("integrationMethod"),
        "connection_type": payload.get("connectionType") or "INTERNET",
        "description": payload.get("description"),
        "payload_format_version": payload_format if integration_type == _AWS_PROXY else None,
        "timeout_in_millis": payload.get("timeoutInMillis") or 30000,
    }
    ctx.stores.apigwctl.set(ctx.env, _integration_key(ctx.api_id, integration_id), record)
    await converge(ctx.stores, ctx.env, ctx.api_id, ctx.proxy, ctx.gateway_port)
    return _json(_integration_wire(integration_id, record), status=201)


async def _get_integration(ctx: _Ctx) -> Response:
    record = ctx.stores.apigwctl.get(ctx.env, _integration_key(ctx.api_id, ctx.child_id))
    if record is None:
        return _not_found(f"No integration found with id {ctx.child_id}")
    return _json(_integration_wire(ctx.child_id, record))


async def _update_integration(ctx: _Ctx) -> Response:
    key = _integration_key(ctx.api_id, ctx.child_id)
    record = ctx.stores.apigwctl.get(ctx.env, key)
    if record is None:
        return _not_found(f"No integration found with id {ctx.child_id}")
    changes = {field: ctx.payload[wire] for wire, field in _INTEGRATION_UPDATABLE.items() if wire in ctx.payload}
    record = {**record, **changes}
    ctx.stores.apigwctl.set(ctx.env, key, record)
    await converge(ctx.stores, ctx.env, ctx.api_id, ctx.proxy, ctx.gateway_port)
    return _json(_integration_wire(ctx.child_id, record))


_INTEGRATION_UPDATABLE = {
    "integrationUri": "integration_uri",
    "integrationMethod": "integration_method",
    "description": "description",
    "timeoutInMillis": "timeout_in_millis",
}


async def _delete_integration(ctx: _Ctx) -> Response:
    key = _integration_key(ctx.api_id, ctx.child_id)
    if ctx.stores.apigwctl.get(ctx.env, key) is None:
        return _not_found(f"No integration found with id {ctx.child_id}")
    ctx.stores.apigwctl.delete(ctx.env, key)
    await converge(ctx.stores, ctx.env, ctx.api_id, ctx.proxy, ctx.gateway_port)
    return Response(status_code=204)


async def _create_route(ctx: _Ctx) -> Response:
    if _api(ctx.stores, ctx.env, ctx.api_id) is None:
        return _not_found(f"No API found with id {ctx.api_id}")
    route_key_value = ctx.payload.get("routeKey") or ""
    if not route_key_value:
        return _bad_request("routeKey is required")
    route_id = _mint_id("rt")
    record = {
        "route_key": route_key_value,
        "target": ctx.payload.get("target"),
        "authorization_type": ctx.payload.get("authorizationType") or "NONE",
        "api_key_required": bool(ctx.payload.get("apiKeyRequired", False)),
        "operation_name": ctx.payload.get("operationName"),
    }
    ctx.stores.apigwctl.set(ctx.env, _route_key_store(ctx.api_id, route_id), record)
    await converge(ctx.stores, ctx.env, ctx.api_id, ctx.proxy, ctx.gateway_port)
    return _json(_route_wire(route_id, record), status=201)


async def _get_route(ctx: _Ctx) -> Response:
    record = ctx.stores.apigwctl.get(ctx.env, _route_key_store(ctx.api_id, ctx.child_id))
    if record is None:
        return _not_found(f"No route found with id {ctx.child_id}")
    return _json(_route_wire(ctx.child_id, record))


async def _update_route(ctx: _Ctx) -> Response:
    key = _route_key_store(ctx.api_id, ctx.child_id)
    record = ctx.stores.apigwctl.get(ctx.env, key)
    if record is None:
        return _not_found(f"No route found with id {ctx.child_id}")
    changes = {field: ctx.payload[wire] for wire, field in _ROUTE_UPDATABLE.items() if wire in ctx.payload}
    record = {**record, **changes}
    ctx.stores.apigwctl.set(ctx.env, key, record)
    await converge(ctx.stores, ctx.env, ctx.api_id, ctx.proxy, ctx.gateway_port)
    return _json(_route_wire(ctx.child_id, record))


_ROUTE_UPDATABLE = {
    "routeKey": "route_key",
    "target": "target",
    "authorizationType": "authorization_type",
    "operationName": "operation_name",
}


async def _delete_route(ctx: _Ctx) -> Response:
    key = _route_key_store(ctx.api_id, ctx.child_id)
    if ctx.stores.apigwctl.get(ctx.env, key) is None:
        return _not_found(f"No route found with id {ctx.child_id}")
    ctx.stores.apigwctl.delete(ctx.env, key)
    await converge(ctx.stores, ctx.env, ctx.api_id, ctx.proxy, ctx.gateway_port)
    return Response(status_code=204)


async def _create_stage(ctx: _Ctx) -> Response:
    if _api(ctx.stores, ctx.env, ctx.api_id) is None:
        return _not_found(f"No API found with id {ctx.api_id}")
    stage_name = ctx.payload.get("stageName") or ""
    if not stage_name:
        return _bad_request("stageName is required")
    record = {
        "stage_name": stage_name,
        "auto_deploy": bool(ctx.payload.get("autoDeploy", False)),
        "description": ctx.payload.get("description"),
        "created_date": _iso(ctx.now),
    }
    ctx.stores.apigwctl.set(ctx.env, _stage_key(ctx.api_id, stage_name), record)
    # No converge: a stage changes nothing about routing. odin serves the
    # `$default` stage's paths at the API's root, which is what `$default` means
    # -- and it is the only stage `iac/hcl.py` emits. A stage with any other
    # name is STORED (so tofu plans clean) and serves nothing, which
    # docs/limits.md says out loud rather than leaving to be discovered.
    return _json(_stage_wire(record), status=201)


async def _get_stage(ctx: _Ctx) -> Response:
    record = ctx.stores.apigwctl.get(ctx.env, _stage_key(ctx.api_id, ctx.child_id))
    if record is None:
        return _not_found(f"No stage found with name {ctx.child_id}")
    return _json(_stage_wire(record))


async def _update_stage(ctx: _Ctx) -> Response:
    key = _stage_key(ctx.api_id, ctx.child_id)
    record = ctx.stores.apigwctl.get(ctx.env, key)
    if record is None:
        return _not_found(f"No stage found with name {ctx.child_id}")
    record = {**record, **({"auto_deploy": bool(ctx.payload["autoDeploy"])} if "autoDeploy" in ctx.payload else {})}
    ctx.stores.apigwctl.set(ctx.env, key, record)
    return _json(_stage_wire(record))


async def _delete_stage(ctx: _Ctx) -> Response:
    key = _stage_key(ctx.api_id, ctx.child_id)
    if ctx.stores.apigwctl.get(ctx.env, key) is None:
        return _not_found(f"No stage found with name {ctx.child_id}")
    ctx.stores.apigwctl.delete(ctx.env, key)
    return Response(status_code=204)


def _iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


# --- dispatch --------------------------------------------------------------


class _Ctx:
    """One request's inputs, assembled once. A small object rather than eight
    positional parameters repeated across sixteen handlers -- the shape
    `elbv2ctl` reaches for with `(params, env, stores, proxy)` and this needs
    two more of."""

    __slots__ = ("stores", "env", "payload", "now", "proxy", "gateway_port", "api_id", "child_id")

    def __init__(
        self, stores: SynthStores, env: str, payload: dict, now: float,
        proxy: ApiProxy, gateway_port: int | None, api_id: str, child_id: str,
    ) -> None:
        self.stores, self.env, self.payload, self.now = stores, env, payload, now
        self.proxy, self.gateway_port = proxy, gateway_port
        self.api_id, self.child_id = api_id, child_id


_Handler = Callable[[_Ctx], Awaitable[Response]]
_HANDLERS: dict[str, _Handler] = {
    "CreateApi": _create_api,
    "GetApi": _get_api,
    "UpdateApi": _update_api_handler,
    "DeleteApi": _delete_api,
    "CreateIntegration": _create_integration,
    "GetIntegration": _get_integration,
    "UpdateIntegration": _update_integration,
    "DeleteIntegration": _delete_integration,
    "CreateRoute": _create_route,
    "GetRoute": _get_route,
    "UpdateRoute": _update_route,
    "DeleteRoute": _delete_route,
    "CreateStage": _create_stage,
    "GetStage": _get_stage,
    "UpdateStage": _update_stage,
    "DeleteStage": _delete_stage,
}


async def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    proxy: ApiProxy | None = None, gateway_port: int | None = None, path: str = "",
) -> Response:
    """`synth.pure_answer`'s `apigateway:` branch.

    `path` is threaded in because this is a REST service: the api id and the
    child id live in the URL, not the body, and `classify` has already parsed
    them once. Re-parsing here rather than widening classify's return type keeps
    that function's `(action, resource)` contract intact -- the same call
    `classify` makes to extract the resource, made again for the ids."""
    op = action.removeprefix(f"{SERVICE}:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error(SERVICE, "BadRequestException", f"The action {op} is not valid.", 400)
    api_id, child_id = path_ids(path)
    ctx = _Ctx(stores, env, _body(body), now, proxy or ApiProxy(), gateway_port, api_id, child_id)
    return await handler(ctx)


def path_ids(path: str) -> tuple[str, str]:
    """`(apiId, childId)` from a `/v2/apis/...` path -- `("", "")` for one that
    carries neither.

    BOTH IDS ARE PERCENT-DECODED, and getting that wrong cost a real bug that a
    unit test would not have found. `gateway/app.py` hands this the RAW,
    percent-encoded path (`_raw_target`, which SigV4 canonicalization requires),
    and the AWS SDK **encodes the `$` in `$default`**. So a stage created as
    `$default` from the request BODY was then looked up as `%24default` from the
    PATH, and `tofu apply` failed with:

        Error: reading API Gateway v2 Stage ($default): couldn't find resource

    This docstring previously asserted the opposite -- that the provider sends
    the `$` unencoded, "measured on the wire". It was measured on Starlette's
    `request.url.path`, which is already DECODED. Re-measured against the real
    raw path:

        op=CreateStage raw_path='/v2/apis/apid0a48523/stages'          child_id=''
        op=GetStage    raw_path='/v2/apis/apid0a48523/stages/%24default' child_id='%24default'

    A confident comment about a value read one layer away from the wire is the
    same mistake as a guard reading a signal that never arrives; it is written
    out here so the next reader does not re-derive it from the wrong layer."""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 3 or segments[0] != "v2" or segments[1] != "apis":
        return "", ""
    return unquote(segments[2]), unquote(segments[4]) if len(segments) > 4 else ""


def account_arn(api_id: str) -> str:
    """The `execution_arn` shape, for anything that needs to name the API in a
    policy. `ACCOUNT` is odin's one account id everywhere."""
    return f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}"
