"""Security finding #1a/b: the control app has no authentication of its
own, so the bind address IS the access control -- default to loopback
everywhere `odin start` can launch uvicorn, and warn loudly the moment a
caller opts into anything wider."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest
import respx
import typer

from odin import __main__ as main_mod, util
from odin.cli import doctor as doctor_mod
from odin.gateway.stores import SynthStores
from odin.reconcile.reconciler import LoopHealth


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


class FakeProc:
    """A launched uvicorn, as `_await_serving` sees it: a pid and a `poll()`
    that reports whether it is still alive."""

    def __init__(self, pid: int = 12345, exit_code: int | None = None):
        self.pid = pid
        self.returncode = exit_code

    def poll(self):
        return self.returncode


def _skip_readiness(monkeypatch):
    """For the tests that are about the ARGV, not about waiting."""
    monkeypatch.setattr(main_mod, "_await_serving", lambda *a, **kw: None)


def test_start_background_passes_host_through_to_the_subprocess_argv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    _skip_readiness(monkeypatch)
    captured = {}

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
    _skip_readiness(monkeypatch)
    captured = {}

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
    # Dev mode's two prerequisites, stubbed rather than borrowed from whichever
    # machine runs the suite: this asserts an argv, and it used to pass or fail
    # on whether the developer happened to have bun on PATH and `bun install`
    # already run in ui/.
    monkeypatch.setattr(main_mod.shutil, "which", lambda tool: "/usr/local/bin/bun")
    monkeypatch.setattr(main_mod, "_require_ui_deps", lambda purpose: None)

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


# A port nothing serves (RFC 863 discard, unused on macOS). `odin status` asks
# a live server about its reconcilers over HTTP, and these tests are about the
# STORE-LOCK half -- pointing them at a URL that cannot answer keeps them from
# depending on whatever happens to be listening on 4200.
NO_SERVER_URL = "http://127.0.0.1:9"


async def test_status_is_honest_about_a_server_odin_did_not_start(tmp_path, monkeypatch, capsys, store_lock):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):  # UNKNOWN loops -- see the exit-2 tests below
        await main_mod.status(url=NO_SERVER_URL)
    out = capsys.readouterr().out
    assert "Odin is running" in out and str(os.getpid()) in out
    assert "store lock" in out and "no pidfile" in out


async def test_status_says_nothing_is_running_for_a_process_that_only_looks_like_one(
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
        with pytest.raises(typer.Exit):
            await main_mod.status()
        assert "Odin is not running" in capsys.readouterr().out
    finally:
        decoy.kill()
        decoy.wait()


async def test_status_exits_nonzero_when_odin_is_not_running(tmp_path, monkeypatch, capsys):
    """v0.7.3 printed "Odin is not running." and exited 0, so `odin status &&
    odin apply` applied against a server that wasn't there. The sentence and
    the code have to say the same thing."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit) as exit_info:
        await main_mod.status()
    assert exit_info.value.exit_code == 1
    assert "Odin is not running" in capsys.readouterr().out


async def test_status_exits_zero_only_when_every_loop_is_confirmed_ticking(
    tmp_path, monkeypatch, capsys, store_lock
):
    """0 is this command's documented "running, AND every env's reconciler is
    ticking", so it takes a real `/health` answer to earn it -- see
    `test_status_exits_two_when_convergence_is_unknown` for what the store lock
    alone buys."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_reconciler_health", lambda url: [
        LoopHealth(env="default", ticking=True, ticks=7).model_dump(),
    ])
    await main_mod.status(url=NO_SERVER_URL)  # no typer.Exit at all == exit 0
    assert "Odin is running" in capsys.readouterr().out


async def test_status_reports_the_pidfile_path_as_managed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text(str(os.getpid()))
    with pytest.raises(typer.Exit):  # loops UNKNOWN at this URL
        await main_mod.status(url=NO_SERVER_URL)
    assert f"Odin is running (pid {os.getpid()}, pidfile)" in capsys.readouterr().out


async def test_status_calls_reconciler_health_unknown_when_the_server_does_not_answer(
    tmp_path, monkeypatch, capsys, store_lock
):
    """The store lock proves odin is UP; it proves nothing about the loops
    inside it. Not being able to ask must read as UNKNOWN -- never as healthy,
    and never as a failure invented from a URL guess (the default is
    localhost:4200, so a server on another port would otherwise fail a gate
    that has nothing wrong with it)."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):
        await main_mod.status(url=NO_SERVER_URL)
    captured = capsys.readouterr()
    assert "Odin is running" in captured.out
    assert "UNKNOWN" in captured.err and "ODIN_URL" in captured.err


