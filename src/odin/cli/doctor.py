"""`odin doctor` — preflight checks that this machine can actually RUN Odin
(not just start it): Colima up, the docker CLI + tofu on PATH, plus optional
niceties (limactl, bun, claude).

`run_checks` is the pure core: it takes the check names to run and a
subprocess-runner callable (fakeable in tests — the same runner seam
`ColimaRuntime` already exposes) and returns typed `CheckResult`s. The Typer
command renders them (✓/✗/○) and exits 1 only when a REQUIRED check fails;
optional tooling never blocks.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Literal, Protocol

import typer

from odin.cli.app import app


class Proc(Protocol):
    returncode: int
    stdout: str
    stderr: str


# run(args, input=None) -> Proc — the exact seam ColimaRuntime(runner=…) takes,
# so ONE fake covers both the `which`/`colima status` calls and docker lookups.
Runner = Callable[..., Proc]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Literal["ok", "fail", "skip"]  # skip = optional and absent — never blocks
    required: bool
    detail: str
    fix: str = ""  # exact shell command to fix, when status != "ok"


# (name, required, fix) for every plain is-it-on-PATH tool check.
_TOOLS: tuple[tuple[str, bool, str], ...] = (
    ("docker", True, "brew install colima"),  # colima front-ends the docker CLI (ColimaRuntime)
    ("tofu", True, "brew install opentofu"),  # Apply shells out to it (simulate/runner.py)
    ("limactl", False, "brew install lima"),  # only LimaRuntime / EC2-as-Lima-VM needs it
    ("bun", False, "curl -fsSL https://bun.sh/install | bash"),  # dev-only: building the UI
    ("claude", False, "see https://docs.claude.com/claude-code"),  # translate has a fallback
)

ALL_CHECKS: tuple[str, ...] = ("colima", *(name for name, _, _ in _TOOLS))


def _subprocess_run(args: list[str], input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, input=input)


def _which(run: Runner, tool: str) -> str:
    proc = run(["which", tool])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _check_tool(run: Runner, name: str, required: bool, fix: str) -> CheckResult:
    path = _which(run, name)
    status = "ok" if path else ("fail" if required else "skip")
    return CheckResult(name, status, required, path or "not found on PATH", "" if path else fix)


def _check_colima(run: Runner) -> CheckResult:
    path = _which(run, "colima")
    if not path:
        return CheckResult("colima", "fail", True, "not found on PATH", "brew install colima")
    if run(["colima", "status"]).returncode != 0:
        return CheckResult("colima", "fail", True, f"{path} — installed but not running",
                           "colima start")
    return CheckResult("colima", "ok", True, f"{path} — running")


def run_checks(which: Iterable[str], run: Runner) -> list[CheckResult]:
    """Run the named checks through `run` (the subprocess seam); results come
    back in the order asked."""
    checks: dict[str, Callable[[], CheckResult]] = {"colima": partial(_check_colima, run)}
    checks.update({name: partial(_check_tool, run, name, required, fix)
                   for name, required, fix in _TOOLS})
    return [checks[name]() for name in which]


_ICONS = {"ok": "✓", "fail": "✗", "skip": "○"}


@app.command()
def doctor() -> None:
    """Preflight: verify this machine has everything Odin needs to run."""
    results = run_checks(ALL_CHECKS, _subprocess_run)
    for result in results:
        typer.echo(f" {_ICONS[result.status]} {result.name:<15} {result.detail}")
        if result.fix:
            typer.echo(f"{'':<18}fix: {result.fix}")
    blockers = [r for r in results if r.required and r.status == "fail"]
    if blockers:
        typer.echo(f"\n{len(blockers)} required check(s) failed.")
        raise typer.Exit(1)
    typer.echo("\nAll required checks passed.")
