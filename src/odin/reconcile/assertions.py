"""Deterministic health assertions — the verifier the LLM is forbidden to be.

Scope: a Postgres (rds) is healthy when a real connection + `SELECT 1`
succeeds. The AWS-shaped PROVISIONED kinds (s3/sqs/sns/dynamodb) are checked
directly against their backing (see reconciler._observe_provisioned), not here.
"""
from __future__ import annotations

import asyncio


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


async def pg_ready(host: str, port: int, user: str, password: str, db: str = "postgres") -> bool:
    try:
        return await asyncio.to_thread(_pg_connect, host, port, user, password, db)
    except Exception:
        return False
