"""Deterministic health assertions — the verifier the LLM is forbidden to be.

Scope: a Postgres (rds) is healthy when a real connection + `SELECT 1`
succeeds. The AWS-shaped PROVISIONED kinds (s3/sqs/sns/dynamodb) are checked
directly against their backing (see reconciler._observe_provisioned), not here.
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


async def pg_ready(host: str, port: int, user: str, password: str, db: str = "postgres") -> PgReady:
    try:
        ok = await asyncio.to_thread(_pg_connect, host, port, user, password, db)
        return PgReady(ok=ok)
    except Exception as exc:
        return PgReady(ok=False, error=str(exc))
