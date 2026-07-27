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


def _pg_connect(host: str, port: int, user: str, password: str, db: str) -> bool:
    import psycopg2

    conn = psycopg2.connect(
        host=host, port=port, user=user, password=password,
        dbname=db, connect_timeout=3,
    )
    cur = conn.cursor()
    cur.execute("SELECT 1")
    ok = cur.fetchone()[0] == 1
    conn.close()
    return ok


def pg_ready_sync(host: str, port: int, user: str, password: str, db: str = "postgres") -> PgReady:
    """The blocking form. THIS is the assertion that gates an
    `aws_db_instance` reaching `available`, so a Postgres that boots but never
    accepts connections fails the apply instead of being reported up.

    KNOWN BLOCKING BOUNDARY (v0.7.7, left deliberately visible). psycopg2 has
    no async API, so this call blocks whatever thread -- or, now, whatever
    EVENT LOOP -- it runs on, for up to `connect_timeout=3` seconds. The
    de-threading pass removed the threads that used to keep it off the loop
    (`asyncio.to_thread` in the drift sweep, the rdsctl daemon thread), so
    `pg_ready` below now blocks the shared control loop for real. The fix is
    the psycopg v3 `AsyncConnection` swap, which is a separate, riskier stage;
    naming the limit here beats hiding it behind a thread pool that the owner
    directive rules out anyway."""
    try:
        return PgReady(ok=_pg_connect(host, port, user, password, db))
    except Exception as exc:
        return PgReady(ok=False, error=str(exc))


async def pg_ready(host: str, port: int, user: str, password: str, db: str = "postgres") -> PgReady:
    """The coroutine form callers await. NOT `await pg_ready_sync(...)` --
    that awaited a plain `PgReady` dataclass and raised
    `TypeError: object PgReady can't be used in 'await' expression` on every
    single call. See `pg_ready_sync` for the psycopg2 blocking boundary this
    inherits."""
    return pg_ready_sync(host, port, user, password, db)


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
