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

    def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.statuses[spec.name] = "running"
        if self.next_port:
            self.ports[spec.name] = self.next_port

    def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.statuses.pop(name, None)
        self.ports.pop(name, None)

    def status(self, name: str) -> str:
        return self.statuses.get(name, "absent")

    def host_port(self, name: str, container_port: int) -> int:
        return self.ports.get(name, 0)

    def logs(self, name: str, tail: int = 20) -> str:
        return f"fake logs of {name}"


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


def test_ensure_boots_from_the_right_image_with_code_mounted_and_handler_as_command(tmp_path, listener):
    runtime = FakeRuntime()
    rt = FunctionRuntime(runtime, root=tmp_path)

    code_dir = rt.extract_code(ENV, FN, _zip_bytes({"lambda_function.py": "x"}))
    name = container_name(ENV, FN)
    runtime.next_port = listener

    port = rt.ensure(ENV, FN, "python3.12", "lambda_function.lambda_handler", {"FOO": "bar"}, code_dir)
    assert port == listener

    (spec,) = runtime.runs
    assert spec.name == name
    assert spec.image == "public.ecr.aws/lambda/python:3.12"
    assert spec.command == ("lambda_function.lambda_handler",)
    assert spec.volumes == {str(code_dir): "/var/task"}
    assert spec.env == {"FOO": "bar"}
    assert spec.labels == {"odin-env": ENV, "odin-lambda-fn": FN}


def test_ensure_stops_any_existing_container_before_recreating(tmp_path, listener):
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.statuses[name] = "exited"  # a stale remnant from a prior run
    rt = FunctionRuntime(runtime, root=tmp_path)
    runtime.next_port = listener
    rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)
    assert name in runtime.stopped


def test_ensure_falls_back_to_the_default_image_for_an_unknown_runtime(tmp_path, listener):
    runtime = FakeRuntime()
    runtime.next_port = listener
    rt = FunctionRuntime(runtime, root=tmp_path)
    rt.ensure(ENV, FN, "ruby3.4-does-not-exist", "h.h", {}, tmp_path)
    assert runtime.runs[0].image == RUNTIME_IMAGES[DEFAULT_RUNTIME]


def test_ensure_raises_when_the_port_never_opens(tmp_path):
    runtime = FakeRuntime()
    rt = FunctionRuntime(runtime, root=tmp_path, ready_timeout=0.2, poll_interval=0.05)
    # host_port stays 0 (never published) -- readiness must time out, not hang.
    with pytest.raises(RuntimeError, match="never became ready"):
        rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)


def test_ensure_raises_when_the_port_is_published_but_nothing_listens(tmp_path):
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
        rt.ensure(ENV, FN, "python3.12", "h.h", {}, tmp_path)


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
        self.send_response(200)
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
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2)


def test_invoke_posts_the_payload_to_the_invoke_path_and_returns_the_body(tmp_path, rie_server):
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.statuses[name] = "running"
    runtime.ports[name] = rie_server.server_address[1]
    rt = FunctionRuntime(runtime, root=tmp_path)

    result = rt.invoke(ENV, FN, b'{"key": "value"}')
    assert result.payload == b'{"ok": true}'
    assert result.function_error is None
    assert rie_server.received == [b'{"key": "value"}']


def test_invoke_surfaces_the_function_error_header(tmp_path, rie_server):
    rie_server.response = json.dumps({"errorType": "ValueError", "errorMessage": "boom"}).encode()
    rie_server.function_error = "Unhandled"
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.ports[name] = rie_server.server_address[1]
    rt = FunctionRuntime(runtime, root=tmp_path)

    result = rt.invoke(ENV, FN, b"{}")
    assert result.function_error == "Unhandled"
    assert json.loads(result.payload)["errorType"] == "ValueError"


def test_invoke_raises_when_the_container_is_not_running(tmp_path):
    rt = FunctionRuntime(FakeRuntime(), root=tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        rt.invoke(ENV, FN, b"{}")


# --- delete / status --------------------------------------------------------


def test_delete_stops_the_container_and_removes_the_code_dir(tmp_path):
    runtime = FakeRuntime()
    rt = FunctionRuntime(runtime, root=tmp_path)
    code_dir = rt.extract_code(ENV, FN, _zip_bytes({"a.py": "x"}))
    assert code_dir.exists()

    rt.delete(ENV, FN)
    assert runtime.stopped == [container_name(ENV, FN)]
    assert not code_dir.exists()


def test_status_delegates_to_the_runtime_driver(tmp_path):
    runtime = FakeRuntime()
    name = container_name(ENV, FN)
    runtime.statuses[name] = "running"
    rt = FunctionRuntime(runtime, root=tmp_path)
    assert rt.status(ENV, FN) == "running"
    assert rt.status(ENV, "ghost") == "absent"
