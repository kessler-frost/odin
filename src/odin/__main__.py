"""Odin CLI — start and stop the server."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer

from odin.cli import commands as _commands  # noqa: F401  (registers the control-surface commands)
from odin.cli import doctor as _doctor  # noqa: F401  (registers `odin doctor`)
from odin.cli import http
from odin.cli.app import app
from odin.cli.doctor import BUN_INSTALL
from odin.compute.instances import vm_name
from odin.gateway.stores import SynthStores
from odin.util import (
    COMMAND_NOT_FOUND,
    SHUTDOWN_GRACE,
    await_server_exit,
    live_server,
    odin_version,
    pid_alive,
    private_mkdir,
    run_command,
)

ODIN_DIR = Path(".odin")
PID_FILE = ODIN_DIR / "pid"
UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
# The prebuilt UI a released odin ships; present = no bun needed, ever.
BUNDLED_UI = Path(__file__).resolve().parent / "_ui"
# The other build prerequisite, next to `doctor.BUN_INSTALL`: bun being
# installed says nothing about the UI's OWN dependencies being installed, and
# odin needs both. Spelled `cd ui && ...` because the user is standing in the
# repo root when `odin start` tells them this.
UI_DEPS_INSTALL = "cd ui && bun install"
DEFAULT_PORT = 4200
BACKEND_DEV_PORT = 4201
DEFAULT_HOST = "127.0.0.1"
# Security finding #1a/b: odin has no authentication of its own -- every
# route from `/apply` down to `/tf/*` drives real `docker run`/Lima VMs with
# no login, no token, nothing. Binding to a LAN/0.0.0.0 address turns "my
# laptop" into "anyone on this network can run containers on my laptop", so
# loopback is the only default; a wider bind is opt-in and loud about it.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# How long `odin start` waits for the server it just launched to actually
# answer, and how often it asks. uvicorn accepts connections only AFTER its
# lifespan startup returns, and odin's lifespan does real work first: it starts
# the gateway listener, takes the store lock, reaps orphaned EC2 VMs and
# resumes a reconciler for every env already in the store. So the wait is
# proportional to what this store holds, and the ceiling is generous on purpose
# -- a store with several envs to resume is slow, not broken. Timing it out is
# reported, never assumed (see `_await_serving`).
READY_TIMEOUT = 120.0
_READY_POLL = 0.1
# Enough of the log to show the error uvicorn died on (a port already in use,
# an import error) without pasting a whole startup transcript into the terminal.
_LOG_TAIL_LINES = 12


def _print_version(value: bool) -> None:
    """`odin --version`, which the field test found missing entirely -- the one
    thing that would have made a stale version stamp noticeable (`odin export`
    was writing `odin_version 0.5.3` into manifests two releases later)."""
    if value:
        typer.echo(f"odin {odin_version()}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_print_version, is_eager=True,
        help="Show odin's version and exit.",
    ),
) -> None:
    """Odin — a local-first, AWS-compatible cloud on your own machine."""


def _warn_if_non_loopback(host: str) -> None:
    if host not in _LOOPBACK_HOSTS:
        typer.echo(
            f"WARNING: binding to {host} -- odin has no authentication. "
            "Anyone who can reach this port can run containers on this machine.",
            err=True,
        )


def _already_running() -> bool:
    """Whether a control app is live against `.odin` -- printed, and NOT an
    error.

    Two things had to survive here. The MECHANISM is the kernel `flock`
    (v0.7.2 gave the server one precisely so liveness could be OBSERVED
    rather than inferred): `odin start` used to read only `.odin/pid`, which
    ONLY `odin start` itself writes, so the one command that launches a
    second server was the one still blind to a server the README's own
    `uvicorn odin.server:create_app --factory` had started -- two reconcilers
    driving the same envs' containers. `live_server` asks the kernel, so this
    is now the same check everywhere.

    The EXIT CODE stays 0, the odd one out among the exit-code fixes:
    `odin start` asks for an end state (odin up) and that end state holds, so
    an idempotent `odin start && odin apply` in a script must keep working.
    What a second `start` cannot honour is a different `--port`/`--host`, so
    it says so out loud rather than letting the flags look applied.

    Returning False also means nothing is running, which makes a leftover
    pidfile stale by definition -- cleared here so the next start isn't
    blocked by a corpse.
    """
    server = live_server(ODIN_DIR)
    if server is None:
        _clear_stale_pidfile()
        return False
    typer.echo(
        f"Odin is already running ({server.detail}) — nothing was started, and any "
        "--port/--host you passed was NOT applied."
    )
    typer.echo(f"Stop it with {server.how_to_stop} first to restart it with new flags.")
    return True


def _probe_address(host: str, port: int) -> str:
    """The address to knock on for a server bound to `host`.

    A wildcard bind (`0.0.0.0`, `::`) accepts on every interface but is not
    itself a destination, so the probe goes to loopback -- which that server is
    listening on too. IPv6 literals get bracketed, as a URL requires.
    """
    probe = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    authority = f"[{probe}]" if ":" in probe else probe
    return f"http://{authority}:{port}"


def _serving(address: str) -> bool:
    """Whether odin is answering at `address` RIGHT NOW -- one real GET
    /health, so a True here is the same round trip `odin world` is about to
    make, not an inference from a pid or a lock. Nothing listening yet is an
    ordinary answer (False) while a server is still starting, never an error."""
    try:
        return httpx.get(f"{address}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def _await_serving(proc: subprocess.Popen, address: str, timeout: float) -> str | None:
    """Wait for `proc` to answer at `address`. None once it does; otherwise the
    reason it did not, in the caller's words.

    `odin stop` learned this in v0.7.4 -- it reported success ~0.9s before the
    process actually died, which broke the documented `odin stop && odin clean
    --all` remedy -- and `start` had the identical shape at the other end:
    `subprocess.Popen` returns as soon as the FORK succeeds, which says nothing
    about whether uvicorn ever bound the port. Measured on this machine with an
    empty store, `odin start` returned at 0.2s while /health first answered at
    0.7s (5.0s on a cold import cache), and `odin start && odin world` failed 3
    times out of 3 with "Could not reach odin server -- is it running? Try
    `odin start`", naming the command that had just claimed success.

    So this polls the same signal `await_server_exit` polls for the mirror
    question, differing only in which one is the real one: liveness is the
    kernel store lock, but SERVING is not -- lifespan takes that lock before it
    resumes any reconciler and therefore before uvicorn accepts a single
    connection (measured: lock at 0.661s, /health at 0.714s in the same run).
    An HTTP answer is the only evidence that the thing the next command needs
    is there.
    """
    deadline = time.monotonic() + timeout
    while not _serving(address):
        if proc.poll() is not None:
            return f"the server process exited with code {proc.returncode} before it served anything"
        if time.monotonic() >= deadline:
            return f"the server did not answer {address}/health within {timeout:.0f}s"
        time.sleep(_READY_POLL)
    return None


def _report_start_failure(reason: str, proc: subprocess.Popen, log_path: Path) -> typer.Exit:
    """Say what odin observed, show what the server said, and name the next
    command -- for both shapes of failure, because they need different ones."""
    typer.echo(f"Odin did not come up: {reason}.", err=True)
    tail = log_path.read_text().splitlines()[-_LOG_TAIL_LINES:] if log_path.is_file() else []
    typer.echo(f"Its output ({log_path}):", err=True)
    for line in tail or ["(the log is empty)"]:
        typer.echo(f"  {line}", err=True)
    still_up = proc.poll() is None
    if not still_up:  # the pid in the file is a corpse, so it is stale by definition
        PID_FILE.unlink(missing_ok=True)
    typer.echo(
        f"The process is still running (pid {proc.pid}) and may yet come up — "
        f"`odin status` to check again, `odin stop` to shut it down."
        if still_up else
        f"Nothing is running now; fix what {log_path} reports and run `odin start` again.",
        err=True,
    )
    return typer.Exit(1)


def _refuse(purpose: str, detail: str, fix: str, note: str) -> typer.Exit:
    """The one shape every "you're missing a build prerequisite" refusal takes:
    what odin could not do, what it OBSERVED, the exact command that fixes
    that, and a note. One shape so two prerequisites cannot drift into two
    different-looking failures."""
    typer.echo(f"Cannot {purpose}: {detail}.", err=True)
    typer.echo(f"fix: {fix}", err=True)
    typer.echo(note, err=True)
    return typer.Exit(1)


def _refuse_without_bun(purpose: str, detail: str) -> typer.Exit:
    """The sentence + exact command a machine without `bun` gets instead of a
    traceback.

    `odin start` is the first command anyone runs, and from a clone it shells
    out to `bun` to build the UI. Through v0.7.4 a machine without bun got a
    40-line rich traceback ending in `FileNotFoundError: [Errno 2] No such file
    or directory: 'bun'` (`subprocess.run(..., check=True)`) -- the same shape
    as the v0.7.3 blocker where `odin doctor` died on a missing `docker`. A
    tool the user simply has not installed yet is an ordinary state of a
    healthy machine: a FINDING, never a crash.

    The fix string is `doctor.BUN_INSTALL`, imported rather than retyped, so
    `odin start` and `odin doctor` cannot drift into two spellings of one
    remedy.
    """
    return _refuse(
        purpose, detail, BUN_INSTALL,
        "     then open a new shell so PATH picks it up. `odin doctor` re-checks it.\n"
        "     (A released odin ships the UI prebuilt and needs no bun; this is a clone.)",
    )


def _require_bun(purpose: str) -> None:
    """Ask PATH before shelling out. This is an OBSERVATION, not a guess from
    an exit code -- and it is what separates "you don't have bun" from "your
    build broke", two failures that need completely different sentences."""
    if shutil.which("bun") is None:
        raise _refuse_without_bun(purpose, "`bun` is not installed (nothing named `bun` on PATH)")


