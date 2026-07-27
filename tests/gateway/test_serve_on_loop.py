"""The gateway runs on the control app's OWN event loop (v0.7.7 de-threading).

Until now odin ran two event loops in two OS threads: uvicorn's, and a second
uvicorn inside `threading.Thread(name="odin-gateway")`. `serve_on_loop` replaces
the thread with an `asyncio` task on the caller's loop. One loop is what makes a
synchronous read-modify-write atomic without a lock at all -- nothing preempts
it without an `await` -- so this is the change the lock deletions elsewhere in
v0.7.7 rest on.

Everything below drives a REAL uvicorn on a REAL port with REAL SigV4-signed
requests and asks the KERNEL (via `socket`) whether the port is held. Nothing
here fabricates the signal it measures:

  * `signal.getsignal(SIGINT)` is read while a real gateway is serving. That is
    the guard on `_NoSignalServer`, and it is the reason the override exists --
    uvicorn's `serve()` really does steal SIGINT once it is on the main thread
    (see that class's docstring for the probe output).
  * The port is proved released by RE-BINDING it after the block exits, which is
    stronger than a refused connect: a socket in TIME_WAIT still refuses, but a
    socket still owned by a live listener cannot be re-bound -- with the caveat
    `_rebindable` documents about wildcard binds.
  * `threading.enumerate()` and `asyncio.all_tasks()` are both read for real, so
    "a task, not a thread" is a measurement rather than a claim about the source.

Six of these were written by mutating the source and watching them fail; two
did NOT fail the first time and are the reason `_connectable`,
`test_the_listener_task_is_finished_before_the_block_returns` and the held
exception in `test_an_app_that_cannot_start_still_gives_the_port_back` exist.
Each of those three says in its own docstring which mutation it was blind to.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from starlette.applications import Starlette

from odin.gateway.app import GatewayState, _await_started, create_gateway_app, serve_on_loop
from odin.gateway.keys import KeyStore
from odin.gateway.policy import Statement
from odin.gateway.stores import SynthStores
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime
from tests.reconcile.test_reconciler import FakeAws

# The gateway binds 0.0.0.0 in production for a documented reason (containers
# reach it via host.docker.internal). Tests bind loopback so a parallel run
# never triggers a firewall prompt or collides with another agent's server.
HOST = "127.0.0.1"


async def _swallow(*_args: object, **_kwargs: object) -> None:
    return None


def _gateway(tmp_path: Path) -> tuple[object, str, str]:
    """A real gateway app with a real principal, allowed everything. `sts` is
    the service the calls below use: `GetCallerIdentity` is answered right after
    verify(), so it needs no backing container and proves the whole
    identify -> verify -> answer path over a real socket."""
    keystore = KeyStore(tmp_path / "keys")
    stores = SynthStores(tmp_path / "synth")
    access_key, secret_key = keystore.issue("default", "api")
    state = GatewayState()
    state.update("default", {"api": [Statement(actions=("*",), resources=("*",))]}, {})
    app = create_gateway_app(state, keystore, stores, _swallow)
    return app, access_key, secret_key


def _signed(access_key: str, secret_key: str, url: str, service: str = "sts") -> dict[str, str]:
    """Headers from botocore's own SigV4Auth -- the gateway verifies the real
    signature, so a hand-built header set would prove nothing."""
    request = AWSRequest(method="POST", url=url, data="Action=GetCallerIdentity&Version=2011-06-15")
    request.headers["Content-Type"] = "application/x-www-form-urlencoded"
    SigV4Auth(Credentials(access_key, secret_key), service, "us-east-1").add_auth(request)
    return dict(request.headers)


def _rebindable(port: int) -> bool:
    """Whether the kernel will hand `HOST:port` to a NEW listener -- the question
    "is the port released?" actually asks. A refused connect is weaker: a
    lingering socket refuses too.

    Only valid when the server under test bound the SAME address. BSD's
    SO_REUSEADDR lets `127.0.0.1:p` bind while `0.0.0.0:p` is held, so this
    answers True against a live `0.0.0.0` listener -- see
    `test_the_real_control_app_runs_its_gateway_on_its_own_loop`, which is why
    that one asks about connectability instead."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


