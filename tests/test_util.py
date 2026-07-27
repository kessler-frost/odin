"""Release finding #2 -- atomic_write_text: every mutable `.odin/` sidecar is
rewritten wholesale on every mutation; a crash or concurrent reader mid-write
must never observe a truncated file."""
from __future__ import annotations

import asyncio
import stat
import threading
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest

from odin import util
from odin.runtime.colima import ColimaRuntime
from odin.util import atomic_write_text, odin_version


def test_write_then_read_roundtrips(tmp_path: Path):
    path = tmp_path / "sub" / "file.json"
    atomic_write_text(path, '{"a": 1}')
    assert path.read_text() == '{"a": 1}'


def test_creates_missing_parent_dir(tmp_path: Path):
    path = tmp_path / "does" / "not" / "exist" / "file.json"
    atomic_write_text(path, "hello")
    assert path.exists()


def test_overwrite_replaces_prior_content(tmp_path: Path):
    path = tmp_path / "file.json"
    atomic_write_text(path, "first")
    atomic_write_text(path, "second")
    assert path.read_text() == "second"


def test_mode_is_applied_before_replace(tmp_path: Path):
    path = tmp_path / "secret.json"
    atomic_write_text(path, "s3cr3t", mode=0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_mode_leaves_default_permissions(tmp_path: Path):
    path = tmp_path / "public.json"
    atomic_write_text(path, "data")
    # No explicit chmod call -- whatever umask-derived mode mkstemp gives it
    # (0600 from tempfile itself) or better; just assert it's readable/no crash.
    assert path.read_text() == "data"


def test_no_leftover_temp_files_on_success(tmp_path: Path):
    path = tmp_path / "file.json"
    atomic_write_text(path, "content")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "file.json"]
    assert leftovers == []


def test_crash_mid_write_leaves_original_file_intact(tmp_path: Path):
    """Simulates a crash between staging the temp file and the atomic
    replace: os.replace raising must leave the ORIGINAL file untouched, and
    must not leak the temp file either."""
    path = tmp_path / "file.json"
    atomic_write_text(path, "original")

    with patch("odin.util.os.replace", side_effect=OSError("simulated crash")):
        with pytest.raises(OSError):
            atomic_write_text(path, "new-content-that-should-never-land")

    assert path.read_text() == "original"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "file.json"]
    assert leftovers == []  # the temp file was cleaned up, not left dangling


# ------------------------------------------------------ odin_version (LOW-16)
# The field test found `odin export` stamping `odin_version 0.5.3` into every
# archive manifest while pyproject.toml said 0.7.0: the installed editable
# dist-info was stale, so `importlib.metadata` answered honestly about a
# distribution nobody had rebuilt. Backup compatibility checks rest on that
# stamp, so in a source checkout pyproject.toml -- the file a human edits --
# wins over installed metadata, and there is no second literal to drift.


def test_version_matches_pyproject_in_a_source_checkout():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert odin_version() == declared


def test_version_prefers_pyproject_over_stale_installed_metadata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(util, "_pkg_version", lambda name: "0.5.3")
    assert odin_version() != "0.5.3"


def test_version_falls_back_to_installed_metadata_outside_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An installed wheel has no pyproject.toml above the package."""
    monkeypatch.setattr(util, "_PYPROJECT", tmp_path / "pyproject.toml")
    monkeypatch.setattr(util, "_pkg_version", lambda name: "9.9.9")
    assert odin_version() == "9.9.9"


def test_version_ignores_a_pyproject_belonging_to_another_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    other = tmp_path / "pyproject.toml"
    other.write_text('[project]\nname = "not-odin"\nversion = "1.2.3"\n')
    monkeypatch.setattr(util, "_PYPROJECT", other)
    monkeypatch.setattr(util, "_pkg_version", lambda name: "9.9.9")
    assert odin_version() == "9.9.9"


def test_version_says_unknown_rather_than_inventing_a_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No checkout and no installed distribution: the honest answer is "I don't
    know", never a hardcoded number that will silently rot (v0.7.0's fallback
    still claimed 0.4.0)."""
    monkeypatch.setattr(util, "_PYPROJECT", tmp_path / "nope.toml")
    monkeypatch.setattr(util, "_pkg_version", _raise_not_found)
    assert odin_version() == "0.0.0+unknown"


def _raise_not_found(name: str) -> str:
    raise PackageNotFoundError(name)


