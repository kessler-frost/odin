"""Deterministic health assertions — the verifier the LLM is forbidden to be.

Skeleton scope: an app is healthy when its HTTP endpoint answers; a Postgres is
healthy when a real connection + `SELECT 1` succeeds. M4 generalizes these into
a per-kind probe registry.
"""
from __future__ import annotations

import asyncio

import httpx


async def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return 200 <= resp.status_code < 400
    except (httpx.HTTPError, OSError):
        return False


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
