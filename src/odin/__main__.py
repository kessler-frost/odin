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
from odin.util import live_server, pid_alive

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


def _warn_if_non_loopback(host: str) -> None:
    if host not in _LOOPBACK_HOSTS:
        typer.echo(
            f"WARNING: binding to {host} -- odin has no authentication. "
            "Anyone who can reach this port can run containers on this machine.",
            err=True,
        )


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

    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if pid_alive(pid):
            typer.echo(f"Odin is already running (pid {pid}). Use `odin stop` first.")
            return
        PID_FILE.unlink()

    _build_ui()
    typer.echo(f"Starting Odin on http://{host}:{port}")

    if foreground:
        import uvicorn
        # A foreground server is just as live as a backgrounded one, so it gets
        # the same pidfile: without it `odin status`/`odin stop` from a second
        # terminal, and `odin import`'s live-store refusal, had nothing cheap to
        # find. (`live_server` would still catch it by process scan, but only
        # because uvicorn.run reuses this process -- the pidfile is exact.)
        ODIN_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
        uvicorn.run("odin.server:create_app", factory=True, host=host, port=port)
        PID_FILE.unlink(missing_ok=True)
    else:
        ODIN_DIR.mkdir(parents=True, exist_ok=True)
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
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if pid_alive(pid):
            typer.echo(f"Odin is already running (pid {pid}). Use `odin stop` first.")
            return
        PID_FILE.unlink()

    typer.echo(f"Starting Odin dev mode on http://{host}:{port}")
    typer.echo(f"  Vite  → :{port}  (HMR)")
    typer.echo(f"  API   → :{BACKEND_DEV_PORT}  (auto-reload)")

    ODIN_DIR.mkdir(parents=True, exist_ok=True)
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
    """Stop the Odin server."""
    server = live_server(ODIN_DIR)
    if server is None:
        typer.echo(f"Odin is not running{_clear_stale_pidfile()}.")
        return
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
    """Check if Odin is running."""
    server = live_server(ODIN_DIR)
    if server is None:
        typer.echo(f"Odin is not running{_clear_stale_pidfile()}.")
        return
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
            odin_dir.mkdir()
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
