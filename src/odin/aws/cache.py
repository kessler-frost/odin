"""ElastiCache clusters as REAL per-cluster `redis:7-alpine` containers (W2.8)
-- the substrate binding for `gateway/models/cachectl.py`.

Shape mirrors `compute/functions.py::FunctionRuntime` and `aws/rds.py::
PostgresRds`, NOT `aws/backings.py`: this is a MANY-per-resource binding (one
Redis container per cache cluster, like one Postgres container per rds node),
not backings.py's one-shared-container-per-env-per-kind shape. Container
naming: `odin-cache-{env}-{cluster_id}` -- matching `PostgresRds`'s own
`odin-rds-{env}-{db_id}` convention, and the ONLY name this module ever passes
to the runtime driver. Every container the driver starts is labelled `odin=1`
(runtime/colima.py), so `docker ps --filter label=odin=1` and `odin clean`
find these like any other odin container.

Lives under `aws/` (with rds.py/backings.py, odin's AWS *data*-service
substrates) rather than `compute/` (the EC2/ECS/Lambda compute substrates).
Unlike rds.py it is NOT a reconciler seam: `elasticache` is a TF-owned kind,
so the gateway's own CreateCacheCluster/DeleteCacheCluster handlers drive this
lifecycle and the reconciler only ever OBSERVES the result
(reconcile/tf_status.py).

`redis:7-alpine` is Redis 7.x under the 3-clause BSD licence -- permissive,
fine per odin's licence rule, and ~15MB.

Readiness is a REAL Redis `PING` over the RESP wire, not a bare TCP connect:
Redis accepts a socket slightly before it will serve commands, and the whole
point of `available` in DescribeCacheClusters is that a consumer's very next
`SET` works. `resp_call` below is a deliberately tiny, dependency-free RESP2
client -- odin ships no `redis` package (nor needs one: a readiness PING, a
version read, and whatever a test asserts with is the entire data-plane
surface odin itself ever drives; real consumers dial the published port with
their own client).
"""
from __future__ import annotations

import socket
import time

from odin.runtime.colima import ColimaRuntime, ContainerSpec

REDIS_IMAGE = "redis:7-alpine"  # Redis 7.x, BSD-3-Clause
REDIS_PORT = 6379
READY_TIMEOUT = 120.0  # first-run image pull included (a ~15MB fetch)

# Owner directive B4 (the same cap compute/tasks.py + compute/functions.py
# apply): a runaway cache can't eat the host. ElastiCache's node type is the
# real-AWS knob for this, but it names EC2-class hardware odin has no mapping
# for, so cachectl.py accepts the node type verbatim (the `ImageId` precedent
# in gateway/models/ec2compute.py) and every cluster gets this fixed cap.
DEFAULT_MEMORY_MIB = 256.0


def container_name(env: str, cluster_id: str) -> str:
    return f"odin-cache-{env}-{cluster_id}"


def resp_call(port: int, *args: str, host: str = "127.0.0.1", timeout: float = 2.0) -> str | None:
    """Run one real Redis command on a host-published port and return its
    reply with the RESP type byte stripped -- so `PING` -> `"PONG"`, `SET k v`
    -> `"OK"`, `GET k` -> the value (None when the key is missing), and an
    ERROR reply -> its message text. Raises `OSError` when nothing is
    listening yet; `ping()` below is the caller that treats that as normal.
    """
    payload = f"*{len(args)}\r\n" + "".join(f"${len(a)}\r\n{a}\r\n" for a in args)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(payload.encode())
        with sock.makefile("rb") as stream:
            return _reply(stream)


def _reply(stream) -> str | None:
    """One RESP2 reply. `+simple` / `-error` / `:integer` are all "the rest of
    the line"; `$N` is a bulk string whose N bytes follow (N < 0 == nil)."""
    line = stream.readline().rstrip(b"\r\n").decode()
    if line[:1] != "$":
        return line[1:]
    length = int(line[1:])
    return None if length < 0 else stream.read(length + 2)[:length].decode()


def ping(port: int, timeout: float = 1.0) -> bool:
    try:
        return resp_call(port, "PING", timeout=timeout) == "PONG"
    except OSError:
        return False  # not listening yet -- the readiness loop's normal case


def engine_version(port: int) -> str:
    """The REAL Redis version the container is running (`INFO server`), so
    DescribeCacheClusters advertises the substrate's own version rather than a
    hardcoded fiction. `""` when unreadable -- cachectl.py falls back to its
    documented default then."""
    try:
        info = resp_call(port, "INFO", "server") or ""
    except OSError:
        return ""
    line = next((row for row in info.splitlines() if row.startswith("redis_version:")), "")
    return line.partition(":")[2].strip()


class RedisCache:
    """Per-cluster Redis container lifecycle on an injectable `RuntimeDriver`
    -- the same seam `BackingAws`/`FunctionRuntime`/`TaskRuntime` use, so a
    unit test injects a fake runtime with no real Docker involved."""

    def __init__(self, runtime=None, ready_timeout: float = READY_TIMEOUT, poll_interval: float = 0.5) -> None:
        # `runtime` defaults lazily (ec2compute.py's `vm or InstanceVm()`
        # precedent) so cachectl.py's `pure_answer` can build one per call
        # with no shared state; the timeouts are constructor knobs purely for
        # testability -- real callers keep the module defaults.
        self._rt = runtime or ColimaRuntime()
        self._ready_timeout = ready_timeout
        self._poll_interval = poll_interval

    def ensure(self, env: str, cluster_id: str, memory_mib: float = DEFAULT_MEMORY_MIB) -> int:
        """(Re)create the cluster's Redis container and block until it answers
        a real PING; returns the host port Docker published. Raises on a
        boot/readiness failure -- the caller (cachectl.py's background thread)
        turns that into a real, provider-visible failure state, never a silent
        hang (the same contract as `FunctionRuntime.ensure`)."""
        name = container_name(env, cluster_id)
        self._rt.stop(name)  # clear any exited remnant (PostgresRds's own contract)
        self._rt.run_container(ContainerSpec(
            name=name, image=REDIS_IMAGE, ports={REDIS_PORT: 0},
            labels={"odin-env": env, "odin-cache-cluster": cluster_id},
            memory_mib=memory_mib,
        ))
        return self._await_ready(name)

    def _await_ready(self, name: str) -> int:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            port = self._rt.host_port(name, REDIS_PORT)
            if port and ping(port):
                return port
            time.sleep(self._poll_interval)
        raise RuntimeError(f"{name} redis never became ready:\n{self._rt.logs(name)}")

    def host_port(self, env: str, cluster_id: str) -> int:
        return self._rt.host_port(container_name(env, cluster_id), REDIS_PORT)

    def status(self, env: str, cluster_id: str) -> str:
        return self._rt.status(container_name(env, cluster_id))

    def delete(self, env: str, cluster_id: str) -> None:
        self._rt.stop(container_name(env, cluster_id))
