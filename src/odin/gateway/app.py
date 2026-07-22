"""The odin gateway: the checking reverse proxy every workload's AWS SDK
call passes through (PRD C1.5 -- ".../research-iam-gateway.md" §1/§Q4
productionized).

One catch-all Starlette route per SigV4-signed request: identify the
caller and target service from the credential scope (works even before/if
verification fails, so an auth-failure response can still be
protocol-shaped) -> verify the signature -> map the request to an
(action, resource) pair -> check the caller's edge-compiled policy ->
forward to the env's backing container. S3 (RustFS) enforces SigV4, so its
forward is re-signed with the backing's own credentials; sqs/sns/dynamodb
(goaws/dynalite) ignore auth entirely, so their forward passes the
caller's original signed headers through untouched (research §Q4). Every
deny -- auth failure, unmappable request, policy deny, or a backing that
isn't up -- fires `on_deny` so it becomes a debuggable `access_denied`
event (PRD R6) rather than a silent failure.

The gateway itself holds no data beyond this request-scoped routing table
(GatewayState) -- rebuilt wholesale by `update()` on every Apply/tick,
never a cache that outlives one (PRD: "the gateway is stateless"). It DOES
hold a `SynthStores` (see gateway/stores.py) for the synthesized
control-plane (gateway/synth.py, S-plan task S1) -- tags, SNS topic
attributes, delete-confirmation markers -- which is the deliberate opposite:
it must OUTLIVE a tick, so it's never rebuilt by `update()`.

STS is a deliberate exception to the verify -> classify -> evaluate ->
forward pipeline: GetCallerIdentity isn't scoped to any canvas resource (no
edge could ever grant/deny it), so it's answered right after verify(),
before classify() even runs -- see synth.py's module docstring for the full
justification. Every other synth-owned action (sqs/sns/dynamodb tag CRUD,
SNS topic attributes, delete-confirmation shims) DOES go through the normal
classify -> evaluate gate; synth only decides whether an ALLOWED request is
answered directly or forwarded -- "synth never bypasses policy" (S1 brief).
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from odin.aws.backings import ACCESS_KEY as BACKING_ACCESS_KEY
from odin.aws.backings import REGION as BACKING_REGION
from odin.aws.backings import SECRET_KEY as BACKING_SECRET_KEY
from odin.gateway import classify as classify_mod
from odin.gateway import errors, sigv4, synth
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore, Principal
from odin.gateway.policy import Statement, evaluate
from odin.gateway.stores import SynthStores

# Stripped before ANY forward -- hop-by-hop / connection-specific, never
# meaningful to replay to the backing (httpx recomputes content-length and
# resolves the true Host from the forward URL itself).
_HOP_BY_HOP = {"host", "content-length", "transfer-encoding", "connection"}
# ALSO stripped before an s3 re-sign, since resign() computes fresh ones
# under the backing's own credentials.
_SIGNING_HEADERS = {"authorization", "x-amz-date", "x-amz-content-sha256"}
_RESPONSE_HOP_BY_HOP = {"transfer-encoding", "connection"}

OnDeny = Callable[[Principal | None, str | None, str | None, str], Awaitable[None]]


@dataclass
class _EnvGatewayState:
    statements_by_node: dict[str, list[Statement]] = field(default_factory=dict)
    backing_ports: dict[str, int] = field(default_factory=dict)


# The OPERATOR principal (S2 CONTRACT ADDENDUM): full allow within whatever
# env issued its keys, WITHOUT any canvas edge -- a tofu run isn't a
# workload node, so it can never have a compiled iam statement of its own.
# Special-cased in `statements_for` (never entered into
# `_EnvGatewayState.statements_by_node`), so `update()` wholesale-rebuilding
# that map every reconciler tick can never touch it -- workload principals
# are unaffected either way.
_OPERATOR_STATEMENTS: list[Statement] = [Statement(actions=("*",), resources=("*",))]


class GatewayState:
    """Per-env edge-compiled policies + backing routing table.

    `update()` replaces an env's entry wholesale (never patched
    incrementally) so a stale edge can never survive a reconcile pass --
    the same "no cache that outlives an Apply" invariant as the rest of
    the gateway. All lookups are env-scoped; an unknown env behaves as
    empty (default-deny, no backing registered) rather than raising --
    except the OPERATOR principal (`gateway.keys.OPERATOR_NODE_ID`), which
    always resolves to full-allow (see `_OPERATOR_STATEMENTS` above).
    """

    def __init__(self) -> None:
        self._envs: dict[str, _EnvGatewayState] = {}

    def update(self, env: str, statements_by_node: dict[str, list[Statement]], backing_ports: dict[str, int]) -> None:
        self._envs[env] = _EnvGatewayState(statements_by_node=statements_by_node, backing_ports=backing_ports)

    def statements_for(self, env: str, node_id: str) -> list[Statement]:
        if node_id == OPERATOR_NODE_ID:
            return _OPERATOR_STATEMENTS
        env_state = self._envs.get(env)
        return env_state.statements_by_node.get(node_id, []) if env_state else []

    def backing_port(self, env: str, service: str) -> int | None:
        env_state = self._envs.get(env)
        return env_state.backing_ports.get(service) if env_state else None


def _strip(headers: dict[str, str], drop: set[str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _raw_target(request: Request) -> tuple[str, str]:
    """(raw percent-encoded path, raw query string) exactly as sent on the
    wire. SigV4 verification and the forward URL must use these, not
    ASGI's decoded `request.url.path` -- botocore's canonicalization needs
    the request's ORIGINAL percent-encoding (research §Q1)."""
    return request.scope["raw_path"].decode("latin-1"), request.scope["query_string"].decode("latin-1")