def _vite_bin() -> Path:
    """The binary `ui/package.json`'s own scripts run -- `vite` for `dev`,
    `vite build` for the bundle (pinned to the real manifest by
    `test_the_deps_probe_names_the_binary_ui_package_json_actually_runs`).
    `bun install` is what links it here, so its presence is the closest thing
    to "the UI's dependencies are installed" that can be OBSERVED, rather than
    inferred from an exit code a dozen other failures also produce."""
    return UI_DIR / "node_modules" / ".bin" / "vite"


def _refuse_without_ui_deps(purpose: str) -> typer.Exit:
    """A clone whose `ui/node_modules` was never installed -- the OTHER way the
    UI build cannot run, and the one odin used to misdiagnose.

    Measured on a fresh clone (bun 1.2.15, on PATH, `bun --version` fine):

        $ bun run build
        $ vite build
        /bin/bash: vite: command not found
        error: script "build" exited with code 127          # exit 127

    bun ran FINE; the script bun ran could not find its own tool. But
    `_run_in_ui` also translated a failed *exec of bun* into 127, so those two
    became one number and `_build_ui` read this one as "`bun` is on PATH but
    could not be run" and printed `curl -fsSL https://bun.sh/install | bash`.
    Reinstalling bun fixes nothing here: the user lands exactly where they
    started, with the real missing step (`bun install`) never named.

    So the two are now separated at the source -- `UiRun.launched` says whether
    bun ever started, and this precondition is observed BEFORE shelling out at
    all, off the filesystem.
    """
    return _refuse(
        purpose,
        f"the UI's dependencies are not installed (no {_vite_bin()})",
        UI_DEPS_INSTALL,
        "     bun is not the problem -- odin found it on PATH. What's missing is\n"
        "     the UI's node_modules, which is what `vite: command not found`\n"
        "     from a `bun run` actually means.\n"
        "     (A released odin ships the UI prebuilt and needs neither.)",
    )


