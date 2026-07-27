"""V4b -- compute/functions.py: FunctionRuntime, the RIE-container substrate
binding for gateway/models/lambdactl.py's Lambda functions.

Unit-level (injected container runtime, the same `FakeRuntime` shape
tests/aws/test_backings.py uses for BackingAws -- no real Docker involved).
The one piece that genuinely can't be fake-runtime-only is `_await_ready`'s
TCP-connect readiness probe: it dials a real socket, so these tests bind a
REAL local TCP listener and point `FakeRuntime.host_port` at it, exactly the
way a real Docker-published port would behave -- deterministic, no Docker,
but a real socket handshake all the same. Invoke similarly starts a tiny
real local HTTP server standing in for the RIE endpoint (harness.py's
CaptureSink pattern, inlined here since Lambda's response shape -- a
FunctionError HEADER -- is specific to this module).
"""
from __future__ import annotations

import io
import json
import socket
import threading
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from odin.compute.functions import DEFAULT_RUNTIME, RUNTIME_IMAGES, FunctionRuntime, container_name
from odin.runtime.colima import ContainerSpec

ENV = "default"
FN = "hello"


@dataclass
class FakeRuntime:
    # `next_port`: what `run_container` publishes for the NEXT container it
    # starts (0 = "not published yet", the real-world state right after a
    # fresh `docker run`). Deliberately NOT settable by writing straight into
    # `ports` before calling `ensure()` -- `ensure()` always `stop()`s the
    # old container FIRST (clearing `ports`), so a pre-seeded entry would
    # just get wiped before `run_container` ever runs; found empirically
    # (this file's first draft hung for the full 180s default timeout).
    runs: list[ContainerSpec] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    next_port: int = 0
    exit_codes: dict[str, int] = field(default_factory=dict)
    # `None` keeps the old "always some text" behaviour; `""` is the state the
    # readiness message used to render as a dangling colon, and it is a REAL
    # one -- measured on a live `alpine sleep 300` container, `docker logs`
    # answered `''` while `status` answered `running` (see `_not_ready_reason`).
    log_text: str | None = None

    async def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.statuses[spec.name] = "running"
        if self.next_port:
            self.ports[spec.name] = self.next_port

    async def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.statuses.pop(name, None)
        self.ports.pop(name, None)

    async def status(self, name: str) -> str:
        return self.statuses.get(name, "absent")

    async def exit_code(self, name: str) -> int:
        return self.exit_codes.get(name, 0)

    async def host_port(self, name: str, container_port: int) -> int:
        return self.ports.get(name, 0)

    async def logs(self, name: str, tail: int = 20) -> str:
        return self.log_text if self.log_text is not None else f"fake logs of {name}"


