"""`await self._run(...)` where `self._run` defaults to a SYNC function.

The sixth v0.7.7 bug class: a half-converted dependency-injection seam. A
callable is injected for testability with a sync default, the conversion pass
adds `await` at the CALL sites, and the DEFAULT is never converted. The types
never line up, but nothing says so until the default is actually used --
which, for both live instances, is the production path.

MEASURED by executing the real production default, not inferred:

    InstanceVm()            -> await vm._lima("list","-q")
        TypeError: object _Proc can't be used in 'await' expression
    NebulaManager(dir)      -> await mgr.create_ca(env)
        TypeError: object _Proc can't be used in 'await' expression

`gateway/models/ec2compute.py` constructs `vm or InstanceVm()` at five sites,
including `pure_answer` itself, so the default runner IS what production uses:
every `ec2:*` call that reaches `_lima` raises. `fabric/nebula.py` is reached
the same way through `ensure_network`.

The tell is that the same file is internally inconsistent about it.
`fabric/nebula.py` awaits `self._run` at lines 361 and 381, and calls the very
same `_default_runner` WITHOUT `await` at line 1081. One of those is wrong on
any reading.

Why the four existing ratchets miss it:
  * `test_no_blocking_in_coroutines.py` keys on a fixed `BLOCKING` set of
    dotted names; `self._run` is not in it and could not be.
  * `test_await_precedence.py` keys on `await f(...).attr` chaining; this is a
    bare `await f(...)`, correctly parenthesised and still wrong.
  * `test_create_task_not_awaited.py` keys on a scheduler's argument.
  * `test_cli_commands_are_sync.py` is about typer decorators.
None of them can see "the thing being awaited is not a coroutine function".

A ratchet like its siblings: known offenders carry an owner, anything new
fails, and a stale entry fails too.

LIMIT, stated rather than implied: this resolves an attribute to a sync
function only when both the assignment and the `def` are in the SAME module,
which is what both live instances look like (`self._run = runner or
_default_runner`). A default imported from elsewhere, or reached through a
name this sweep cannot resolve, is a false negative. It is a net, not a proof.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "odin"

# (module path, awaited attribute) -> owner + why it is still here.
# Both original entries -- `compute/instances.py` and `fabric/nebula.py`, whose
# `_default_runner` was a plain `def` awaited at its call sites -- are FIXED.
# nebula's was 83 of the 90 failures then outstanding in tests/gateway; both
# now run through `util.run_command_async`, which keeps `run_command`'s rc-127
# contract for a binary that is not on PATH. Empty is the correct state, and
# the stale-entry test below is what forced this cleanup rather than letting
# the list rot into a description of bugs nobody has.
ALLOWED: dict[tuple[str, str], str] = {}


def dotted(node: ast.AST) -> str:
    bits: list[str] = []
    while isinstance(node, ast.Attribute):
        bits.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        bits.append(node.id)
    return ".".join(reversed(bits))


def _module_kinds(dotted: str) -> tuple[set[str], set[str]]:
    """(sync defs, async defs) declared at the top level of an odin module."""
    if not dotted.startswith("odin."):
        return set(), set()
    path = SRC.joinpath(*dotted.split(".")[1:]).with_suffix(".py")
    if not path.exists():
        path = SRC.joinpath(*dotted.split(".")[1:], "__init__.py")
    if not path.exists():
        return set(), set()
    tree = ast.parse(path.read_text())
    return (
        {n.name for n in tree.body if isinstance(n, ast.FunctionDef)},
        {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)},
    )


def _imported_kinds(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names this module imported FROM odin, split by sync vs async."""
    sync_names: set[str] = set()
    async_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        mod_sync, mod_async = _module_kinds(node.module)
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name in mod_sync:
                sync_names.add(local)
            elif alias.name in mod_async:
                async_names.add(local)
    return sync_names, async_names


