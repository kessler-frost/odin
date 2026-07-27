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
import contextlib
import socket
import threading
import time
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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

log = logging.getLogger("odin.gateway")

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
    BEFORE uvicorn resolves its own actual bound port (the lifespan's
    `serve_on_loop` -- port=0 resolves lazily), so the value can only be
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
        # So `_unhandled_failure` can answer in THIS service's wire format. Set
        # here, the earliest point it is known, because the handler's whole job
        # is to cover paths that never reached their own error return.
        request.state.service = service

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
        # worker thread so the loop stays free to serve the re-entrant calls.
        #
        # THE SENTENCE THAT USED TO FOLLOW WAS FALSE, and v0.7.7's de-threading
        # is what made it matter. It read "every other synth answer is fast
        # in-memory work"; a full trace of `synth.pure_answer`'s dispatch found
        # roughly nine actions that shell out to `docker` or `nebula-cert` on
        # this very line -- `ecs:DescribeServices`/`ListTasks`/`DescribeTasks`
        # (a `docker logs` per task plus up to two `docker inspect` per running
        # task, and terraform's steady-state waiter POLLS these),
        # `ecs:DeleteService`, `lambda:DeleteFunction`,
        # `elasticloadbalancing:DeleteLoadBalancer`/`DescribeTargetHealth` (a
        # SYNC `httpx.get` per target, 0.3s timeout each), `ec2:CreateVpc` (two
        # `nebula-cert` runs), `ec2:CreateKeyPair` (`ssh-keygen`), and
        # `rds:ModifyDBInstance` (a psycopg2 connect, `connect_timeout=3`).
        # Measured on this machine rather than guessed: `docker inspect` 11.0ms
        # median, `docker logs --tail 20` 9.9ms, `docker run -d` 69.7ms,
        # `docker rm -f -v` 78.6ms, `ssh-keygen -t rsa -b 2048` 48.3ms. Until
        # those go async they block the CONTROL app's loop too, not just this
        # one. They are deliberately NOT wrapped in a thread here -- a hidden
        # thread is what v0.7.7 is removing -- and are recorded as the honest
        # boundary for the gateway-models stage that follows.
        #
        # This one stays a thread for now because it is the only site whose
        # blocking is UNBOUNDED (a user handler, up to 30s) AND re-entrant. Its
        # real fix is `compute/functions.py` using `httpx.AsyncClient`, at which
        # point this line becomes a plain `await` and the last `to_thread` in
        # the request path goes with it. `tests/gateway/test_lambda_reentrancy.py`
        # is what will keep that honest.
        answer = lambda: synth.pure_answer(  # noqa: E731
            action, resource, principal.env, body, stores, now, backing_port, query_params,
            keystore=keystore, gateway_port=gateway_port() if gateway_port else None, rds=rds,
        )
        pure = await answer() if action == "lambda:Invoke" else answer()
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
    return Starlette(
        routes=[
            Route("/", catch_all, methods=methods),
            Route("/{path:path}", catch_all, methods=methods),
        ],
        exception_handlers={Exception: _unhandled_failure},
    )


async def _unhandled_failure(request: Request, exc: Exception) -> Response:
    """Anything `catch_all` did not anticipate, answered in the asked-for
    service's own wire format instead of as a bare-text 500.

    Probed before it was written: a backing that dies between the port read and
    the forward makes httpx raise inside `catch_all`, and the exception went
    straight out of the ASGI app -- so what botocore actually received was
    uvicorn's plain `Internal Server Error`, with no AWS error document in it at
    all. That is the same race `BackingUnavailable` covers on the control app,
    seen from the other side.

    A transport failure IS the "backing isn't there" case this module already
    has a word for, so it reuses `ServiceUnavailable` (503) -- which SDKs treat
    as retryable, correctly, since the next Apply re-creates the backing.
    Everything else is a genuine surprise and gets a 500 `InternalFailure`.

    Deliberately built from nothing but the exception and `request.state`: a
    handler of last resort that touches disk, docker or the stores can raise on
    its own, and then the bare 500 is back. The detail is logged rather than
    returned, for the reason `errors.internal_failure` documents."""
    service = getattr(request.state, "service", "s3")
    log.error("gateway: unhandled %s serving %s %s", type(exc).__name__, request.method, request.url.path, exc_info=exc)
    if isinstance(exc, httpx.HTTPError):
        return errors.service_unavailable(service)
    return errors.internal_failure(service, type(exc).__name__)


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


# Security finding #1 NOTE, and it applies to BOTH entry points below:
# `host="0.0.0.0"` is deliberate and NOT the same bug as the control app's old
# default (see __main__.py) -- containers (a workload's AWS SDK, tofu's own
# provider) reach this gateway via `host.docker.internal`, which resolves to the
# HOST's real interface, not its loopback; a `127.0.0.1` bind would be
# unreachable from inside a container. This is safe specifically because every
# request here is SigV4-verified (`catch_all` -> `sigv4.verify`, this module's
# own docstring) before anything is classified or forwarded -- unlike the
# control app, which has no equivalent per-request check.
_DEFAULT_HOST = "0.0.0.0"