@pytest.fixture
def listener():
    """A real local TCP listener standing in for a container's published
    RIE port -- `_await_ready`'s socket-connect probe dials this for real."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    yield port
    sock.close()


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buf.getvalue()


# --- container_name / RUNTIME_IMAGES -------------------------------------


def test_container_name_is_the_odin_convention():
    assert container_name(ENV, FN) == f"odin-lambda-{ENV}-{FN}"


def test_runtime_images_map_the_hcl_offered_runtimes():
    assert RUNTIME_IMAGES["python3.12"] == "public.ecr.aws/lambda/python:3.12"
    assert RUNTIME_IMAGES[DEFAULT_RUNTIME] == RUNTIME_IMAGES["python3.12"]


# --- extract_code ----------------------------------------------------------


def test_extract_code_writes_the_zip_contents_to_var_task_dir(tmp_path):
    rt = FunctionRuntime(FakeRuntime(), root=tmp_path)
    code_dir = rt.extract_code(ENV, FN, _zip_bytes({"lambda_function.py": "def h(e,c): return e"}))
    assert (code_dir / "lambda_function.py").read_text() == "def h(e,c): return e"
    assert code_dir == rt.code_dir(ENV, FN)


def test_extract_code_wipes_stale_files_from_a_prior_deploy(tmp_path):
    rt = FunctionRuntime(FakeRuntime(), root=tmp_path)
    rt.extract_code(ENV, FN, _zip_bytes({"old.py": "x", "lambda_function.py": "old"}))
    rt.extract_code(ENV, FN, _zip_bytes({"lambda_function.py": "new"}))
    code_dir = rt.code_dir(ENV, FN)
    assert not (code_dir / "old.py").exists()
    assert (code_dir / "lambda_function.py").read_text() == "new"


# --- ensure() / readiness --------------------------------------------------


async def test_ensure_boots_from_the_right_image_with_code_mounted_and_handler_as_command(tmp_path, listener):
    runtime = FakeRuntime()
    rt = FunctionRuntime(runtime, root=tmp_path)

    code_dir = rt.extract_code(ENV, FN, _zip_bytes({"lambda_function.py": "x"}))
    name = container_name(ENV, FN)
    runtime.next_port = listener

    port = await rt.ensure(ENV, FN, "python3.12", "lambda_function.lambda_handler", {"FOO": "bar"}, code_dir)
    assert port == listener

    (spec,) = runtime.runs
    assert spec.name == name
    assert spec.image == "public.ecr.aws/lambda/python:3.12"
    assert spec.command == ("lambda_function.lambda_handler",)
    assert spec.volumes == {str(code_dir): "/var/task"}
    assert spec.env == {"FOO": "bar"}
    assert spec.labels == {"odin-env": ENV, "odin-lambda-fn": FN}


async def test_ensure_stops_any_existing_container_before_recreating(tmp_path, listener):
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.statuses[name] = "exited"  # a stale remnant from a prior run
    rt = FunctionRuntime(runtime, root=tmp_path)
    runtime.next_port = listener
    await rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)
    assert name in runtime.stopped


async def test_ensure_falls_back_to_the_default_image_for_an_unknown_runtime(tmp_path, listener):
    runtime = FakeRuntime()
    runtime.next_port = listener
    rt = FunctionRuntime(runtime, root=tmp_path)
    await rt.ensure(ENV, FN, "ruby3.4-does-not-exist", "h.h", {}, tmp_path)
    assert runtime.runs[0].image == RUNTIME_IMAGES[DEFAULT_RUNTIME]


# --- owner directive B4: the function's real MemorySize caps the container --


async def test_ensure_passes_memory_mib_through_to_the_container_spec(tmp_path, listener):
    runtime = FakeRuntime()
    runtime.next_port = listener
    rt = FunctionRuntime(runtime, root=tmp_path)
    await rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path, memory_mib=256)
    assert runtime.runs[0].memory_mib == 256.0


async def test_ensure_leaves_memory_mib_unset_when_not_given(tmp_path, listener):
    # Back-compat: a caller that predates this (or genuinely has none) keeps
    # today's unbounded behavior rather than a silently invented default --
    # lambdactl.py always sets one in practice (memory_size defaults to 128).
    runtime = FakeRuntime()
    runtime.next_port = listener
    rt = FunctionRuntime(runtime, root=tmp_path)
    await rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)
    assert runtime.runs[0].memory_mib is None


async def test_ensure_raises_when_the_port_never_opens(tmp_path):
    runtime = FakeRuntime()
    rt = FunctionRuntime(runtime, root=tmp_path, ready_timeout=0.2, poll_interval=0.05)
    # host_port stays 0 (never published) -- readiness must time out, not hang.
    with pytest.raises(RuntimeError, match="never became ready"):
        await rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)


async def test_ensure_raises_when_the_port_is_published_but_nothing_listens(tmp_path):
    runtime = FakeRuntime()
    # A port docker CLAIMS is published, but nothing is actually accepting
    # connections on it yet -- readiness must still time out, not false-
    # positive on the port number alone.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    runtime.next_port = dead_port
    rt = FunctionRuntime(runtime, root=tmp_path, ready_timeout=0.2, poll_interval=0.05)
    with pytest.raises(RuntimeError, match="never became ready"):
        await rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)


# --- what the readiness failure SAYS (OPEN-BUGS #5) --------------------------


async def test_a_readiness_failure_on_a_silent_container_still_states_a_reason(tmp_path):
    """The bug: `f"{name} RIE never became ready:\\n{logs}"` with empty logs was
    a dangling colon and a blank line. Measured on a REAL container (`docker run
    -d alpine sleep 300`, nothing published, nothing logged) driving the REAL
    `_await_ready` to a REAL timeout:

        'odin-lambda-p1bprobe3-quiet RIE never became ready:\\n'

    ...while `status` said `running`. The empty-logs half is replayed here; the
    integration was proved against docker, not fabricated."""
    runtime = FakeRuntime(log_text="")
    rt = FunctionRuntime(runtime, root=tmp_path, ready_timeout=0.2, poll_interval=0.05)
    with pytest.raises(RuntimeError) as raised:
        await rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)
    message = str(raised.value)
    assert message == (
        f"odin-lambda-{ENV}-{FN} RIE never became ready: nothing accepted a TCP connection "
        "on its published port 8080 within 0.2s. Container: running. "
        "It has logged nothing, so the container state above is the whole of it."
    )
    # The properties, independent of the wording: nothing trails off, and the
    # two readings that are ALWAYS available are both in it.
    assert not message.rstrip().endswith(":")
    assert "running" in message and "0.2s" in message


async def test_a_readiness_failure_on_a_dead_container_reports_its_exit_code(tmp_path):
    """The other branch, also measured for real (`sh -c 'echo bootstrap said
    no; exit 5'` -> status `exited`, exit_code 5, logs `bootstrap said no`).
    The exit code is reported ONLY here: a RUNNING container's is `0`, and
    printing "exit code 0" under a failure sends the reader the wrong way."""
    name = container_name(ENV, FN)
    runtime = FakeRuntime(log_text="bootstrap said no", exit_codes={name: 5})
    runtime.statuses[name] = "exited"
    rt = FunctionRuntime(runtime, root=tmp_path, ready_timeout=0.2, poll_interval=0.05)
    with pytest.raises(RuntimeError) as raised:
        await rt._await_ready(name)
    message = str(raised.value)
    assert "Container: exited, exit code 5." in message
    assert message.endswith("Its logs:\nbootstrap said no")


async def test_a_running_container_is_never_reported_with_an_exit_code(tmp_path):
    """`exit_code` on a live container is 0 -- the guard must not print it."""
    runtime = FakeRuntime(log_text="")
    rt = FunctionRuntime(runtime, root=tmp_path, ready_timeout=0.2, poll_interval=0.05)
    with pytest.raises(RuntimeError) as raised:
        await rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)
    assert "exit code" not in str(raised.value)


# --- invoke() ----------------------------------------------------------------


class _RieHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.received.append(body)  # type: ignore[attr-defined]
        response, error = self.server.response, self.server.function_error  # type: ignore[attr-defined]
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/json")
        if error:
            self.send_header("X-Amz-Function-Error", error)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


@pytest.fixture
def rie_server():
    """A tiny real local HTTP server standing in for a function's RIE
    container -- proves `invoke()` genuinely POSTs to the invoke path and
    relays the response body + FunctionError header, not a mocked call."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RieHandler)
    server.received = []
    server.response = b'{"ok": true}'
    server.function_error = None
    # Real RIE answers 200 for a handler that RAISES and 502 for an init or
    # runtime-exit failure -- both configurable here, because that status
    # (and never a header) is part of how odin now detects a failed invoke.
    server.status = 200
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2)


