"""`odin doctor` — preflight checks that this machine can actually RUN Odin
(not just start it): Colima up, the docker CLI + tofu on PATH, disk headroom,
plus optional niceties (limactl, bun, claude, the prebaked dynalite image).

`run_checks` is the pure core: it takes the check names to run and a
subprocess-runner callable (fakeable in tests — the same runner seam
`ColimaRuntime` already exposes) and returns typed `CheckResult`s. The Typer
command renders them (✓/✗/○) and exits 1 only when a REQUIRED check fails;
optional tooling never blocks.

v0.7.7 — WHY THIS FILE HAS AN `asyncio.run` BRIDGE AND NOT AN ASYNC COMMAND.
`odin doctor` is a synchronous CLI command and stays one. The de-threading
pass made the container-runtime driver fully async, and doctor reaches it
through `ColimaRuntime(runner=…)` for two of its checks (`memory` calls
`ensure_host`, `dynalite-image` calls `image_exists`), so `run_checks` had to
become a coroutine to keep using that ONE runner seam — the property this
module's whole fake-ability rests on. The alternative, re-implementing
`docker info` / `docker image inspect` locally to stay sync, would have
duplicated the driver and let doctor drift from what an Apply really does,
which is the exact defect LOW-14/LOW-15 already recorded here.

So the async stops at the command boundary: `doctor()` is a plain `def` that
calls `asyncio.run(...)`. That is legitimate and thread-free — a CLI process
owns its loop, and there is no outer loop to nest inside.

MEASURED, not assumed: the previous stage left this as `async def doctor(...)`,
and **Typer 0.26.7 does not await async commands**. Probed with
`CliRunner().invoke` on a minimal async command: exit_code 0, empty stdout,
body never executed, `RuntimeWarning: coroutine 'hello' was never awaited`.
`odin doctor` was therefore exiting 0 while running ZERO checks and printing
nothing — a false green in the one tool whose entire job is to report that a
prerequisite is missing. Do not turn these back into `async def` commands
without re-probing that.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
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
from odin.util import run_command_async

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


# run(args, input=None) -> awaitable Proc — the exact seam
# ColimaRuntime(runner=…) takes, so ONE fake covers both the `which`/`colima
# status` calls and the docker lookups the driver makes. Async since v0.7.7,
# because that seam is (see the module docstring).
Runner = Callable[..., Awaitable[Proc]]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Literal["ok", "fail", "skip"]  # skip = optional and absent — never blocks
    required: bool
    detail: str
    fix: str = ""  # exact shell command to fix, when status != "ok"


# The one install command for `bun`, shared with `odin start` -- which shells
# out to it to build the UI from a clone and has to name the SAME remedy doctor
# does. Two spellings of one fix is how a user ends up doubting both.
BUN_INSTALL = "curl -fsSL https://bun.sh/install | bash"

# (name, required, fix, when_optional) for every plain is-it-on-PATH tool check.
# `when_optional` is the precise consequence of NOT having it -- "optional" on
# its own was misleading for `limactl` (field test LOW-15), since every EC2 node
# is a real Lima VM and an EC2 canvas simply cannot work without it.
_TOOLS: tuple[tuple[str, bool, str, str], ...] = (
    # `brew install colima` does NOT bring the docker CLI -- `brew deps colima`
    # is `lima`, nothing else. Fresh-user finding BLOCK-2: the installer's own
    # brew list left a fresh Mac with colima and no `docker`, and doctor then
    # told the user to install the thing they already had.
    ("docker", True, "brew install docker", ""),
    ("tofu", True, "brew install opentofu", ""),  # Apply shells out to it (simulate/runner.py)
    ("limactl", False, "brew install lima",
     "REQUIRED for any canvas with an EC2 node (each one is a real Lima VM) and for "
     "LimaRuntime; every other kind runs on containers and needs none of it"),
    ("bun", False, BUN_INSTALL,
     "needed only to build the UI from a clone -- the released package ships one prebuilt"),
    ("claude", False, "see https://docs.claude.com/claude-code",
     "needed only by \"what's wrong here?\" (POST /agent/debug); translation is deterministic"),
    # Field test 6 F8: BOTH of these were undocumented AND unchecked, and doctor
    # printed "All required checks passed." without ever looking at either --
    # the same false-green shape as the hardcoded disk floor, one layer out.
    #
    # OPTIONAL, on `limactl`'s precedent and for the same reason: the mesh is
    # ONE feature, and everything the product does today (rds, s3, sqs, sns,
    # dynamodb, lambda, ecs, alb, elasticache, IAM, apply, destroy) runs without
    # either binary. Failing doctor -- exit 1, "1 required check(s) failed" --
    # on a machine that will never draw a VPC would invent a blocker, which is
    # the mirror of the false green. What fixes the false green is the ROW: an
    # absent binary is now printed, named, and given `brew install nebula`.
    #
    # Two rows, one formula, because they break at different moments with
    # different consequences -- PROBED with a PATH holding neither (nothing was
    # uninstalled), against the real fabric:
    #   nebula-cert absent -> ensure_network() raises RuntimeError("nebula-cert
    #     ca failed: nebula-cert: command not found"), so `CreateVpc` fails and
    #     an apply of any canvas with a VPC node fails with it.
    #   nebula absent      -> LighthouseManager.ensure_started() returns False
    #     and logs "nebula not found on PATH; lighthouse not started" -- into the
    #     SERVER LOG, which is why doctor is the only place a user finds out
    #     before drawing anything.
    ("nebula", False, "brew install nebula",
     "REQUIRED to run an env's Nebula lighthouse (started by the first member to join -- an EC2 VM, "
     "or a backing on a VPC canvas with no EC2 node at all); "
     "without it odin logs `nebula not found on PATH; lighthouse not started` to the "
     "server log and the mesh never forms"),
    ("nebula-cert", False, "brew install nebula",
     "REQUIRED for any canvas with a VPC node -- CreateVpc signs that env's Nebula CA, and "
     "without it the apply fails with `nebula-cert ca failed: nebula-cert: command not found`"),
)

ALL_CHECKS: tuple[str, ...] = (
    "colima", *(name for name, _, _, _ in _TOOLS), "disk", "memory", "dynalite-image",
)


async def _subprocess_run(args: list[str], input: str | None = None) -> Proc:
    """The runner every check goes through -- `util.run_command_async`, so a
    tool that isn't installed comes back as rc 127 rather than raising. A
    doctor that can crash on a missing binary is a doctor that reports nothing
    on the machines that need it most (BLOCK-2: no `docker` -> traceback, zero
    rows).

    The rc-127 contract is IDENTICAL to the sync `run_command`'s and is pinned
    for both twins in `tests/test_util.py` -- `create_subprocess_exec` raises
    `FileNotFoundError` at creation for an absent binary exactly as
    `subprocess.run` does, and the same guard turns it into a result."""
    return await run_command_async(args, input=input)


async def _which(run: Runner, tool: str) -> str:
    proc = await run(["which", tool])
    return proc.stdout.strip() if proc.returncode == 0 else ""


async def _check_tool(run: Runner, name: str, required: bool, fix: str, when_optional: str) -> CheckResult:
    path = await _which(run, name)
    status = "ok" if path else ("fail" if required else "skip")
    absent = "not found on PATH" + (f" -- {when_optional}" if when_optional else "")
    return CheckResult(name, status, required, path or absent, "" if path else fix)


async def _check_colima(run: Runner) -> CheckResult:
    path = await _which(run, "colima")
    if not path:
        return CheckResult("colima", "fail", True, "not found on PATH", "brew install colima")
    proc = await run(["colima", "status"])
    if proc.returncode != 0:
        # Colima's OWN last line, when it said anything. "dependency check
        # failed for VM: lima not found" is a different problem from "not
        # running", and collapsing both to `colima start` sends the user to a
        # command that fails differently (fresh-user FRICTION-4).
        said = (proc.stderr or proc.stdout).strip().splitlines()
        return CheckResult("colima", "fail", True,
                           f"{path} — {said[-1] if said else 'installed but not running'}",
                           "colima start")
    return CheckResult("colima", "ok", True, f"{path} — running")


async def _check_disk(root: Path) -> CheckResult:
    # Async only so the dispatch table in `run_checks` is uniform -- there is
    # no I/O to await here; `disk_usage` is a single `statvfs`.
    free_gib = disk_usage(root).free / 2**30
    floor = default_min_disk_gib()  # the SAME number admission rejects an Apply on
    ok = free_gib > floor
    return CheckResult(
        "disk", "ok" if ok else "fail", True,
        f"{free_gib:.1f} GiB free on the volume holding {root} "
        f"(Apply needs >{floor:.0f} GiB; ODIN_MIN_DISK_GIB overrides)",
        # Odin's own biggest reclaimable is named here rather than left to be
        # discovered: an rds data volume outlives its container by design, so a
        # user short on disk has somewhere concrete to look before hunting.
        # Printed only on FAILURE -- the row is a check, not an advert -- and it
        # names the command instead of a count, because a count would need a
        # docker read this check does not do and cannot honestly guess at.
        "" if ok else f"free up disk space (>{floor:.0f} GiB needed; no single command). "
                      "`odin volumes` lists the Docker volumes odin is holding and which are orphaned",
    )


async def _check_memory(run: Runner, root: Path) -> CheckResult:
    """The admission budget, straight from `check_admission` itself (an empty
    Stack, so this is a pure read): the number an Apply is rejected against.

    Informational, never a blocker -- an unknown total means the container
    runtime didn't answer, which the `colima` check above already reports as the
    real failure, and admission skips its own memory check in that case too."""
    # BLOCK-2: no docker CLI = nothing to ask, and `colima start` is the wrong
    # remedy for it. Named as its own answer rather than collapsed into the
    # daemon-is-silent one, which sends the user off to start a running Colima.
    if not await _which(run, "docker"):
        return CheckResult(
            "memory", "skip", False,
            "unknown -- there is no `docker` CLI on PATH to ask for a memory total, so "
            "Apply's memory admission check is skipped entirely (see the docker row)",
            "brew install docker",
        )
    host = await ColimaRuntime(runner=run).ensure_host()
    budget_mib = check_admission(Stack(), host, root).budget_mib
    known = budget_mib > 0
    detail = (
        f"{budget_mib / 1024:.1f} GiB admission budget of {host.total_mem_mib / 1024:.1f} GiB "
        # Names the CONTAINER pool's variable specifically: this row reports the
        # container runtime's memory, and the EC2/VM pool has its own budget
        # (`ODIN_VM_MEMORY_BUDGET_MIB`) that this number says nothing about.
        "total reported by the container runtime -- Apply rejects a canvas estimated above it "
        "(ODIN_CONTAINER_MEMORY_BUDGET_MIB overrides; the older ODIN_MEMORY_BUDGET_MIB still works)"
        if known else
        "unknown -- the container runtime reported no memory total, so Apply's memory "
        "admission check is skipped entirely"
    )
    return CheckResult("memory", "ok" if known else "skip", False, detail,
                       "" if known else "colima start")


async def _check_dynalite_image(run: Runner) -> CheckResult:
    # Guard on the docker CLI first: without it there's no daemon to ask, and
    # the note below (skip + the prebake offer) is still the right answer.
    # Two statements rather than one `and` chain: `await` short-circuits fine,
    # but keeping the driver call on its own line makes the guard readable.
    present = bool(await _which(run, "docker")) and await ColimaRuntime(runner=run).image_exists(DYNALITE_IMAGE)
    detail = f"{DYNALITE_IMAGE} " + (
        "present" if present
        else "absent — first Apply with DynamoDB will build it (one-time npm install)"
    )
    return CheckResult("dynalite-image", "ok" if present else "skip", False, detail,
                       "" if present else "odin doctor --prebake")


async def run_checks(which: Iterable[str], run: Runner, disk_path: Path | None = None) -> list[CheckResult]:
    """Run the named checks through `run` (the subprocess seam); results come
    back in the order asked. `disk_path` defaults to the current directory —
    the volume Odin's images, containers, and `.odin/` state land on.

    Sequential on purpose. These are cheap `which` calls and two docker reads,
    and running them in order keeps the rendered table deterministic; there is
    nothing here worth a `TaskGroup`."""
    root = disk_path or Path.cwd()
    # `partial`, never `lambda`: a `lambda` cannot contain `await`, and these
    # entries are coroutine FUNCTIONS that the loop below calls and awaits.
    checks: dict[str, Callable[[], Awaitable[CheckResult]]] = {
        "colima": partial(_check_colima, run),
        "disk": partial(_check_disk, root),
        "memory": partial(_check_memory, run, root),
        "dynalite-image": partial(_check_dynalite_image, run),
    }
    checks.update({name: partial(_check_tool, run, name, required, fix, when_optional)
                   for name, required, fix, when_optional in _TOOLS})
    results: list[CheckResult] = []
    for name in which:
        results.append(await checks[name]())
    return results


_ICONS = {"ok": "✓", "fail": "✗", "skip": "○"}


async def _prebake() -> None:
    if not await _which(_subprocess_run, "docker"):
        typer.echo("docker not found on PATH — there is nothing to build the image with.")
        typer.echo("fix: brew install docker")
        raise typer.Exit(1)
    runtime = ColimaRuntime()
    present = await runtime.image_exists(DYNALITE_IMAGE)
    state = "present" if present else "absent — building now (one-time npm install)"
    typer.echo(f"before: {DYNALITE_IMAGE} {state}")
    await BackingAws(runtime).ensure_dynalite_image()
    # ASK THE DAEMON AGAIN rather than asserting the happy path. This line used
    # to print "present (just built)" unconditionally, directly under a call
    # that was not even awaited -- so `odin doctor --prebake` built nothing and
    # reported success, the same shape as `doctor` itself silently running zero
    # checks. The status is now the OUTCOME of a real second read, and a build
    # that did not land exits non-zero saying what is actually there.
    landed = await runtime.image_exists(DYNALITE_IMAGE)
    if not landed:
        typer.echo(
            f"after:  {DYNALITE_IMAGE} STILL ABSENT — the build reported no error but the "
            "image is not in the daemon. Run `docker images` and check the daemon's disk.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"after:  {DYNALITE_IMAGE} present ({'already there' if present else 'just built'})")


@app.command()
def doctor(
    prebake: bool = typer.Option(
        False, "--prebake",
        help=f"Build the {DYNALITE_IMAGE} image now (a one-time npm install inside a "
             "container) instead of making the first DynamoDB Apply wait for it. Builds "
             "and exits -- it runs no checks. Documented in the README under Install.",
    ),
) -> None:
    """Preflight: verify this machine has everything Odin needs to run."""
    # The whole async boundary of this command, in two calls. See the module
    # docstring: Typer does NOT await an `async def` command, so the bridge has
    # to be here, inside a sync command body.
    if prebake:
        asyncio.run(_prebake())
        return
    results = asyncio.run(run_checks(ALL_CHECKS, _subprocess_run))
    for result in results:
        typer.echo(f" {_ICONS[result.status]} {result.name:<15} {result.detail}")
        if result.fix:
            typer.echo(f"{'':<18}fix: {result.fix}")
    blockers = [r for r in results if r.required and r.status == "fail"]
    if blockers:
        typer.echo(f"\n{len(blockers)} required check(s) failed.")
        raise typer.Exit(1)
    # The sentence, plus what it does NOT cover. Field test 6 F8 read "All
    # required checks passed." as "this machine is ready" -- fair, since nothing
    # else on the last line qualified it -- while an absent dependency sat in a
    # ○ row above (or, then, in no row at all). The optional rows are the ones
    # that decide whether a PARTICULAR canvas works, so the summary now counts
    # them instead of leaving a reader to notice.
    skipped = [r for r in results if r.status == "skip"]
    typer.echo("\nAll required checks passed." + (
        f" {len(skipped)} optional check(s) reported something "
        f"({', '.join(r.name for r in skipped)}) -- read the ○ rows: each names the "
        "canvas or feature it is required for."
        if skipped else ""
    ))
