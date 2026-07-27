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
ALLOWED: dict[tuple[str, str], str] = {
    ("api/debug.py", "fetch_logs"):
        "v0.7.7 stage C, owner dethread-control-src",
    ("compute/instances.py", "_lima"):
        "v0.7.7 stage C, owner dethread-control-src (two sites)",
    ("server.py", "health"):
        "v0.7.7 stage C, owner dethread-control-src",
}


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