async def test_status_exits_two_when_convergence_is_unknown(tmp_path, monkeypatch, capsys, store_lock):
    """Field test 6 F1. This branch used to exit 0 -- the code that says
    "running, AND every env's reconciler is ticking" -- one line after printing
    that the second half is UNKNOWN, so a monitoring script gating on `odin
    status` read UNKNOWN as healthy. It is reachable by a plain `odin status`
    whenever the server is on a non-default port, which this command's own
    message anticipates.

    2, not 1: odin has NOT observed a stopped loop, and reporting one it never
    saw would be its own false claim. 2 is also what the README's contract
    already assigns to an unreachable server, which is exactly what happened
    here."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit) as exit_info:
        await main_mod.status(url=NO_SERVER_URL)
    assert exit_info.value.exit_code == 2
    captured = capsys.readouterr()
    assert "Odin is running" in captured.out  # the half odin DID verify
    assert "UNKNOWN" in captured.err


async def test_status_unknown_is_a_different_code_from_a_down_loop(tmp_path, monkeypatch, store_lock):
    """The distinction the fix exists to make: "I could not tell" and "a loop is
    down" are different answers and must not share a code. Mutation guard -- 2
    collapsing back to 1 would make UNKNOWN indistinguishable from an observed
    outage, and back to 0 would restore the finding."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_reconciler_health", lambda url: [
        LoopHealth(env="default", ticking=False, verdict="its task was CANCELLED").model_dump(),
    ])
    with pytest.raises(typer.Exit) as down:
        await main_mod.status(url=NO_SERVER_URL)

    monkeypatch.setattr(main_mod, "_reconciler_health", lambda url: "did not answer")
    with pytest.raises(typer.Exit) as unknown:
        await main_mod.status(url=NO_SERVER_URL)

    assert (down.value.exit_code, unknown.value.exit_code) == (1, 2)


async def test_status_names_a_malformed_url_rather_than_claiming_no_answer(
    tmp_path, monkeypatch, capsys, store_lock
):
    """F9's `odin status` half. `httpx.InvalidURL` is not an `httpx.HTTPError`,
    so a non-numeric port used to escape this command's own except clause as a
    traceback; and a schemeless value is caught but "did not answer <url>/health"
    is the wrong diagnosis, because odin never made a request at all."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit) as exit_info:
        await main_mod.status(url="localhost:4720")
    assert exit_info.value.exit_code == 2
    err = capsys.readouterr().err
    assert "'localhost:4720' is not a usable odin URL" in err
    assert "missing an 'http://' or 'https://' protocol" in err
    assert "UNKNOWN" in err

    with pytest.raises(typer.Exit) as bad_port:
        await main_mod.status(url="http://localhost:notaport")
    assert bad_port.value.exit_code == 2
    assert "Invalid port" in capsys.readouterr().err


async def test_status_exits_nonzero_and_names_the_env_when_a_reconciler_is_down(
    tmp_path, monkeypatch, capsys, store_lock
):
    """A live server whose loop died is the same false green as a server that
    isn't there: `odin status && odin apply` must not apply into an env nothing
    converges."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_reconciler_health", lambda url: [
        LoopHealth(env="prod", ticking=True, ticks=40).model_dump(),
        LoopHealth(env="default", ticking=False, verdict="... its task was CANCELLED ...").model_dump(),
    ])
    with pytest.raises(typer.Exit) as exit_info:
        await main_mod.status(url=NO_SERVER_URL)
    assert exit_info.value.exit_code == 1
    assert "RECONCILER DOWN" in capsys.readouterr().err


async def test_status_reports_the_converging_reconcilers_by_name(tmp_path, monkeypatch, capsys, store_lock):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_reconciler_health", lambda url: [
        LoopHealth(env="default", ticking=True, ticks=40).model_dump(),
        LoopHealth(env="prod", ticking=True, ticks=12).model_dump(),
    ])
    await main_mod.status(url=NO_SERVER_URL)  # exit 0
    assert "2 reconciler(s) converging: default, prod" in capsys.readouterr().out


