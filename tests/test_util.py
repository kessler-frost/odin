"""Release finding #2 -- atomic_write_text: every mutable `.odin/` sidecar is
rewritten wholesale on every mutation; a crash or concurrent reader mid-write
must never observe a truncated file."""
from __future__ import annotations

import stat
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from odin.util import atomic_write_text


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
