"""Odin CLI — start and stop the server."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import typer

from odin.cli import commands as _commands  # noqa: F401  (registers the control-surface commands)
from odin.cli import doctor as _doctor  # noqa: F401  (registers `odin doctor`)
from odin.cli.app import app
from odin.cli.doctor import BUN_INSTALL
from odin.compute.instances import vm_name
from odin.gateway.stores import SynthStores
from odin.util import (
    COMMAND_NOT_FOUND,
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
    typer.echo(f"Cannot {purpose}: {detail}.", err=True)
    typer.echo(f"fix: {BUN_INSTALL}", err=True)
    typer.echo(
        "     then open a new shell so PATH picks it up. `odin doctor` re-checks it.\n"
        "     (A released odin ships the UI prebuilt and needs no bun; this is a clone.)",
        err=True,
    )
    return typer.Exit(1)


def _require_bun(purpose: str) -> None:
    """Ask PATH before shelling out. This is an OBSERVATION, not a guess from
    an exit code -- and it is what separates "you don't have bun" from "your
    build broke", two failures that need completely different sentences."""
    if shutil.which("bun") is None:
        raise _refuse_without_bun(purpose, "`bun` is not installed (nothing named `bun` on PATH)")


def _run_in_ui(args: list[str]) -> int:
    """`args` in `ui/`, returning its exit code -- and 127 rather than an
    exception if the binary could not be executed at all, the same translation
    `util.run_command` makes for every other tool odin shells out to. Output is
    NOT captured: bun's own error is the diagnosis and belongs on the user's
    terminal, not inside a traceback frame."""
    try:
        return subprocess.run(args, cwd=str(UI_DIR)).returncode
    except OSError:
        return COMMAND_NOT_FOUND


def _build_ui() -> None:
    """Make sure there is a UI to serve -- or say which of the three things
    went wrong and how to fix that one."""
    if BUNDLED_UI.exists():
        return  # UI ships bundled with the installed package
    if (UI_DIR / "dist").exists():
        typer.echo("UI already built (ui/dist exists). Run `bun run build` in ui/ to rebuild.")
        return
    _require_bun("build the UI")
    typer.echo("Building UI …")
    code = _run_in_ui(["bun", "run", "build"])
    if code == COMMAND_NOT_FOUND:
        # PATH said yes and exec said no -- a dangling shim, a wrong-arch
        # binary. Same remedy, different evidence, and it must say which.
        raise _refuse_without_bun("build the UI", "`bun` is on PATH but could not be run")
    if code != 0:
        typer.echo(f"Cannot build the UI: `bun run build` failed (exit {code}) in {UI_DIR}.",
                   err=True)
        typer.echo("fix: read bun's output above; `bun install` in ui/ is the usual missing step.",
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


def _start_dev(port: int, host: str = DEFAULT_HOST) -> None:
    """Dev mode startup."""
    if _already_running():
        return
    # The other `bun` in this file: dev mode runs Vite itself rather than a
    # built bundle, and a raw `Popen(["bun", ...])` tracebacks identically.
    # Checked BEFORE the pidfile and the backend are created, so a machine
    # without bun is refused rather than half-started.
    _require_bun("run dev mode (it serves the UI through Vite)")

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