def test_reconciler_health_reads_the_real_health_body(monkeypatch):
    """The parse, against the shape `GET /health` really serves (built here by
    the same `LoopHealth` the route serializes). The whole round trip -- real
    server, real route, real `odin status` binary -- is proven in the e2e."""
    body = {
        "ok": True, "gateway": {"port": 4599},
        "reconcilers": [LoopHealth(env="default", ticking=True, ticks=3).model_dump()],
    }
    with respx.mock:
        respx.get("http://odin.test/health").mock(return_value=httpx.Response(200, json=body))
        assert main_mod._reconciler_health("http://odin.test/") == body["reconcilers"]


def test_reconciler_health_is_unknown_rather_than_empty_when_the_field_is_missing():
    """An answer with no `reconcilers` key says nothing about the loops behind
    it -- reading that as "none are down" is exactly the inference this whole
    change exists to remove. Unknown is now the REASON rather than a bare None,
    so the caller cannot guess at one (F9)."""
    with respx.mock:
        respx.get("http://odin.test/health").mock(return_value=httpx.Response(200, json={"ok": True}))
        unknown = main_mod._reconciler_health("http://odin.test")
    assert isinstance(unknown, str)
    assert "no `reconcilers` field" in unknown


async def test_status_cleans_a_stale_pidfile_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text("999999")
    with pytest.raises(typer.Exit):
        await main_mod.status()
    assert "not running (cleaned up a stale PID file)" in capsys.readouterr().out
    assert not main_mod.PID_FILE.exists()


async def test_stop_signals_a_server_odin_did_not_start(tmp_path, monkeypatch, capsys):
    """The SIGTERM lands, and the lock comes free the way a real server's
    does -- by the process the signal reached going away."""
    monkeypatch.chdir(tmp_path)
    lock = util.hold_store_lock(tmp_path / ".odin")
    killed = []

    def exits_on_sigterm(pid, sig):
        killed.append((pid, sig))
        lock.release()  # what a real server's death does to its store lock

    monkeypatch.setattr(main_mod.os, "kill", exits_on_sigterm)
    await main_mod.stop()
    assert killed == [(os.getpid(), main_mod.signal.SIGTERM)]
    assert "Stopped." in capsys.readouterr().out


async def test_stop_says_stopped_only_once_the_store_is_really_free(tmp_path, monkeypatch, capsys):
    """FIELD TEST 5. `odin stop` printed `Stopped.` and returned rc=0 in 0.17s
    while the server lived another 0.91s -- so `odin stop && odin clean --all`
    was REFUSED in two of three back-to-back trials, by the very guard that
    tells users to run `odin stop` first. SIGTERM is a request; this command's
    own --help promises an end state.

    The wait is on the real signal, never a sleep: the server here releases the
    store lock late, and `stop` must not return before it does."""
    monkeypatch.chdir(tmp_path)
    lock = util.hold_store_lock(tmp_path / ".odin")
    linger = 0.6
    released_at = []

    def exits_slowly(pid, sig):
        def _exit() -> None:
            released_at.append(time.monotonic())
            lock.release()
        threading.Timer(linger, _exit).start()

    monkeypatch.setattr(main_mod.os, "kill", exits_slowly)
    started = time.monotonic()
    await main_mod.stop()  # no typer.Exit == exit 0, and it is EARNED
    returned_at = time.monotonic()
    assert "Stopped." in capsys.readouterr().out
    assert returned_at - started >= linger  # it waited
    assert returned_at >= released_at[0]  # ...for THIS, not for a clock
    assert util.live_server(main_mod.ODIN_DIR) is None  # and the store really is free


async def test_stop_that_never_comes_down_says_so_and_exits_one(tmp_path, monkeypatch, capsys):
    """The honest other half: a server that ignores SIGTERM. The contract is
    "0 once odin is down, 1 if it is still up", so a wait that runs out is a
    failure with the reason named -- and the pidfile SURVIVES it, because it
    is the evidence the next `odin status`/`odin stop` needs."""
    monkeypatch.chdir(tmp_path)
    lock = util.hold_store_lock(tmp_path / ".odin")
    main_mod.PID_FILE.write_text(str(os.getpid()))
    monkeypatch.setattr(main_mod, "SHUTDOWN_GRACE", 0.5)  # a wedged server is not worth 20s here
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: None)  # SIGTERM ignored
    try:
        with pytest.raises(typer.Exit) as exit_info:
            await main_mod.stop()
    finally:
        lock.release()
    assert exit_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Stopped." not in captured.out
    assert "did NOT exit within" in captured.err and "clean --all" in captured.err
    assert main_mod.PID_FILE.exists()


