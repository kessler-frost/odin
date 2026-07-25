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

import os
import subprocess
import tempfile
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

# Every file odin writes that can carry a secret or a credential, and every
# directory such a file lives in. SECURITY.md's whole secrets argument rests on
# these two numbers ("`SecureString` buys you the file mode and nothing else"),
# so they live in ONE place and every writer names this constant rather than
# repeating an octal literal.
SECRET_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700

# Single source of truth for the running version: the installed package's own
# metadata (kept in lockstep with pyproject.toml's `version` by the build).
# The literal fallback only fires for an editable/unpackaged checkout where
# `importlib.metadata` has nothing to look up -- it still needs to say
# SOMETHING plausible rather than raise out of app startup.
_FALLBACK_VERSION = "0.4.0"


def odin_version() -> str:
    try:
        return _pkg_version("odin")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def pid_alive(pid: int) -> bool:
    """Whether a process with this PID exists (signal 0 probes without
    delivering anything)."""
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


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
