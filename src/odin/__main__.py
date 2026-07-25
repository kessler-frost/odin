"""Odin CLI — start and stop the server."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import typer

from odin.cli import commands as _commands  # noqa: F401  (registers the control-surface commands)
from odin.cli import doctor as _doctor  # noqa: F401  (registers `odin doctor`)
from odin.cli.app import app
from odin.util import live_server, odin_version, pid_alive, private_mkdir

ODIN_DIR = Path(".odin")
PID_FILE = ODIN_DIR / "pid"
UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
DEFAULT_PORT = 4200
BACKEND_DEV_PORT = 4201
DEFAULT_HOST = "127.0.0.1"
# Security finding #1a/b: odin has no authentication of its own -- every
# route from `/apply` down to `/tf/*` drives real `docker run`/Lima VMs with
# no login, no token, nothing. Binding to a LAN/0.0.0.0 address turns "my
# laptop" into "anyone on this network can run containers on my laptop", so
# loopback is the only default; a wider bind is opt-in and loud about it.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
    """True when a live pidfile server means this `start` must not launch a
    second one -- printed, and NOT an error.

    Exit 0 is deliberate here, and the odd one out among the exit-code fixes:
    `odin start` asks for an end state (odin up) and that end state holds, so
    an idempotent `odin start && odin apply` in a script should keep working.
    What a second `start` cannot honour is a different `--port`/`--host`, so
    it says so out loud rather than letting the flags look applied.
    """
    if not PID_FILE.exists():
        return False
    pid = int(PID_FILE.read_text().strip())
    if not pid_alive(pid):
        PID_FILE.unlink()
        return False
    typer.echo(
        f"Odin is already running (pid {pid}) — nothing was started, and any --port/--host "
        "you passed was NOT applied. Run `odin stop` first to restart it with new flags."
    )
    return True


def _build_ui() -> None:
    if (Path(__file__).resolve().parent / "_ui").exists():
        return  # UI ships bundled with the installed package
    dist = UI_DIR / "dist"
    if not dist.exists():
        typer.echo("Building UI …")
        subprocess.run(["bun", "run", "build"], cwd=str(UI_DIR), check=True)
    else:
        typer.echo("UI already built (ui/dist exists). Run `bun run build` in ui/ to rebuild.")


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
        typer.echo(
            f"Odin started in background (pid {proc.pid}). "
            f"Logs: {log_path}. Use `odin stop` to shut down."
        )


def _start_dev(port: int, host: str = DEFAULT_HOST) -> None:
    """Dev mode startup."""
    if _already_running():
        return

    typer.echo(f"Starting Odin dev mode on http://{host}:{port}")
    typer.echo(f"  Vite  → :{port}  (HMR)")
    typer.echo(f"  API   → :{BACKEND_DEV_PORT}  (auto-reload)")

    private_mkdir(ODIN_DIR)
    PID_FILE.write_text(str(os.getpid()))

    log_path = ODIN_DIR / "dev.log"
    log = log_path.open("w")

    def _relay(stream: object) -> None:
        for line in stream:
            text = line.decode(errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            log.write(text)
            log.flush()

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "odin.server:create_app",
         "--factory", "--host", host, "--port", str(BACKEND_DEV_PORT),
         "--reload", "--reload-dir", "src"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    frontend = subprocess.Popen(
        ["bun", "run", "dev", "--port", str(port)],
        cwd=str(UI_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    threading.Thread(target=_relay, args=(backend.stdout,), daemon=True).start()
    threading.Thread(target=_relay, args=(frontend.stdout,), daemon=True).start()

    procs = [backend, frontend]

    def _shutdown(*_: object) -> None:
        for p in procs:
            p.terminate()
        PID_FILE.unlink(missing_ok=True)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Wait for either subprocess to exit (not os.wait() which catches any child)
    while all(p.poll() is None for p in procs):
        signal.pause()
    _shutdown()
    for p in procs:
        p.wait()


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
    cannot signal.
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
    PID_FILE.unlink(missing_ok=True)
    typer.echo("Stopped.")


@app.command()
def status() -> None:
    """Is Odin running? Exit 0 if it is, 1 if it is not.

    `status` is a question, so the exit code is the answer -- the shell
    convention every other predicate follows (`test`, `pgrep`, `systemctl
    is-active`). Through v0.7.3 it printed "Odin is not running." and exited
    0, which made `odin status && odin apply` apply against a server that
    wasn't there and gave a CI gate nothing to check but the sentence.
    """
    server = live_server(ODIN_DIR)
    if server is None:
        typer.echo(f"Odin is not running{_clear_stale_pidfile()}.")
        raise typer.Exit(1)
    typer.echo(f"Odin is running ({server.detail}).")


@app.command()
def clean(all: bool = typer.Option(False, "--all", help="Wipe entire .odin/ directory (canvas, registry, infra, session, everything)")) -> None:
    """Remove test artifacts, stray PNGs, and dev logs. Use --all for full reset."""
    import shutil
    root = Path.cwd()
    removed = []
    odin_dir = root / ".odin"

    if all:
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