def test_stop_waits_the_full_documented_grace(tmp_path, monkeypatch):
    """The bound is the store-wide one (`util.SHUTDOWN_GRACE`, what `odin
    import` already waits), not a number invented here: a real server's
    lifespan stops every reconciler and the gateway thread before it releases
    the lock, which takes seconds."""
    assert main_mod.SHUTDOWN_GRACE is util.SHUTDOWN_GRACE
    assert util.SHUTDOWN_GRACE >= 10


async def test_stop_never_signals_a_pid_it_cannot_vouch_for(tmp_path, monkeypatch, capsys):
    """The lock is held (so a server IS up) but the pid stamp is unreadable --
    the one window where odin knows something is there and not what. It says
    that instead of signalling a guess: v0.7.1 guessed, and named a shell."""
    monkeypatch.chdir(tmp_path)
    lock = util.hold_store_lock(tmp_path / ".odin")
    (tmp_path / ".odin" / util.STORE_LOCK_NAME).write_text("")
    killed = []
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    try:
        # ...and exits 1, because odin is still up and this command just said so.
        with pytest.raises(typer.Exit) as exit_info:
            await main_mod.stop()
    finally:
        lock.release()
    assert exit_info.value.exit_code == 1
    out = capsys.readouterr().out
    assert killed == [] and "cannot identify the process" in out and "lsof" in out


async def test_stop_with_nothing_running_is_a_success(tmp_path, monkeypatch, capsys):
    """The deliberate asymmetry with `odin status`: `stop` asks for an end
    state, and "odin is down" is exactly the end state it asked for."""
    monkeypatch.chdir(tmp_path)
    await main_mod.stop()  # no typer.Exit == exit 0
    assert "Odin is not running" in capsys.readouterr().out


def test_start_on_an_already_running_odin_says_the_flags_were_not_applied(
    tmp_path, monkeypatch, capsys
):
    """Still exit 0 -- `odin start && odin apply` must stay idempotent -- but
    a second `start` cannot honour a different --port/--host, and the message
    is the only place that can say so."""
    monkeypatch.chdir(tmp_path)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text(str(os.getpid()))
    monkeypatch.setattr(main_mod, "_build_ui", lambda: pytest.fail("must not build or launch"))
    main_mod.start(port=9999, foreground=False, dev=False, host=main_mod.DEFAULT_HOST)
    out = capsys.readouterr().out
    assert "already running" in out and "NOT applied" in out


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
    _skip_readiness(monkeypatch)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text("999999")
    started = []

    # `pid_alive` really does shell out to `kill -0`, and that is the whole
    # point of this test -- so the real Popen keeps serving it.
    real_popen = main_mod.subprocess.Popen

    def fake_popen(argv, **kwargs):
        if argv[:2] == ["kill", "-0"]:
            return real_popen(argv, **kwargs)
        started.append(argv)
        return FakeProc(pid=4242)

    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)
    main_mod.start(port=4200, foreground=False, dev=False, host="127.0.0.1")
    assert started, "a stale pidfile must not block a start"
    assert main_mod.PID_FILE.read_text() == "4242"


# --- v0.7.5: a missing build tool is a finding, not a traceback -------------
# `odin start` from a clone shells out to `bun`, and without it printed a
# 40-line rich traceback ending in `FileNotFoundError: [Errno 2] No such file
# or directory: 'bun'` -- on the first command a newcomer runs, the same shape
# as the v0.7.3 blocker where `odin doctor` died on a missing `docker`.