def test_concurrent_writers_never_produce_a_partial_file(tmp_path: Path):
    """Many threads hammering the SAME path concurrently: every reader that
    observes the file at all must see one writer's full content, never a
    half-written mix (os.replace is atomic on the same filesystem)."""
    path = tmp_path / "hammered.json"
    atomic_write_text(path, "seed")
    payloads = [f"payload-{i}" * 200 for i in range(20)]
    errors: list[Exception] = []

    def writer(text: str) -> None:
        try:
            atomic_write_text(path, text)
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final = path.read_text()
    assert final == "seed" or any(final == p for p in payloads)


# --- run_command: a missing binary is a RESULT, never an exception ---------

def test_run_command_reports_a_missing_binary_as_127():
    """Fresh-user BLOCK-2: `odin doctor` crashed with a FileNotFoundError on a
    Mac with no `docker` CLI. Every odin runner seam goes through here, so the
    answer for "that tool isn't installed" is the shell's own 127 plus a
    message -- something every caller already handles."""
    result = util.run_command(["odin-no-such-binary-exists", "--version"])
    assert result.returncode == util.COMMAND_NOT_FOUND
    assert result.stdout == ""
    assert "odin-no-such-binary-exists" in result.stderr
    assert "command not found" in result.stderr


def test_run_command_passes_through_a_real_command():
    result = util.run_command(["echo", "hello"])
    assert (result.returncode, result.stdout.strip()) == (0, "hello")


# --- run_command_async: the same contract, without a thread ------------------
# Shelling out is odin's dominant blocking work (one `docker inspect` measured
# at 10.45 ms, vs 0.63-0.84 ms for a boto3 call to a local backing), and
# `asyncio.create_subprocess_exec` is natively async -- so these convert rather
# than getting wrapped in `to_thread`, which would just hide a thread pool.

@pytest.mark.parametrize(
    "args,stdin",
    [
        (["echo", "hello"], None),
        (["sh", "-c", "echo out; echo err >&2; exit 3"], None),
        (["cat"], "piped-stdin\n"),
        (["odin-no-such-binary-exists", "--version"], None),
    ],
    ids=["success", "nonzero-with-both-streams", "stdin", "missing-binary"],
)
async def test_the_async_twin_answers_exactly_what_the_sync_one_does(args, stdin):
    """The two share every caller, so any divergence is a bug in whichever the
    caller did not expect. Asserted as full-object equality rather than field
    by field, so a new field cannot quietly escape the comparison."""
    assert await util.run_command_async(args, input=stdin) == util.run_command(args, input=stdin)


async def test_a_missing_binary_is_still_a_result_and_not_a_raise():
    """The contract `odin doctor` depends on: `create_subprocess_exec` raises
    FileNotFoundError at creation for an absent binary just as `subprocess.run`
    does, and the same guard has to turn it into rc 127."""
    result = await util.run_command_async(["odin-no-such-binary-exists", "--version"])
    assert result.returncode == util.COMMAND_NOT_FOUND
    assert result.stdout == ""
    assert "odin-no-such-binary-exists" in result.stderr
    assert "command not found" in result.stderr


async def test_undecodable_output_is_replaced_rather_than_raising():
    """`text=True` decodes with the locale's codec, which can raise on a
    container log full of odd bytes. Explicit utf-8 + errors="replace" means
    the async seam mangles where the sync one could blow up."""
    result = await util.run_command_async(["printf", r"\xff\xfe"])
    assert result.returncode == 0
    assert result.stdout  # replacement chars, not an exception


async def test_it_does_not_block_the_event_loop():
    """The whole point. While a 300ms subprocess runs, the loop must stay free
    to run other tasks -- if this were a blocking call the ticker would be
    starved and record far fewer ticks."""
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    result = await util.run_command_async(["sh", "-c", "sleep 0.3; echo done"])
    ticker.cancel()

    assert result.stdout.strip() == "done"
    assert ticks > 10, f"the loop only ran {ticks} ticks during a 300ms subprocess"


def test_run_command_feeds_stdin():
    result = util.run_command(["cat"], input="piped")
    assert (result.returncode, result.stdout) == (0, "piped")


async def test_colima_runtime_survives_a_missing_docker_cli(monkeypatch: pytest.MonkeyPatch):
    """The seam fix, end to end: with nothing named `docker` on PATH,
    `ensure_host()` answers "I don't know" instead of raising."""
    monkeypatch.setenv("PATH", "/nonexistent")
    assert (await ColimaRuntime().ensure_host()).total_mem_mib == 0