class _NoSignalServer(uvicorn.Server):
    """A uvicorn server that never touches the process's signal handlers.

    `uvicorn.Server.serve()` is exactly `capture_signals()` + `_serve()`, and
    `capture_signals` is a no-op ONLY off the main thread -- which is why
    `serve_in_thread` never had to think about it. Run as a TASK on the control
    app's own loop, this server IS on the main thread, and it really does take
    SIGINT away from the outer uvicorn for the whole serving window. Probed
    against uvicorn 0.49.0 rather than assumed, printing the real
    `signal.getsignal(SIGINT)` at each stage:

        after installing OUTER (stand-in for the outer uvicorn's handler):
            SIGINT handler = <function OUTER>
        WHILE the nested server is serving via serve():
            SIGINT handler = <bound method Server.handle_exit ...>   is it OUTER? False
        WHILE the nested server is serving via _serve():
            SIGINT handler = <function OUTER>                        is it OUTER? True

    With the handler stolen, Ctrl-C would set the GATEWAY's `should_exit`, not
    the control app's: the gateway would tear itself down first and the control
    app would only follow once uvicorn's own end-of-`capture_signals` re-raise
    reached it. That kills the gateway underneath an `/apply-full` that is
    mid-`tofu apply` and still needs it to answer AWS calls. The background
    thread never did that; neither does this.

    Overriding the hook rather than calling `_serve` directly keeps the public
    `serve()` as the entry point. The guard against a future uvicorn changing
    this is a test that reads the REAL `signal.getsignal(SIGINT)` while a real
    gateway is serving on the loop
    (`tests/gateway/test_serve_on_loop.py::test_serving_on_the_loop_does_not_take_the_process_signal_handlers`),
    not this docstring."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _await_started(server: uvicorn.Server, serving: asyncio.Task, port: int, timeout: float = 15.0) -> None:
    """Wait until uvicorn is really accepting connections on `port`, or say what
    stopped it -- never sit out the timeout for a server that already gave up.

    Reads `server.started`, which is a signal that arrives AND means what it is
    read to mean -- probed, because `_bound_socket` binds without listening and
    a bound-not-listening port refuses connections. Five real runs, each
    printing whether a connect succeeded in the same breath as the flag
    flipping:

        bound-but-not-served connectable? False
        started after 1 ticks of 10ms; connectable IMMEDIATELY? True

    (`Server.startup` sets `started = True` only after `loop.create_server`
    returns, and that is the call that puts the socket into listen state.)

    `serving.done()` is watched alongside it because uvicorn has TWO ways to not
    start, and only one of them is an exception. `Server._serve` reads

        await self.startup(sockets=sockets)
        if not self.should_exit: await self.main_loop()
        if self.started: await self.shutdown(sockets=sockets)

    -- so an ASGI app whose own lifespan fails makes `startup()` set
    `should_exit` and RETURN, and `serve()` then completes normally with
    `started` still False. Watching only for a raised exception would wait out
    the whole 15s for that one. `await serving` re-raises the real failure when
    there was one and falls through to the named RuntimeError when there was
    not; the deadline stays as the backstop for a server that neither starts nor
    finishes. The confusing 15s timeout `_bound_socket`'s docstring describes
    was the thread version's only symptom for every one of these."""
    deadline = time.monotonic() + timeout
    while not server.started and not serving.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    if serving.done():
        await serving
    if not server.started:
        raise RuntimeError(f"gateway did not start on :{port} within {timeout}s")