async def test_a_real_sigv4_call_is_served_on_the_callers_own_loop(tmp_path):
    """The whole point, end to end: a real signed request over a real socket,
    answered by a gateway that is a TASK on this test's loop -- and no
    `odin-gateway` thread anywhere in the process while it happens."""
    app, access_key, secret_key = _gateway(tmp_path)
    async with serve_on_loop(app, host=HOST, port=0) as port:
        assert not [t for t in threading.enumerate() if t.name == "odin-gateway"], (
            "the gateway is supposed to be a task on this loop, not a thread"
        )
        url = f"http://{HOST}:{port}/"
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=_signed(access_key, secret_key, url), content=b"Action=GetCallerIdentity&Version=2011-06-15")

    assert response.status_code == 200, response.text
    assert "GetCallerIdentityResponse" in response.text


async def test_the_loop_keeps_serving_while_a_gateway_request_is_in_flight(tmp_path):
    """Sharing one loop must not serialize it. Ten concurrent signed calls are
    answered while this task is also running its own `asyncio.sleep` clock --
    if the gateway had taken the loop over, neither would make progress."""
    app, access_key, secret_key = _gateway(tmp_path)
    async with serve_on_loop(app, host=HOST, port=0) as port:
        url = f"http://{HOST}:{port}/"
        body = b"Action=GetCallerIdentity&Version=2011-06-15"
        async with httpx.AsyncClient() as client:
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                for _ in range(20):
                    await asyncio.sleep(0.005)
                    ticks += 1

            calls = [client.post(url, headers=_signed(access_key, secret_key, url), content=body) for _ in range(10)]
            responses, _ = await asyncio.gather(asyncio.gather(*calls), tick())

    assert [r.status_code for r in responses] == [200] * 10
    assert ticks == 20, "this task's own clock stopped while the gateway was serving"


async def test_serving_on_the_loop_does_not_take_the_process_signal_handlers(tmp_path):
    """`_NoSignalServer`'s reason for existing, measured rather than asserted.

    uvicorn's `Server.serve()` installs its own SIGINT/SIGTERM handlers whenever
    it runs on the MAIN thread -- which a task on the control app's loop does,
    and a background thread does not. Left alone, Ctrl-C would set the GATEWAY's
    `should_exit` instead of the control app's, tearing the gateway down
    underneath an `/apply-full` that is mid-`tofu apply` and still needs it.

    The sentinel stands in for the outer uvicorn's handler. This test is the
    guard: break `_NoSignalServer.capture_signals` and it fails here."""
    def sentinel(*_args: object) -> None:  # stands in for the outer uvicorn's handle_exit
        return None

    original = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, sentinel)
    try:
        app, _access_key, _secret_key = _gateway(tmp_path)
        async with serve_on_loop(app, host=HOST, port=0):
            during = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, original)

    assert during is sentinel, (
        f"the gateway took the process's SIGINT handler while serving: {during!r}. "
        "Ctrl-C would stop the gateway instead of the control app."
    )


async def test_leaving_the_block_releases_the_port_promptly(tmp_path):
    """Shutdown correctness, asked of the kernel. The port must be genuinely
    re-bindable -- not merely refusing connections -- and it must happen
    promptly: uvicorn notices `should_exit` on its own 0.1s `main_loop` tick."""
    app, _access_key, _secret_key = _gateway(tmp_path)
    async with serve_on_loop(app, host=HOST, port=0) as port:
        assert not _rebindable(port), "the gateway is supposed to be holding this port"
        started = time.monotonic()
    elapsed = time.monotonic() - started

    assert _rebindable(port), f"port {port} is still held after serve_on_loop exited"
    assert elapsed < 2.0, f"shutdown took {elapsed:.2f}s (measured ~0.2s)"


