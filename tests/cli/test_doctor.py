"""Unit tests for `odin doctor` — fake subprocess runner + fake runtime, no
real Colima/Docker calls (real-infra checks live behind `-m integration`)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import odin.__main__  # noqa: F401 — registers start/stop/… so `app` stays a multi-command group
from odin.aws.backings import DYNALITE_IMAGE
from odin.cli import doctor as doctor_mod
from odin.cli.app import app
from odin.cli.doctor import ALL_CHECKS, run_checks

runner = CliRunner()

GIB = 2**30


@dataclass(frozen=True)
class FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


ALL_OK = {
    "which colima": FakeProc(0, "/opt/homebrew/bin/colima\n"),
    "colima status": FakeProc(0),
    "which docker": FakeProc(0, "/opt/homebrew/bin/docker\n"),
    "which tofu": FakeProc(0, "/opt/homebrew/bin/tofu\n"),
    "which limactl": FakeProc(0, "/opt/homebrew/bin/limactl\n"),
    "which bun": FakeProc(0, "/Users/me/.bun/bin/bun\n"),
    "which claude": FakeProc(0, "/opt/homebrew/bin/claude\n"),
    # Real paths from this machine: `brew install nebula` ships both binaries,
    # symlinked out of one Cellar/nebula/<version>/bin.
    "which nebula": FakeProc(0, "/opt/homebrew/bin/nebula\n"),
    "which nebula-cert": FakeProc(0, "/opt/homebrew/bin/nebula-cert\n"),
    "docker image inspect -f {{.Id}} " + DYNALITE_IMAGE: FakeProc(0, "sha256:abc\n"),
    # `ensure_host` -> the memory check's total (48 GiB in bytes, 8 CPUs)
    "docker info --format {{.MemTotal}} {{.NCPU}}": FakeProc(0, f"{48 * GIB} 8\n"),
}


def make_run(overrides: dict[str, FakeProc] | None = None):
    """A fake runner: known command lines answer from the table, everything
    else fails (rc 1, empty output) — exactly how `which`/docker behave.

    A COROUTINE FUNCTION since v0.7.7, because the seam it stands in for is
    one: `doctor._subprocess_run` is `async def`, `_which` awaits it, and
    `ColimaRuntime(runner=…)` awaits the runner it is handed
    (`runtime/colima.py::_cli` -> `await self._run(...)`). A sync fake here
    would not merely be untidy -- it would stand in for an async seam, which is
    how a suite goes green over broken production code. It self-reports rather
    than passing quietly (`await` on a plain `FakeProc` raises TypeError), and
    that is exactly what the 27 failures this port fixes were."""
    responses = ALL_OK | (overrides or {})

    async def run(args: list[str], input: str | None = None) -> FakeProc:
        return responses.get(" ".join(args), FakeProc(1))

    return run


def by_name(results):
    return {r.name: r for r in results}


def fake_disk(free_bytes: int):
    return lambda path: SimpleNamespace(total=500 * GIB, used=0, free=free_bytes)


def patch_disk(monkeypatch, free_bytes: int = 50 * GIB) -> None:
    monkeypatch.setattr(doctor_mod, "disk_usage", fake_disk(free_bytes))


# --- run_checks core -------------------------------------------------------

async def test_all_checks_pass(monkeypatch):
    patch_disk(monkeypatch)
    results = await run_checks(ALL_CHECKS, make_run(), disk_path=Path.cwd())
    assert [r.status for r in results] == ["ok"] * len(ALL_CHECKS)
    assert [r.name for r in results] == list(ALL_CHECKS)


async def test_required_tool_missing_fails_with_fix():
    results = by_name(await run_checks(["tofu"], make_run({"which tofu": FakeProc(1)})))
    tofu = results["tofu"]
    assert (tofu.status, tofu.required, tofu.fix) == ("fail", True, "brew install opentofu")


async def test_colima_not_installed():
    colima = by_name(await run_checks(["colima"], make_run({"which colima": FakeProc(1)})))["colima"]
    assert (colima.status, colima.fix) == ("fail", "brew install colima")


async def test_colima_installed_but_not_running():
    colima = by_name(await run_checks(["colima"], make_run({"colima status": FakeProc(1)})))["colima"]
    assert (colima.status, colima.fix) == ("fail", "colima start")
    assert "not running" in colima.detail


async def test_optional_tools_missing_are_skips():
    absent = {"which limactl": FakeProc(1), "which bun": FakeProc(1), "which claude": FakeProc(1)}
    results = await run_checks(["limactl", "bun", "claude"], make_run(absent))
    assert [(r.status, r.required) for r in results] == [("skip", False)] * 3
    assert [r.fix for r in results] == [
        "brew install lima",
        "curl -fsSL https://bun.sh/install | bash",
        "see https://docs.claude.com/claude-code",
    ]


# --- disk headroom ---------------------------------------------------------

async def test_disk_low_fails(monkeypatch):
    patch_disk(monkeypatch, free_bytes=5 * GIB)
    disk = by_name(await run_checks(["disk"], make_run(), disk_path=Path.cwd()))["disk"]
    assert (disk.status, disk.required) == ("fail", True)
    assert "5.0 GiB free" in disk.detail
    assert "free up disk space" in disk.fix


async def test_disk_headroom_ok(monkeypatch):
    patch_disk(monkeypatch, free_bytes=50 * GIB)
    disk = by_name(await run_checks(["disk"], make_run(), disk_path=Path.cwd()))["disk"]
    assert (disk.status, disk.fix) == ("ok", "")


async def test_disk_floor_follows_odin_min_disk_gib(monkeypatch):
    """LOW-14: doctor hardcoded 10 GiB while admission read the env var, so its
    docstring's claim that the two "agree on what 'enough disk' means" held only
    at the default."""
    patch_disk(monkeypatch, free_bytes=50 * GIB)
    monkeypatch.setenv("ODIN_MIN_DISK_GIB", "999")
    disk = by_name(await run_checks(["disk"], make_run(), disk_path=Path.cwd()))["disk"]
    assert disk.status == "fail"
    assert "999 GiB" in disk.fix and "ODIN_MIN_DISK_GIB" in disk.detail


# --- memory: the budget an Apply is actually admitted against --------------

async def test_memory_reports_the_admission_budget(monkeypatch):
    """LOW-15: doctor said nothing about memory, though admission can hard-
    reject an Apply on it. The number quoted here comes from `check_admission`
    itself, so the two cannot drift apart."""
    patch_disk(monkeypatch)
    memory = by_name(await run_checks(["memory"], make_run(), disk_path=Path.cwd()))["memory"]
    assert (memory.status, memory.required) == ("ok", False)
    assert "33.6 GiB admission budget of 48.0 GiB" in memory.detail  # 0.7 x 48
    assert "ODIN_MEMORY_BUDGET_MIB" in memory.detail


async def test_memory_budget_honours_the_env_override(monkeypatch):
    patch_disk(monkeypatch)
    monkeypatch.setenv("ODIN_MEMORY_BUDGET_MIB", "2048")
    memory = by_name(await run_checks(["memory"], make_run(), disk_path=Path.cwd()))["memory"]
    assert "2.0 GiB admission budget" in memory.detail


async def test_memory_is_a_skip_not_a_blocker_when_the_runtime_says_nothing(monkeypatch):
    patch_disk(monkeypatch)
    silent = {"docker info --format {{.MemTotal}} {{.NCPU}}": FakeProc(1)}
    memory = by_name(await run_checks(["memory"], make_run(silent), disk_path=Path.cwd()))["memory"]
    assert (memory.status, memory.required, memory.fix) == ("skip", False, "colima start")
    assert "admission check is skipped" in memory.detail


async def test_limactl_says_exactly_when_it_is_optional():
    """LOW-15: reported as a bare `○` while every EC2 node is a real Lima VM."""
    limactl = by_name(await run_checks(["limactl"], make_run({"which limactl": FakeProc(1)})))["limactl"]
    assert limactl.status == "skip"
    assert "REQUIRED for any canvas with an EC2 node" in limactl.detail


# --- nebula: the dependency doctor never looked at (field test 6 F8) --------
#
# The absence was simulated with a PATH holding neither binary (nothing was
# uninstalled) and the consequences below were PROBED against the real fabric
# before being written down -- `ensure_network` really does raise
# `RuntimeError: nebula-cert ca failed: nebula-cert: command not found`, and
# `LighthouseManager.ensure_started` really does return False and log
# "nebula not found on PATH; lighthouse not started".

async def test_nebula_binaries_are_checked_at_all():
    """The finding itself: `odin doctor | grep -i nebula` printed NOTHING while
    the mesh needs the binary, and doctor still said everything passed."""
    results = by_name(await run_checks(ALL_CHECKS, make_run(), disk_path=Path.cwd()))
    assert "nebula" in results and "nebula-cert" in results


async def test_nebula_absent_is_a_skip_not_a_blocker():
    """Optional, on `limactl`'s precedent: the mesh is one feature, and every
    kind odin ships today works without it. A required-fail here would invent a
    blocker on every machine that never draws a VPC."""
    absent = {"which nebula": FakeProc(1), "which nebula-cert": FakeProc(1)}
    results = await run_checks(["nebula", "nebula-cert"], make_run(absent))
    assert [(r.status, r.required) for r in results] == [("skip", False)] * 2
    assert [r.fix for r in results] == ["brew install nebula"] * 2


async def test_nebula_rows_name_the_consequence_each_binary_has():
    """"optional" on its own is what made `limactl` misleading. Each row carries
    the failure the user would otherwise meet at apply time -- and they are
    DIFFERENT failures, which is why this is two rows and not one."""
    absent = {"which nebula": FakeProc(1), "which nebula-cert": FakeProc(1)}
    results = by_name(await run_checks(["nebula", "nebula-cert"], make_run(absent)))
    assert "lighthouse" in results["nebula"].detail
    assert "REQUIRED for any canvas with a VPC node" in results["nebula-cert"].detail
    # the verbatim string the real fabric raises, so the row matches the error
    assert "nebula-cert ca failed" in results["nebula-cert"].detail


def test_the_summary_line_no_longer_reads_as_a_clean_bill_of_health(monkeypatch):
    """F8's other half: "All required checks passed." was the LAST line while an
    absent dependency sat above it. The sentence still holds (required checks
    did pass) so it is unchanged -- but it no longer stands alone."""
    patch_disk(monkeypatch)
    absent = {"which nebula": FakeProc(1), "which nebula-cert": FakeProc(1)}
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run(absent))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "○ nebula " in result.output and "○ nebula-cert" in result.output
    assert "fix: brew install nebula" in result.output
    assert "All required checks passed." in result.output
    assert "2 optional check(s) reported something (nebula, nebula-cert)" in result.output


def test_the_summary_line_stays_bare_when_nothing_was_skipped(monkeypatch):
    patch_disk(monkeypatch)
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run())
    result = runner.invoke(app, ["doctor"])
    assert result.output.rstrip().endswith("All required checks passed.")


# --- dynalite prebake offer ------------------------------------------------

async def test_dynalite_image_present_is_ok():
    note = by_name(await run_checks(["dynalite-image"], make_run()))["dynalite-image"]
    assert (note.status, note.required, note.fix) == ("ok", False, "")


async def test_dynalite_image_absent_is_informational_note():
    absent = {"docker image inspect -f {{.Id}} " + DYNALITE_IMAGE: FakeProc(1)}
    note = by_name(await run_checks(["dynalite-image"], make_run(absent)))["dynalite-image"]
    assert (note.status, note.required) == ("skip", False)
    assert "first Apply with DynamoDB will build it (one-time npm install)" in note.detail
    assert note.fix == "odin doctor --prebake"


async def test_dynalite_note_survives_missing_docker():
    absent = {"which docker": FakeProc(1)}
    note = by_name(await run_checks(["dynalite-image"], make_run(absent)))["dynalite-image"]
    assert note.status == "skip"


# --- the CLI command -------------------------------------------------------

def test_cli_exit_zero_with_optional_missing(monkeypatch):
    patch_disk(monkeypatch)
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run({"which limactl": FakeProc(1)}))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "○ limactl" in result.output
    assert "brew install lima" in result.output
    assert "All required checks passed." in result.output


def test_cli_exit_one_on_required_failure(monkeypatch):
    patch_disk(monkeypatch)
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run({"which tofu": FakeProc(1)}))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "✗ tofu" in result.output
    assert "fix: brew install opentofu" in result.output


def test_docker_absent_prints_every_row_and_exits_one(monkeypatch):
    """Fresh-user BLOCK-2: `brew install colima` brings only `lima`, so a Mac
    that ran the install one-liner has no `docker` -- and doctor died there with
    a FileNotFoundError traceback out of `_check_memory`, printing ZERO rows.
    Every check must still be reported, docker named as the failure, with the
    remedy that actually installs it."""
    patch_disk(monkeypatch)
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run({"which docker": FakeProc(1)}))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    for name in ALL_CHECKS:  # the FULL table, not a stack trace
        assert f" {name:<15}" in result.output
    assert "✗ docker" in result.output
    assert "fix: brew install docker" in result.output
    assert "1 required check(s) failed." in result.output


async def test_memory_names_the_missing_docker_cli_not_colima_start(monkeypatch):
    """The memory row's remedy has to match its cause: with no docker CLI
    there is nothing to ask, and `colima start` is a dead end."""
    patch_disk(monkeypatch)
    memory = by_name(await run_checks(["memory"], make_run({"which docker": FakeProc(1)})))["memory"]
    assert (memory.status, memory.required, memory.fix) == ("skip", False, "brew install docker")
    assert "no `docker` CLI on PATH" in memory.detail


async def test_colima_failure_carries_colimas_own_words(monkeypatch):
    """FRICTION-4: colima's real complaint ("dependency check failed for VM:
    lima not found") was collapsed to "installed but not running", so the user
    ran `colima start` and got a different error."""
    said = "FATA[0000] dependency check failed for VM: lima not found, run 'brew install lima'"
    colima = by_name(await run_checks(["colima"], make_run({"colima status": FakeProc(1, "", said)})))["colima"]
    assert colima.status == "fail"
    assert "lima not found" in colima.detail


def test_prebake_without_docker_refuses_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run({"which docker": FakeProc(1)}))
    result = runner.invoke(app, ["doctor", "--prebake"])
    assert result.exit_code == 1
    assert "docker not found on PATH" in result.output
    assert "brew install docker" in result.output


def test_cli_all_ok(monkeypatch):
    patch_disk(monkeypatch)
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run())
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert result.output.count("✓") == len(ALL_CHECKS)


# --- odin doctor --prebake -------------------------------------------------

class FakeRuntime:
    def __init__(self, images: set[str]):
        self.images = images

    # async, like the real `ColimaRuntime.image_exists` it stands in for --
    # a sync fake here would let `_prebake`'s `await` fail on a plain bool.
    async def image_exists(self, tag: str) -> bool:
        return tag in self.images


class FakeBacking:
    ensured: list[str] = []

    def __init__(self, runtime):
        self.runtime = runtime

    # async, like the real `BackingAws.ensure_dynalite_image`.
    async def ensure_dynalite_image(self) -> None:
        FakeBacking.ensured.append(DYNALITE_IMAGE)
        self.runtime.images.add(DYNALITE_IMAGE)


def patch_prebake(monkeypatch, images: set[str]) -> None:
    FakeBacking.ensured = []
    monkeypatch.setattr(doctor_mod, "ColimaRuntime", lambda *a, **kw: FakeRuntime(images))
    monkeypatch.setattr(doctor_mod, "BackingAws", FakeBacking)


def test_prebake_builds_absent_image(monkeypatch):
    patch_prebake(monkeypatch, images=set())
    result = runner.invoke(app, ["doctor", "--prebake"])
    assert result.exit_code == 0
    assert FakeBacking.ensured == [DYNALITE_IMAGE]
    assert f"before: {DYNALITE_IMAGE} absent" in result.output
    assert "just built" in result.output


def test_prebake_with_image_already_present(monkeypatch):
    patch_prebake(monkeypatch, images={DYNALITE_IMAGE})
    result = runner.invoke(app, ["doctor", "--prebake"])
    assert result.exit_code == 0
    assert FakeBacking.ensured == [DYNALITE_IMAGE]  # idempotent call-through, still invoked
    assert f"before: {DYNALITE_IMAGE} present" in result.output
    assert "already there" in result.output


class FakeBackingThatBuildsNothing(FakeBacking):
    """A build that raises nothing and leaves no image behind — the exact state
    the old unconditional "present (just built)" line reported as success."""

    async def ensure_dynalite_image(self) -> None:
        FakeBacking.ensured.append(DYNALITE_IMAGE)  # attempted, and still no image


def test_prebake_that_built_nothing_says_so_instead_of_claiming_it_built(monkeypatch):
    """Honesty rule 2, in the one branch the other two prebake tests cannot
    reach: the closing status is the OUTCOME of a real second `image_exists`
    read, never an assertion that the happy path happened. `_prebake` used to
    print "present (just built)" unconditionally, under a call that was not even
    awaited — so it reported a build it never performed.

    This test exists because the guard was NOT ratcheted: mutating
    `landed = await runtime.image_exists(...)` to `landed = True` left all 29
    tests in this file green. With this test it is the one that fails."""
    FakeBacking.ensured = []
    monkeypatch.setattr(doctor_mod, "_subprocess_run", make_run())  # docker present
    monkeypatch.setattr(doctor_mod, "ColimaRuntime", lambda *a, **kw: FakeRuntime(set()))
    monkeypatch.setattr(doctor_mod, "BackingAws", FakeBackingThatBuildsNothing)
    result = runner.invoke(app, ["doctor", "--prebake"])
    assert result.exit_code == 1
    assert FakeBacking.ensured == [DYNALITE_IMAGE]  # the build really was attempted
    assert "STILL ABSENT" in result.stderr
    assert "just built" not in result.stdout
