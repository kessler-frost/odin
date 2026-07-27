"""Typer SILENTLY DROPS an `async def` command. Exit 0, nothing run.

Measured on this machine against the installed typer (0.26.7), not assumed:

    @app.command()
    def syncish():   ->  exit=0  body_ran=True   output='sync ran'
    @app.command()
    async def asyncish():  ->  exit=0  body_ran=False  output=''

No error, no warning on stderr, no non-zero status. The coroutine is created,
never awaited, and discarded. To a user, and to any script checking `$?`, that
is indistinguishable from the command having succeeded.

That makes it the worst failure shape odin has: a command that reports success
it did not achieve. `odin doctor` -- the tool whose entire job is telling you
which prerequisite is missing -- was left `async def` by the v0.7.7
de-threading pass, so it exited 0 having run zero checks and printed nothing.
The de-threading directive is not a reason to make a CLI entry point async;
where a command needs async work underneath, the entry point stays synchronous
and bridges with `asyncio.run(...)`, which is thread-free and explicit.

This test walks the CLI modules and fails on any Typer-registered command that
is a coroutine function. It is deliberately narrow -- it checks the decorator
shape, not what the body does.

EXPECTED TO FAIL until `cli/doctor.py`'s `doctor()` is made synchronous again
(owner: the v0.7.7 control-plane work). Deliberately given NO allowlist, unlike
its sibling ratchets: there is exactly one offender, the fix is one keyword,
and the failure mode -- a command that exits 0 having done nothing -- is the
one this repo can least afford to leave amber. Do not silence it.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "odin"

# Decorators that register a Typer command. `callback` too: an async
# `@app.callback()` is dropped the same way.
REGISTRARS = {"command", "callback"}


def _registers_a_command(node: ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(func, ast.Attribute) and func.attr in REGISTRARS:
            return True
    return False


def _async_commands() -> list[tuple[str, str, int]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncFunctionDef) and _registers_a_command(node):
                found.append((path.relative_to(SRC).as_posix(), node.name, node.lineno))
    return found


def test_no_typer_command_is_a_coroutine():
    offenders = _async_commands()
    assert not offenders, (
        "Typer silently drops async commands — exit 0, body never runs, nothing "
        "printed. Keep the entry point sync and bridge with asyncio.run(...):\n"
        + "\n".join(f"  {rel}:{line}  async def {name}()" for rel, name, line in offenders)
    )


def test_typer_really_does_drop_async_commands():
    """The probe this file is built on, kept executable rather than quoted.

    If a future typer starts supporting async commands, this fails and the
    guard above can be reconsidered — instead of the docstring quietly
    describing a version nobody runs any more.
    """
    import gc
    import warnings

    import typer
    from typer.testing import CliRunner

    app = typer.Typer()
    ran = {"sync": False, "async": False}

    @app.command()
    def syncish():
        ran["sync"] = True

    @app.command()
    async def asyncish():
        ran["async"] = True

    runner = CliRunner()
    sync_result = runner.invoke(app, ["syncish"])

    # Dropping the coroutine IS the behaviour under test, so this test creates
    # exactly the orphan that `conftest.py::_fail_on_unawaited_coroutines`
    # exists to fail on. It is contained here rather than excused: an inner
    # `catch_warnings` receives the warning, and collecting inside that block
    # means the fixture's outer recorder never sees it. No opt-out marker, so
    # nothing else can reach for the same escape hatch.
    with warnings.catch_warnings(record=True) as dropped:
        warnings.simplefilter("always")
        async_result = runner.invoke(app, ["asyncish"])
        gc.collect()
    assert any("never awaited" in str(w.message) for w in dropped), (
        "expected typer to create and discard the coroutine; if no orphan "
        "appeared, typer's handling has changed and this guard needs revisiting"
    )

    assert sync_result.exit_code == 0 and ran["sync"], "a sync command should run"
    assert not ran["async"], (
        "typer now runs async commands — this guard's premise has changed, "
        "re-check whether CLI entry points still need to be synchronous"
    )
    assert async_result.exit_code == 0, (
        "the danger is precisely that it exits 0; if typer now fails loudly, "
        "this guard matters much less"
    )
