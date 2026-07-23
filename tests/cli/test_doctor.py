"""Unit tests for `odin doctor` — fake subprocess runner + fake runtime, no
real Colima/Docker calls (real-infra checks live behind `-m integration`)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import odin.__main__  # noqa: F401 — registers start/stop/… so `app` stays a multi-command group
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
