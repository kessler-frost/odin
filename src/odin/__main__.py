"""Odin CLI — start and stop the server."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import typer

app = typer.Typer(help="Odin server CLI", no_args_is_help=True, add_completion=False)

ODIN_DIR = Path(".odin")
PID_FILE = ODIN_DIR / "pid"
UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
DEFAULT_PORT = 4200
BACKEND_DEV_PORT = 4201


def _build_ui() -> None:
    dist = UI_DIR / "dist"
    if not dist.exists():
        typer.echo("Building UI …")
        subprocess.run(["bun", "run", "build"], cwd=str(UI_DIR), check=True)
    else:
        typer.echo("UI already built (ui/dist exists). Run `bun run build` in ui/ to rebuild.")


def _pid_exists(pid: int) -> bool:
    """Check whether a process with the given PID is alive."""
    result = subprocess.run(["kill", "-0", str(pid)], capture_output=True)
    return result.returncode == 0


@app.command()
def start(
    port: int = typer.Option(DEFAULT_PORT, "-p", "--port", help="Port (default: 4200)"),
    foreground: bool = typer.Option(False, "-f", "--foreground", help="Run in foreground"),
    dev: bool = typer.Option(False, "-d", "--dev", help="Dev mode: Vite HMR + uvicorn reload"),
) -> None:
    """Start the Odin server."""
    if dev:
        _start_dev(port)
        return

    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if _pid_exists(pid):
            typer.echo(f"Odin is already running (pid {pid}). Use `odin stop` first.")
            return
        PID_FILE.unlink()

    _build_ui()
    typer.echo(f"Starting Odin on http://localhost:{port}")

    if foreground:
        import uvicorn
        uvicorn.run("odin.server:create_app", factory=True, host="0.0.0.0", port=port)
    else:
        ODIN_DIR.mkdir(parents=True, exist_ok=True)
        log_path = ODIN_DIR / "server.log"
        log = log_path.open("w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "odin.server:create_app",
             "--factory", "--host", "0.0.0.0", "--port", str(port)],
            stdout=log, stderr=log, start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        typer.echo(
            f"Odin started in background (pid {proc.pid}). "
            f"Logs: {log_path}. Use `odin stop` to shut down."
        )


def _start_dev(port: int) -> None:
    """Dev mode startup."""
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if _pid_exists(pid):
            typer.echo(f"Odin is already running (pid {pid}). Use `odin stop` first.")
            return
        PID_FILE.unlink()

    typer.echo(f"Starting Odin dev mode on http://localhost:{port}")
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
         "--factory", "--host", "0.0.0.0", "--port", str(BACKEND_DEV_PORT),
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


@app.command()
def stop() -> None:
    """Stop the Odin server."""
    if not PID_FILE.exists():
        typer.echo("Odin is not running (no PID file found).")
        return
    pid = int(PID_FILE.read_text().strip())
    if not _pid_exists(pid):
        PID_FILE.unlink(missing_ok=True)
        typer.echo(f"Odin is not running (cleaned up stale pid {pid}).")
        return
    typer.echo(f"Stopping Odin (pid {pid}) …")
    os.kill(pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)
    typer.echo("Stopped.")


@app.command()
def status() -> None:
    """Check if Odin is running."""
    if not PID_FILE.exists():
        typer.echo("Odin is not running.")
        return
    pid = int(PID_FILE.read_text().strip())
    if _pid_exists(pid):
        typer.echo(f"Odin is running (pid {pid}).")
    else:
        typer.echo("Odin is not running (stale PID file). Cleaning up.")
        PID_FILE.unlink(missing_ok=True)


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
        if not _pid_exists(pid):
            PID_FILE.unlink()
            removed.append(".odin/pid")

    typer.echo(f"Cleaned: {', '.join(removed)}" if removed else "Nothing to clean.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
