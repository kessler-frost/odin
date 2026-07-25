"""Deterministic health assertions — the verifier the LLM is forbidden to be.

Scope: a Postgres (rds) is healthy when a real connection + `SELECT 1`
succeeds. The AWS-shaped PROVISIONED kinds (s3/sqs/sns/dynamodb) are checked
directly against their backing (see reconciler._observe_provisioned), not here.

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
    """The blocking form -- W2.7's callers are all already off the event loop:
    `gateway/models/rdsctl.py`'s create waiter runs on its own daemon thread
    (the same shape every other substrate-booting gateway model uses), and
    `reconcile/drift.py`'s reality sweep runs inside `asyncio.to_thread`.
    THIS is the assertion that gates an `aws_db_instance` reaching
    `available`, so a Postgres that boots but never accepts connections fails
    the apply instead of being reported up."""
    try:
        return PgReady(ok=_pg_connect(host, port, user, password, db))
    except Exception as exc:
        return PgReady(ok=False, error=str(exc))


async def pg_ready(host: str, port: int, user: str, password: str, db: str = "postgres") -> PgReady:
    return await asyncio.to_thread(pg_ready_sync, host, port, user, password, db)