@pytest.fixture
def clone_without_bun(tmp_path, monkeypatch):
    """A clone (no bundled `_ui`), nothing built yet, and no bun on PATH."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "BUNDLED_UI", tmp_path / "_ui")
    monkeypatch.setattr(main_mod, "UI_DIR", tmp_path / "ui")
    monkeypatch.setattr(main_mod.shutil, "which", lambda tool: None)
    monkeypatch.setattr(
        main_mod.subprocess, "run",
        lambda *a, **kw: pytest.fail("must not shell out to a tool that isn't installed"))
    return tmp_path


def test_start_without_bun_names_the_fix_instead_of_raising(clone_without_bun, capsys):
    with pytest.raises(typer.Exit) as exc:
        main_mod.start(port=4200, foreground=False, dev=False, host="127.0.0.1")

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "`bun` is not installed" in err            # a sentence...
    assert f"fix: {doctor_mod.BUN_INSTALL}" in err    # ...and the exact command
    assert "bun.sh/install" in err


def test_dev_mode_without_bun_refuses_before_it_starts_anything(clone_without_bun, capsys):
    """Checked before the pidfile and the backend exist, so a machine without
    bun is refused rather than half-started."""
    with pytest.raises(typer.Exit) as exc:
        main_mod._start_dev(port=4200, host="127.0.0.1")

    assert exc.value.exit_code == 1
    assert "`bun` is not installed" in capsys.readouterr().err
    assert not main_mod.PID_FILE.exists()


def test_the_bun_fix_is_the_one_doctor_prints(clone_without_bun, capsys):
    """One remedy, one spelling: `odin doctor`'s bun row and `odin start`'s
    refusal both come from `doctor.BUN_INSTALL`."""
    row, = doctor_mod.run_checks(["bun"], lambda args, input=None: util.CommandResult(1, ""))
    with pytest.raises(typer.Exit):
        main_mod._require_bun("build the UI")
    assert row.fix and row.fix in capsys.readouterr().err


@pytest.fixture
def clone_with_bun_and_deps(tmp_path, monkeypatch):
    """A clone with bun on PATH AND `ui/node_modules` installed -- i.e. past
    both preconditions, so `_build_ui` really reaches the build."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "BUNDLED_UI", tmp_path / "_ui")
    monkeypatch.setattr(main_mod, "UI_DIR", tmp_path / "ui")
    monkeypatch.setattr(main_mod.shutil, "which", lambda tool: "/usr/local/bin/bun")
    vite = tmp_path / "ui" / "node_modules" / ".bin" / "vite"
    vite.parent.mkdir(parents=True)
    vite.touch()
    return tmp_path


def test_a_failed_ui_build_is_reported_not_raised(clone_with_bun_and_deps, monkeypatch, capsys):
    """bun IS installed and its build fails -- a different problem, a different
    sentence, and still no traceback."""
    monkeypatch.setattr(main_mod, "_run_in_ui", lambda args: main_mod.UiRun(True, 1))

    with pytest.raises(typer.Exit) as exc:
        main_mod._build_ui()

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "`bun run build` failed (exit 1)" in err
    assert "read bun's output above" in err