def _offenders_in(tree: ast.Module, rel: str) -> list[tuple[str, str, int, str]]:
    """(module, attribute, line, sync function it resolves to)."""
    sync_funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    async_funcs = {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    # ...and the IMPORTED ones. Without this the check only saw defaults defined
    # in the same file, so `self._probe = probe or pg_ready_sync` was invisible
    # -- `pg_ready_sync` is imported from reconcile.assertions. The checker
    # exited 0 with a live TypeError in the same tree (the rds half of every
    # drift sweep), which is the "guard that silently never fires" shape this
    # repo keeps paying for. Found by a reviewer, not by the guard.
    imported_sync, imported_async = _imported_kinds(tree)
    sync_funcs |= imported_sync
    async_funcs |= imported_async

    sync_attrs: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        refs = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        hits = refs & sync_funcs
        # `runner or _an_async_default` is fine — only an all-sync set is a bug.
        if not hits or (refs & async_funcs):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                sync_attrs.setdefault(dotted(target), set()).update(hits)

    found: list[tuple[str, str, int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)):
            continue
        name = dotted(node.value.func)
        if name in sync_attrs:
            found.append((rel, name, node.lineno, ", ".join(sorted(sync_attrs[name]))))
    return found


def _offenders() -> list[tuple[str, str, int, str]]:
    out: list[tuple[str, str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        out += _offenders_in(ast.parse(path.read_text()), path.relative_to(SRC).as_posix())
    return out


def test_nothing_awaits_a_sync_default():
    unexpected = [o for o in _offenders() if (o[0], o[1]) not in ALLOWED]
    assert not unexpected, (
        "an awaited callable defaults to a SYNCHRONOUS function — TypeError the "
        "moment the default is used:\n"
        + "\n".join(
            f"  {rel}:{line}  await {attr}(...)  -> {attr} = {src} (sync)"
            for rel, attr, line, src in unexpected
        )
    )


def test_the_allowlist_has_no_stale_entries():
    """A caveat that outlives its fix is a claim you cannot back."""
    live = {(rel, attr) for rel, attr, _, _ in _offenders()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, "fixed, so remove from ALLOWED:\n" + "\n".join(
        f"  {rel}  -> {attr}" for rel, attr in stale
    )


def test_the_checker_separates_the_broken_form_from_the_correct_one():
    """Mutation test for the checker.

    It must key on the DEFAULT being sync. An injected callable whose default
    is a coroutine function is the correct pattern and must never be flagged,
    and neither must a sync attribute that is called without `await`.
    """
    import textwrap

    def offenders(src: str):
        return _offenders_in(ast.parse(textwrap.dedent(src)), "x.py")

    # broken: sync default, awaited
    assert offenders("""
        def _default_runner(args): ...
        class M:
            def __init__(self, runner=None):
                self._run = runner or _default_runner
            async def go(self):
                return await self._run(["x"])
    """), "a sync default that is awaited must be flagged"

    # broken: plain sync assignment, awaited
    assert offenders("""
        def _sync(a): ...
        class M:
            def __init__(self):
                self._run = _sync
            async def go(self):
                return await self._run(1)
    """)

    # correct: the default IS a coroutine function
    assert not offenders("""
        async def _default_runner(args): ...
        class M:
            def __init__(self, runner=None):
                self._run = runner or _default_runner
            async def go(self):
                return await self._run(["x"])
    """), "an async default is the fix and must never be flagged"

    # correct: sync attribute, called WITHOUT await
    assert not offenders("""
        def _sync(a): ...
        class M:
            def __init__(self):
                self._run = _sync
            def go(self):
                return self._run(1)
    """), "calling a sync attribute without await is simply correct"

    # correct: `runner or <async default>` where only the async one is a def
    assert not offenders("""
        async def _adef(a): ...
        def _unrelated(): ...
        class M:
            def __init__(self, runner=None):
                self._run = runner or _adef
            async def go(self):
                return await self._run(1)
    """)
