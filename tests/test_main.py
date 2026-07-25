"""Security finding #1a/b: the control app has no authentication of its
own, so the bind address IS the access control -- default to loopback
everywhere `odin start` can launch uvicorn, and warn loudly the moment a
caller opts into anything wider."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
import typer

from odin import __main__ as main_mod, util
from odin.gateway.stores import SynthStores


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


# --- `odin status`/`stop` honesty for BOTH launch paths (field tests B5, 3) ---
# v0.7.0 read `.odin/pid` and nothing else, so a server started the way the
# README documents (`uvicorn odin.server:create_app --factory`) made `odin
# status` print "Odin is not running" while `/health` answered ok -- and that
# same blind spot let `odin import` restore into the live store. v0.7.1 fixed
# that by matching `odin.server:create_app` against every process's command
# line, and then called an engineer's own shell a live server. So liveness is
# now the store lock a real server holds (util.hold_store_lock), and these tests
# hold a real one rather than faking any seam.


@pytest.fixture
def store_lock(tmp_path):
    """What a server started outside `odin start` leaves behind for odin to
    find: an exclusive lock on this store, held by a live process."""
    lock = util.hold_store_lock(tmp_path / ".odin")
    yield lock
    lock.release()


def test_status_is_honest_about_a_server_odin_did_not_start(tmp_path, monkeypatch, capsys, store_lock):
    monkeypatch.chdir(tmp_path)
    main_mod.status()
    out = capsys.readouterr().out
    assert "Odin is running" in out and str(os.getpid()) in out
    assert "store lock" in out and "no pidfile" in out


def test_status_says_nothing_is_running_for_a_process_that_only_looks_like_one(
    tmp_path, monkeypatch, capsys
):
    """Field test 3: with no server anywhere, `odin status` must say so --
    even while a process (an ops wrapper, or the operator's own shell) has
    `odin.server:create_app` sitting in its argv."""
    monkeypatch.chdir(tmp_path)
    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         "uvicorn odin.server:create_app --factory --port 4510"],
        cwd=tmp_path,
    )
    try:
        main_mod.status()
        assert "Odin is not running" in capsys.readouterr().out
    finally:
        decoy.kill()
        decoy.wait()


def test_status_reports_the_pidfile_path_as_managed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text(str(os.getpid()))
    main_mod.status()
    assert f"Odin is running (pid {os.getpid()}, pidfile)" in capsys.readouterr().out


def test_status_cleans_a_stale_pidfile_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text("999999")
    main_mod.status()
    assert "not running (cleaned up a stale PID file)" in capsys.readouterr().out
    assert not main_mod.PID_FILE.exists()


def test_stop_signals_a_server_odin_did_not_start(tmp_path, monkeypatch, capsys, store_lock):
    monkeypatch.chdir(tmp_path)
    killed = []
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    main_mod.stop()
    assert killed == [(os.getpid(), main_mod.signal.SIGTERM)]
    assert "Stopped." in capsys.readouterr().out


def test_stop_never_signals_a_pid_it_cannot_vouch_for(tmp_path, monkeypatch, capsys):
    """The lock is held (so a server IS up) but the pid stamp is unreadable --
    the one window where odin knows something is there and not what. It says
    that instead of signalling a guess: v0.7.1 guessed, and named a shell."""
    monkeypatch.chdir(tmp_path)
    lock = util.hold_store_lock(tmp_path / ".odin")
    (tmp_path / ".odin" / util.STORE_LOCK_NAME).write_text("")
    killed = []
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    try:
        main_mod.stop()
    finally:
        lock.release()
    out = capsys.readouterr().out
    assert killed == [] and "cannot identify the process" in out and "lsof" in out


def test_version_flag_prints_the_real_version(capsys):
    """LOW-16: there was no `odin --version` at all, which is exactly why a
    version stamp could go two releases stale without anyone noticing."""
    with pytest.raises(typer.Exit):
        main_mod._print_version(True)
    assert capsys.readouterr().out.strip() == f"odin {util.odin_version()}"


def test_version_callback_is_a_no_op_without_the_flag(capsys):
    main_mod._print_version(False)
    assert capsys.readouterr().out == ""


# --- v0.7.4: the two lock-lifecycle holes v0.7.3's own author flagged -------


def test_start_refuses_when_a_lock_holding_server_left_no_pidfile(tmp_path, monkeypatch, capsys):
    """The hole: `odin start` read ONLY `.odin/pid`, which only `odin start`
    writes -- so against a server launched the way the README documents
    (`uvicorn odin.server:create_app --factory`) it started a SECOND one on the
    same store, two reconcilers driving the same envs' containers. `odin
    status`/`stop`/`import` all used the kernel lock already; now this does
    too."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    started = []
    monkeypatch.setattr(main_mod.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    lock = util.hold_store_lock(tmp_path / ".odin")
    try:
        main_mod.start(port=4200, foreground=False, dev=False, host="127.0.0.1")
    finally:
        lock.release()
    out = capsys.readouterr().out
    assert started == []                       # no second server
    assert "Odin is already running" in out
    assert "store lock" in out                 # ...and it names the evidence, not a guess
    assert not main_mod.PID_FILE.exists()


def test_dev_start_refuses_against_the_same_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr(main_mod.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    lock = util.hold_store_lock(tmp_path / ".odin")
    try:
        main_mod._start_dev(port=4200, host="127.0.0.1")
    finally:
        lock.release()
    assert started == []
    assert "Odin is already running" in capsys.readouterr().out


def test_start_still_clears_a_stale_pidfile_and_proceeds(tmp_path, monkeypatch):
    """The other direction, unregressed: a dead pid must not block a start."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text("999999")
    started = []

    class FakeProc:
        pid = 4242

    # `pid_alive` really does shell out to `kill -0`, and that is the whole
    # point of this test -- so the real Popen keeps serving it.
    real_popen = main_mod.subprocess.Popen

    def fake_popen(argv, **kwargs):
        if argv[:2] == ["kill", "-0"]:
            return real_popen(argv, **kwargs)
        started.append(argv)
        return FakeProc()

    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)
    main_mod.start(port=4200, foreground=False, dev=False, host="127.0.0.1")
    assert started, "a stale pidfile must not block a start"
    assert main_mod.PID_FILE.read_text() == "4242"


def test_clean_all_refuses_to_delete_the_store_a_live_server_holds(tmp_path, monkeypatch, capsys):
    """`--all` is `rm -rf .odin`, lock file included. See `clean`'s own comment
    for the three things that breaks; the load-bearing one is that the next
    server locks a NEW inode and succeeds, so two servers each believe they
    hold this store."""
    monkeypatch.chdir(tmp_path)
    lock = util.hold_store_lock(tmp_path / ".odin")
    (tmp_path / ".odin" / "keep.json").write_text("{}")
    try:
        with pytest.raises(typer.Exit) as exit_info:
            main_mod.clean(all=True)
    finally:
        lock.release()
    assert exit_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Refusing" in captured.out and "store lock" in captured.out
    assert "Stop it with" in captured.err
    # nothing was touched: the store, and the lock a live server is holding
    assert (tmp_path / ".odin" / "keep.json").exists()
    assert (tmp_path / ".odin" / util.STORE_LOCK_NAME).exists()


def test_clean_all_still_wipes_the_store_when_nothing_is_running(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".odin").mkdir()
    (tmp_path / ".odin" / "world.json").write_text("{}")
    main_mod.clean(all=True)
    assert "full reset" in capsys.readouterr().out
    assert not (tmp_path / ".odin" / "world.json").exists()


# --- ...and the store is the only record of what odin owns (field test 4) ---
#
# The engineer wiped a scratch store and then had to `docker rm` an
# `odin-aws-*-sneak` container BY HAND, BY EXACT NAME: no odin command could
# find it any more, because the thing that knew was what they deleted. A "full
# reset" that leaves real containers running is a leak with a friendly name.


def _fake_machine(monkeypatch, containers: list[str] = (), vms: list[str] = ()):
    """`docker ps` / `limactl list` without a machine -- the two shell-outs
    `clean --all`'s resource check makes, and nothing else."""
    def run(argv, input=None):
        if argv[0] == "docker":
            return util.CommandResult(0, "\n".join(containers) + "\n")
        return util.CommandResult(0, "\n".join(vms) + "\n")

    monkeypatch.setattr(main_mod, "run_command", run)


def test_clean_all_refuses_while_an_env_still_has_real_containers(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".odin" / "sneak").mkdir(parents=True)
    _fake_machine(monkeypatch, containers=["odin-aws-rustfs-sneak", "odin-aws-goaws-sneak"])

    with pytest.raises(typer.Exit) as exit_info:
        main_mod.clean(all=True)

    assert exit_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "only record of them" in captured.out
    assert "odin-aws-rustfs-sneak" in captured.out and "odin-aws-goaws-sneak" in captured.out
    assert "`odin destroy --env sneak`" in captured.err   # the supported command, while it still works
    assert (tmp_path / ".odin" / "sneak").exists()        # nothing deleted


def test_clean_all_names_the_envs_real_vms_too(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".odin" / "sneak").mkdir(parents=True)
    stores = SynthStores(tmp_path / ".odin")
    stores.ec2compute.set("sneak", "instance:i-abc", {"instance_id": "i-abc"})
    # A VM this store claims, one it does not, and the user's own machine.
    _fake_machine(monkeypatch, vms=["odin-ec2-sneak-i-abc", "odin-ec2-other-i-zzz", "veronica"])

    with pytest.raises(typer.Exit):
        main_mod.clean(all=True)

    out = capsys.readouterr().out
    assert "odin-ec2-sneak-i-abc" in out
    # Exact names from this store's own records, never a prefix sweep: a VM
    # belonging to another env -- or to the user -- is never even a candidate.
    assert "odin-ec2-other-i-zzz" not in out
    assert "veronica" not in out


def test_clean_all_proceeds_when_the_envs_own_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".odin" / "spent").mkdir(parents=True)
    _fake_machine(monkeypatch)
    main_mod.clean(all=True)
    assert "full reset" in capsys.readouterr().out
    assert not (tmp_path / ".odin" / "spent").exists()


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