async def test_invoke_posts_the_payload_to_the_invoke_path_and_returns_the_body(tmp_path, rie_server):
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.statuses[name] = "running"
    runtime.ports[name] = rie_server.server_address[1]
    rt = FunctionRuntime(runtime, root=tmp_path)

    result = await rt.invoke(ENV, FN, b'{"key": "value"}')
    assert result.payload == b'{"ok": true}'
    assert result.function_error is None
    assert rie_server.received == [b'{"key": "value"}']


async def test_invoke_surfaces_the_function_error_header(tmp_path, rie_server):
    rie_server.response = json.dumps({"errorType": "ValueError", "errorMessage": "boom"}).encode()
    rie_server.function_error = "Unhandled"
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.ports[name] = rie_server.server_address[1]
    rt = FunctionRuntime(runtime, root=tmp_path)

    result = await rt.invoke(ENV, FN, b"{}")
    assert result.function_error == "Unhandled"
    assert json.loads(result.payload)["errorType"] == "ValueError"


# --- field test 3: RIE never sends the header, so read the response --------
#
# Verified against a real `public.ecr.aws/lambda/python:3.12` container: a
# handler that raises comes back `200 OK` with the error payload as the body
# and NO `X-Amz-Function-Error` header anywhere. odin read only that header,
# so `function_error` was ALWAYS None -- `aws lambda invoke` on a crashing
# function looked like a success to any CI job keying on `FunctionError`, and
# the `last_invocation_error` the World verdict projects was always None too.


