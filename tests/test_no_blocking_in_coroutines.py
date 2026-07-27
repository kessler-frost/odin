"""A coroutine that blocks is invisible. This makes it visible.

The v0.7.7 de-threading work removed odin's thread pool, and in doing so it
introduced a defect strictly WORSE than the threads it replaced. `await f()`
on a synchronous function raises TypeError, so that mistake reports itself.
The opposite mistake does not: an `async def` whose body still blocks awaits
perfectly happily and stalls the reconciler and the gateway together, with
nothing in the logs. A thread that blocks is fine; a coroutine that blocks
looks exactly like working code.

The real incident: `gateway/app.py` carried a comment saying one site had to
STAY a thread because lambda invoke blocking is unbounded (30s) and
re-entrant, and that the thread could only go once `compute/functions.py` used
`httpx.AsyncClient`. A blanket rewrite removed the thread without the
prerequisite, leaving a blocking `httpx.post(timeout=30.0)` on the shared
loop -- able to freeze everything for 30s and to DEADLOCK a re-entrant invoke,
because the loop that would serve the callback is the one blocked inside the
invoke. No test failed. An AST sweep found it in seconds.

So this is a ratchet, not a cleanup: every known offender is listed in
`ALLOWED` with its owner and reason, and anything NEW fails. Fixing one means
deleting its line here.

LIMITS, stated rather than implied:
  * it only sees calls written literally inside an `async def`. A coroutine
    that calls a SYNC helper which blocks is invisible to it -- an alias, not
    a guarantee.
  * it is a static check. The runtime companion is asyncio's debug mode
    (`asyncio.run(..., debug=True)` plus `loop.slow_callback_duration`), which
    catches blocking this list never thought to name (a C extension, a DNS
    lookup). Measured: it reports `Executing <Task ...> took 0.410 seconds`
    -- the duration and a location, but it names the outer TASK, not the inner
    coroutine. The two checks are complementary: this one pinpoints what it
    knows about, that one notices what nobody predicted.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "odin"

# Dotted names that block the calling thread. The sync `httpx` API is here
# because it is the easiest of all of these to reach for by habit.
BLOCKING = {
    "time.sleep",
    "subprocess.run", "subprocess.call", "subprocess.check_output",
    "subprocess.check_call", "subprocess.Popen",
    "httpx.get", "httpx.post", "httpx.put", "httpx.delete", "httpx.patch",
    "httpx.head", "httpx.request", "httpx.stream", "httpx.Client",
    "requests.get", "requests.post", "requests.request",
    "socket.create_connection",
    "urllib.request.urlopen",
    "psycopg2.connect",
}

# Known offenders: (module path, coroutine name, call) -> why it is still here.
# Delete an entry when you fix it; do NOT add one to make a new failure go away
# without an owner and a reason.
ALLOWED: dict[tuple[str, str, str], str] = {
    ("compute/instances.py", "_discover_ip", "time.sleep"):
        "v0.7.7 stage C, owner dethread-control-src: becomes await asyncio.sleep.",
    ("aws/rds.py", "set_password", "psycopg2.connect"):
        "v0.7.7 stage E: psycopg2 has no async API; the fix is the psycopg v3 "
        "AsyncConnection swap, which is a driver change and deliberately not "
        "part of the de-threading pass. Boundary left visible on purpose.",
}


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _aliases(tree: ast.Module) -> dict[str, str]:
    """local name -> real module, so `import time as t; t.sleep()` is caught.

    Found by mutation-testing this file: the first fault injection used
    `import time as _probe_time` and the checker did not fire. That was a bad
    injection AND a real hole -- a rename is exactly how this check would rot
    silently. Covers `import x as y`, `from a import b as c`, and the plain
    forms.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _canonical(name: str, aliases: dict[str, str]) -> str:
    head, _, rest = name.partition(".")
    real = aliases.get(head, head)
    return f"{real}.{rest}" if rest else real


