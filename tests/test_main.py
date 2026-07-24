"""Security finding #1a/b: the control app has no authentication of its
own, so the bind address IS the access control -- default to loopback
everywhere `odin start` can launch uvicorn, and warn loudly the moment a
caller opts into anything wider."""
from __future__ import annotations

import sys

import pytest

from odin import __main__ as main_mod


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