@contextlib.asynccontextmanager
async def serve_on_loop(app: Starlette, host: str = _DEFAULT_HOST, port: int = 0) -> AsyncIterator[int]:
    """Serve `app` on the CALLER'S event loop for the duration of the block,
    yielding the actual bound port. The control app's lifespan is the one
    production caller (`server.py`).

    A task, not a thread: odin used to run two event loops in two threads (the
    control app's and the gateway's), and one loop is what makes a synchronous
    read-modify-write atomic without a lock at all -- nothing preempts it
    without an `await`. `uvicorn.Server.serve` is a coroutine, so no rewrite is
    involved; `_NoSignalServer` is the one thing that has to differ from the
    threaded version, and says why.

    A PLAIN TASK, and NOT `asyncio.TaskGroup`, which was written first and
    measured wrong. CPython's TaskGroup treats the `async with` BODY's exception
    as one more task failure -- `taskgroups.py::_aexit` appends it to
    `self._errors` and raises `BaseExceptionGroup` -- so wrapping the control
    app's lifespan in one changed what a failing lifespan raises. The real
    failure from `tests/gateway/test_serve_on_loop.py`, before this was backed
    out:

        ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
          +---------------- 1 ----------------
          | ZeroDivisionError: something in the lifespan body blew up

    Every `except SomeError` around a lifespan stops matching when that happens.
    A group is also the wrong SHAPE here: it exists to collect failures from
    siblings, and this has exactly one child, whose lifetime is the block's. The
    group's other habit costs too -- on the error path it ABORTS (cancels) its
    children, so a lifespan that raised would kill the gateway mid-flight
    instead of letting `should_exit` shut it down gracefully. The plain task
    below is awaited on both paths, so both get the graceful stop.

    What the block still guarantees without the group: the listener cannot
    outlive it (the `finally` sets `should_exit` and then AWAITS the task, so
    the port is gone before the caller continues), a `serve()` that fails
    surfaces as itself instead of being swallowed by a dead thread
    (`_await_started`), and shutdown is not a `join(timeout=5)` that can
    silently give up. `should_exit` is noticed on uvicorn's own 0.1s `main_loop`
    tick -- measured 0.192-0.194s from `should_exit` to the serve coroutine
    returning, over five runs, with the port confirmed refusing connections
    immediately after.

    `asyncio`, not `anyio`, deliberately: odin's own source imports `anyio`
    nowhere (it arrives only transitively, under Starlette) while `asyncio.Lock`
    / `create_task` / `gather` / `wait_for` / `create_subprocess_exec` are
    already the idiom throughout `src/odin`. One idiom beats two.

    `port=0` still resolves through `_bound_socket`, which BINDS now and hands
    the live socket to uvicorn, so the port cannot be stolen in between (see its
    docstring -- that was a real bug). `contextlib.closing` is belt-and-braces
    for the one path uvicorn does not cover: it closes the handed-over socket
    itself in `shutdown()`, but `shutdown()` only runs `if self.started`, so a
    server that never started would otherwise leak the listening socket. A
    second `close()` on an already-closed socket is a no-op (probed: `fileno()`
    is already -1)."""
    sock = _bound_socket(host, port)
    actual_port = sock.getsockname()[1]
    server = _NoSignalServer(uvicorn.Config(app, host=host, port=actual_port, log_level="warning"))
    with contextlib.closing(sock):
        serving = asyncio.create_task(server.serve(sockets=[sock]), name="odin-gateway")
        try:
            await _await_started(server, serving, actual_port)
            yield actual_port
        finally:
            server.should_exit = True
            # `gather(..., return_exceptions=True)`, never a bare await, for the
            # reason `Reconciler.stop()` documents at length: this runs in the
            # control app's lifespan `finally`, and an await that re-raises
            # there skips the rest of teardown -- odin has already shipped that
            # bug once, and the thing skipped was the store lock. The outcome is
            # REPORTED rather than dropped; a listener that died on its own is
            # exactly what a user chasing "the gateway stopped answering" needs
            # in the log.
            outcome = (await asyncio.gather(serving, return_exceptions=True))[0]
            if isinstance(outcome, BaseException):
                log.error("gateway listener on :%d ended with %r", actual_port, outcome)


def serve_in_thread(app: Starlette, host: str = _DEFAULT_HOST, port: int = 0) -> tuple[uvicorn.Server, threading.Thread, int]:
    """TEST HELPER ONLY -- run `app` on a background uvicorn thread, for a
    SYNCHRONOUS caller that needs a real listening port.

    Production no longer uses this: the gateway runs on the control app's own
    loop (`serve_on_loop`), and the odin server has no other listener. What
    keeps it here is the class of test that cannot be written any other way --
    `tests/gateway/test_unhandled.py::test_a_real_boto3_client_parses_a_real_gateway_failure`
    drives a REAL boto3 client over a REAL socket to prove botocore parses
    odin's error document, which `httpx.ASGITransport` provably cannot answer
    (see that module's docstring), and pytest calls it from a plain `def`. Same
    for the two ECS e2e tests and `test_debug_e2e`, which serve the CONTROL app
    this way; the gateway then rides that thread's loop, exactly as it rides
    uvicorn's in production.

    `port=0` resolves a free port up front so the actual bound port is known
    before uvicorn ever starts. Returns (server, thread, actual_port); stop via
    `stop_in_thread`. Signals are not an issue here for the reason
    `_NoSignalServer` documents: off the main thread, uvicorn's own
    `capture_signals` is already a no-op."""
    sock = _bound_socket(host, port)
    actual_port = sock.getsockname()[1]
    config = uvicorn.Config(app, host=host, port=actual_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), name="odin-gateway", daemon=True)
    thread.start()
    _wait_until_serving(actual_port)
    return server, thread, actual_port


def stop_in_thread(server: uvicorn.Server, thread: threading.Thread) -> None:
    """The `serve_in_thread` counterpart -- test-only, same as its opener."""
    server.should_exit = True
    thread.join(timeout=5)
