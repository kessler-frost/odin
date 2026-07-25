"""Security finding #1a/b: the control app has no authentication of its
own, so the bind address IS the access control -- default to loopback
everywhere `odin start` can launch uvicorn, and warn loudly the moment a
caller opts into anything wider."""
from __future__ import annotations

import os
import sys

import pytest
import typer

from odin import __main__ as main_mod, util


def test_default_host_constant_is_loopback():
    assert main_mod.DEFAULT_HOST == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "10.0.0.1", ""])
def test_warns_on_any_non_loopback_host(host, capsys):
    main_mod._warn_if_non_loopback(host)
    captured = capsys.readouterr()
    assert "no authentication" in captured.err
    assert host in captured.err or host == ""


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_no_warning_for_loopback_hosts(host, capsys):
    main_mod._warn_if_non_loopback(host)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_start_foreground_passes_host_through_to_uvicorn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    captured = {}

    def fake_uvicorn_run(app_path, factory, host, port):
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setitem(
        sys.modules, "uvicorn",
        type("FakeUvicorn", (), {"run": staticmethod(fake_uvicorn_run)}),
    )

    main_mod.start(port=4200, foreground=True, dev=False, host="0.0.0.0")

    assert captured == {"host": "0.0.0.0", "port": 4200}


def test_start_background_passes_host_through_to_the_subprocess_argv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    captured = {}

    class FakeProc:
        pid = 12345

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)

    main_mod.start(port=4200, foreground=False, dev=False, host="192.168.1.5")

    argv = captured["argv"]
    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "192.168.1.5"


def test_start_defaults_to_loopback_when_host_not_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    captured = {}

    class FakeProc:
        pid = 12345

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)

    main_mod.start(port=4200, foreground=False, dev=False, host=main_mod.DEFAULT_HOST)

    argv = captured["argv"]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"


def test_dev_mode_backend_subprocess_gets_the_host_argv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    class FakeProc:
        pid = 1
        stdout = []

        def poll(self):
            return 0

        def wait(self):
            return 0

        def terminate(self):
            pass

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return FakeProc()

    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main_mod.signal, "signal", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod.signal, "pause", lambda: None)

    main_mod._start_dev(port=4200, host="203.0.113.9")

    backend_argv = calls[0]
    assert backend_argv[backend_argv.index("--host") + 1] == "203.0.113.9"


# --- `odin status`/`stop` honesty for BOTH launch paths (field test B5) -------
# v0.7.0 read `.odin/pid` and nothing else, so a server started the way the
# README documents (`uvicorn odin.server:create_app --factory`) made `odin
# status` print "Odin is not running" while `/health` answered ok -- and that
# same blind spot let `odin import` restore into the live store.

_UVICORN_ARGV = "/x/.venv/bin/python -m uvicorn odin.server:create_app --factory --port 4310"


def _fake_probes(monkeypatch, ps, cwd):
    """Fake the one OS seam `live_server` uses (`ps` + `lsof`)."""
    def fake(args):
        stdout = f"p1\nfcwd\nn{cwd}\n" if args[0] == "lsof" else ps
        return type("P", (), {"returncode": 0, "stdout": stdout, "stderr": ""})

    monkeypatch.setattr(util, "_proc_run", fake)


def test_status_is_honest_about_a_server_odin_did_not_start(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _fake_probes(monkeypatch, ps=f" 4242 {_UVICORN_ARGV}\n", cwd=str(tmp_path))
    main_mod.status()
    out = capsys.readouterr().out
    assert "Odin is running" in out and "4242" in out and "outside `odin start`" in out


def test_status_reports_the_pidfile_path_as_managed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _fake_probes(monkeypatch, ps="", cwd=str(tmp_path))
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text(str(os.getpid()))
    main_mod.status()
    assert f"Odin is running (pid {os.getpid()}, pidfile)" in capsys.readouterr().out


def test_status_cleans_a_stale_pidfile_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _fake_probes(monkeypatch, ps="", cwd=str(tmp_path))
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text("999999")
    main_mod.status()
    assert "not running (cleaned up a stale PID file)" in capsys.readouterr().out
    assert not main_mod.PID_FILE.exists()


def test_stop_signals_a_server_odin_did_not_start(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _fake_probes(monkeypatch, ps=f" 4242 {_UVICORN_ARGV}\n", cwd=str(tmp_path))
    killed = []
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    main_mod.stop()
    assert killed == [(4242, main_mod.signal.SIGTERM)]
    assert "Stopped." in capsys.readouterr().out


def test_version_flag_prints_the_real_version(capsys):
    """LOW-16: there was no `odin --version` at all, which is exactly why a
    version stamp could go two releases stale without anyone noticing."""
    with pytest.raises(typer.Exit):
        main_mod._print_version(True)
    assert capsys.readouterr().out.strip() == f"odin {util.odin_version()}"


def test_version_callback_is_a_no_op_without_the_flag(capsys):
    main_mod._print_version(False)
    assert capsys.readouterr().out == ""


def test_start_foreground_writes_and_removes_the_pidfile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    seen = {}

    def fake_uvicorn_run(app_path, factory, host, port):
        seen["pid_while_running"] = main_mod.PID_FILE.read_text().strip()

    monkeypatch.setitem(
        sys.modules, "uvicorn",
        type("FakeUvicorn", (), {"run": staticmethod(fake_uvicorn_run)}),
    )
    main_mod.start(port=4200, foreground=True, dev=False, host="127.0.0.1")
    assert seen["pid_while_running"] == str(os.getpid())
    assert not main_mod.PID_FILE.exists()
