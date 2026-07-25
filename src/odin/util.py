"""Small shared helpers: atomic sidecar writes, file modes, the running
version, liveness.

Every mutable JSON/text sidecar under `.odin/` (spec revisions, HEAD,
world.json, gateway JsonStores, credential keys, the Nebula overlay, the
saved canvas) is rewritten WHOLESALE on every mutation -- a crash mid-write,
or a concurrent reader opening the path at the wrong instant, must never
observe a truncated/partial file. `os.replace` is atomic on POSIX for a
same-filesystem rename, so every writer below stages the new content in a
sibling temp file and renames it into place instead of writing the target
path directly.

The file MODE is the other half of the same job, and it lives here for the
same reason: SECURITY.md rests odin's entire secrets-at-rest argument on
`0600`, so `SECRET_FILE_MODE`/`PRIVATE_DIR_MODE` are named once and every
writer -- the spec store, the canvas, the gateway sidecars, the event log,
the tofu workspace, the export archive -- reaches for the same constant
instead of an octal literal of its own.

`odin_version` and `pid_alive` live here because more than one surface needs
them and neither should be duplicated: the FastAPI app stamps its version,
`odin export`'s manifest records it; `odin start/stop/status` test the
pidfile, and `odin import` refuses to restore into a live store.
"""
from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

# Every file odin writes that can carry a secret or a credential, and every
# directory such a file lives in. SECURITY.md's whole secrets argument rests on
# these two numbers ("`SecureString` buys you the file mode and nothing else"),
# so they live in ONE place and every writer names this constant rather than
# repeating an octal literal.
SECRET_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700

# The running version, from the ONE file a human edits (pyproject.toml) when
# we're in a source checkout, and from installed metadata otherwise. Order
# matters: `importlib.metadata` answers about the installed DISTRIBUTION, which
# in an editable checkout is only as fresh as the last `uv sync` -- the field
# test found `odin export` stamping `odin_version 0.5.3` into archive manifests
# while pyproject.toml said 0.7.0, because the dist-info was two releases
# stale. Backup format-compatibility messages quote this, so a wrong answer is
# worse than a slow one. There is deliberately NO literal version anywhere in
# the source tree to drift out of step with pyproject.toml.
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_UNKNOWN_VERSION = "0.0.0+unknown"


def _pyproject_version() -> str | None:
    """pyproject.toml's `[project] version`, if this really is odin's own
    checkout (the `name` guard: `parents[2]` is only odin's repo root for a
    source/editable layout, and must never pick up a stranger's manifest)."""
    project = tomllib.loads(_PYPROJECT.read_text()).get("project", {}) if _PYPROJECT.is_file() else {}
    return project.get("version") if project.get("name") == "odin" else None


def _metadata_version() -> str | None:
    try:
        return _pkg_version("odin")
    except PackageNotFoundError:
        return None


def odin_version() -> str:
    """odin's version, or `0.0.0+unknown` when neither source is available --
    an honest "I don't know" rather than a hardcoded number that rots (the old
    fallback still claimed 0.4.0 three releases later)."""
    return _pyproject_version() or _metadata_version() or _UNKNOWN_VERSION


def pid_alive(pid: int) -> bool:
    """Whether a process with this PID exists (signal 0 probes without
    delivering anything)."""
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


# The store lock. A running control app holds an exclusive `flock` on this file
# for its entire lifetime (odin.server's lifespan takes it), so "is a server up
# against THIS store?" is answered by trying to take the same lock: the kernel
# either hands it over (nobody is there) or refuses (somebody is). It is the
# process itself that holds it, so the answer cannot be stale -- the lock dies
# with the process, `kill -9` included -- and it cannot be forged by text that
# merely looks like a server.
STORE_LOCK_NAME = "lock"

# How long `odin import` will wait for a server that is on its way out. A real
# uvicorn with the reconciler in its lifespan takes well over 6 seconds to stop
# (reconcilers drain, then the gateway thread joins), so the scripted
# `odin stop; sleep 5; odin import` an operator writes during a restore would
# otherwise hit a guard that "feels arbitrary". Waiting is honest: it is exactly
# how long the store stays in use.
SHUTDOWN_GRACE = 20.0
_LOCK_POLL = 0.25