async def test_the_real_control_app_runs_its_gateway_on_its_own_loop(tmp_path):
    """The production wiring, not just the helper: `create_app`'s lifespan must
    put the gateway on THIS loop and leave no `odin-gateway` thread behind.

    Measured the same way on a real `odin` server (uvicorn :5130, gateway pinned
    to :5196, `ps -M` on the pid holding both ports): 10 OS threads before this
    change, 9 after, with both listeners on the one process either way.

    Connectability, not `_rebindable`, is the check here -- production binds
    `0.0.0.0` while the probe would bind `127.0.0.1`, and BSD's SO_REUSEADDR
    lets a specific address bind a port a wildcard socket already holds (this
    test asserted `not _rebindable(...)` first and failed for exactly that
    reason). Binding `0.0.0.0` from a test would also risk a firewall prompt."""
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(),
        aws=FakeAws(), backings=False, gateway_port=0,
    )
    async with app.router.lifespan_context(app):
        health = (await _health(app)).json()
        port = health["gateway"]["port"]
        assert isinstance(port, int) and port > 0, health
        assert not [t for t in threading.enumerate() if t.name == "odin-gateway"]
        assert _gateway_tasks(), "the gateway should be a task on this very loop"
        assert _connectable(port), "the gateway is not answering on the port /health advertises"

    assert not _connectable(port), "the lifespan exited with the gateway still listening"
    assert not _gateway_tasks(), "the lifespan exited while its gateway task was still running"


async def _health(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://odin.test") as client:
        return await client.get("/health")


def _connectable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, port)) == 0


def _gateway_tasks() -> list[asyncio.Task]:
    """The live serve task, by the name `serve_on_loop` gives it.
    `asyncio.all_tasks()` lists only tasks that are NOT done, so an empty result
    means finished-or-never-started, never "still running"."""
    return [task for task in asyncio.all_tasks() if task.get_name() == "odin-gateway"]


async def test_the_listener_task_is_finished_before_the_block_returns(tmp_path):
    """Releasing the PORT is not the same as finishing the SERVER, and this is
    the half a port check cannot see.

    Found by mutation: deleting the `await` on the serve task left
    `test_leaving_the_block_releases_the_port_promptly` passing, because
    `contextlib.closing` hands the port back on the way out regardless. What
    that mutation really breaks is draining -- uvicorn's `shutdown()` closes
    listeners, asks live connections to finish and waits for them, and skipping
    the await returns from the lifespan with all of that still in flight, on a
    loop that is itself about to stop. So the task is asserted, not just the
    port."""
    app, _access_key, _secret_key = _gateway(tmp_path)
    async with serve_on_loop(app, host=HOST, port=0):
        assert _gateway_tasks(), "the serve task should be running inside the block"

    assert not _gateway_tasks(), "serve_on_loop returned while its listener was still running"


async def test_the_port_is_released_even_when_the_block_raises(tmp_path):
    """A listener must not outlive its block on the failure path either -- that
    is what makes the task-group version stronger than `join(timeout=5)`, which
    could silently give up and leave the port held."""
    app, _access_key, _secret_key = _gateway(tmp_path)
    port_holder: dict[str, int] = {}

    with pytest.raises(ZeroDivisionError):
        async with serve_on_loop(app, host=HOST, port=0) as port:
            port_holder["port"] = port
            raise ZeroDivisionError("something in the lifespan body blew up")

    assert _rebindable(port_holder["port"]), "the port survived a failing block"


