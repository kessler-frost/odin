"""A reconciler that stopped converging must be visible in every surface a
user looks at -- `/world`, `/health`, and (via /health) `odin status`.

Before this, a dead loop was invisible: `_run`'s `except Exception` cannot see
`asyncio.CancelledError` (a BaseException), nothing ever inspected the task
afterwards, and `/world` happily served its last snapshot with every phase
still reading `healthy`. These tests kill the REAL task inside the REAL app and
then ask the real routes, so nothing about the signal is fabricated.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from odin import util
from odin.reconcile.reconciler import LoopHealth, Reconciler
from odin.server import _watch_reconcilers, create_app
from odin.spec.models import ResourceDesired, Stack, WorldDelta
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime
from tests.reconcile.test_reconciler import FakeAws

BUCKET = ResourceDesired(id="uploads", kind="s3")


def _seeded_store(tmp_path) -> SpecStore:
    """A store that already has an applied s3 node observed `healthy` -- the
    state whose phases go stale the moment the loop stops."""
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=(BUCKET,)))
    store.apply_delta(WorldDelta(
        env="default", resource_id="uploads", kind="s3", phase="healthy",
        facts={"BUCKET": "uploads"},
    ))
    return store


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://odin.test")


async def test_world_and_health_report_a_dead_reconciler_and_mark_phases_stale(tmp_path):
    app = create_app(
        runtime=FakeRuntime(), store=_seeded_store(tmp_path), rds=FakeRds(),
        aws=FakeAws(), backings=False,
    )
    async with app.router.lifespan_context(app):
        client = await _client(app)
        async with client:
            body = (await client.get("/world")).json()
            assert body["reconciler"]["ticking"] is True, body["reconciler"]
            assert body["reconciler"]["verdict"] is None
            assert body["resources"][0]["phase"] == "healthy"
            assert body["resources"][0]["verdict"] is None

            # Kill it for real, from inside the loop that owns it.
            task = app.state.reconcilers["default"]._task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            body = (await client.get("/world")).json()
            assert body["reconciler"]["ticking"] is False
            assert "CANCELLED" in body["reconciler"]["verdict"]
            # The phase is unchanged and still `healthy` -- which is why the
            # resource itself has to carry the staleness, or a reader
            # iterating `resources` concludes "converging".
            resource = body["resources"][0]
            assert resource["phase"] == "healthy"
            assert resource["verdict"].startswith("[STALE:")
            assert "not a live reading" in resource["verdict"]

            health = (await client.get("/health")).json()
            assert health["ok"] is True  # the HTTP server IS serving; that is a different question
            assert [loop["ticking"] for loop in health["reconcilers"]] == [False]
            assert "is NOT converging" in health["reconcilers"][0]["verdict"]


async def test_a_dead_reconciler_does_not_erase_a_resources_own_verdict(tmp_path):
    """A crashed resource's real reason is not less true because the loop then
    died -- the staleness is PREFIXED, never substituted."""
    store = _seeded_store(tmp_path)
    store.apply_delta(WorldDelta(
        env="default", resource_id="uploads", kind="s3", phase="crashed",
        verdict="the s3 backing is no longer reachable",
    ))
    app = create_app(
        runtime=FakeRuntime(), store=store, rds=FakeRds(), aws=FakeAws(), backings=False,
    )
    async with app.router.lifespan_context(app):
        client = await _client(app)
        async with client:
            task = app.state.reconcilers["default"]._task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            verdict = (await client.get("/world")).json()["resources"][0]["verdict"]
    assert verdict.startswith("[STALE:")
    assert verdict.endswith("the s3 backing is no longer reachable")


async def test_world_never_starts_a_reconciler_just_by_being_read(tmp_path):
    """A read must not have the side effect of minting a loop for an env that
    does not exist -- and it must still be honest that nothing converges it."""
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), aws=FakeAws(), backings=False,
    )
    async with app.router.lifespan_context(app):
        client = await _client(app)
        async with client:
            body = (await client.get("/world", params={"env": "never-applied"})).json()
    assert "never-applied" not in app.state.reconcilers
    assert body["reconciler"]["ticking"] is False
    assert "no reconciler is running for env 'never-applied'" in body["reconciler"]["verdict"]
    assert body["resources"] == []  # nothing to mislabel


class _RecordingWs:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast(self, message: dict) -> None:
        self.messages.append(message)


async def test_the_watchdog_logs_and_broadcasts_a_dead_loop_once_per_transition(tmp_path, caplog):
    store = _seeded_store(tmp_path)
    recon = Reconciler(store, FakeRuntime(), aws=FakeAws(), poll_interval=0.01)
    await recon.start()
    task = recon._task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    ws = _RecordingWs()
    with caplog.at_level(logging.ERROR, logger="odin"):
        watchdog = asyncio.create_task(_watch_reconcilers({"default": recon}, ws, interval=0.01))
        await asyncio.sleep(0.08)  # many checks, one condition
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)

    assert len(ws.messages) == 1, ws.messages  # per transition, never per check
    assert ws.messages[0]["type"] == "log" and ws.messages[0]["level"] == "error"
    assert ws.messages[0]["source"] == "reconciler"
    assert "is NOT converging" in ws.messages[0]["text"]
    assert sum("NOT converging" in record.getMessage() for record in caplog.records) == 1


class _StubLoop:
    def __init__(self) -> None:
        self.state = LoopHealth(env="default", ticking=False, verdict="down for a reason")

    def health(self) -> LoopHealth:
        return self.state


async def test_the_watchdog_announces_the_recovery_too(caplog):
    loop = _StubLoop()
    ws = _RecordingWs()
    with caplog.at_level(logging.WARNING, logger="odin"):
        watchdog = asyncio.create_task(_watch_reconcilers({"default": loop}, ws, interval=0.01))
        await asyncio.sleep(0.05)
        loop.state = LoopHealth(env="default", ticking=True, ticks=7)
        await asyncio.sleep(0.05)
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
    assert any("converging again" in record.getMessage() for record in caplog.records)


async def test_the_watchdog_survives_one_reconciler_raising(caplog):
    class _Broken:
        def health(self):
            raise RuntimeError("boom")

    ws = _RecordingWs()
    good = _StubLoop()
    with caplog.at_level(logging.ERROR, logger="odin"):
        watchdog = asyncio.create_task(
            _watch_reconcilers({"broken": _Broken(), "default": good}, ws, interval=0.01)
        )
        await asyncio.sleep(0.05)
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
    # The broken one is reported, and the healthy pass still reached the other.
    assert any("watchdog failed" in record.getMessage() for record in caplog.records)
    assert [message["env"] for message in ws.messages] == ["default"]


async def test_shutdown_completes_even_when_a_reconciler_task_is_already_dead(tmp_path):
    """The same dead loop also broke odin's SHUTDOWN. `await Reconciler.stop()`
    awaited the task bare, so a cancelled one re-raised CancelledError out of
    `create_app`'s lifespan `finally` -- before the gateway listener was
    stopped (v0.7.7: leaving `serve_on_loop`; `stop_in_thread` before that)
    and before `store_lock.release()`. Exiting the lifespan without raising IS the
    assertion; the freed store lock is the consequence that used to be
    skipped."""
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), aws=FakeAws(), backings=False,
    )
    async with app.router.lifespan_context(app):
        task = app.state.reconcilers["default"]._task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert util.live_server(tmp_path) is not None  # the lock is held while serving
    assert util.live_server(tmp_path) is None  # ...and released on the way out
