"""Deterministic health assertions — the verifier the LLM is forbidden to be.

Scope: a Postgres (rds) is healthy when a real connection + `SELECT 1`
succeeds. The AWS-shaped PROVISIONED kinds (s3/sqs/sns/dynamodb) are checked
directly against their backing (see reconciler._observe_provisioned), not here.

`mesh_ready_sync` is the second assertion, and it exists because EVERY other
probe in odin dials the published HOST port -- so odin could advertise a
`*_MESH` endpoint (the SG-gated overlay address, the only governed path) that
had been dead for minutes while reporting the resource `healthy`. Field test
2 hit exactly that, twice.

W2.7 moved rds onto Terraform, so this assertion's two callers are now the
GATEWAY's RDS model (`gateway/models/rdsctl.py` -- its CreateDBInstance
waiter only reports `available` once this passes, which is what a
`tofu apply` blocks on) and the reality sweep (`reconcile/drift.py` -- a
previously-available instance that stops answering is real drift). It stayed
here, in `reconcile/`, deliberately: it is still "the verifier the LLM is
forbidden to be", not a gateway wire concern.
"""
from __future__ import annotations

import asyncio

import asyncpg

from dataclasses import dataclass


@dataclass(frozen=True)
class PgReady:
    """`pg_ready`'s result -- `ok` is the boolean the reconciler gates
    healthy on; `error` is the real exception text (observability v1: this
    used to be swallowed entirely, so a persistently-misconfigured rds node
    -- bad creds, wrong db name, anything short of "still booting" -- just
    sat in `starting` forever with zero diagnostic trail)."""

    ok: bool
    error: str | None = None


_CONNECT_TIMEOUT = 3.0


def _reason(host: str, port: int, exc: BaseException) -> str:
    """The failure text, ALWAYS naming the address.

    Load-bearing, and the reason this helper exists rather than `str(exc)`:
    `server.py::_known_faults` quotes this sentence WITH the host and port as
    the line that rescues an apply whose provider error reads
    `last error: %!s(<nil>)`. psycopg2 happened to include the address in its
    own message; asyncpg does not, and its connect timeout raises a bare
    `TimeoutError` whose `str()` is EMPTY. So the shape is rebuilt here instead
    of inherited from whatever the driver felt like saying.

    `timeout expired` is likewise the exact wording `_known_faults` matches --
    keep it verbatim.
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        detail = "timeout expired"
    else:
        detail = str(exc).strip() or exc.__class__.__name__
    return f'connection to server at "{host}", port {port} failed: {detail}'


async def pg_ready(host: str, port: int, user: str, password: str, db: str = "postgres") -> PgReady:
    """THE assertion that gates an `aws_db_instance` reaching `available`, so a
    Postgres that boots but never accepts connections fails the apply instead
    of being reported up.

    asyncpg (Apache-2.0), not psycopg2 (LGPL): odin's licence rule is
    permissive-only, and psycopg2 had quietly been the exception. It is also
    the only Postgres driver here with a native async API, which matters more
    than it sounds -- MEASURED against a real wedged Postgres (`docker pause`,
    which is field test 6's F4, i.e. the documented failure mode rather than a
    hypothetical one):

        psycopg2   connect 3008.4 ms   heartbeat ticks:   1   worst gap 3011.5 ms
        async      connect 3033.3 ms   heartbeat ticks: 264   worst gap   12.4 ms

    A healthy or still-booting Postgres fast-fails in 0.42-5.8 ms and would
    NOT have justified this on its own (~1.4% of the loop while booting).
    A wedged one blocks 857 ms per second of wall clock -- 85.7% -- sustained
    for up to `_CREATE_TIMEOUT` = 180 s, freezing the gateway and the
    reconciler together. That is what decided it.
    """
    conn = None
    try:
        conn = await asyncpg.connect(
            host=host, port=port, user=user, password=password,
            database=db, timeout=_CONNECT_TIMEOUT,
        )
        return PgReady(ok=await conn.fetchval("SELECT 1") == 1)
    except Exception as exc:
        return PgReady(ok=False, error=_reason(host, port, exc))
    finally:
        if conn is not None:
            await conn.close()


@dataclass(frozen=True)
class MeshReady:
    """`mesh_ready_sync`'s result. `error` is the probe's REAL output (busybox
    `nc -v`'s own text), never invented -- it becomes the World verdict a user
    reads when the overlay path is down."""

    ok: bool
    error: str | None = None


# The success token. `exec_sh` hands back stdout only, so the script PRINTS
# proof rather than relying on an exit code we'd have to plumb through the
# runtime port -- and an empty stdout (container gone, exec refused, docker
# unavailable) is then automatically "not proven", never a false green.
MESH_PROBE_TOKEN = "odin-mesh-ok"  # noqa: S105 -- a marker string, not a secret


def mesh_probe_script(overlay_ip: str, port: int, timeout: float = 3.0) -> str:
    """The whole probe: one bounded TCP connect to the OVERLAY address.

    `nc -z` (zero-I/O scan) is present in the sidecar image's busybox
    (verified: BusyBox v1.36.1 on alpine:3.20 -- `-z`, `-v` and `timeout` all
    supported), so this needs no addition to the image and works on an image
    already baked on a user's machine. Bounded TWICE on purpose: `-w` bounds
    nc's own connect, and `timeout` bounds nc itself, so a wedged namespace
    can never hang a reconciler tick."""
    return (
        f"timeout {int(timeout) + 2} nc -vz -w {int(timeout)} {overlay_ip} {port} 2>&1"
        f" && echo {MESH_PROBE_TOKEN}"
    )


async def mesh_ready_sync(runtime, sidecar: str, overlay_ip: str, port: int, timeout: float = 3.0) -> MeshReady:
    """Does the address odin PUBLISHES as the mesh endpoint actually answer,
    on the overlay, right now?

    Run from INSIDE the member's own network namespace (the nebula sidecar
    shares it, `fabric/sidecar.py`), which is the only rootless place a check
    can stand: the macOS host is deliberately NOT a mesh data-plane member (a
    host tun device would need a sudoers grant -- see fabric/nebula.py's R4
    note), so the host itself cannot dial an overlay IP at all.

    WHAT THIS PROVES: the nebula tun device exists in the target's CURRENT
    namespace with the overlay IP assigned, and the upstream process really is
    listening on that address -- i.e. `endpoint_mesh` names something alive.
    It is exactly what was false in field test 2: with the sidecar stranded in
    a dead container's namespace, the overlay IP was up but nothing answered
    on it, while the host-port probe stayed green.

    WHAT IT DOES NOT PROVE: that a given REMOTE peer can reach it -- that also
    needs the env's lighthouse (discovery + relay) and depends on the peer's
    certificate satisfying this member's compiled SG firewall. Lighthouse
    liveness is checked separately (`reconcile/mesh_health.py`); the SG part is
    a policy decision, not a fault, so no probe should ever "fix" it."""
    out = await runtime.exec_sh(sidecar, mesh_probe_script(overlay_ip, port, timeout))
    if MESH_PROBE_TOKEN in out:
        return MeshReady(ok=True)
    return MeshReady(ok=False, error=out.strip() or f"nothing answered at {overlay_ip}:{port} on the overlay")
