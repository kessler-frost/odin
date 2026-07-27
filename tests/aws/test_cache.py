"""W2.8 -- aws/cache.py: RedisCache, the per-cluster `redis:7-alpine`
substrate binding for gateway/models/cachectl.py.

Unit-level (injected container runtime -- the same `FakeRuntime` shape
tests/aws/test_backings.py and tests/test_compute/test_functions.py use, no
real Docker involved). The one piece that genuinely can't be fake-runtime-only
is `_await_ready`'s readiness probe: it speaks REAL RESP over a REAL socket,
so these tests stand up a tiny local Redis-shaped listener and point
`FakeRuntime.host_port` at it -- deterministic, no Docker, but a real
protocol handshake all the same (test_functions.py's own precedent).
"""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field

import pytest

from odin.aws.cache import (
    DEFAULT_MEMORY_MIB,
    REDIS_IMAGE,
    REDIS_PORT,
    RedisCache,
    container_name,
    engine_version,
    ping,
    resp_call,
)
from odin.runtime.colima import ContainerSpec

ENV = "default"
CLUSTER = "sessions"


@dataclass
class FakeRuntime:
    # `next_port`: what `run_container` publishes for the NEXT container it
    # starts (0 == "not published yet", the real state right after a fresh
    # `docker run`). `ensure()` always `stop()`s first (clearing `ports`), so
    # a pre-seeded entry would be wiped before `run_container` ever ran.
    runs: list[ContainerSpec] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    next_port: int = 0
    # What `_not_ready_reason` reads besides the logs. `exit_codes` is keyed by
    # container name; a live container reports 0, which is REAL docker's answer
    # for a running container (`{{.State.ExitCode}}`, measured) and the reason
    # `_not_ready_reason` prints it only for one that is NOT running.
    exit_codes: dict[str, int] = field(default_factory=dict)
    log_text: str = ""

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

    def exit_code(self, name: str) -> int:
        return self.exit_codes.get(name, 0)

    def host_port(self, name: str, container_port: int) -> int:
        return self.ports.get(name, 0)

    def logs(self, name: str, tail: int = 20) -> str:
        return self.log_text


