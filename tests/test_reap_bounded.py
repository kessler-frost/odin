"""`util.reap` must never hang, and the case that hangs it is not exotic.

`reap` exists to kill and COLLECT a subprocess that a cancellation left behind
-- collecting it is what closes asyncio's transport and stops
`PytestUnraisableExceptionWarning: Event loop is closed` coming out of the
garbage collector. The obvious implementation (`kill()` then `await
proc.wait()`) is wrong in a way no ordinary test notices:

`Process.wait()` does NOT resolve when the process dies. CPython resolves its
waiters in `BaseSubprocessTransport._call_connection_lost`, which `_try_finish`
gates on every pipe being disconnected. A killed process that left a GRANDCHILD
holding the inherited stdout pipe therefore never reaches EOF, and `wait()`
never returns -- with the process we killed provably gone.

`reap` runs in a `finally` on the CANCELLATION path, so an unbounded wait there
blocks shutdown forever: worse than the warning it removes. This pins the
bound.

MUTATION-TESTED: replace `reap`'s bounded wait with a bare `await proc.wait()`
and `test_reap_returns_even_when_a_grandchild_holds_the_pipe` hangs until its
own timeout and fails. Both other tests still pass under that mutation, which
is exactly why this one has to exist.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from odin.util import REAP_TIMEOUT, reap

# `trap '' TERM` stops sh from exec-ing the sleep, so there really are two
# processes: the shell we kill, and a grandchild that outlives it holding the
# stdout pipe we handed the shell.
_LEAVES_A_GRANDCHILD = "trap '' TERM; sleep 600"

# Deliberately NOT tight, and deliberately not a multiple of REAP_TIMEOUT. What
# is under test is HANG vs NO-HANG, and a tight bound would measure the machine
# rather than the code (`reap` returns in ~1ms or, unfixed, in `sleep 600`'s
# lifetime -- there is nothing in between to be precise about). A fixed slack
# also keeps this honest if REAP_TIMEOUT is ever retuned down.
_NO_HANG = REAP_TIMEOUT + 5.0


async def _spawn(script: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", script, stdout=asyncio.subprocess.PIPE,
    )


def _children(pid: int) -> list[str]:
    return subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()


def _alive(pid: str) -> bool:
    return subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0


async def test_reap_returns_even_when_a_grandchild_holds_the_pipe() -> None:
    """THE test. An unbounded `await proc.wait()` never returns here."""
    proc = await _spawn(_LEAVES_A_GRANDCHILD)
    await asyncio.sleep(0.3)
    grandchildren = _children(proc.pid)
    assert grandchildren, "the fixture must really leave a grandchild, or this proves nothing"
    try:
        async with asyncio.timeout(_NO_HANG):
            await reap(proc)
    finally:
        for pid in grandchildren:
            subprocess.run(["kill", "-9", pid], capture_output=True)
    # The process we were asked to reap is dead. That is reap's real contract;
    # collecting the exit status is best-effort by construction, because the
    # grandchild can hold the pipe open for longer than any bound.
    assert not _alive(str(proc.pid)), "reap must leave no live process behind"


async def test_reap_collects_an_ordinary_child_promptly() -> None:
    """The common path: no grandchild, so the pipe hits EOF and the child is
    really collected. Deliberately does NOT assert that it beat the bound --
    `_process_exited` sets `returncode` the moment the child dies whether or not
    `wait()` ever resolves, so there is no in-band witness here for "the wait
    returned" as against "the wait timed out", and an elapsed-time assertion
    would measure the machine instead. `reap`'s own docstring carries the
    ~1ms figure; this pins the outcome."""
    proc = await _spawn("sleep 600")
    await asyncio.sleep(0.2)
    async with asyncio.timeout(_NO_HANG):
        await reap(proc)
    assert proc.returncode == -9, proc.returncode


async def test_reap_is_a_no_op_once_the_child_has_been_collected() -> None:
    """`finally` means this runs on EVERY return, not just the cancelled one --
    so the success path must cost nothing and must not re-signal anything."""
    proc = await _spawn("exit 3")
    await proc.communicate()
    assert proc.returncode == 3
    await reap(proc)
    assert proc.returncode == 3, "reap must not disturb an already-collected child"


@pytest.mark.parametrize("script", ["exit 0", "sleep 600", _LEAVES_A_GRANDCHILD])
async def test_reap_never_raises(script: str) -> None:
    """Whatever state the child is in, reap is cleanup code in a `finally`: it
    must not replace the exception being unwound with one of its own. Covers
    the already-exited-but-uncollected window, where `kill()` targets a pid the
    OS may have reaped."""
    proc = await _spawn(script)
    kids = _children(proc.pid)
    async with asyncio.timeout(_NO_HANG):
        await reap(proc)
    for pid in kids:
        subprocess.run(["kill", "-9", pid], capture_output=True)
