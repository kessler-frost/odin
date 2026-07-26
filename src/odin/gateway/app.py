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
deny -- auth failure, unmappable request, policy deny -- fires `on_deny` so
it becomes a debuggable `access_denied` event (PRD R6) rather than a silent
failure. A backing that ISN'T UP is deliberately NOT one of those (field test
2, finding B6): the policy check has already passed and the answer is a real
503, so it fires `on_unavailable` and lands as its own event type instead of
polluting the authorization audit stream.

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

import asyncio
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
# Field test 2, finding B6: a BACKING BEING DOWN is not an authorization
# verdict, and must not ride the `access_denied` stream a security review
# reads. Same call shape as `OnDeny` (so server.py's two closures are twins),
# deliberately a SEPARATE seam so the two conditions can never be conflated
# again -- the policy check has already PASSED by the time this fires.
OnUnavailable = Callable[[Principal | None, str | None, str | None, str], Awaitable[None]]


async def _ignore_unavailable(
    principal: Principal | None, action: str | None, resource: str | None, service: str,
) -> None:
    """The default `on_unavailable`: the ~20 unit tests that only exercise the
    DENY stream shouldn't each have to pass a second sink. Production
    (`server.py`) always passes a real one, and
    `tests/gateway/test_proxy.py::test_backing_unavailable_...` asserts it."""
    return None


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
    gateway_port: Callable[[], int | None] | None = None,
    rds=None,
    on_unavailable: OnUnavailable = _ignore_unavailable,
) -> Starlette:
    """The gateway ASGI app. `forward_client` lets tests substitute a
    fake-backing transport (httpx.ASGITransport over a recording ASGI
    echo) for the real one used in production. `gateway_port` is a zero-arg
    callable (never a plain int) for the SAME reason server.py's own
    `create_apply_router`/`create_tf_router` take one: this app is built
    BEFORE uvicorn resolves its own actual bound port (server.py's
    `serve_in_thread` -- port=0 resolves lazily), so the value can only be
    read at request time. Fix-wave 2b finding #2: threaded down to
    ec2compute/ecsctl/lambdactl (via synth.pure_answer) so a workload
    substrate's injected AWS_ENDPOINT_URL points at THIS gateway's real
    port, never a stale/guessed one. `rds` (task W2.7) is the RDS model's
    substrate seam -- a `PostgresRds`-shaped stand-in for tests, None in
    production (the model builds a per-env real one itself; see
    gateway/models/rdsctl.py::_substrate)."""
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
            return errors.access_denied(service, action, resource)

        now = time.monotonic()
        # Computed once, ahead of pure_answer: ecr's control-plane model
        # (all-synth, like ec2/iam) still needs the registry:2 backing's
        # OWN live port to build repositoryUri (gateway/models/ecr.py),
        # threaded through as the same value the forward path below would
        # otherwise look up on its own.
        backing_port = state.backing_port(principal.env, service)
        # `lambda:Invoke` runs the function's handler synchronously inside its
        # REAL RIE container -- a blocking wait up to the function's timeout
        # (compute/functions.py). Run ON the event loop it would freeze the
        # whole gateway for the duration, so the handler's OWN re-entrant AWS
        # calls back through here (a boto3 PutItem/PutObject during the
        # invocation) could never be accepted -- they'd time out and the invoke
        # would return empty (field-test finding #1). Hand JUST that action to a
        # worker thread so the loop stays free to serve the re-entrant calls;
        # every other synth answer is fast in-memory work (the substrate-booting
        # ones already return immediately, finishing on their own daemon
        # thread), kept inline to avoid a needless thread hop on the hot forward
        # path (the re-entrant PutItem/PutObject calls themselves).
        answer = lambda: synth.pure_answer(  # noqa: E731
            action, resource, principal.env, body, stores, now, backing_port, query_params,
            keystore=keystore, gateway_port=gateway_port() if gateway_port else None, rds=rds,
        )
        pure = await asyncio.to_thread(answer) if action == "lambda:Invoke" else answer()
        if pure is not None:
            return pure

        if backing_port is None:
            # NOT a denial (finding B6): the policy check above already PASSED,
            # and the response is a real 503/`ServiceUnavailable`. Reporting it
            # through `on_deny` put a service-unavailable condition in an
            # authorization costume and flooded the `access_denied` audit
            # stream during a wedged destroy. `resource` is the AWS resource the
            # call named; `service` (the last argument) is what is actually
            # down.
            await on_unavailable(principal, action, resource, service)
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


def _bound_socket(host: str, port: int) -> socket.socket:
    """Bind NOW and hand the live socket to uvicorn, so the port cannot be
    stolen between choosing it and serving on it.

    `port=0` used to be resolved by binding a throwaway socket, reading its
    number, and CLOSING it -- uvicorn then bound the same number a moment
    later. Anything else on the machine could take it in that window, and
    under parallel runs something did (intermittent EADDRINUSE, and the
    symptom was a confusing 15s `_wait_until_serving` timeout rather than a
    bind error). Holding the socket removes the window entirely.

    An explicitly requested port still fails loudly here: a caller who asked
    for 4266 must not silently be given something else."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


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
    `stop_in_thread`.

    Security finding #1 NOTE: `host="0.0.0.0"` here is deliberate and NOT
    the same bug as the control app's old default (see __main__.py) --
    containers (a workload's AWS SDK, tofu's own provider) reach this
    gateway via `host.docker.internal`, which resolves to the HOST's real
    interface, not its loopback; a `127.0.0.1` bind would be unreachable
    from inside a container. This is safe specifically because every
    request here is SigV4-verified (`catch_all` -> `sigv4.verify`, this
    module's own docstring) before anything is classified or forwarded --
    unlike the control app, which has no equivalent per-request check."""
    sock = _bound_socket(host, port)
    actual_port = sock.getsockname()[1]
    config = uvicorn.Config(app, host=host, port=actual_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), name="odin-gateway", daemon=True)
    thread.start()
    _wait_until_serving(actual_port)
    return server, thread, actual_port


def stop_in_thread(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)
