"""Field-test finding (v0.7.0, agent A "B5"): the documented "import refuses to
run while odin is up" guardrail never fired.

The engineer's server was up and healthy (`/health` -> ok) but `odin status`
said "Odin is not running", so `odin import` restored into a LIVE store, exit 0,
no warning, and the running server adopted the env immediately -- the exact
corruption `_refuse_live_server`'s docstring exists to prevent. Cause: the check
read only `.odin/pid`, which ONLY `odin start` writes, while the repo's own
README and CLAUDE.md document running the app as
`uvicorn odin.server:create_app --factory`.

So both launch paths are tested here, and the process-scan path is tested for
the thing that makes it safe as well as the thing that makes it work: a server
running against a DIFFERENT store must not block this store's restore.

The OS is faked at exactly one seam (`util._proc_run`, the only place either
`ps` or `lsof` is invoked); everything above it is the production code path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from odin import util
from odin.backup import BackupError, export_env, import_archive

UVICORN = "/x/.venv/bin/python -m uvicorn odin.server:create_app --factory --host 127.0.0.1 --port 4310"


@dataclass
class FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _fake_os(monkeypatch: pytest.MonkeyPatch, ps: str, cwd: str | None) -> None:
    """`ps` output for the process scan, and the cwd `lsof` reports for any pid.
    `cwd=None` = lsof answered nothing (not installed, or refused)."""
    lsof = FakeProc(stdout=f"p1234\nfcwd\nn{cwd}\n") if cwd is not None else FakeProc(returncode=1)
    monkeypatch.setattr(
        util, "_proc_run",
        lambda args: lsof if args[0] == "lsof" else FakeProc(stdout=ps),
    )


def _store(tmp_path: Path) -> Path:
    root = tmp_path / ".odin"
    (root / "e").mkdir(parents=True)
    (root / "e" / "HEAD").write_text("rev")
    return root


def test_no_server_no_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fake_os(monkeypatch, ps="  501 /usr/bin/vim notes.txt\n", cwd=None)
    assert util.live_server(_store(tmp_path)) is None


def test_odin_start_path_is_detected_via_the_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Launch path A: `odin start` (background or dev) writes `.odin/pid`."""
    _fake_os(monkeypatch, ps="", cwd=None)
    root = _store(tmp_path)
    (root / "pid").write_text(str(os.getpid()))
    server = util.live_server(root)
    assert server is not None and server.pid == os.getpid() and server.managed


def test_stale_pidfile_is_not_a_live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fake_os(monkeypatch, ps="", cwd=None)
    root = _store(tmp_path)
    (root / "pid").write_text("999999")  # never alive
    assert util.live_server(root) is None


def test_uvicorn_path_is_detected_with_no_pidfile_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Launch path B: the command the README documents. No pidfile exists."""
    root = _store(tmp_path)
    _fake_os(monkeypatch, ps=f" 4242 {UVICORN}\n", cwd=str(tmp_path))
    server = util.live_server(root)
    assert server is not None
    assert server.pid == 4242 and not server.managed and "uvicorn" in server.command


def test_a_server_on_another_store_does_not_block_this_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The second-instance case the ROADMAP explicitly blesses ("a separate
    working directory with its own store") must stay restorable."""
    root = _store(tmp_path)
    _fake_os(monkeypatch, ps=f" 4242 {UVICORN}\n", cwd=str(tmp_path / "elsewhere"))
    assert util.live_server(root) is None


def test_an_unknowable_cwd_fails_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No lsof (or no permission): a destructive restore must assume the worst
    rather than assume the server is someone else's."""
    root = _store(tmp_path)
    _fake_os(monkeypatch, ps=f" 4242 {UVICORN}\n", cwd=None)
    assert util.live_server(root) is not None


def test_import_refuses_the_uvicorn_launch_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The whole point: B5's exact scenario now refuses, with a fix that works
    for a server `odin stop` cannot help with."""
    root = _store(tmp_path)
    archive = tmp_path / "e.tar.gz"
    _fake_os(monkeypatch, ps="", cwd=None)
    export_env(root, "e", archive)

    _fake_os(monkeypatch, ps=f" 4242 {UVICORN}\n", cwd=str(tmp_path))
    with pytest.raises(BackupError) as exc:
        import_archive(archive, root, env="restored")
    message = str(exc.value)
    assert "4242" in message and "kill 4242" in message  # a fix `odin stop` can't give
    assert not (root / "restored").exists()
