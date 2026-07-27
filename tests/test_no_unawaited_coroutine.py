"""Calling an `async def` without `await` runs nothing, and says nothing.

The inverse of `test_no_awaited_sync_seam.py`, and the more dangerous
direction: `await` on a sync function RAISES and reports itself, while calling
a coroutine function without `await` produces a coroutine object that is
silently discarded or misused downstream. The body never runs.

Five bugs of this exact class were found on 2026-07-27, none by the test suite:

  fabric/sidecar.py::_join    ensure_started / allocate_host_ip un-awaited, so
                              EVERY backing mesh join silently did nothing --
                              the lighthouse never started and a coroutine
                              object was passed in as an IP address. Behind
                              `ensure()`'s broad except, that surfaced as a
                              decorative security group.
  tests (x4)                  own_containers compared to [] -- the teardown
                              assertion "every container this test made is
                              gone" FAILED while zero containers had leaked.
                              It asserted the opposite of the truth.

## Why this took three attempts to make shippable

A bare-name scan is useless here, and that is the whole design note. Three
independent attempts produced 87, 89 and 255 hits, nearly all name collisions:
`Path.exists`, `str.join`, `subprocess.run`, boto3's `client`. A checker wrong
two times in three trains people to ignore it, which is worse than the gap it
fills -- so it was deliberately NOT shipped until it could be made precise.

What makes it precise is SCOPE RESOLUTION, not cleverness:
  * a name is async only if it is an `async def` in THIS file, or imported via
    `from X import Y` where Y is an `async def` in X
  * **a local sync `def` WINS over an async import of the same name** -- this
    single rule removed most of the false positives
Measured: src went 87 -> 25 -> 0, tests went 89 -> 18 -> the 4 real bugs.

## Legitimately un-awaited, and why each is not a bug

  asyncio.create_task/ensure_future/gather/wait_for/shield/as_completed
      the coroutine is being SCHEDULED; awaiting it first would defeat the
      point (see test_create_task_not_awaited.py for that inverse mistake)
  gateway.models.background(...)
      odin's fire-and-forget helper -- create_task plus a strong reference,
      because the loop holds only a weak one and an unreferenced task can be
      garbage-collected mid-flight
  asyncio.run(...)
      the sync bridge at CLI entry points, which stay `def` because Typer
      silently drops an `async def` command (test_cli_commands_are_sync.py)
  async with <acm>()
      an `@asynccontextmanager` returns its manager synchronously; the
      `async with` does the awaiting (test_await_precedence.py checks the
      inverse spelling)

LIMIT, stated rather than implied: only calls by bare NAME are resolved.
`obj.method()` is not, because the receiver's type is not statically knowable
here -- and that is exactly how the `sidecar.py` bug was written
(`self._lighthouse.ensure_started(...)`). So this is a net for one shape, not
a proof for the class. Attribute-call resolution would need real type
inference; until then the runtime guard in `conftest.py` covers what this
misses.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "odin"

SCHEDULERS = {
    "create_task", "ensure_future", "gather", "wait_for", "shield",
    "as_completed", "background", "run",
}

# (module path, coroutine name) -> owner + why. Empty is the correct state.
ALLOWED: dict[tuple[str, str], str] = {}


def _module_kinds(path: Path) -> tuple[set[str], set[str]]:
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return set(), set()
    return (
        {n.name for n in tree.body if isinstance(n, ast.FunctionDef)},
        {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)},
    )


def _resolve(dotted: str) -> Path | None:
    for base in (REPO / "src", REPO):
        direct = base.joinpath(*dotted.split(".")).with_suffix(".py")
        if direct.exists():
            return direct
        pkg = base.joinpath(*dotted.split("."), "__init__.py")
        if pkg.exists():
            return pkg
    return None


def _async_in_scope(tree: ast.Module) -> set[str]:
    """Names that are coroutine functions AS THIS FILE SEES THEM."""
    local_async = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    local_sync = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        module = _resolve(node.module)
        if module is None:
            continue
        _, module_async = _module_kinds(module)
        for alias in node.names:
            if alias.name in module_async:
                imported.add(alias.asname or alias.name)
    # a local sync def of the same name shadows the import — this one rule is
    # what removed most of the false positives
    return (local_async | imported) - local_sync


def _legitimately_bare(tree: ast.Module) -> set[int]:
    """ids of call nodes that are SUPPOSED to be un-awaited."""
    ok: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name in SCHEDULERS:
                for arg in node.args:
                    ok.add(id(arg.value if isinstance(arg, ast.Starred) else arg))
        elif isinstance(node, ast.AsyncWith):
            for item in node.items:
                ok.add(id(item.context_expr))
        elif isinstance(node, ast.Lambda):
            # A call inside a lambda is a coroutine FACTORY, not an immediate
            # call — the coroutine is built only when the lambda runs. odin
            # uses this deliberately (`agent/translate.py`: pass a factory to
            # `refine_in_background` so a CANCELLED task never leaves an
            # un-awaited coroutine behind), and the code says so at the site.
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    ok.add(id(inner))
    return ok


def _offenders() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text())
        in_scope = _async_in_scope(tree)
        if not in_scope:
            continue
        awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
        bare_ok = _legitimately_bare(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in awaited or id(node) in bare_ok:
                continue
            if isinstance(node.func, ast.Name) and node.func.id in in_scope:
                found.append((rel, node.lineno, node.func.id))
    return sorted(set(found))


def test_no_coroutine_is_called_without_await():
    unexpected = [(r, ln, n) for r, ln, n in _offenders() if (r, n) not in ALLOWED]
    assert not unexpected, (
        "a coroutine function is called without `await` — its body never runs, "
        "and nothing raises:\n"
        + "\n".join(f"  {rel}:{line}  {name}(...)" for rel, line, name in unexpected)
    )


def test_the_allowlist_has_no_stale_entries():
    """A caveat that outlives its fix is a claim you cannot back."""
    live = {(rel, name) for rel, _, name in _offenders()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, "fixed, so remove from ALLOWED:\n" + "\n".join(
        f"  {rel} -> {name}" for rel, name in stale
    )


def test_the_checker_separates_the_bug_from_its_legitimate_neighbours():
    """Mutation test. The neighbours matter as much as the bug: this shape is
    surrounded by forms that look identical and are correct, and flagging them
    is how a static check gets ignored."""
    def flagged(src: str) -> bool:
        tree = ast.parse(src)
        in_scope = _async_in_scope(tree)
        awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
        bare_ok = _legitimately_bare(tree)
        return any(
            isinstance(n, ast.Call) and id(n) not in awaited and id(n) not in bare_ok
            and isinstance(n.func, ast.Name) and n.func.id in in_scope
            for n in ast.walk(tree)
        )

    coro = "async def f():\n    pass\n"
    # THE BUG
    assert flagged(coro + "async def g():\n    f()\n")
    # the legitimate neighbours
    assert not flagged(coro + "async def g():\n    await f()\n")
    assert not flagged(coro + "async def g():\n    asyncio.create_task(f())\n")
    assert not flagged(coro + "async def g():\n    await asyncio.gather(f(), f())\n")
    assert not flagged(coro + "async def g():\n    background(f())\n")
    assert not flagged(coro + "def g():\n    asyncio.run(f())\n")
    assert not flagged("async def acm():\n    yield\nasync def g():\n    async with acm():\n        pass\n")
    # and the shadowing rule: a LOCAL sync def of the same name wins
    assert not flagged("from odin.util import run_command_async\ndef run_command_async(x):\n    return x\ndef g():\n    run_command_async(1)\n")