def _require_ui_deps(purpose: str) -> None:
    """The sibling of `_require_bun`, and the same discipline: ask the real
    thing before shelling out, so "your deps aren't installed" can never be
    guessed from an exit code that a dozen other failures also produce."""
    if not _vite_bin().exists():
        raise _refuse_without_ui_deps(purpose)


@dataclass(frozen=True)
class UiRun:
    """What shelling out into `ui/` actually did.

    `launched` is the distinction the old `-> int` could not carry: it collapsed
    "bun could not be exec'd at all" (an `OSError`) into 127, which a bun that
    ran perfectly ALSO returns when its script's own tool is missing. One number
    for two opposite causes is how a missing `bun install` came out as "reinstall
    bun". `code` is meaningful only when `launched`.
    """
    launched: bool
    code: int


def _run_in_ui(args: list[str]) -> UiRun:
    """`args` in `ui/`. Output is NOT captured: bun's own error is the diagnosis
    and belongs on the user's terminal, not inside a traceback frame."""
    try:
        return UiRun(True, subprocess.run(args, cwd=str(UI_DIR)).returncode)
    except OSError:
        return UiRun(False, COMMAND_NOT_FOUND)


def _build_ui() -> None:
    """Make sure there is a UI to serve -- or say which of the four things went
    wrong and how to fix that ONE."""
    if BUNDLED_UI.exists():
        return  # UI ships bundled with the installed package
    if (UI_DIR / "dist").exists():
        typer.echo("UI already built (ui/dist exists). Run `bun run build` in ui/ to rebuild.")
        return
    _require_bun("build the UI")
    _require_ui_deps("build the UI")
    typer.echo("Building UI …")
    run = _run_in_ui(["bun", "run", "build"])
    if not run.launched:
        # PATH said yes and exec said no -- a dangling shim, a wrong-arch
        # binary. Keyed on the exec failure itself, never on 127: a bun that
        # launched fine returns 127 whenever its script's tool is missing.
        raise _refuse_without_bun("build the UI", "`bun` is on PATH but could not be run")
    if run.code != 0:
        typer.echo(f"Cannot build the UI: `bun run build` failed (exit {run.code}) in {UI_DIR}.",
                   err=True)
        typer.echo("fix: read bun's output above -- it is bun's own diagnosis. odin already "
                   "checked the two usual suspects: bun is on PATH, and the UI's "
                   "dependencies are installed.",
                   err=True)
        raise typer.Exit(1)


