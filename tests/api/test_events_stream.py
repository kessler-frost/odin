"""The SSE event stream that replaced the WebSocket (v0.8.7).

The socket it replaces was strictly server->client: the UI made zero `.send()`
calls and the endpoint discarded everything it received. What that cost was
reconnection — the browser hand-rolled `onclose -> setTimeout(connect, delay)`
with its own backoff — which `EventSource` does natively.

`tests/api/test_ws.py`'s three cases moved here unchanged in meaning: broadcast
delivers, the durable log persists, and events are scoped per env. What is new is
everything about a stream that can fall BEHIND, which a socket's `send_json`
never made anyone think about.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from odin.api.events import HEARTBEAT_SECONDS, ConnectionManager, event_stream, sse


async def _drain(manager: ConnectionManager, count: int, timeout: float = 2.0) -> list[str]:
    """`count` frames off a live stream, with a bound so a hang fails instead of
    stalling the suite."""
    frames: list[str] = []
    stream = event_stream(manager)
    async with asyncio.timeout(timeout):
        frames.append(await anext(stream))  # the `: connected` comment
        while len(frames) < count + 1:
            frames.append(await anext(stream))
    return frames


# --- the three cases that came from test_ws.py --------------------------------


async def test_broadcast_reaches_a_live_stream_and_persists(tmp_path):
    manager = ConnectionManager(tmp_path)
    stream = event_stream(manager)
    assert await anext(stream) == ": connected\n\n"

    await manager.broadcast({"type": "world_delta", "env": "default", "resource_id": "db"})
    frame = await asyncio.wait_for(anext(stream), timeout=2)

    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert json.loads(frame[len("data: "):])["resource_id"] == "db"
    assert manager.get_events("default")[0]["resource_id"] == "db", "the durable log is the source of truth"


async def test_events_are_scoped_per_env(tmp_path):
    manager = ConnectionManager(tmp_path)
    await manager.broadcast({"type": "log", "env": "prod", "line": "a"})
    await manager.broadcast({"type": "log", "env": "staging", "line": "b"})

    assert [e["line"] for e in manager.get_events("prod")] == ["a"]
    assert [e["line"] for e in manager.get_events("staging")] == ["b"]


async def test_a_broadcast_with_no_listeners_still_persists(tmp_path):
    """Reconciliation runs whether or not anyone is watching, and `/events` is
    what a tab opened later reads."""
    manager = ConnectionManager(tmp_path)
    await manager.broadcast({"type": "world_delta", "env": "default", "resource_id": "db"})
    assert manager.subscriber_count == 0
    assert len(manager.get_events("default")) == 1


# --- the subscriber set --------------------------------------------------------


async def test_every_open_stream_gets_every_message(tmp_path):
    manager = ConnectionManager(tmp_path)
    a, b = event_stream(manager), event_stream(manager)
    await anext(a)
    await anext(b)
    assert manager.subscriber_count == 2

    await manager.broadcast({"type": "canvas_updated", "env": "default", "rev": "r1"})
    for stream in (a, b):
        frame = await asyncio.wait_for(anext(stream), timeout=2)
        assert json.loads(frame[len("data: "):])["rev"] == "r1"


async def test_a_closed_stream_stops_being_a_subscriber(tmp_path):
    """`subscribe`'s `finally` runs however the generator ends — returned, raised
    through, or CANCELLED when the client hangs up, which is the common case."""
    manager = ConnectionManager(tmp_path)
    stream = event_stream(manager)
    await anext(stream)
    assert manager.subscriber_count == 1

    await stream.aclose()
    assert manager.subscriber_count == 0


async def test_broadcast_never_raises_when_nobody_is_reading(tmp_path):
    """A broken viewer must never stall reconciliation — the property the old
    manager protected with a try/except around `send_json`."""
    manager = ConnectionManager(tmp_path)
    stream = event_stream(manager)
    await anext(stream)
    for i in range(500):  # far past the queue depth, deliberately unread
        await manager.broadcast({"type": "log", "env": "default", "line": str(i)})
    assert len(manager.get_events("default")) == 500


# --- falling behind ------------------------------------------------------------


async def test_a_viewer_that_falls_too_far_behind_has_its_stream_ENDED(tmp_path):
    """The design decision worth testing: a stalled tab is DROPPED, not buffered
    without limit and not silently skipped.

    Silently skipping one delta leaves a tab subtly and permanently wrong with
    nothing to show for it. Ending the stream is self-healing: EventSource
    reconnects and the UI backfills from `/events`.
    """
    manager = ConnectionManager(tmp_path)
    stream = event_stream(manager)
    await anext(stream)

    for i in range(400):  # past _QUEUE_DEPTH without reading
        await manager.broadcast({"type": "log", "env": "default", "line": str(i)})
    assert manager.subscriber_count == 0, "the stalled viewer should have been dropped"

    # It drains what it did receive, then the stream ENDS rather than hanging.
    with pytest.raises(StopAsyncIteration):
        async with asyncio.timeout(5):
            while True:
                await anext(stream)


async def test_one_slow_viewer_does_not_affect_a_healthy_one(tmp_path):
    manager = ConnectionManager(tmp_path)
    slow, fast = event_stream(manager), event_stream(manager)
    await anext(slow)
    await anext(fast)

    async def keep_reading():
        with contextlib_suppress():
            while True:
                await anext(fast)

    reader = asyncio.create_task(keep_reading())
    for i in range(400):
        await manager.broadcast({"type": "log", "env": "default", "line": str(i)})
        await asyncio.sleep(0)
    reader.cancel()

    assert manager.subscriber_count == 1, "the healthy viewer must survive the slow one being dropped"


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(StopAsyncIteration, asyncio.CancelledError)


# --- the wire format -----------------------------------------------------------


def test_a_frame_ends_with_a_BLANK_line():
    """The blank line is what marks an event complete. Without it a client waits
    forever for a message it has already received in full."""
    frame = sse({"type": "log", "line": "hello"})
    assert frame == 'data: {"type": "log", "line": "hello"}\n\n'


def test_a_frame_is_one_line_of_json_even_when_the_payload_has_newlines():
    """A crash tail carries newlines, and a raw newline inside `data:` would
    split one event into two malformed ones. `json.dumps` escapes them."""
    frame = sse({"type": "log", "line": "Traceback\n  File x\nBoom"})
    assert frame.count("\n\n") == 1
    assert frame.rstrip("\n").count("\n") == 0


async def test_an_idle_stream_sends_a_heartbeat_comment(monkeypatch):
    """Proxies close quiet connections. A comment costs nothing and clients
    ignore it -- EventSource never surfaces it as a message."""
    monkeypatch.setattr("odin.api.events.HEARTBEAT_SECONDS", 0.05)
    manager = ConnectionManager(".odin-unused")
    stream = event_stream(manager)
    assert await anext(stream) == ": connected\n\n"
    assert await asyncio.wait_for(anext(stream), timeout=2) == ": ping\n\n"


def test_the_heartbeat_is_frequent_enough_to_matter():
    """A heartbeat slower than a common proxy idle timeout (30-60s) would not do
    its job."""
    assert 0 < HEARTBEAT_SECONDS <= 30


# --- shutdown ------------------------------------------------------------------


async def test_close_all_ends_every_open_stream(tmp_path):
    """The bug this caught cost the most time of anything in the migration.

    uvicorn's graceful shutdown waits for in-flight requests, and an SSE response
    IS an in-flight request that by design never finishes -- `event_stream` loops
    forever emitting heartbeats. Measured with one browser tab open: the server
    ignored two SIGTERMs, was still alive after four minutes, and held its
    gateway port so nothing could restart. A user pressing Ctrl-C would have hung
    identically.
    """
    manager = ConnectionManager(tmp_path)
    a, b = event_stream(manager), event_stream(manager)
    await anext(a)
    await anext(b)
    assert manager.subscriber_count == 2

    manager.close_all()
    assert manager.subscriber_count == 0
    for stream in (a, b):
        with pytest.raises(StopAsyncIteration):
            async with asyncio.timeout(2):
                while True:
                    await anext(stream)


async def test_close_all_ends_a_stream_whose_queue_is_FULL(tmp_path):
    """The sentinel needs ROOM. A full queue rejects it, and the stream would go
    on waiting -- which is the same hang, reached a different way. `broadcast`'s
    overflow path had this exact bug first."""
    manager = ConnectionManager(tmp_path)
    stream = event_stream(manager)
    await anext(stream)
    for i in range(300):  # fill it without reading
        await manager.broadcast({"type": "log", "env": "default", "line": str(i)})

    manager.close_all()
    with pytest.raises(StopAsyncIteration):
        async with asyncio.timeout(5):
            while True:
                await anext(stream)


async def test_close_all_with_nobody_connected_is_a_no_op(tmp_path):
    manager = ConnectionManager(tmp_path)
    manager.close_all()
    assert manager.subscriber_count == 0
