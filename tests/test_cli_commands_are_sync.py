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

    Run in a SUBPROCESS on purpose. Dropping the coroutine is the behaviour
    under test, so an in-process probe necessarily creates the exact orphan
    that `conftest.py::_fail_on_unawaited_coroutines` exists to fail on. Two
    in-process containment attempts were tried and MEASURED as not working —
    the discarded coroutine survives an inner `catch_warnings` + `gc.collect()`
    and is only finalized at interpreter exit, so the fixture caught it at
    teardown anyway. Adding a marker-based opt-out would have worked and was
    rejected: the moment that escape hatch exists, it gets used to silence real
    forgotten awaits. A subprocess needs no exemption, and measures typer in a
    clean interpreter besides.
    """
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent(
        """
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
        s = runner.invoke(app, ["syncish"])
        a = runner.invoke(app, ["asyncish"])
        print(f"sync={s.exit_code},{ran['sync']} async={a.exit_code},{ran['async']}")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, f"probe itself failed:\n{done.stderr}"
    out = done.stdout.strip()

    assert "sync=0,True" in out, f"a sync command should run; probe said {out!r}"
    assert "async=0,False" in out, (
        "typer's handling of async commands has CHANGED — this guard's premise "
        f"was `exit 0 with the body never run`, probe said {out!r}. Re-check "
        "whether CLI entry points still need to be synchronous."
    )
