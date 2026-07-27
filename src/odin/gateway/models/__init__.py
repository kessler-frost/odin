"""Per-service gateway model modules (research-coverage §3: "the coverage
work generalizes synth.py from a handful of gap-fill handlers into
per-service model modules"). Each module owns create/describe/delete over a
per-env state store for a service that has NO backing container -- the
module IS the whole service, dispatched from `synth.pure_answer`.

Also home to `background()`, the one place v0.7.7's thread->task conversion
is explained, because every model in here used to spawn a daemon thread for
the same shape of work (boot a container, wait for it, write the outcome
back into the store) and they all now spawn a task instead.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Coroutine, Iterable
from typing import Any

log = logging.getLogger("odin.gateway.models")

# WHY A MODULE-LEVEL SET RATHER THAN A TaskGroup (v0.7.7, the lifetime
# question the de-threading brief asks to answer explicitly).
#
# A `threading.Thread(daemon=True)` had a lifetime these call sites relied on
# without ever writing it down: the interpreter holds a reference for as long
# as the thread runs, and the thread dies with the process. `asyncio` gives
# neither for free. The event loop keeps only a WEAK reference to a running
# task, so a bare `asyncio.create_task(...)` whose result nobody stores can be
# garbage-collected mid-flight -- a boot that simply stops happening, with no
# error anywhere, which is precisely the "silent hang" every one of these
# modules' docstrings forbids.
#
# `asyncio.TaskGroup` is the structured answer and is the wrong tool HERE: it
# requires a live `async with` scope that OUTLIVES its children, and these are
# fire-and-forget from a request handler that must return the `pending` /
# `creating` response immediately (that render-before-spawn ordering is itself
# load-bearing -- see `ec2compute._run_instances`). Owning a group would mean
# threading a nursery from the app lifespan into every model's dispatch, and
# awaiting it would turn an async create back into a synchronous one.
#
# So: a strong reference in a module-level set, discarded by a done-callback.
# That is the documented CPython remedy for the GC hazard, and it reproduces
# the daemon-thread semantics these call sites were written against -- runs
# unattended, holds nothing open, dies with the process. What it deliberately
# does NOT reproduce is silence: a task that raises has its exception logged
# here, whereas a thread that raised printed to stderr and was forgotten.
_background: set[asyncio.Task] = set()


def _reap(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # Every caller of `background()` already wraps its own body in the
        # broad try/except its docstring explains, so reaching here means a
        # failure that escaped even that -- never swallowed, because an
        # unretrieved task exception is otherwise only reported at GC time.
        log.error("background task %s failed", task.get_name(), exc_info=task.exception())


def background(coro: Coroutine[Any, Any, None], name: str = "") -> asyncio.Task:
    """Run `coro` unattended, holding a strong reference for its whole life.

    The direct replacement for this package's old
    `threading.Thread(..., daemon=True).start()`. Returns the task so a caller
    that wants to WAIT for the work it just started can (`converge_services`
    and friends hand the list to their `wait_for_*` twin) -- but a caller that
    drops the return value is safe too, which is the entire point."""
    task = asyncio.create_task(coro, name=name or getattr(coro, "__name__", "odin-background"))
    _background.add(task)
    task.add_done_callback(_reap)
    return task


async def join(converging: Iterable[Awaitable[None]], timeout: float) -> None:
    """Wait for every started background item, but never longer than `timeout`.

    The exact replacement for the `for thread in converging:
    thread.join(remaining)` loop each `wait_for_*` used to open with, and
    `asyncio.wait` is the primitive that reproduces it -- NOT
    `asyncio.timeout` around a `gather`, which CANCELS the children when the
    budget runs out. A daemon thread that outran its join kept running and
    still recorded its outcome, and these tasks must too: `_finish_create` /
    `_finish_deploy` write the real `failed` reason AFTER the wait gives up on
    them, and that reason is what the apply's own verdict is built from.
    Cancelling them would replace a real explanation with silence."""
    pending = [asyncio.ensure_future(item) for item in converging]
    if pending:
        await asyncio.wait(pending, timeout=max(0.0, timeout))