def test_a_bun_that_cannot_be_executed_is_reported_not_raised(
    clone_with_bun_and_deps, monkeypatch, capsys
):
    """The backstop: PATH says yes, exec says no (a dangling shim, a wrong-arch
    binary). The OSError is caught, so nothing here can raise -- and this is the
    ONE input that may produce the "could not be run" sentence."""
    def exec_fails(args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(main_mod.subprocess, "run", exec_fails)

    with pytest.raises(typer.Exit) as exc:
        main_mod._build_ui()

    assert exc.value.exit_code == 1
    assert "could not be run" in capsys.readouterr().err


# --- the `vite: command not found` misdiagnosis ------------------------------
# Reproduced on a fresh clone (no `ui/node_modules`), bun 1.2.15 on PATH and
# perfectly runnable:
#     $ bun run build
#     $ vite build
#     /bin/bash: vite: command not found
#     error: script "build" exited with code 127
# `odin start` answered "Cannot build the UI: `bun` is on PATH but could not be
# run." and prescribed `curl -fsSL https://bun.sh/install | bash` -- advice that
# fixes nothing, because bun was never the problem. The missing step was
# `cd ui && bun install`, and odin never said the word.


@pytest.fixture
def clone_without_ui_deps(clone_without_bun, monkeypatch):
    """The bug's own machine: a clone with bun installed and runnable, and
    `ui/node_modules` never installed.

    Built on `clone_without_bun`, so its "must not shell out" guard still
    stands -- which doubles as the proof that this diagnosis is a FILESYSTEM
    observation and not a reading of an exit code. odin has to know before it
    runs anything.
    """
    monkeypatch.setattr(main_mod.shutil, "which", lambda tool: "/usr/local/bin/bun")
    return clone_without_bun


def test_a_clone_that_never_ran_bun_install_is_told_to_bun_install(
    clone_without_ui_deps, capsys
):
    """The bug's own case, end to end. bun on PATH, `ui/node_modules` absent."""
    with pytest.raises(typer.Exit) as exc:
        main_mod._build_ui()

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert f"fix: {main_mod.UI_DEPS_INSTALL}" in err     # the command that works
    assert "cd ui && bun install" in err
    assert "dependencies are not installed" in err
    # ...and NOT one word of the advice that sent the user in a circle:
    assert doctor_mod.BUN_INSTALL not in err
    assert "bun.sh/install" not in err
    assert "could not be run" not in err


def test_a_child_exit_127_is_never_reported_as_a_bun_that_cannot_run(
    clone_with_bun_and_deps, monkeypatch, capsys
):
    """The SHAPE fix, independent of the precondition above: 127 out of a bun
    that launched fine means the SCRIPT's tool was missing, never that bun was.
    Both used to arrive as the integer 127; only `UiRun.launched` can tell them
    apart, and it is what the message is keyed on now."""
    monkeypatch.setattr(main_mod, "_run_in_ui", lambda args: main_mod.UiRun(True, 127))

    with pytest.raises(typer.Exit) as exc:
        main_mod._build_ui()

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "`bun run build` failed (exit 127)" in err
    assert "could not be run" not in err
    assert doctor_mod.BUN_INSTALL not in err


def test_run_in_ui_reports_a_real_exit_127_as_launched(clone_with_bun_and_deps):
    """`_run_in_ui` against a REAL process that really exits 127 -- the number
    bun really returns for `vite: command not found`. `launched` must be True:
    the discriminator has to survive the actual value, not just a fabricated
    `UiRun`."""
    run = main_mod._run_in_ui([sys.executable, "-c", "raise SystemExit(127)"])
    assert (run.launched, run.code) == (True, 127)


def test_run_in_ui_reports_an_unexecutable_binary_as_not_launched(clone_with_bun_and_deps):
    """The other side, also against the real OS: a path that cannot be exec'd."""
    missing = clone_with_bun_and_deps / "no-such-bun"
    run = main_mod._run_in_ui([str(missing)])
    assert run.launched is False


def test_the_deps_probe_names_the_binary_ui_package_json_actually_runs():
    """Rule 1, applied to the guard's premise. `_require_ui_deps` looks for
    `node_modules/.bin/vite` because that is what `ui/package.json`'s own
    scripts invoke -- read from the real manifest, so swapping the UI's build
    tool breaks this test instead of silently making the check meaningless."""
    manifest = json.loads((main_mod.UI_DIR / "package.json").read_text())
    tools = {script.split()[0] for script in manifest["scripts"].values()}

    assert tools == {"vite"}, "the deps probe is pinned to vite; scripts changed"
    assert main_mod._vite_bin().parts[-3:] == ("node_modules", ".bin", "vite")


def test_dev_mode_without_ui_deps_refuses_before_it_starts_anything(
    clone_without_ui_deps, capsys
):
    """The sibling. `bun run dev` fails identically on a fresh clone (`vite
    --port "4311"` -> `vite: command not found`, exit 127), and dev mode never
    looked -- so odin wrote the pidfile, launched the backend, and left Vite
    dead in a relayed log line."""
    with pytest.raises(typer.Exit) as exc:
        main_mod._start_dev(port=4200, host="127.0.0.1")

    assert exc.value.exit_code == 1
    assert "cd ui && bun install" in capsys.readouterr().err
    assert not main_mod.PID_FILE.exists()


def test_a_bundled_ui_needs_no_bun_at_all(tmp_path, monkeypatch, capsys):
    """A released odin ships the UI prebuilt, so the bun check must not fire
    there -- the message says so and this is what makes that true."""
    monkeypatch.chdir(tmp_path)
    bundled = tmp_path / "_ui"
    bundled.mkdir()
    monkeypatch.setattr(main_mod, "BUNDLED_UI", bundled)
    monkeypatch.setattr(main_mod.shutil, "which", lambda tool: pytest.fail("must not ask for bun"))
    main_mod._build_ui()
    assert capsys.readouterr().err == ""


# --- v0.7.5: `odin start` returns only once odin is SERVING -----------------
# The twin of v0.7.4's `odin stop` fix, at the other end of the lifecycle.
# `subprocess.Popen` returns when the FORK succeeds, so `start` announced a
# running server ~0.5-4s before uvicorn had bound anything: measured on this
# machine, `start` returned at 0.204s while GET /health first answered at
# 0.714s (2.65s/2.695s cold), and `odin start && odin world` failed 3 of 3
# with "Could not reach odin server -- is it running? Try `odin start`". After
# the fix, 3 of 3 passed and /health answered the instant `start` returned.


def test_start_does_not_return_until_health_actually_answers(tmp_path, monkeypatch, capsys):
    """The load-bearing one: `start` must poll a REAL round trip, not a fork."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    monkeypatch.setattr(main_mod, "_READY_POLL", 0.0)
    monkeypatch.setattr(main_mod.subprocess, "Popen", lambda argv, **kw: FakeProc())

    probes = []

    def fake_serving(address):
        probes.append(address)
        return len(probes) > 3  # the server takes a few polls to come up

    monkeypatch.setattr(main_mod, "_serving", fake_serving)

    main_mod.start(port=4200, foreground=False, dev=False, host="127.0.0.1")

    assert len(probes) == 4, "start returned before /health answered"
    assert probes == ["http://127.0.0.1:4200"] * 4
    out = capsys.readouterr().out
    assert "up and serving at http://127.0.0.1:4200" in out


def test_start_fails_when_the_server_dies_before_it_serves(tmp_path, monkeypatch, capsys):
    """A port already in use is the everyday case (verified for real: uvicorn
    exits 1 with "address already in use"). Exit 1, uvicorn's own words, and no
    pidfile left pointing at a corpse."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    monkeypatch.setattr(main_mod, "_serving", lambda address: False)
    monkeypatch.setattr(main_mod.subprocess, "Popen", lambda argv, **kw: FakeProc(exit_code=1))

    with pytest.raises(typer.Exit) as exc:
        main_mod.start(port=4200, foreground=False, dev=False, host="127.0.0.1")

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "did not come up" in err
    assert "exited with code 1" in err
    assert "server.log" in err                      # where to look
    assert "run `odin start` again" in err          # what to do
    assert not main_mod.PID_FILE.exists()           # the pid in it is dead


def test_start_is_honest_when_the_wait_runs_out(tmp_path, monkeypatch, capsys):
    """A bounded wait that expires reports the bound and leaves the process
    alone -- it is still running and may yet come up, and saying otherwise
    would be the same invented success in the opposite direction."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_build_ui", lambda: None)
    monkeypatch.setattr(main_mod, "_serving", lambda address: False)
    monkeypatch.setattr(main_mod, "READY_TIMEOUT", 0.05)
    monkeypatch.setattr(main_mod, "_READY_POLL", 0.0)
    monkeypatch.setattr(main_mod.subprocess, "Popen", lambda argv, **kw: FakeProc(pid=777))

    with pytest.raises(typer.Exit) as exc:
        main_mod.start(port=4200, foreground=False, dev=False, host="127.0.0.1")

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "did not answer http://127.0.0.1:4200/health within 0s" in err
    assert "still running (pid 777)" in err
    assert "`odin stop`" in err
    assert main_mod.PID_FILE.read_text() == "777"   # still stoppable


def test_start_on_an_already_running_odin_never_waits(tmp_path, monkeypatch, capsys):
    """The exit-code decision `_already_running` documents survives the new
    wait: nothing was launched, so there is nothing to wait FOR, and a script's
    `odin start && odin apply` must not stall on a `--port` odin never bound."""
    monkeypatch.chdir(tmp_path)
    main_mod.ODIN_DIR.mkdir()
    main_mod.PID_FILE.write_text(str(os.getpid()))
    monkeypatch.setattr(
        main_mod, "_await_serving", lambda *a, **kw: pytest.fail("must not wait"))

    main_mod.start(port=9999, foreground=False, dev=False, host=main_mod.DEFAULT_HOST)

    assert "already running" in capsys.readouterr().out


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:4200"),
    ("localhost", "http://localhost:4200"),
    ("192.168.1.5", "http://192.168.1.5:4200"),
    # A wildcard bind accepts everywhere but is not itself a destination.
    ("0.0.0.0", "http://127.0.0.1:4200"),
    ("::", "http://[::1]:4200"),
    ("::1", "http://[::1]:4200"),
])
def test_probe_address_is_somewhere_that_bind_really_answers(host, expected):
    assert main_mod._probe_address(host, 4200) == expected


def test_serving_is_false_while_nothing_listens():
    """A closed port is an ordinary state of a server still starting, so the
    probe answers rather than raises -- the same call `_await_serving` makes
    hundreds of times per start."""
    assert main_mod._serving("http://127.0.0.1:1") is False


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
    stores.ec2compute.set("sneak", "instance:i-abc", {"instance_id": "i-abc", "state_name": "running"})
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