@app.command()
def start(
    port: int = typer.Option(DEFAULT_PORT, "-p", "--port", help="Port (default: 4200)"),
    foreground: bool = typer.Option(False, "-f", "--foreground", help="Run in foreground"),
    dev: bool = typer.Option(False, "-d", "--dev", help="Dev mode: Vite HMR + uvicorn reload"),
    host: str = typer.Option(
        DEFAULT_HOST, "--host",
        help="Bind address (default: 127.0.0.1 -- loopback only). "
             "odin has no authentication; only widen this if you know what you're doing.",
    ),
) -> None:
    """Start the Odin server."""
    _warn_if_non_loopback(host)

    if dev:
        _start_dev(port, host)
        return

    if _already_running():
        return

    _build_ui()
    typer.echo(f"Starting Odin on http://{host}:{port}")

    if foreground:
        import uvicorn
        # A foreground server is just as live as a backgrounded one, so it gets
        # the same pidfile: without it `odin status`/`odin stop` from a second
        # terminal, and `odin import`'s live-store refusal, had nothing cheap to
        # find. (`live_server` would still catch it by process scan, but only
        # because uvicorn.run reuses this process -- the pidfile is exact.)
        private_mkdir(ODIN_DIR)
        PID_FILE.write_text(str(os.getpid()))
        uvicorn.run("odin.server:create_app", factory=True, host=host, port=port)
        PID_FILE.unlink(missing_ok=True)
    else:
        private_mkdir(ODIN_DIR)
        log_path = ODIN_DIR / "server.log"
        log = log_path.open("w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "odin.server:create_app",
             "--factory", "--host", host, "--port", str(port)],
            stdout=log, stderr=log, start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        # Popen returned, which proves a fork -- not a server. Say what has
        # actually happened so far, then wait for the thing the next command in
        # `odin start && odin apply` needs.
        address = _probe_address(host, port)
        typer.echo(f"Launched uvicorn (pid {proc.pid}); waiting for it to answer {address}/health …")
        failure = _await_serving(proc, address, READY_TIMEOUT)
        if failure is not None:
            raise _report_start_failure(failure, proc, log_path)
        typer.echo(
            f"Odin is up and serving at {address} (pid {proc.pid}). "
            f"Logs: {log_path}. Use `odin stop` to shut down."
        )


async def _relay(stream: asyncio.StreamReader, log) -> None:
    """Pump one child's merged stdout+stderr to this terminal and to dev.log.

    v0.7.7: an asyncio task, not a daemon thread. `create_subprocess_exec`
    hands back a real `StreamReader`, so `async for line in stream` IS the
    whole loop -- there was never any need for a thread here, only for a
    non-blocking read. The loop ends by itself at EOF, i.e. when the child's
    pipe closes, which is what makes `await`ing these at shutdown a drain
    rather than a hang.
    """
    async for line in stream:
        text = line.decode(errors="replace")
        sys.stdout.write(text)
        sys.stdout.flush()
        log.write(text)
        log.flush()