def create_gateway_app(
    state: GatewayState,
    keystore: KeyStore,
    stores: SynthStores,
    on_deny: OnDeny,
    forward_client: httpx.AsyncClient | None = None,
) -> Starlette:
    """The gateway ASGI app. `forward_client` lets tests substitute a
    fake-backing transport (httpx.ASGITransport over a recording ASGI
    echo) for the real one used in production."""
    client = forward_client or httpx.AsyncClient()

    async def catch_all(request: Request) -> Response:
        body = await request.body()
        headers = dict(request.headers.items())
        path, query = _raw_target(request)
        incoming_url = f"http://{headers.get('host', '')}{path}" + (f"?{query}" if query else "")

        identified = sigv4.identify(headers)
        access_key, service = (identified[0], identified[2]) if identified else (None, "s3")

        principal = keystore.lookup(access_key) if access_key else None
        if principal is None:
            await on_deny(None, None, None, "unknown-key")
            return errors.auth_error(service, "InvalidClientTokenId", "The AWS access key was not found")

        if sigv4.verify(request.method, incoming_url, headers, body, keystore.secret_for) is None:
            await on_deny(principal, None, None, "bad-signature")
            return errors.auth_error(service, "SignatureDoesNotMatch", "The request signature did not match")

        if service == "sts":
            # Not resource-scoped -- no edge could ever grant/deny this, so
            # it's answered for any verified principal (see module + synth.py
            # docstrings).
            return synth.get_caller_identity(principal.env, principal)

        query_params = dict(parse_qsl(query, keep_blank_values=True))
        classified = classify_mod.classify(service, request.method, path, query_params, headers, body)
        if classified is None:
            await on_deny(principal, None, None, "unmappable-action")
            return errors.access_denied(service, "unmappable-action")

        action, resource = classified
        statements = state.statements_for(principal.env, principal.node_id)
        if not evaluate(statements, action, resource):
            await on_deny(principal, action, resource, "denied")
            return errors.access_denied(service, action)

        now = time.monotonic()
        pure = synth.pure_answer(action, resource, principal.env, body, stores, now)
        if pure is not None:
            return pure

        backing_port = state.backing_port(principal.env, service)
        if backing_port is None:
            await on_deny(principal, action, resource, "backing-unavailable")
            return errors.service_unavailable(service)

        backing_url = f"http://127.0.0.1:{backing_port}{path}" + (f"?{query}" if query else "")
        forward_headers = _strip(headers, _HOP_BY_HOP)
        if service == "s3":
            forward_headers = sigv4.resign(
                request.method, backing_url, _strip(forward_headers, _SIGNING_HEADERS), body,
                BACKING_ACCESS_KEY, BACKING_SECRET_KEY, "s3", BACKING_REGION,
            )
        upstream = await client.request(request.method, backing_url, headers=forward_headers, content=body)
        response_body = upstream.content
        response_headers = _strip(dict(upstream.headers), _RESPONSE_HOP_BY_HOP)
        if upstream.status_code < 300 and synth.is_postprocess_action(action):
            rewritten = synth.postprocess(action, resource, principal.env, body, response_body, stores, headers.get("host", ""), now)
            if rewritten != response_body:
                response_headers["content-length"] = str(len(rewritten))
            response_body = rewritten
        return Response(content=response_body, status_code=upstream.status_code, headers=response_headers)

    methods = ["GET", "PUT", "POST", "DELETE", "HEAD"]
    return Starlette(routes=[
        Route("/", catch_all, methods=methods),
        Route("/{path:path}", catch_all, methods=methods),
    ])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_serving(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"gateway did not start on :{port} within {timeout}s")


def serve_in_thread(app: Starlette, host: str = "0.0.0.0", port: int = 0) -> tuple[uvicorn.Server, threading.Thread, int]:
    """Run `app` on a background uvicorn thread -- the in-process
    second-listener pattern (the deleted `aws/embed.py::start_ministack`'s
    shape). `port=0` resolves a free port up front (same trick that
    function used) so the actual bound port is known before uvicorn ever
    starts, rather than needing to introspect it from another thread
    afterward. Returns (server, thread, actual_port); stop via
    `stop_in_thread`."""
    actual_port = port or _free_port()
    config = uvicorn.Config(app, host=host, port=actual_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="odin-gateway", daemon=True)
    thread.start()
    _wait_until_serving(actual_port)
    return server, thread, actual_port


def stop_in_thread(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)