class _FakeRedis:
    """A real TCP listener that speaks just enough RESP2 to stand in for a
    Redis server: PING, INFO server, SET, GET. Exercises `resp_call`'s actual
    encode/decode path rather than mocking it away."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.commands: list[list[str]] = []
        self.data: dict[str, str] = {}
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as stream:
            argc = int(stream.readline().rstrip(b"\r\n")[1:])
            args = []
            for _ in range(argc):
                length = int(stream.readline().rstrip(b"\r\n")[1:])
                args.append(stream.read(length + 2)[:length].decode())
            self.commands.append(args)
            conn.sendall(self._answer(args))

    def _answer(self, args: list[str]) -> bytes:
        verb = args[0].upper()
        if verb == "PING":
            return b"+PONG\r\n"
        if verb == "INFO":
            body = "# Server\r\nredis_version:7.4.2\r\nos:Linux\r\n"
            return f"${len(body)}\r\n{body}\r\n".encode()
        if verb == "SET":
            self.data[args[1]] = args[2]
            return b"+OK\r\n"
        if verb == "GET":
            value = self.data.get(args[1])
            if value is None:
                return b"$-1\r\n"
            return f"${len(value)}\r\n{value}\r\n".encode()
        return b"-ERR unknown command\r\n"

    def close(self) -> None:
        self._stop = True
        self._sock.close()


@pytest.fixture
def redis_server():
    server = _FakeRedis()
    yield server
    server.close()


# --- naming ----------------------------------------------------------------


def test_container_name_is_the_odin_convention():
    assert container_name(ENV, CLUSTER) == f"odin-cache-{ENV}-{CLUSTER}"


# --- the RESP client -------------------------------------------------------


def test_resp_call_round_trips_a_real_set_get(redis_server):
    assert resp_call(redis_server.port, "SET", "k", "hello") == "OK"
    assert resp_call(redis_server.port, "GET", "k") == "hello"
    assert redis_server.commands[0] == ["SET", "k", "hello"]


def test_resp_call_returns_none_for_a_missing_key(redis_server):
    assert resp_call(redis_server.port, "GET", "nope") is None


def test_resp_call_surfaces_an_error_reply_as_text(redis_server):
    assert resp_call(redis_server.port, "BOGUS") == "ERR unknown command"


def test_ping_is_true_against_a_real_server_false_when_nothing_listens(redis_server):
    assert ping(redis_server.port) is True
    redis_server.close()
    assert ping(redis_server.port) is False


def test_engine_version_reads_the_real_server_version(redis_server):
    assert engine_version(redis_server.port) == "7.4.2"


def test_engine_version_is_empty_when_unreachable(redis_server):
    redis_server.close()
    assert engine_version(redis_server.port) == ""


# --- ensure / lifecycle ----------------------------------------------------


def test_ensure_boots_redis_and_returns_the_published_port(redis_server):
    runtime = FakeRuntime(next_port=redis_server.port)
    cache = RedisCache(runtime=runtime, poll_interval=0.01)
    assert cache.ensure(ENV, CLUSTER) == redis_server.port
    (spec,) = runtime.runs
    assert spec.name == container_name(ENV, CLUSTER)
    assert spec.image == REDIS_IMAGE
    assert spec.ports == {REDIS_PORT: 0}  # 0 == let Docker pick the host port
    assert spec.labels == {"odin-env": ENV, "odin-cache-cluster": CLUSTER}
    assert spec.memory_mib == DEFAULT_MEMORY_MIB  # owner directive B4: always capped


def test_ensure_clears_an_exited_remnant_first(redis_server):
    runtime = FakeRuntime(next_port=redis_server.port)
    runtime.statuses[container_name(ENV, CLUSTER)] = "exited"
    RedisCache(runtime=runtime, poll_interval=0.01).ensure(ENV, CLUSTER)
    assert runtime.stopped == [container_name(ENV, CLUSTER)]


def test_ensure_raises_with_the_container_logs_when_redis_never_answers():
    runtime = FakeRuntime(next_port=1, log_text="LOG LINE")  # nothing listens on port 1
    cache = RedisCache(runtime=runtime, ready_timeout=0.05, poll_interval=0.01)
    with pytest.raises(RuntimeError, match="never became ready"):
        cache.ensure(ENV, CLUSTER)


# --- a readiness timeout that explained NOTHING -----------------------------
#
# `_await_ready` raised `f"{name} redis never became ready:\n{logs}"`, and
# `_ContainerRuntime.logs` answers `""` both for a container that wrote nothing
# and for one the runtime could not read -- so the whole explanation was a colon
# and a blank line. A direct clone of the bug `compute/functions.py`'s
# `_not_ready_reason` was written for.
#
# MEASURED against REAL containers on REAL docker 28.4.0 / Colima, driving THIS
# method to a REAL 6s timeout (`ready_timeout=6.0`, nothing on 6379):
#
#   container                      rendered                   status   exit port logs
#   alpine sleep 300               '…never became ready:\n'   running  0    0    ''
#   alpine sh -c 'exit 5'          '…never became ready:\n'   exited   5    0    ''
#   redis:7-alpine, unpublished    '…:\n' + redis banner      running  0    0    banner
#   socat TCP-LISTEN:6379 /dev/null  (published, mute)        running  0    34045 ''
#
# Row two is the worst of them: the container had EXITED with code 5 and odin's
# entire explanation was a dangling colon. Row three is why the log tail cannot
# be the headline -- that banner ends `Ready to accept connections tcp`, a log
# that reads like SUCCESS under a sentence saying the opposite, while the real
# reason (no published port) sat unread in `host_port`.


def test_a_readiness_timeout_on_a_live_silent_container_still_states_a_reason():
    """Row one, replayed: `status` running, no port, no logs. The old text was
    a colon and a blank line."""
    runtime = FakeRuntime()  # next_port 0 -> host_port stays 0, as docker really does
    cache = RedisCache(runtime=runtime, ready_timeout=0.05, poll_interval=0.01)
    with pytest.raises(RuntimeError) as raised:
        cache.ensure(ENV, CLUSTER)
    message = str(raised.value)
    assert message == (
        f"{container_name(ENV, CLUSTER)} redis never became ready: docker never "
        f"published its {REDIS_PORT}, so nothing could reach it, after 0.05s. "
        "Container: running. It has logged nothing, so the container state above "
        "is the whole of it."
    )
    assert not message.rstrip().endswith(":"), "nothing may trail off"
    assert "\n" not in message, "an empty log must not leave a blank line either"


def test_a_readiness_timeout_on_an_exited_container_names_its_exit_code():
    """Row two, replayed -- the reading that mattered most and was discarded."""
    name = container_name(ENV, CLUSTER)
    runtime = FakeRuntime()
    cache = RedisCache(runtime=runtime, ready_timeout=0.05, poll_interval=0.01)
    runtime.run_container(ContainerSpec(name=name, image=REDIS_IMAGE))
    runtime.statuses[name] = "exited"
    runtime.exit_codes[name] = 5
    with pytest.raises(RuntimeError) as raised:
        cache._await_ready(name)
    assert "Container: exited, exit code 5." in str(raised.value)


def test_a_live_containers_exit_code_is_NOT_reported():
    """`{{.State.ExitCode}}` is `0` for a RUNNING container (measured on rows
    one and three), and "exit code 0" printed under a failure is the kind of
    true-looking detail that sends a reader down the wrong path."""
    runtime = FakeRuntime()
    cache = RedisCache(runtime=runtime, ready_timeout=0.05, poll_interval=0.01)
    with pytest.raises(RuntimeError) as raised:
        cache.ensure(ENV, CLUSTER)
    assert "exit code" not in str(raised.value)


def test_a_readiness_timeout_discriminates_a_MUTE_port_from_an_absent_one():
    """Row four: 6379 really IS published (a real `socat TCP-LISTEN:6379
    /dev/null` measured at host port 34045) and nothing behind it speaks RESP.
    That is a different fault from docker never publishing at all, and the old
    message could not tell them apart -- it said nothing in both cases."""
    runtime = FakeRuntime(next_port=1)  # published, and nothing listens on port 1
    cache = RedisCache(runtime=runtime, ready_timeout=0.05, poll_interval=0.01)
    with pytest.raises(RuntimeError) as raised:
        cache.ensure(ENV, CLUSTER)
    message = str(raised.value)
    assert "published on host port 1, which never answered a Redis PING" in message
    assert "never published" not in message


def test_the_log_tail_is_the_bonus_not_the_headline():
    """Row three: redis's own banner ends `Ready to accept connections tcp`, so
    the log alone reads as success. The reason has to lead with the reading that
    contradicts it."""
    banner = "1:M * Server initialized\n1:M * Ready to accept connections tcp"
    runtime = FakeRuntime(log_text=banner)
    cache = RedisCache(runtime=runtime, ready_timeout=0.05, poll_interval=0.01)
    with pytest.raises(RuntimeError) as raised:
        cache.ensure(ENV, CLUSTER)
    message = str(raised.value)
    assert message.index("never published") < message.index("Ready to accept")
    assert message.endswith(f"Its logs:\n{banner}")


def test_status_host_port_and_delete_track_the_container(redis_server):
    runtime = FakeRuntime(next_port=redis_server.port)
    cache = RedisCache(runtime=runtime, poll_interval=0.01)
    cache.ensure(ENV, CLUSTER)
    assert cache.status(ENV, CLUSTER) == "running"
    assert cache.host_port(ENV, CLUSTER) == redis_server.port
    cache.delete(ENV, CLUSTER)
    assert cache.status(ENV, CLUSTER) == "absent"
    assert cache.host_port(ENV, CLUSTER) == 0