async def _supervise_dev(port: int, host: str, log) -> None:
    """Run Vite + the auto-reloading backend until either exits or we're
    signalled, relaying both children's output the whole time."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    backend = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "odin.server:create_app",
        "--factory", "--host", host, "--port", str(BACKEND_DEV_PORT),
        "--reload", "--reload-dir", "src",
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    frontend = await asyncio.create_subprocess_exec(
        "bun", "run", "dev", "--port", str(port),
        cwd=str(UI_DIR), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    procs = (backend, frontend)

    # TASK LIFETIME. `asyncio` holds only a WEAK reference to a running task,
    # so a bare `create_task(...)` whose result nobody keeps can be garbage
    # collected mid-flight -- the one failure mode a daemon thread did not
    # have. Both references are held in these locals, and this coroutine is
    # awaited until shutdown, so its frame keeps them alive for exactly the
    # window they must run in. They are awaited again at the bottom, which is
    # also what drains the last lines out of a child that has already exited.
    relays = [
        asyncio.create_task(_relay(backend.stdout, log), name="odin-dev-relay-backend"),
        asyncio.create_task(_relay(frontend.stdout, log), name="odin-dev-relay-frontend"),
    ]
    waits = [
        asyncio.create_task(proc.wait(), name=f"odin-dev-wait-{n}")
        for n, proc in (("backend", backend), ("frontend", frontend))
    ]

    # Signals go through the loop rather than `signal.signal` + `signal.pause`:
    # a handler that only resolves a future can never race the loop's own
    # bookkeeping, and it lets the wait below treat "a child died" and "the
    # user hit ^C" as the same event.
    loop = asyncio.get_running_loop()
    stopping = loop.create_future()

    def _request_stop() -> None:
        if not stopping.done():
            stopping.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)
    try:
        await asyncio.wait([*waits, stopping], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)

    for proc in procs:
        if proc.returncode is None:
            proc.terminate()
    PID_FILE.unlink(missing_ok=True)
    stopping.cancel()
    for task in (*waits, *relays):
        await task


def _start_dev(port: int, host: str = DEFAULT_HOST) -> None:
    """Dev mode startup."""
    if _already_running():
        return
    # The other `bun` in this file: dev mode runs Vite itself rather than a
    # built bundle, and a raw `Popen(["bun", ...])` tracebacks identically.
    # Checked BEFORE the pidfile and the backend are created, so a machine
    # without bun is refused rather than half-started.
    #
    # The deps check is the SIBLING of the `_build_ui` bug, and it needs to be
    # here for the same reason: measured on a fresh clone, `bun run dev` fails
    # exactly like `bun run build` does --
    #     $ vite --port "4311"
    #     /bin/bash: vite: command not found
    #     error: script "dev" exited with code 127
    # -- except dev mode never even looked, so odin used to start the backend and
    # the pidfile and leave Vite dead in a relayed log line.
    purpose = "run dev mode (it serves the UI through Vite)"
    _require_bun(purpose)
    _require_ui_deps(purpose)

    typer.echo(f"Starting Odin dev mode on http://{host}:{port}")
    typer.echo(f"  Vite  → :{port}  (HMR)")
    typer.echo(f"  API   → :{BACKEND_DEV_PORT}  (auto-reload)")

    private_mkdir(ODIN_DIR)
    PID_FILE.write_text(str(os.getpid()))

    log_path = ODIN_DIR / "dev.log"
    # The command itself stays synchronous (Typer does not await coroutines --
    # see `cli/doctor.py`'s module docstring for the measurement); `asyncio.run`
    # here is the whole async boundary, and a CLI process owning its own loop is
    # exactly where that call belongs.
    with log_path.open("w") as log:
        asyncio.run(_supervise_dev(port, host, log))


def _clear_stale_pidfile() -> str:
    """Nothing is running, so a leftover pidfile is stale by definition."""
    stale = PID_FILE.exists()
    PID_FILE.unlink(missing_ok=True)
    return " (cleaned up a stale PID file)" if stale else ""


@app.command()
def stop() -> None:
    """Stop the Odin server. Exit 0 once odin is down, 1 if it is still up.

    Nothing running is exit 0 on purpose, unlike `odin status`: `stop` asks
    for an end state rather than a fact, and that end state holds. The one
    non-zero case is the one where it does not -- a server odin can see but
    cannot signal, or one that has not finished exiting.
    """
    server = live_server(ODIN_DIR)
    if server is None:
        typer.echo(f"Odin is not running{_clear_stale_pidfile()}.")
        return
    if server.pid is None:
        # Something holds the store lock but hasn't stamped a pid we can trust.
        # Say that, rather than signal a pid we guessed -- v0.7.1's guard told a
        # field engineer to kill their own shell, and no message is worth that.
        typer.echo(f"Odin is running ({server.detail}), but odin cannot identify the process.")
        typer.echo(f"Stop it by {server.how_to_stop}.")
        raise typer.Exit(1)  # odin is still up, and this command said so
    # A server the user launched themselves (no pidfile -- `uvicorn
    # odin.server:create_app`, the command the README documents) is still THIS
    # store's server, so `odin stop` stops it rather than claiming nothing is
    # running: same SIGTERM uvicorn's own Ctrl-C sends.
    typer.echo(f"Stopping Odin ({server.detail}) …")
    os.kill(server.pid, signal.SIGTERM)
    # SIGTERM is a REQUEST, and this command's own --help promises an end
    # state ("exit 0 once odin is down"). Field test 5 measured the gap:
    # `Stopped.` and rc=0 at 0.17s, the process alive for another 0.91s --
    # long enough that two of three `odin stop && odin clean --all` runs were
    # refused by the guard that points users at THIS command, because the
    # server still held the store lock. So wait for the signal every other
    # liveness question in odin uses (the kernel lock, plus the pid while the
    # pidfile is still there), never a sleep. The server's lifespan stops
    # every reconciler and the gateway listener before releasing the lock,
    # which is why the wait is generous. (v0.7.7: the gateway is a TASK on this
    # app's own loop, not a thread -- `gateway/app.py::serve_on_loop` -- so
    # what the lifespan waits on is that task ending, not a thread join. The
    # wait itself is unchanged; only what it is waiting for was ever a thread.)
    remaining = await_server_exit(ODIN_DIR, SHUTDOWN_GRACE)
    if remaining is not None:
        # Still up, so say so and exit 1 -- the pidfile stays, because it is
        # the evidence `odin status` and the next `odin stop` need.
        typer.echo(
            f"Odin did NOT exit within {SHUTDOWN_GRACE:.0f}s of SIGTERM ({remaining.detail}). "
            "It still holds the store, so `odin clean --all` and `odin import` will refuse.",
            err=True,
        )
        typer.echo(
            f"It may still be shutting down — check {ODIN_DIR / 'server.log'} and re-run "
            f"`odin stop`, or `kill -9 {remaining.pid}` if it is wedged.",
            err=True,
        )
        raise typer.Exit(1)
    PID_FILE.unlink(missing_ok=True)
    typer.echo("Stopped.")


# The exit code for "odin is up, but its reconcilers could not be asked" -- see
# `status`. Distinct from 0 (they ARE converging) and 1 (one is DOWN), and the
# same 2 the README's contract already assigns to "a usage error or an
# unreachable server": at this point the store lock proves odin is up, and the
# server at `--url` is the thing that could not be reached.
_STATUS_UNKNOWN_EXIT = 2


def _reconciler_health(url: str) -> list[dict] | str:
    """Every reconciler's own liveness answer from `GET /health`, or the reason
    this server could not be asked at `url`.

    A reason string is UNKNOWN and is reported as such -- never as healthy. An
    absent `reconcilers` key is the same unknown: a body that predates the field
    proves nothing about the loops behind it.

    Returning the REASON rather than a bare None is what stops the caller
    guessing at one: `http.URL_FAULTS` (a schemeless `ODIN_URL`, a non-numeric
    port) means odin never made a request at all, so "did not answer" would be
    the wrong sentence, and until field test 6 F9 `httpx.InvalidURL` -- which is
    not an `httpx.HTTPError` -- was not caught here at all and came out as a
    traceback."""
    try:
        body = httpx.get(f"{url.rstrip('/')}/health", timeout=5.0).json()
    except http.URL_FAULTS as exc:
        return http.url_fault_reason(url, exc)
    except (httpx.HTTPError, ValueError) as exc:
        return f"did not answer {url.rstrip('/')}/health ({type(exc).__name__})"
    reconcilers = body.get("reconcilers")
    return reconcilers if isinstance(reconcilers, list) else (
        f"answered {url.rstrip('/')}/health with no `reconcilers` field"
    )


@app.command()
def status(
    # The one `Annotated` option in the CLI, and for a real reason: `= http.URL`
    # makes the DEFAULT a typer `OptionInfo`, which is fine for a command only
    # ever reached through typer but not for this one -- `status()` is called
    # directly (tests/test_main.py drives it against a real store lock), and
    # that call must get a usable string, not an object with no `.rstrip`.
    url: Annotated[str, typer.Option(
        "--url", envvar="ODIN_URL", help="Base URL of the running odin server.",
    )] = http.DEFAULT_URL,
) -> None:
    """Is Odin up and reconciling? Exit 0 yes, 1 no, 2 could not tell.

    `status` is a question, so the exit code is the answer -- the shell
    convention every other predicate follows (`test`, `pgrep`, `systemctl
    is-active`). Through v0.7.3 it printed "Odin is not running." and exited
    0, which made `odin status && odin apply` apply against a server that
    wasn't there and gave a CI gate nothing to check but the sentence.

    A live server whose RECONCILER has stopped ticking is the same class of
    false green (`Reconciler.health`): odin answers every request, holds the
    store lock and looks perfect while nothing is being provisioned,
    garbage-collected or drift-checked. So the question this answers is "up AND
    reconciling", and a down loop exits 1 -- which is what makes `odin status
    && odin apply` refuse to apply into an env nothing will converge.

    The store lock is still the liveness evidence; the loops are asked over
    HTTP, so a server on another port needs `--url`/`ODIN_URL`. Not answering
    there is reported as UNKNOWN -- and it exits **2**, its own code.

    Field test 6 F1, and the reasoning is worth keeping because half of it was
    already right. That branch used to exit **0**, on the argument that the lock
    genuinely proves odin is running and that inventing a failure from a URL
    guess would be the mirror of the lie above. The second half of that still
    holds and 1 is still wrong here: odin has NOT observed a stopped loop, and
    saying it did would be its own false report. What did not hold is the
    conclusion, because 0 is not "I could not tell" -- it is this command's
    documented "running, AND every env's reconciler is ticking", which is
    precisely the half odin just said was UNKNOWN. A monitoring script gating
    on `odin status` read that as healthy.

    So UNKNOWN gets a code of its own rather than borrowing either answer, and
    the reachable-by-default case it comes from -- the server on a non-default
    port, which this command's own message anticipates -- now fails a gate
    instead of passing one. `odin status && odin apply` stops at an env whose
    convergence odin cannot vouch for, which is the same contract as the
    reconciler-down branch below.
    """
    server = live_server(ODIN_DIR)
    if server is None:
        typer.echo(f"Odin is not running{_clear_stale_pidfile()}.")
        raise typer.Exit(1)
    typer.echo(f"Odin is running ({server.detail}).")
    loops = _reconciler_health(url)
    if isinstance(loops, str):
        typer.echo(
            f"Odin holds this store, but {loops} -- whether its reconcilers are converging is "
            f"UNKNOWN (pass --url or ODIN_URL if it is on another port).",
            err=True,
        )
        raise typer.Exit(_STATUS_UNKNOWN_EXIT)
    down = [loop for loop in loops if not loop.get("ticking")]
    for loop in down:
        typer.echo(f"RECONCILER DOWN: {loop.get('verdict')}", err=True)
    if down:
        raise typer.Exit(1)
    typer.echo(f"{len(loops)} reconciler(s) converging: {', '.join(loop['env'] for loop in loops) or 'none yet'}.")


# Every container odin creates carries `odin=1` and `odin-env=<env>`, so the
# machine itself can be asked what an env still owns -- no store lookup, and no
# name-prefix guessing.
_ODIN_LABEL = "label=odin=1"


def _store_envs(odin_dir: Path) -> list[str]:
    """The envs THIS store has directories for.

    Deliberately not `SpecStore.list_envs()`, whose `or ["default"]` fallback
    invents an env for an empty store -- and `odin-env=default` containers may
    belong to somebody else's store entirely."""
    return sorted(p.name for p in odin_dir.iterdir() if p.is_dir()) if odin_dir.is_dir() else []


def _env_containers(env: str) -> list[str]:
    result = run_command([
        "docker", "ps", "-a", "--filter", _ODIN_LABEL, "--filter", f"label=odin-env={env}",
        "--format", "{{.Names}}",
    ])
    return [name for name in result.stdout.splitlines() if name.strip()]


def _env_vms(odin_dir: Path, env: str) -> list[str]:
    """This env's REAL Lima VMs: the exact names `vm_name(env, instance_id)`
    builds from the store's own instance records, intersected with the VMs that
    actually exist. Exact names only, never a prefix match -- the same
    discipline `ec2compute.reap_orphaned_vms` keeps, and what makes it
    impossible for a user's own VM (`veronica`) to be named here."""
    stores = SynthStores(odin_dir)
    expected = {
        vm_name(env, record["instance_id"])
        for key, record in stores.ec2compute.items(env).items() if key.startswith("instance:")
    }
    existing = set(run_command(["limactl", "list", "-q"]).stdout.split())
    return sorted(expected & existing)


def _refuse_if_the_store_still_owns_anything(odin_dir: Path) -> None:
    """`--all` deletes the only record that these containers and VMs are
    odin's.

    FIELD TEST 4's second finding: after wiping a scratch store the engineer
    had to `docker rm` an `odin-aws-*-sneak` container BY HAND, BY EXACT NAME,
    because no odin command could find it any more. A "full reset" that leaves
    real resources running with nothing able to name them is a leak with a
    friendly name.

    So this refuses and points at `odin destroy`, rather than reclaiming here.
    Teardown is not `docker rm -f`: `/destroy` also runs `tofu destroy`,
    reclaims EC2 VMs, purges the env's network records (which is what stops its
    nebula lighthouse), and revokes the env's gateway keys. Half of that from a
    file-cleaning command would be a different kind of leak -- and `odin
    destroy` still WORKS at this point, which is the whole reason to stop
    before the deletion rather than after it.
    """
    owned = {
        env: (_env_containers(env), _env_vms(odin_dir, env))
        for env in _store_envs(odin_dir)
    }
    live = {env: found for env, found in owned.items() if any(found)}
    if not live:
        return
    typer.echo("Refusing: this store still owns real resources, and .odin/ is the only record of them.")
    for env, (containers, vms) in live.items():
        typer.echo(f"  env {env!r}:")
        if containers:
            typer.echo(f"    containers: {', '.join(containers)}")
        if vms:
            typer.echo(f"    VMs:        {', '.join(vms)}")
    typer.echo(
        "Deleting .odin/ would orphan them -- nothing left would know they are odin's.\n"
        "Tear them down first (start odin, then "
        f"{', '.join(f'`odin destroy --env {env}`' for env in live)}, then stop odin), "
        "and run `odin clean --all` again.",
        err=True,
    )
    raise typer.Exit(1)


@app.command()
def clean(all: bool = typer.Option(False, "--all", help="Wipe entire .odin/ directory (canvas, registry, infra, session, everything)")) -> None:
    """Remove test artifacts, stray PNGs, and dev logs. Use --all for full reset."""
    root = Path.cwd()
    removed = []
    odin_dir = root / ".odin"

    if all:
        # `--all` is `rm -rf .odin`, and that includes `.odin/lock` -- the ONE
        # piece of evidence that a server is up. Deleting it under a live
        # server breaks three things at once:
        #   * the running server keeps its lock (flock lives on the INODE, not
        #     the name), but nothing can find it any more: `odin status` and
        #     `odin stop` say "not running" and `odin import` restores straight
        #     into a live store -- the v0.7.0 bug, re-created by hand.
        #   * the NEXT server takes a lock on a brand-new inode and succeeds, so
        #     TWO servers each believe they exclusively hold this store, both
        #     reconciling the same envs' real containers.
        #   * the store itself (spec revisions, world.json, gateway records,
        #     tofu workspaces) is pulled out from under a process writing to it.
        # None of that is recoverable by re-running anything, so this refuses
        # rather than repairs. The narrower clean below touches no lock and no
        # store state, so it stays unguarded.
        server = live_server(ODIN_DIR)
        if server is not None:
            typer.echo(f"Refusing: odin is running against .odin/ ({server.detail}).")
            typer.echo(f"`--all` deletes the store, INCLUDING the lock that server holds. "
                       f"Stop it with {server.how_to_stop} first.", err=True)
            raise typer.Exit(1)
        # ...and the store is also the only thing that knows which real
        # containers and VMs belong to odin. Checked AFTER the lock (a live
        # server is the more urgent hazard) and BEFORE any deletion, which is
        # the only moment `odin destroy` can still find them.
        _refuse_if_the_store_still_owns_anything(odin_dir)
        if odin_dir.exists():
            shutil.rmtree(odin_dir)
            private_mkdir(odin_dir)
            removed.append(".odin/ (full reset)")
        for png in root.glob("*.png"):
            png.unlink()
            removed.append(png.name)
        typer.echo(f"Cleaned: {', '.join(removed)}" if removed else "Nothing to clean.")
        return

    test_results = odin_dir / "test-results"
    if test_results.exists():
        shutil.rmtree(test_results)
        removed.append(".odin/test-results/")

    for png in root.glob("*.png"):
        png.unlink()
        removed.append(png.name)

    for log_name in ("dev.log", "events.jsonl"):
        log_file = odin_dir / log_name
        if log_file.exists():
            log_file.unlink()
            removed.append(f".odin/{log_name}")

    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if not pid_alive(pid):
            PID_FILE.unlink()
            removed.append(".odin/pid")

    typer.echo(f"Cleaned: {', '.join(removed)}" if removed else "Nothing to clean.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