def _invoker(tmp_path, rie_server) -> FunctionRuntime:
    runtime = FakeRuntime()
    runtime.ports[container_name(ENV, FN)] = rie_server.server_address[1]
    return FunctionRuntime(runtime, root=tmp_path)


# The exact bytes a real RIE returns for a Python handler that raises.
_REAL_RIE_ERROR_BODY = json.dumps({
    "errorMessage": "boom from odin probe",
    "errorType": "ValueError",
    "requestId": "94369cd1-0724-4ea2-87e7-a1c399933fe2",
    "stackTrace": ['  File "/var/task/app.py", line 2, in handler\n'],
}).encode()


async def test_a_raised_handler_is_a_function_error_even_with_no_header(tmp_path, rie_server):
    rie_server.response = _REAL_RIE_ERROR_BODY
    rie_server.function_error = None  # real RIE sends no header -- this is the bug
    rie_server.status = 200

    result = await _invoker(tmp_path, rie_server).invoke(ENV, FN, b"{}")

    assert result.function_error == "Unhandled"
    assert result.payload == _REAL_RIE_ERROR_BODY  # the payload is relayed untouched


async def test_an_init_or_exit_failure_is_a_function_error_too(tmp_path, rie_server):
    """RIE answers 502 for an import failure or a runtime exit; real Lambda
    reports those as a 200 + FunctionError, which is what odin returns."""
    rie_server.response = json.dumps(
        {"errorType": "Runtime.ImportModuleError", "errorMessage": "Unable to import module 'app'"}
    ).encode()
    rie_server.status = 502

    assert (await _invoker(tmp_path, rie_server).invoke(ENV, FN, b"{}")).function_error == "Unhandled"


async def test_a_successful_invocation_still_has_no_function_error(tmp_path, rie_server):
    rie_server.response = json.dumps({"statusCode": 200, "body": "ok"}).encode()

    assert (await _invoker(tmp_path, rie_server).invoke(ENV, FN, b"{}")).function_error is None


async def test_a_non_json_response_body_is_not_mistaken_for_an_error(tmp_path, rie_server):
    rie_server.response = b"\x00\x01not json at all"

    assert (await _invoker(tmp_path, rie_server).invoke(ENV, FN, b"{}")).function_error is None


async def test_a_handler_returning_only_one_error_key_is_not_an_error(tmp_path, rie_server):
    """Both keys, or it's just a payload that happens to mention an error."""
    rie_server.response = json.dumps({"errorMessage": "handled internally, returned 200"}).encode()

    assert (await _invoker(tmp_path, rie_server).invoke(ENV, FN, b"{}")).function_error is None


async def test_invoke_raises_when_the_container_is_not_running(tmp_path):
    rt = FunctionRuntime(FakeRuntime(), root=tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        await rt.invoke(ENV, FN, b"{}")


# --- delete / status --------------------------------------------------------


async def test_delete_stops_the_container_and_removes_the_code_dir(tmp_path):
    runtime = FakeRuntime()
    rt = FunctionRuntime(runtime, root=tmp_path)
    code_dir = rt.extract_code(ENV, FN, _zip_bytes({"a.py": "x"}))
    assert code_dir.exists()

    await rt.delete(ENV, FN)
    assert runtime.stopped == [container_name(ENV, FN)]
    assert not code_dir.exists()


async def test_status_delegates_to_the_runtime_driver(tmp_path):
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.statuses[name] = "running"
    rt = FunctionRuntime(runtime, root=tmp_path)
    assert await rt.status(ENV, FN) == "running"
    assert await rt.status(ENV, "ghost") == "absent"