def _offenders() -> list[tuple[str, str, str, int]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text())
        aliases = _aliases(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = _canonical(_dotted(node.func), aliases)
                    if name in BLOCKING:
                        found.append((rel, fn.name, name, node.lineno))
    return found


def test_no_new_blocking_calls_inside_coroutines():
    unexpected = [
        (rel, fn, call, line)
        for rel, fn, call, line in _offenders()
        if (rel, fn, call) not in ALLOWED
    ]
    assert not unexpected, "blocking call(s) inside a coroutine — these stall the shared event loop:\n" + "\n".join(
        f"  {rel}:{line}  async {fn}()  ->  {call}" for rel, fn, call, line in unexpected
    )


def test_the_allowlist_has_no_stale_entries():
    """An allowlist that outlives its fix is a caveat that oversells the
    danger, and the next reader trusts it. When an entry's call is gone, its
    line here has to go too."""
    live = {(rel, fn, call) for rel, fn, call, _ in _offenders()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, "fixed, so remove from ALLOWED:\n" + "\n".join(
        f"  {rel}  async {fn}()  ->  {call}" for rel, fn, call in stale
    )


# --- a THREAD lock held across an await is a deadlock, not a stall -----------
#
# The worst shape this refactor can leave behind, and the one that is hardest
# to reason about from the diff alone:
#
#     with some_threading_lock:      # task A acquires
#         await something()          # A yields to the loop
#
# Task B now calls `threading.Lock.acquire()`, which is a blocking C call, so
# the ENTIRE LOOP stops inside B's frame. A is the only one who can release the
# lock, and A can never be resumed. That is a permanent deadlock, not a pause,
# and it needs two tasks and real timing to reproduce -- so a test suite is
# unlikely to catch it and a reviewer is unlikely to see it.
#
# Before de-threading this was harmless: the blocked thread simply waited while
# other threads ran. The lock did not change; what changed is that there is now
# only one thread to block.
#
# Detected by name because a lock's TYPE is not statically knowable here. That
# is a net, not a proof -- an oddly-named lock slips through.
LOCKISH = ("lock", "guard", "mutex", "semaphore")

THREAD_LOCK_ALLOWED: dict[tuple[str, int], str] = {
    ("fabric/nebula.py", 745): "v0.7.7 stage D, owner dethread-control-src: "
                               "`_lock_for_dir` is a threading.Lock held across "
                               "awaits at 750-751. DEADLOCK, not a stall.",
    ("compute/instances.py", 458): "v0.7.7 stage D, owner dethread-control-src. "
                                   "THE WORST INSTANCE: a threading.Semaphore "
                                   "held across `await self._lima(create/start)` "
                                   "-- a VM boot, MINUTES long. The semaphore "
                                   "exists to bound concurrent boots, so it is "
                                   "contended BY DESIGN; the second task blocks "
                                   "the whole loop for the length of a VM boot.",
    ("compute/instances.py", 737): "v0.7.7 stage D, owner dethread-control-src: "
                                   "the same semaphore across `_lima start`.",
}


def _thread_locks_across_awaits() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            # `async with` on an asyncio.Lock is CORRECT and must not be flagged;
            # only a synchronous `with` can block the loop.
            if not isinstance(node, ast.With):
                continue
            head = source.splitlines()[node.lineno - 1].strip()
            if not any(word in head.lower() for word in LOCKISH):
                continue
            awaits = [n.lineno for n in ast.walk(node) if isinstance(n, ast.Await)]
            if awaits:
                found.append((rel, node.lineno, f"awaits at {awaits}"))
    return found


def test_no_thread_lock_is_held_across_an_await():
    unexpected = [
        (rel, line, detail) for rel, line, detail in _thread_locks_across_awaits()
        if (rel, line) not in THREAD_LOCK_ALLOWED
    ]
    assert not unexpected, (
        "a synchronous lock is held across an `await` — the task that yields "
        "cannot be resumed while another task blocks the whole loop acquiring "
        "it. Use asyncio.Lock + `async with`, or drop the lock if the critical "
        "section has no await:\n"
        + "\n".join(f"  {rel}:{line}  ({detail})" for rel, line, detail in unexpected)
    )


def test_the_thread_lock_checker_separates_the_dangerous_shape_from_the_safe_ones():
    """Mutation test. `with lock:` with no await is safe (nothing can preempt
    it on one loop, so it may not even need the lock); `async with lock:` is
    the correct asyncio form. Only the sync-lock-plus-await combination is a
    deadlock, and only that one may be flagged."""
    def flagged(src: str) -> bool:
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.With):
                continue
            head = src.splitlines()[node.lineno - 1].strip()
            if not any(w in head.lower() for w in LOCKISH):
                continue
            if any(isinstance(n, ast.Await) for n in ast.walk(node)):
                return True
        return False

    assert flagged("async def g():\n    with self._lock:\n        await f()\n")
    assert not flagged("async def g():\n    with self._lock:\n        h()\n")
    assert not flagged("async def g():\n    async with self._lock:\n        await f()\n")
    assert not flagged("async def g():\n    with open(p) as fh:\n        await f()\n")


def test_the_checker_actually_detects_a_blocking_call():
    """Mutation test for the checker itself. A guard nobody has broken on
    purpose is a guard nobody knows fires -- four of odin's shipped guards
    silently never fired, which is why this exists."""
    tree = ast.parse("import time\nasync def f():\n    time.sleep(1)\n")
    calls = [
        _dotted(n.func)
        for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef)
        for n in ast.walk(fn) if isinstance(n, ast.Call)
    ]
    assert "time.sleep" in calls and "time.sleep" in BLOCKING

    # ...and does NOT flag the same call in a synchronous function
    sync_tree = ast.parse("import time\ndef f():\n    time.sleep(1)\n")
    sync_calls = [
        _dotted(n.func)
        for fn in ast.walk(sync_tree) if isinstance(fn, ast.AsyncFunctionDef)
        for n in ast.walk(fn) if isinstance(n, ast.Call)
    ]
    assert sync_calls == []
