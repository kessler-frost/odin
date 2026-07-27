"""`await f(...).items()` reads `.items` off the COROUTINE, not the result.

`await` binds looser than attribute access, subscription, and calls, so
`await f(...).items()` parses as `await (f(...).items())`. The correct form is
`(await f(...)).items()`. Every one of these is a real bug, and they are
easy to write and hard to see.

They arrived here in bulk: the v0.7.7 de-threading pass inserted `await` at the
START of call expressions, which is right for a bare call and wrong the moment
the result is chained. Six were found in one sweep, two of them written by the
same automated pass that was supposed to be mechanical:

    if self.VM not in await self._lima("list", "-q").split():        # WRONG
    if "server version" in await self._cli("info").lower():          # WRONG
    f"...{await self._rt.logs(name, 5).strip() or 'none'}"           # WRONG
    if await self._probe_db(record).ok or ...                        # WRONG
    for label, verdict in await live_verdicts(...).items()           # WRONG

Some fail loudly (`'coroutine' object has no attribute 'items'`), which is the
lucky case. Others do not: the chained call can succeed on the coroutine object
and quietly do the wrong thing, and the orphaned coroutine then only shows up
as an unawaited-coroutine warning attributed to some unrelated test.

A ratchet like `test_no_blocking_in_coroutines.py`: known offenders are listed
with an owner, anything new fails, and a stale entry fails too.

LIMIT, stated rather than implied: this matches on the coroutine's NAME, so a
sync function sharing a name with some coroutine elsewhere in the tree can be
flagged (a false positive), and a coroutine reached through a variable this
sweep cannot resolve can be missed (a false negative). It is a net, not a
proof.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "odin"

# (module path, line-anchoring coroutine name) -> owner + why it is still here.
# Delete an entry when it is fixed.
ALLOWED: dict[tuple[str, str], str] = {}


def _coroutine_names() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncFunctionDef):
                names.add(node.name)
    return names


def _innermost_call_name(node: ast.expr) -> str | None:
    """Peel `.attr` / `[k]` off until the call underneath is reached."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Attribute):
            return fn.attr
        if isinstance(fn, ast.Name):
            return fn.id
    return None


def _offenders() -> list[tuple[str, int, str]]:
    coros = _coroutine_names()
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Await):
                continue
            value = node.value
            name = None
            if isinstance(value, (ast.Attribute, ast.Subscript)):
                name = _innermost_call_name(value)
            elif isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                name = _innermost_call_name(value.func)
            if name and name in coros:
                found.append((rel, node.lineno, name))
    return found


def test_await_binds_to_the_call_not_to_the_chain():
    unexpected = [
        (rel, line, name) for rel, line, name in _offenders()
        if (rel, name) not in ALLOWED
    ]
    assert not unexpected, (
        "`await` applied to a chained expression -- it reads the attribute off the "
        "COROUTINE. Wrap the call: `(await f(...)).x`\n"
        + "\n".join(f"  {rel}:{line}  -> await {name}(...).…" for rel, line, name in unexpected)
    )


def test_the_allowlist_has_no_stale_entries():
    """A caveat that outlives its fix is a claim you cannot back."""
    live = {(rel, name) for rel, _, name in _offenders()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, "fixed, so remove from ALLOWED:\n" + "\n".join(
        f"  {rel}  -> {name}" for rel, name in stale
    )


# --- the sibling shape: `async with await <acm>` -----------------------------
#
# `@contextlib.asynccontextmanager` returns its context manager SYNCHRONOUSLY;
# the `async with` does the awaiting. So `async with await serve_on_loop(...)`
# raises `TypeError: object _AsyncGeneratorContextManager can't be used in
# 'await' expression` -- and it did, inside `create_app`'s lifespan, meaning
# the real server could not start at all.
#
# The inverse must stay SILENT: `async with reconciler.hold()` is correct and
# extremely common. A checker that flags both is noise, and a static check that
# cries wolf two times in three is one people learn to ignore -- which is worse
# than not having it.
ASYNC_WITH_ALLOWED: dict[str, str] = {
    "server.py": "v0.7.7 stage C, owner dethread-control-src: `async with await "
                 "serve_on_loop(...)` — delete the await, keep the async with.",
}


def _async_with_offenders() -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncWith):
                for item in node.items:
                    if isinstance(item.context_expr, ast.Await):
                        found.append((rel, node.lineno))
    return found


def test_async_with_does_not_await_its_context_manager():
    unexpected = [(rel, line) for rel, line in _async_with_offenders() if rel not in ASYNC_WITH_ALLOWED]
    assert not unexpected, (
        "`async with await <acm>` — an @asynccontextmanager returns its manager "
        "synchronously; the `async with` awaits it:\n"
        + "\n".join(f"  {rel}:{line}" for rel, line in unexpected)
    )


def test_the_async_with_checker_stays_silent_on_the_correct_form():
    """The false-positive direction, tested explicitly. `async with acm()` is
    the overwhelmingly common CORRECT form; flagging it would make the whole
    check noise."""
    def flagged(src: str) -> bool:
        return any(
            isinstance(item.context_expr, ast.Await)
            for node in ast.walk(ast.parse(src)) if isinstance(node, ast.AsyncWith)
            for item in node.items
        )

    assert flagged("async def g():\n    async with await acm():\n        pass\n")
    assert not flagged("async def g():\n    async with acm():\n        pass\n")
    assert not flagged("async def g():\n    async with acm() as x, other() as y:\n        pass\n")
    # a plain `with` on an awaited value is a different thing and not ours
    assert not flagged("async def g():\n    with await thing():\n        pass\n")


def test_the_checker_detects_the_wrong_form_and_accepts_the_right_one():
    """Mutation test for the checker itself: it must separate the two forms,
    not merely notice that an `await` and a `.` appear on the same line."""
    def flagged(src: str) -> bool:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Await):
                value = node.value
                if isinstance(value, (ast.Attribute, ast.Subscript)):
                    return _innermost_call_name(value) == "f"
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                    return _innermost_call_name(value.func) == "f"
        return False

    assert flagged("async def g():\n    return await f().items()\n")      # wrong form
    assert flagged("async def g():\n    return await f().ok\n")           # wrong form
    assert flagged("async def g():\n    return await f()[0]\n")           # wrong form
    assert not flagged("async def g():\n    return (await f()).items()\n")  # right form
    assert not flagged("async def g():\n    return await f()\n")            # bare call