@dataclass(frozen=True)
class LiveServer:
    """A control app running against a given store, and the EVIDENCE for it.

    `managed` = odin started it (there's a pidfile), so `odin stop` can stop it;
    otherwise the user launched uvicorn themselves and only their own
    kill/Ctrl-C will. `pid` is None when something demonstrably holds the store
    lock but hasn't stamped its pid in the file yet -- rare, and the honest
    answer is "a process I can't name", never a guess.
    """

    pid: int | None
    evidence: str
    managed: bool = False

    @property
    def detail(self) -> str:
        who = f"pid {self.pid}" if self.pid is not None else "an unidentified process"
        return f"{who}, {self.evidence}"

    @property
    def how_to_stop(self) -> str:
        if self.managed:
            return "`odin stop`"
        if self.pid is None:
            return "stopping whatever holds that lock (`lsof` on the lock file names it)"
        return f"kill {self.pid} (or Ctrl-C in its terminal)"


def _flock(fd: int) -> bool:
    """Try to take the exclusive lock on `fd`, without blocking. True = we got
    it, so nobody else held it. False means one thing ONLY: another process
    holds it (EWOULDBLOCK).

    Every other error -- a filesystem that doesn't implement flock, a fd we
    can't lock -- answers True, i.e. "nothing was proven". That direction is
    deliberate: an unprovable liveness answer must never block a restore. This
    is the one place the OS's answer is interpreted, for both the holder and
    the prober.
    """
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        return exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN)


@dataclass(frozen=True)
class StoreLock:
    """The lock a live server holds, released when it exits (or `release()`s)."""

    fd: int

    def release(self) -> None:
        os.close(self.fd)


def hold_store_lock(root: Path) -> StoreLock:
    """Claim `root/lock` for the life of this process and stamp our pid in it.

    The server calls this once, in its lifespan; the kernel drops the lock when
    the process ends however it ends. The pid is written only once the lock is
    OURS, so a reader that finds the lock held is reading a pid that really
    holds it -- the file is evidence, never an assertion on its own.
    """
    private_mkdir(root)
    fd = os.open(root / STORE_LOCK_NAME, os.O_RDWR | os.O_CREAT, SECRET_FILE_MODE)
    if _flock(fd):
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    return StoreLock(fd)


def _lock_holder(root: Path) -> LiveServer | None:
    """Whoever holds this store's lock, if anyone -- the launch-path-independent
    half of `live_server`, and the half that cannot false-positive: it asks the
    kernel who holds a lock, not what a command line looks like."""
    path = root / STORE_LOCK_NAME
    if not path.is_file():
        return None
    fd = os.open(path, os.O_RDONLY)
    free = _flock(fd)
    os.close(fd)  # drops the lock again if we were the ones who just took it
    raw = path.read_text().strip()
    # Both halves of the evidence are things just observed, never inferred:
    # somebody holds this lock, and there is no pidfile naming them (so `odin
    # stop` is not the fix and this is not a server `odin start` launched).
    return None if free else LiveServer(
        pid=int(raw) if raw.isdigit() else None,
        evidence=f"holding the store lock {path}, no pidfile",
    )


def _pidfile_server(root: Path) -> LiveServer | None:
    pidfile = root / "pid"
    raw = pidfile.read_text().strip() if pidfile.is_file() else ""
    alive = raw.isdigit() and pid_alive(int(raw))
    return LiveServer(pid=int(raw), evidence="pidfile", managed=True) if alive else None


def live_server(root: Path) -> LiveServer | None:
    """The control app running against the store at `root`, if any.

    Liveness must NOT depend on who started the process (v0.7.0 tested only
    `.odin/pid`, which only `odin start` writes, so `odin status` lied about the
    `uvicorn odin.server:create_app --factory` the README documents and `odin
    import` restored straight into a live store) -- and it must NOT depend on
    what a process's argv looks like either. v0.7.1 scanned `ps` for the string
    `odin.server:create_app` and refused an import because the engineer's own
    SHELL had those words in its command line, then told them to kill it. Both
    failures come from inferring liveness instead of observing it.

    So both answers here are things only a real server can produce: a pidfile
    `odin start` wrote whose pid is still alive, and an exclusive lock on the
    store that a process is holding open right now. The pidfile goes first --
    it's cheap, exact, and it's what tells us `odin stop` will work -- and it
    also covers the second or two between `odin start` forking uvicorn and
    uvicorn's lifespan taking the lock.
    """
    return _pidfile_server(root) or _lock_holder(root)


