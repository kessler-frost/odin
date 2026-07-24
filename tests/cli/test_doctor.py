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
    "docker image inspect -f {{.Id}} " + DYNALITE_IMAGE: FakeProc(0, "sha256:abc\n"),
}


def make_run(overrides: dict[str, FakeProc] | None = None):
    """A fake runner: known command lines answer from the table, everything
    else fails (rc 1, empty output) — exactly how `which`/docker behave."""
    responses = ALL_OK | (overrides or {})

    def run(args: list[str], input: str | None = None) -> FakeProc:
        return responses.get(" ".join(args), FakeProc(1))

    return run


def by_name(results):
    return {r.name: r for r in results}


def fake_disk(free_bytes: int):
    return lambda path: SimpleNamespace(total=500 * GIB, used=0, free=free_bytes)


def patch_disk(monkeypatch, free_bytes: int = 50 * GIB) -> None:
    monkeypatch.setattr(doctor_mod, "disk_usage", fake_disk(free_bytes))


# --- run_checks core -------------------------------------------------------

def test_all_checks_pass(monkeypatch):
    patch_disk(monkeypatch)
    results = run_checks(ALL_CHECKS, make_run(), disk_path=Path.cwd())
    assert [r.status for r in results] == ["ok"] * len(ALL_CHECKS)
    assert [r.name for r in results] == list(ALL_CHECKS)


def test_required_tool_missing_fails_with_fix():
    results = by_name(run_checks(["tofu"], make_run({"which tofu": FakeProc(1)})))
    tofu = results["tofu"]
    assert (tofu.status, tofu.required, tofu.fix) == ("fail", True, "brew install opentofu")


def test_colima_not_installed():
    colima = by_name(run_checks(["colima"], make_run({"which colima": FakeProc(1)})))["colima"]
    assert (colima.status, colima.fix) == ("fail", "brew install colima")


def test_colima_installed_but_not_running():
    colima = by_name(run_checks(["colima"], make_run({"colima status": FakeProc(1)})))["colima"]
    assert (colima.status, colima.fix) == ("fail", "colima start")
    assert "not running" in colima.detail


def test_optional_tools_missing_are_skips():
    absent = {"which limactl": FakeProc(1), "which bun": FakeProc(1), "which claude": FakeProc(1)}
    results = run_checks(["limactl", "bun", "claude"], make_run(absent))
    assert [(r.status, r.required) for r in results] == [("skip", False)] * 3
    assert [r.fix for r in results] == [
        "brew install lima",
        "curl -fsSL https://bun.sh/install | bash",
        "see https://docs.claude.com/claude-code",
    ]


# --- disk headroom ---------------------------------------------------------

def test_disk_low_fails(monkeypatch):
    patch_disk(monkeypatch, free_bytes=5 * GIB)
    disk = by_name(run_checks(["disk"], make_run(), disk_path=Path.cwd()))["disk"]
    assert (disk.status, disk.required) == ("fail", True)
    assert "5.0 GiB free" in disk.detail
    assert "free up disk space" in disk.fix


def test_disk_headroom_ok(monkeypatch):
    patch_disk(monkeypatch, free_bytes=50 * GIB)
    disk = by_name(run_checks(["disk"], make_run(), disk_path=Path.cwd()))["disk"]
    assert (disk.status, disk.fix) == ("ok", "")


# --- dynalite prebake offer ------------------------------------------------

def test_dynalite_image_present_is_ok():
    note = by_name(run_checks(["dynalite-image"], make_run()))["dynalite-image"]
    assert (note.status, note.required, note.fix) == ("ok", False, "")


def test_dynalite_image_absent_is_informational_note():
    absent = {"docker image inspect -f {{.Id}} " + DYNALITE_IMAGE: FakeProc(1)}
    note = by_name(run_checks(["dynalite-image"], make_run(absent)))["dynalite-image"]
    assert (note.status, note.required) == ("skip", False)
    assert "first Apply with DynamoDB will build it (one-time npm install)" in note.detail
    assert note.fix == "odin doctor --prebake"


def test_dynalite_note_survives_missing_docker():
    absent = {"which docker": FakeProc(1)}
    note = by_name(run_checks(["dynalite-image"], make_run(absent)))["dynalite-image"]
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

    def image_exists(self, tag: str) -> bool:
        return tag in self.images


class FakeBacking:
    ensured: list[str] = []

    def __init__(self, runtime):
        self.runtime = runtime

    def ensure_dynalite_image(self) -> None:
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
