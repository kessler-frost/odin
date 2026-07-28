"""The live event stream: Server-Sent Events, one direction, which is all odin ever used.

## Why this stopped being a WebSocket (owner call, 2026-07-28)

The socket was strictly server->client and the code said so plainly: the UI made
ZERO `.send()` calls, and the server's handler was

    while True:
        await websocket.receive_text()      # return value discarded

-- a full-duplex protocol carrying traffic one way, with the receive loop there
only to notice a disconnect. What that cost was reconnection: `BottomPanel.tsx`
hand-rolled `ws.onclose -> setTimeout(connect, delay)` with its own backoff and
its own `reconnecting` state, because a WebSocket gives you none of that.

`EventSource` reconnects by itself. It is also plain HTTP, which matters here for
a specific reason rather than a general one: odin gets served over Tailscale, and
proxies mishandle the WS upgrade handshake far more often than they mishandle
`text/event-stream`.

## What replaces the socket set

A `set[asyncio.Queue]`, one queue per open stream. `broadcast` is UNCHANGED from
every caller's point of view -- same signature, same durable-log-first ordering
-- which is what let this swap happen without touching the reconciler, the tf
runner, or the canvas router.

## Falling behind, and why a slow viewer is dropped rather than buffered

Each queue is BOUNDED. An unbounded one would let a stalled browser tab grow the
server's memory without limit, and the messages odin sends are not small (a
`world_delta` carries facts, a `log` carries a crash tail). When a queue fills,
the subscriber gets a `None` sentinel and its stream ENDS -- `EventSource` then
reconnects on its own and the UI backfills from `/events`, which is exactly the
recovery path that already existed for a dropped socket.

Dropping the connection rather than the message is the deliberate half. Silently
skipping one delta leaves a tab subtly, permanently wrong with nothing to
indicate it; ending the stream is self-healing and visible (the LED flicks to
reconnecting). The durable per-env log is the source of truth either way.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from odin.util import secure_append_line

# How many undelivered messages one viewer may fall behind by. Generous enough
# that an ordinary render hitch never trips it, small enough that a hung tab
# cannot cost real memory: an apply can emit a few dozen events in a burst.
_QUEUE_DEPTH = 256

# Seconds between `: ping` comments on an idle stream. Proxies and load
# balancers close connections that go quiet, and a comment line is the cheapest
# thing SSE can carry -- clients ignore it, it is not an event.
HEARTBEAT_SECONDS = 15.0


class ConnectionManager:
    """Fan-out to every open SSE stream, plus the durable per-env event log.

    The log is scoped per environment (`<root>/<env>/events.jsonl`, parallel to
    that env's `world.json`), so the log panel never mixes envs.
    """

    def __init__(self, root: Path | str = ".odin") -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._root = Path(root)

    def _log(self, env: str) -> Path:
        return self._root / env / "events.jsonl"

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        """One stream's queue, removed again however the stream ends -- returned,
        raised through, or cancelled when the client hangs up."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Persist, then fan out. Never raises, never blocks on a slow viewer.

        The durable write happens FIRST and unconditionally: it is the source of
        truth `/events` serves, and a viewer that misses a live message backfills
        from it. Broadcasting is best-effort by design -- a broken viewer must
        never stall reconciliation.
        """
        env = message.get("env", "default")
        # 0600, not umask's 0644: a delta's facts/verdict carry live credentials
        # in cleartext (see `secure_append_line`).
        secure_append_line(self._log(env), json.dumps(message))
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # This viewer is too far behind to catch up in order. End its
                # stream instead of dropping a delta into a gap it cannot see:
                # it reconnects and backfills. `None` is the only sentinel this
                # queue ever carries, and a real message is always a dict.
                #
                # Room has to be MADE for the sentinel: the queue is full -- that
                # is why we are here -- so a plain `put_nowait(None)` raises and
                # the sentinel never lands. The first version suppressed that
                # exception, which left the stream draining its buffer and then
                # blocking on `get()` FOREVER, waking only to emit heartbeats: a
                # dropped viewer that never learned it had been dropped. Discard
                # the oldest message to make the slot; this viewer is being
                # dropped anyway and will backfill from `/events`.
                self._subscribers.discard(queue)
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(None)

    def close_all(self) -> None:
        """End every open stream. Called on server SHUTDOWN, and load-bearing.

        MEASURED, and the reason this exists: uvicorn's graceful shutdown waits
        for in-flight requests, and an SSE response is an in-flight request that
        by design never finishes -- `event_stream` loops forever emitting
        heartbeats. With one browser tab open the server ignored TWO SIGTERMs and
        was still alive after four minutes, holding its gateway port so nothing
        could restart. Ctrl-C would have hung a user exactly the same way.

        The sentinel makes each generator return, the response completes, and
        uvicorn can finish. Same `None` the overflow path uses, and the same
        recovery: EventSource reconnects when the server comes back.
        """
        for queue in list(self._subscribers):
            self._subscribers.discard(queue)
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()  # guarantee room, as in `broadcast`
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    def get_events(self, env: str = "default") -> list[dict[str, Any]]:
        path = self._log(env)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sse(message: dict[str, Any]) -> str:
    """One SSE frame. `data:` then a BLANK line -- the blank line is what marks
    the end of an event, and omitting it makes the client wait forever for a
    message it has already received in full."""
    return f"data: {json.dumps(message)}\n\n"


async def event_stream(manager: ConnectionManager) -> AsyncIterator[str]:
    """The response body: every broadcast, plus a heartbeat so an idle stream
    stays open through a proxy.

    Ends when the manager drops this subscriber (see `broadcast`) or when the
    client hangs up, which cancels this generator and runs `subscribe`'s
    `finally`.
    """
    async with manager.subscribe() as queue:
        # An immediate comment so the client's `onopen` fires now rather than at
        # the first real event -- otherwise a quiet system looks like a failed
        # connection, and the UI's status LED would sit on "connecting".
        yield ": connected\n\n"
        while True:
            try:
                async with asyncio.timeout(HEARTBEAT_SECONDS):
                    message = await queue.get()
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if message is None:
                return
            yield sse(message)


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx and friends buffer proxied responses by default, which turns a live
    # stream into a batch delivered whenever the buffer fills. Harmless when odin
    # is dialled directly; load-bearing the moment it is behind anything.
    "X-Accel-Buffering": "no",
}