def await_server_exit(root: Path, timeout: float = SHUTDOWN_GRACE) -> LiveServer | None:
    """`live_server`, but give a server that is already shutting down up to
    `timeout` seconds to actually finish, since it holds the store until it
    does. Returns None once the store is free, or the server still there when
    the wait runs out -- an answer earned by waiting rather than a refusal the
    operator has to guess the timing of."""
    deadline = time.monotonic() + timeout
    server = live_server(root)
    while server is not None and time.monotonic() < deadline:
        time.sleep(_LOCK_POLL)
        server = live_server(root)
    return server


def private_mkdir(directory: Path) -> Path:
    """`mkdir -p`, except every directory this call CREATES is 0700.

    Directory modes are the second half of the file-mode defense: without
    traverse permission on `.odin/<env>/`, a file inside it that some future
    writer forgets to lock down is still unreadable by another local account.
    Only the missing levels are created (and each is created 0700 outright,
    never chmod'd after the fact -- no window where it exists world-readable);
    an EXISTING directory's mode is left exactly as the user has it.
    """
    missing = [p for p in (directory, *directory.parents) if not p.exists()]
    for parent in reversed(missing):
        parent.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=True)
    return directory


def atomic_write_bytes(path: Path, payload: bytes, mode: int | None = None) -> None:
    """Write `payload` to `path` atomically.

    Stages the content in a temp file in the SAME directory as `path` (so
    the final `os.replace` is a same-filesystem rename, never a cross-device
    copy), optionally `chmod`s it BEFORE the replace (a credential file must
    never be briefly world-readable between create and chmod), then swaps it
    in with one atomic rename. A crash or a concurrent read at any point
    before the replace still sees the OLD file, fully intact -- nothing ever
    observes a half-written one. On any failure the temp file is cleaned up
    rather than left behind.
    """
    private_mkdir(path.parent)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        if mode is not None:
            tmp_path.chmod(mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """`atomic_write_bytes` for text (UTF-8) -- the shape every JSON/HCL
    sidecar writer uses."""
    atomic_write_bytes(path, text.encode(), mode)


def secure_append_line(path: Path, line: str) -> None:
    """Append one line to a file that must never be group/world-readable.

    `events.jsonl` is the append-only durable event log, and a WorldDelta's
    facts carry live credentials in cleartext (an rds `DATABASE_URL` embeds
    the password; a crash verdict can quote a workload's issued gateway keys),
    so it needs the same 0600 as `world.json` -- which a plain `open("a")`
    does not give it (umask 022 -> 0644, the v0.7.0 leak). O_CREAT carries the
    mode for a NEW file; the `fchmod` re-tightens a file an older odin already
    left loose, before this line's secret is appended to it.
    """
    private_mkdir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, SECRET_FILE_MODE)
    with os.fdopen(fd, "a") as f:
        os.fchmod(fd, SECRET_FILE_MODE)
        f.write(line + "\n")


def ensure_private_file(path: Path) -> Path:
    """Guarantee `path` exists and is owner-only, WITHOUT touching a byte of
    its contents.

    For files a foreign tool writes and rewrites on its own schedule:
    `terraform.tfstate` and its `.backup` are tofu's, not odin's, and tofu
    creates them 0644 under the default umask. Creating them 0600 first is
    what makes the mode stick -- tofu's local state manager rewrites state
    IN PLACE (open the same inode O_RDWR, truncate, write; no rename), so the
    creation mode is the mode forever. The `chmod` heals a workspace an older
    odin already left loose.
    """
    private_mkdir(path.parent)
    os.close(os.open(path, os.O_RDONLY | os.O_CREAT, SECRET_FILE_MODE))
    path.chmod(SECRET_FILE_MODE)
    return path
