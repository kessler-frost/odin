"""`asyncio.create_task(await f())` runs `f` to completion FIRST.

The `await` happens before `create_task` ever sees an argument, so nothing is
scheduled concurrently — the caller simply runs `f` inline. When `f` is a
background loop, the caller never returns at all.

That is not hypothetical. The v0.7.7 de-threading pass produced four of these,
each wrapping an infinite loop:

    Reconciler.start()  ->  asyncio.create_task(await self._run())
    server lifespan     ->  asyncio.create_task(await _keep_store_lock(...))
    server lifespan     ->  asyncio.create_task(await _watch_reconcilers(...))
    translate           ->  asyncio.create_task(await _run())

`Reconciler._run` is `while not self._stop: ...`, so **every** `await
recon.start()` hung forever — in tests and in `create_app`'s lifespan alike.
It is why full-suite runs blew past 600s instead of failing, which reads as a
slow machine rather than a bug and cost real time to track down.

The shape is nastier than the precedence trap because it does not raise. The
types are all valid; the program simply stops making progress. A hang is the
one failure that looks identical to "still working".

A ratchet like the sibling checks: known offenders are listed with an owner,
anything new fails, and a stale entry fails too.

Applies to the whole family that takes a coroutine to schedule: `create_task`,
`ensure_future`, `gather`, `wait`, `wait_for`, `shield`, `as_completed`,
`TaskGroup.create_task`. `gather` deserves its own note -- `gather(*(await f(k)
for k in ks))` is worse still, because the inner `await` turns the generator
into an ASYNC generator that `*` cannot unpack at all.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "odin"

SCHEDULERS = {
    "create_task", "ensure_future", "gather", "wait", "wait_for",
    "shield", "as_completed",
}

# (module path, scheduler) -> owner + why it is still here. Delete when fixed.
ALLOWED: dict[tuple[str, str], str] = {
    ("reconcile/reconciler.py", "create_task"):
        "v0.7.7 stage C, owner dethread-control-src. THE HANG: _run is an "
        "infinite loop, so every `await recon.start()` never returns.",
    ("server.py", "create_task"):
        "v0.7.7 stage C, owner dethread-control-src (two sites: the store-lock "
        "keeper and the reconciler watchdog, both infinite loops).",
    ("agent/translate.py", "create_task"):
        "v0.7.7 stage C, owner dethread-control-src.",
    ("agent/translate.py", "wait_for"):
        "v0.7.7 stage C, owner dethread-control-src. The inner await defeats "
        "the timeout entirely -- all translate refinement is dead.",
    ("agent/debugger.py", "wait_for"):
        "v0.7.7 stage C, owner dethread-control-src. Same defeated timeout.",
    ("reconcile/reconciler.py", "gather"):
        "v0.7.7 stage C, owner dethread-control-src. `gather(*(await f(k) for "
        "k in ks))` -- the await makes an ASYNC generator that `*` cannot "
        "unpack at all, so ensure_backing never runs.",
}


def _scheduler_name(call: ast.Call) -> str | None:
    fn = call.func
    if isinstance(fn, ast.Attribute) and fn.attr in SCHEDULERS:
        return fn.attr
    if isinstance(fn, ast.Name) and fn.id in SCHEDULERS:
        return fn.id
    return None


def _offenders() -> list[tuple[str, int, str]]:
    """Scheduler calls whose argument is itself awaited."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = _scheduler_name(node)
            if name is None:
                continue
            args = list(node.args)
            # gather(*(...)) — the starred generator is the argument
            args += [a.value for a in node.args if isinstance(a, ast.Starred)]
            for arg in args:
                inner = arg
                if isinstance(inner, ast.Starred):
                    inner = inner.value
                if isinstance(inner, ast.Await):
                    found.append((rel, node.lineno, name))
                    break
                # gather(*(await f(k) for k in ks)) -> an ASYNC generator
                if isinstance(inner, (ast.GeneratorExp, ast.ListComp)):
                    if any(isinstance(n, ast.Await) for n in ast.walk(inner)):
                        found.append((rel, node.lineno, name))
                        break
    return found


def test_nothing_awaits_the_coroutine_it_means_to_schedule():
    unexpected = [
        (rel, line, name) for rel, line, name in _offenders()
        if (rel, name) not in ALLOWED
    ]
    assert not unexpected, (
        "a scheduler was handed an ALREADY-AWAITED coroutine, so it runs inline "
        "instead of concurrently — and hangs outright if it loops forever:\n"
        + "\n".join(f"  {rel}:{line}  -> {name}(await ...)" for rel, line, name in unexpected)
    )


def test_the_allowlist_has_no_stale_entries():
    """A caveat that outlives its fix is a claim you cannot back."""
    live = {(rel, name) for rel, _, name in _offenders()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, "fixed, so remove from ALLOWED:\n" + "\n".join(
        f"  {rel}  -> {name}" for rel, name in stale
    )


def test_the_checker_separates_the_broken_form_from_the_correct_one():
    """Mutation test for the checker. It has to key on the awaited ARGUMENT,
    not merely on `await` and `create_task` appearing near each other -- the
    correct form has both on the same line."""
    def flagged(src: str) -> bool:
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and _scheduler_name(node):
                args = [a.value if isinstance(a, ast.Starred) else a for a in node.args]
                for arg in args:
                    if isinstance(arg, ast.Await):
                        return True
                    if isinstance(arg, (ast.GeneratorExp, ast.ListComp)) and any(
                        isinstance(n, ast.Await) for n in ast.walk(arg)
                    ):
                        return True
        return False

    assert flagged("async def g():\n    asyncio.create_task(await f())\n")
    assert flagged("async def g():\n    await asyncio.gather(*(await f(k) for k in ks))\n")
    assert flagged("async def g():\n    asyncio.ensure_future(await f())\n")

    # the CORRECT forms — `await` on the scheduler, never on its argument
    assert not flagged("async def g():\n    t = asyncio.create_task(f())\n    await t\n")
    assert not flagged("async def g():\n    await asyncio.gather(*(f(k) for k in ks))\n")
    assert not flagged("async def g():\n    await asyncio.wait_for(f(), timeout=1)\n")
