"""`odin doctor` — preflight checks that this machine can actually RUN Odin
(not just start it): Colima up, the docker CLI + tofu on PATH, disk headroom,
plus optional niceties (limactl, bun, claude, the prebaked dynalite image).

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
from pathlib import Path
from shutil import disk_usage
from typing import Literal, Protocol

import typer

from odin.aws.backings import DYNALITE_IMAGE, BackingAws
from odin.cli.app import app
from odin.reconcile.admission import check_admission, default_min_disk_gib
from odin.runtime.colima import ColimaRuntime
from odin.spec.models import Stack

# Both live-resource checks are READ-ONLY over `reconcile/admission.py` -- the
# module that can actually hard-fail an Apply -- rather than second guesses at
# its arithmetic. Field test LOW-14/LOW-15: doctor hardcoded a 10 GiB disk floor
# while admission honoured `ODIN_MIN_DISK_GIB` (so the two disagreed at anything
# but the default, contradicting admission's own docstring), and said nothing
# at all about memory even though memory is the one thing that rejects an Apply
# outright, against a ceiling the user had no way to discover in advance.


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


# (name, required, fix, when_optional) for every plain is-it-on-PATH tool check.
# `when_optional` is the precise consequence of NOT having it -- "optional" on
# its own was misleading for `limactl` (field test LOW-15), since every EC2 node
# is a real Lima VM and an EC2 canvas simply cannot work without it.
_TOOLS: tuple[tuple[str, bool, str, str], ...] = (
    ("docker", True, "brew install colima", ""),  # colima front-ends the docker CLI
    ("tofu", True, "brew install opentofu", ""),  # Apply shells out to it (simulate/runner.py)
    ("limactl", False, "brew install lima",
     "REQUIRED for any canvas with an EC2 node (each one is a real Lima VM) and for "
     "LimaRuntime; every other kind runs on containers and needs none of it"),
    ("bun", False, "curl -fsSL https://bun.sh/install | bash",
     "needed only to build the UI from a clone -- the released package ships one prebuilt"),
    ("claude", False, "see https://docs.claude.com/claude-code",
     "needed only by \"what's wrong here?\" (POST /agent/debug); translation is deterministic"),
)

ALL_CHECKS: tuple[str, ...] = (
    "colima", *(name for name, _, _, _ in _TOOLS), "disk", "memory", "dynalite-image",
)


def _subprocess_run(args: list[str], input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, input=input)


def _which(run: Runner, tool: str) -> str:
    proc = run(["which", tool])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _check_tool(run: Runner, name: str, required: bool, fix: str, when_optional: str) -> CheckResult:
    path = _which(run, name)
    status = "ok" if path else ("fail" if required else "skip")
    absent = "not found on PATH" + (f" -- {when_optional}" if when_optional else "")
    return CheckResult(name, status, required, path or absent, "" if path else fix)


def _check_colima(run: Runner) -> CheckResult:
    path = _which(run, "colima")
    if not path:
        return CheckResult("colima", "fail", True, "not found on PATH", "brew install colima")
    if run(["colima", "status"]).returncode != 0:
        return CheckResult("colima", "fail", True, f"{path} — installed but not running",
                           "colima start")
    return CheckResult("colima", "ok", True, f"{path} — running")


def _check_disk(root: Path) -> CheckResult:
    free_gib = disk_usage(root).free / 2**30
    floor = default_min_disk_gib()  # the SAME number admission rejects an Apply on
    ok = free_gib > floor
    return CheckResult(
        "disk", "ok" if ok else "fail", True,
        f"{free_gib:.1f} GiB free on the volume holding {root} "
        f"(Apply needs >{floor:.0f} GiB; ODIN_MIN_DISK_GIB overrides)",
        "" if ok else f"free up disk space (>{floor:.0f} GiB needed; no single command)",
    )


def _check_memory(run: Runner, root: Path) -> CheckResult:
    """The admission budget, straight from `check_admission` itself (an empty
    Stack, so this is a pure read): the number an Apply is rejected against.

    Informational, never a blocker -- an unknown total means the container
    runtime didn't answer, which the `colima` check above already reports as the
    real failure, and admission skips its own memory check in that case too."""
    host = ColimaRuntime(runner=run).ensure_host()
    budget_mib = check_admission(Stack(), host, root).budget_mib
    known = budget_mib > 0
    detail = (
        f"{budget_mib / 1024:.1f} GiB admission budget of {host.total_mem_mib / 1024:.1f} GiB "
        "total reported by the container runtime -- Apply rejects a canvas estimated above it "
        "(ODIN_MEMORY_BUDGET_MIB overrides)"
        if known else
        "unknown -- the container runtime reported no memory total, so Apply's memory "
        "admission check is skipped entirely"
    )
    return CheckResult("memory", "ok" if known else "skip", False, detail,
                       "" if known else "colima start")


def _check_dynalite_image(run: Runner) -> CheckResult:
    # Guard on the docker CLI first: without it there's no daemon to ask, and
    # the note below (skip + the prebake offer) is still the right answer.
    present = bool(_which(run, "docker")) and ColimaRuntime(runner=run).image_exists(DYNALITE_IMAGE)
    detail = f"{DYNALITE_IMAGE} " + (
        "present" if present
        else "absent — first Apply with DynamoDB will build it (one-time npm install)"
    )
    return CheckResult("dynalite-image", "ok" if present else "skip", False, detail,
                       "" if present else "odin doctor --prebake")


def run_checks(which: Iterable[str], run: Runner, disk_path: Path | None = None) -> list[CheckResult]:
    """Run the named checks through `run` (the subprocess seam); results come
    back in the order asked. `disk_path` defaults to the current directory —
    the volume Odin's images, containers, and `.odin/` state land on."""
    root = disk_path or Path.cwd()
    checks: dict[str, Callable[[], CheckResult]] = {
        "colima": partial(_check_colima, run),
        "disk": partial(_check_disk, root),
        "memory": partial(_check_memory, run, root),
        "dynalite-image": partial(_check_dynalite_image, run),
    }
    checks.update({name: partial(_check_tool, run, name, required, fix, when_optional)
                   for name, required, fix, when_optional in _TOOLS})
    return [checks[name]() for name in which]


_ICONS = {"ok": "✓", "fail": "✗", "skip": "○"}


def _prebake() -> None:
    runtime = ColimaRuntime()
    present = runtime.image_exists(DYNALITE_IMAGE)
    state = "present" if present else "absent — building now (one-time npm install)"
    typer.echo(f"before: {DYNALITE_IMAGE} {state}")
    BackingAws(runtime).ensure_dynalite_image()
    typer.echo(f"after:  {DYNALITE_IMAGE} present ({'already there' if present else 'just built'})")


@app.command()
def doctor(
    prebake: bool = typer.Option(
        False, "--prebake",
        help=f"Build the {DYNALITE_IMAGE} image now instead of on the first DynamoDB Apply.",
    ),
) -> None:
    """Preflight: verify this machine has everything Odin needs to run."""
    if prebake:
        _prebake()
        return
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