async def test_an_explicitly_requested_port_is_the_port_that_is_served(tmp_path):
    """`_bound_socket` binds NOW and hands the live socket to uvicorn, so the
    number cannot be stolen in between -- and a caller who asked for a specific
    port must get it or fail, never be silently given another."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as finder:
        finder.bind((HOST, 0))
        wanted = finder.getsockname()[1]

    app, access_key, secret_key = _gateway(tmp_path)
    async with serve_on_loop(app, host=HOST, port=wanted) as port:
        assert port == wanted
        url = f"http://{HOST}:{port}/"
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=_signed(access_key, secret_key, url), content=b"Action=GetCallerIdentity&Version=2011-06-15")
    assert response.status_code == 200


async def test_a_taken_port_fails_loudly_and_leaves_nothing_behind(tmp_path):
    """The other half of the same contract: asked for a port someone else holds,
    this raises OSError at bind time rather than serving somewhere else."""
    app, _access_key, _secret_key = _gateway(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind((HOST, 0))
        squatter.listen(1)
        taken = squatter.getsockname()[1]

        with pytest.raises(OSError):
            async with serve_on_loop(app, host=HOST, port=taken):
                pass  # pragma: no cover -- the bind above is what fails


async def test_an_app_that_cannot_start_still_gives_the_port_back(tmp_path):
    """The leak `contextlib.closing` exists for, driven through a REAL uvicorn
    rather than argued from the source.

    uvicorn closes the socket it was handed inside `shutdown()`, and `_serve`
    only calls `shutdown()` `if self.started`. An ASGI app whose own lifespan
    raises makes `startup()` set `should_exit` and return, so `serve()`
    completes having never started -- and the listening socket odin bound and
    handed over is never closed by anyone else. Both halves are asserted: the
    failure is NAMED (not a 15s hang), and the port comes back.

    The exception is HELD across the port check on purpose, and that detail is
    what makes this a real test rather than a coincidence. Found by mutation:
    with the exception dropped, replacing `contextlib.closing` with a no-op
    still passed, because CPython's refcounting destroys `serve_on_loop`'s frame
    the moment nothing references it and that closes the socket as a side
    effect. Holding the exception holds its traceback, which holds that frame,
    which holds the socket -- exactly what happens in production, where uvicorn
    logs the startup failure with `exc_info`. So the explicit close is the only
    thing that can give the port back here."""
    @contextlib.asynccontextmanager
    async def doomed(_app):
        raise RuntimeError("this app cannot start")
        yield  # pragma: no cover -- unreachable, but makes this a generator

    app = Starlette(lifespan=doomed)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as finder:
        finder.bind((HOST, 0))
        wanted = finder.getsockname()[1]

    with pytest.raises(RuntimeError, match=rf"gateway did not start on :{wanted}") as caught:
        async with serve_on_loop(app, host=HOST, port=wanted):
            pass  # pragma: no cover -- startup never gets here

    assert caught.value.__traceback__ is not None  # the frame chain is alive right now
    assert _rebindable(wanted), f"port {wanted} was left bound by a server that never started"


class _NeverStarts:
    """A server that neither starts nor fails -- the one condition
    `_await_started`'s deadline is the backstop for."""

    started = False


async def _forever() -> None:
    await asyncio.sleep(3600)


async def test_a_server_that_never_starts_is_reported_and_not_waited_on_forever():
    """`_await_started`'s deadline names the port and the timeout instead of
    hanging. This is the backstop only; the two failures that really happen are
    covered below and neither of them waits it out."""
    serving = asyncio.create_task(_forever())
    with pytest.raises(RuntimeError, match=r"gateway did not start on :4266 within 0\.05s"):
        await _await_started(_NeverStarts(), serving, 4266, timeout=0.05)
    serving.cancel()


async def test_a_listener_that_fails_surfaces_its_own_error_at_once(tmp_path):
    """The failure the thread version swallowed. `serve()` raising must come out
    as ITSELF and immediately -- not as a 15s "did not start" timeout, which is
    the confusing symptom `_bound_socket`'s docstring describes.

    Timed, because "immediately" is the whole claim: the deadline is 30x the
    budget asserted here, so a version that waited it out could not pass."""
    async def explodes() -> None:
        raise MemoryError("uvicorn could not come up")

    serving = asyncio.create_task(explodes())
    started = time.monotonic()
    with pytest.raises(MemoryError, match="uvicorn could not come up"):
        await _await_started(_NeverStarts(), serving, 4266, timeout=1.5)
    assert time.monotonic() - started < 0.5


async def test_a_listener_that_returns_without_starting_is_not_waited_out():
    """uvicorn's OTHER way of not starting, and the one an exception-only guard
    misses: `Server._serve` skips `main_loop()` when `startup()` set
    `should_exit` (an ASGI app whose own lifespan failed), so `serve()` RETURNS
    normally with `started` still False. Named as a failure, at once."""
    async def gives_up() -> None:
        return None

    serving = asyncio.create_task(gives_up())
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"gateway did not start on :4266"):
        await _await_started(_NeverStarts(), serving, 4266, timeout=1.5)
    assert time.monotonic() - started < 0.5
