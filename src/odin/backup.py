"""Backup and restore of one environment's `.odin/` state (W2.3).

Losing `.odin/` is unrecoverable today: every non-default env's containers are
orphaned (nothing left records they belong to odin's model of the world) and
the startup reaper, seeing no envs, deletes every odin VM. There was no
snapshot anywhere. This module is the fix — a plain tar.gz of one env's
control-plane state, written and read straight off the filesystem so it works
with the server DOWN (restore-before-start is the whole point).

What an archive holds (`.odin/<env>/`, every file, recursively):

- `stacks/` + `HEAD`  — the immutable desired-state lineage
- `world.json`        — the last observed World (phases + facts)
- `keys.json`         — the env's issued gateway credentials
- `gateway/`          — the gateway's JSON stores + lambda deployment zips
- `tf/`               — `main.tf`, `override.tf`, `terraform.tfstate`
- `goaws.yaml`, `events.jsonl`, `nebula/` — whatever else the env wrote

...minus `tf/.terraform/`, the only deliberate exclusion: hundreds of MB of
downloaded provider plugins that `tofu init` re-materializes deterministically
from the same `main.tf`. Backing them up would make the archive unusable.

Layout inside the archive:

- `manifest.json` — {odin_version, env, created_at, format} at the root
- `env/…`         — the env dir's tree, under a FIXED prefix (not the env's
                    own name), which is what makes `--env <override>` on
                    import a pure choice of destination directory
- `canvas.json`   — the store-GLOBAL `.odin/canvas.json`, at a distinct
                    archive path because it is not part of any one env.
                    Restored only under an explicit `--with-canvas`: a
                    restore must never silently replace the canvas the user
                    is currently drawing on.

This is control-plane state, not data-plane state: it records that a bucket
named `uploads` should exist, never the objects inside it. Restore + Apply
gives you fresh, empty backing containers matching the archived desired
state.
"""
from __future__ import annotations

import io
import os
import shutil
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ValidationError

from odin.util import (
    SECRET_FILE_MODE,
    SHUTDOWN_GRACE,
    LiveServer,
    atomic_write_bytes,
    atomic_write_text,
    await_server_exit,
    live_server,
    odin_version,
)

MANIFEST_NAME = "manifest.json"
ENV_PREFIX = "env"
CANVAS_NAME = "canvas.json"
FORMAT = 1

# `tofu init`'s provider cache: re-downloaded deterministically from the very
# main.tf sitting next to it, and by far the largest thing under an env dir.
EXCLUDED_DIRS = {".terraform"}


class Manifest(BaseModel):
    odin_version: str
    env: str
    created_at: str
    format: int


class ExportResult(BaseModel):
    archive: str
    env: str
    members: tuple[str, ...]
    size: int


class ImportResult(BaseModel):
    archive: str
    env: str
    source_env: str
    odin_version: str
    created_at: str
    files: int
    canvas_restored: bool


class BackupError(Exception):
    """A refusal, carrying the CLI exit code it should produce: 2 = the
    archive isn't something this odin can read, 1 = everything else."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def default_archive_name(env: str) -> str:
    return f"odin-{env}-export.tar.gz"


def _exportable_files(env_dir: Path) -> list[Path]:
    """The env's files, EXCLUDING its canvas.

    The canvas moved into the env dir when it became per-env, which would have
    swept it into this walk -- and an env dir is replaced wholesale on import,
    so that would make `--with-canvas` meaningless: restoring an env's state
    would always clobber whatever the user currently has drawn. It is archived
    as its own top-level entry instead (`_add_canvas`), which keeps the flag's
    promise AND keeps new archives byte-compatible in layout with old ones.
    """
    files = (
        p for p in env_dir.rglob("*")
        if p.is_file()
        and EXCLUDED_DIRS.isdisjoint(p.relative_to(env_dir).parts)
        and p.relative_to(env_dir).parts != (CANVAS_NAME,)
    )
    return sorted(files)


def _known_envs(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def export_env(root: Path, env: str, dest: Path) -> ExportResult:
    """Write a tar.gz of `root/<env>/` (+ the shared canvas + a manifest) to
    `dest`. Pure filesystem work: no server, no HTTP, works with odin down."""
    env_dir = root / env
    if not env_dir.is_dir():
        raise BackupError(
            f"no such environment {env!r} under {root} — nothing to export "
            f"(known: {', '.join(_known_envs(root)) or 'none'})."
        )
    manifest = Manifest(
        odin_version=odin_version(), env=env,
        created_at=datetime.now(UTC).isoformat(), format=FORMAT,
    )
    canvas = env_dir / CANVAS_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _private_archive(dest) as tar:
        _add_bytes(tar, MANIFEST_NAME, manifest.model_dump_json(indent=2).encode())
        for path in _exportable_files(env_dir):
            tar.add(path, arcname=f"{ENV_PREFIX}/{path.relative_to(env_dir).as_posix()}",
                    filter=_owner_only)
        # This env's OWN canvas, at the same top-level archive path the global
        # one used -- so an exported env finally carries the canvas that
        # PRODUCED it, while `--with-canvas` keeps meaning "replace what I have
        # drawn" rather than being implied by every restore.
        _add_canvas(tar, canvas)
    return ExportResult(
        archive=str(dest), env=env, size=dest.stat().st_size,
        members=tuple(sorted(_names(dest))),
    )


@contextmanager
def _private_archive(dest: Path) -> Iterator[tarfile.TarFile]:
    """The archive, created 0600 before a single byte of it exists.

    `odin export` tells the user to "treat it like a private key file" and it
    genuinely is one -- it carries `keys.json`'s operator credentials and every
    canvas secret in cleartext -- but under the default umask `tarfile.open`
    made it 0644 (v0.7.0's leak). The `fchmod` covers the case where `dest`
    already existed with a looser mode, since O_CREAT's mode applies only to a
    file it actually creates.
    """
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
    os.fchmod(fd, SECRET_FILE_MODE)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        yield tar


def _owner_only(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Every member goes in 0600 regardless of its mode on disk: the archive is
    a private-key-grade file as a whole, and an archive written from a store an
    older odin left loose must not carry those loose modes into the restore."""
    info.mode = SECRET_FILE_MODE
    return info


def _add_canvas(tar: tarfile.TarFile, canvas: Path) -> None:
    if canvas.is_file():
        tar.add(canvas, arcname=CANVAS_NAME, filter=_owner_only)


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(payload))


@contextmanager
def _readable_archive(archive: Path) -> Iterator[tarfile.TarFile]:
    """Every read of an archive goes through here, so tar's own failures come
    out as refusals like every other bad input.

    The field test hit this on the disaster-recovery path, the moment a user is
    most likely to be holding a half-copied backup: a truncated archive printed
    an empty message then `Aborted.`, and a non-gzip file dumped ~120 lines of
    raw Python traceback. Both are the same answer to the user -- this file
    isn't a readable archive -- so they get one clear message with tar's own
    diagnosis quoted, and code 2 ("this odin can't read that"), matching the
    valid-tarball-but-not-an-odin-export refusal next door. The `with` covers
    the whole body deliberately: truncation is usually only discovered while
    READING a member, long after `tarfile.open` succeeded."""
    if not archive.is_file():
        raise BackupError(f"no such archive: {archive}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            yield tar
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BackupError(
            f"{archive} is not a readable .tar.gz archive ({exc}). If it is an odin "
            "export it is truncated or corrupt -- re-copy it from wherever it came "
            "from and try again.", 2,
        ) from None


def _names(archive: Path) -> list[str]:
    with _readable_archive(archive) as tar:
        return tar.getnames()


def read_manifest(archive: Path) -> Manifest:
    with _readable_archive(archive) as tar:
        return _manifest(tar, archive)


def _manifest(tar: tarfile.TarFile, archive: Path) -> Manifest:
    if MANIFEST_NAME not in tar.getnames():
        raise BackupError(
            f"{archive} is not an odin export: no {MANIFEST_NAME} at the archive root.", 2
        )
    raw = tar.extractfile(MANIFEST_NAME).read()
    try:
        manifest = Manifest.model_validate_json(raw)
    except ValidationError as exc:
        raise BackupError(
            f"{archive}'s {MANIFEST_NAME} is not a valid odin export manifest: {exc}", 2
        ) from None
    if manifest.format != FORMAT:
        raise BackupError(
            f"{archive} is export format {manifest.format}; this odin ({odin_version()}) "
            f"reads format {FORMAT}. Restore it with the odin version that wrote it "
            f"(the archive says {manifest.odin_version}).", 2
        )
    return manifest


def _checked_relative(member: tarfile.TarInfo) -> PurePosixPath:
    """The member's path, having proven it can only ever land INSIDE the
    destination. Absolute paths, any `..` component, and every non-regular
    member type (symlinks and hardlinks are the classic tar escapes; device
    nodes are never legitimate here) are refused outright — extraction then
    rebuilds each destination from these validated components rather than
    trusting the member name at all."""
    path = PurePosixPath(member.name)
    safe = (
        not path.is_absolute()
        and ".." not in path.parts
        and (member.isfile() or member.isdir())
    )
    if not safe:
        raise BackupError(
            f"refusing to extract {member.name!r}: unsafe archive member "
            "(absolute path, '..' traversal, or a link/device entry). "
            "This archive is malformed or hostile."
        )
    return path


def _destination(
    path: PurePosixPath, root: Path, target: Path, with_canvas: bool
) -> Path | None:
    """Where a validated member lands, or None if this archive path isn't one
    we restore (an unknown top-level entry, or the shared canvas without
    `--with-canvas`)."""
    if path.parts[:1] == (ENV_PREFIX,) and len(path.parts) > 1:
        return target.joinpath(*path.parts[1:])
    if with_canvas and path.parts == (CANVAS_NAME,):
        # A LEGACY archive: back when the canvas was one global file it was
        # stored at the archive root. It now belongs to the env being restored
        # (`.odin/<env>/canvas.json`), which is where anything reads it -- so
        # restoring it to the old global path would put it somewhere nothing
        # looks. New archives carry it inside the env payload instead, and hit
        # the ENV_PREFIX branch above.
        return target / CANVAS_NAME
    return None


def _current_canvas(target: Path) -> str | None:
    """The env's canvas as it stands, or None if it has none. Read BEFORE the
    env dir is replaced so a restore can put it back (see `import_archive`)."""
    path = target / CANVAS_NAME
    return path.read_text() if path.is_file() else None


def import_archive(
    archive: Path, root: Path, env: str | None = None,
    force: bool = False, with_canvas: bool = False,
    ignore_live_server: bool = False,
    on_wait: Callable[[LiveServer], None] = lambda _: None,
) -> ImportResult:
    """Restore an archive into `root`, as `env` (default: the manifest's own
    env). Refuses a live server, an existing env dir without `force`, and any
    unsafe archive member — validating EVERY member before writing a byte."""
    _refuse_live_server(root, ignore_live_server, on_wait)
    manifest = read_manifest(archive)
    target_env = env or manifest.env
    target = root / target_env
    if target.exists() and not force:
        raise BackupError(
            f"environment {target_env!r} already exists at {target} — refusing to overwrite it. "
            "Re-run with --force to replace it, or --env <name> to restore alongside it."
        )
    with _readable_archive(archive) as tar:
        members = [m for m in tar.getmembers() if m.name != MANIFEST_NAME]
        restore = [
            (member, dest)
            for member, path in ((m, _checked_relative(m)) for m in members)
            if member.isfile()
            and (dest := _destination(path, root, target, with_canvas)) is not None
        ]
        # The canvas is per-env, so it lives INSIDE the dir about to be
        # replaced -- which would make `--with-canvas` unkeepable: every
        # restore would clobber whatever the user currently has drawn, flag or
        # no flag. So without the flag its current canvas is carried across the
        # replacement. With the flag, the archive's canvas is written and this
        # keeps nothing.
        keep_canvas = None if with_canvas else _current_canvas(target)
        # Replace, don't merge: a restore of a --force'd env must not leave
        # stale files from the env it overwrote. Only reached once every
        # member has passed _checked_relative above.
        shutil.rmtree(target, ignore_errors=True)
        for member, dest in restore:
            _write(tar, member, dest)
        if keep_canvas is not None:
            atomic_write_text(target / CANVAS_NAME, keep_canvas, mode=0o600)
    return ImportResult(
        archive=str(archive), env=target_env, source_env=manifest.env,
        odin_version=manifest.odin_version, created_at=manifest.created_at,
        files=len(restore),
        canvas_restored=any(dest.name == CANVAS_NAME for _, dest in restore),
    )


def _write(tar: tarfile.TarFile, member: tarfile.TarInfo, dest: Path) -> None:
    """One member's bytes, at its archived permissions MINUS every group/other
    bit — so keys.json and the stack revisions come back 0600 exactly as they
    were written, and a member an older odin archived world-readable (`tf/
    main.tf` and `terraform.tfstate` were 0644 through v0.7.0) does NOT come
    back world-readable. Restoring is also the moment to tighten a store, never
    to loosen one; the `& 0o700` mask is what makes that direction-only."""
    atomic_write_bytes(dest, tar.extractfile(member).read(), mode=member.mode & 0o700)


def _refuse_live_server(
    root: Path, ignore: bool, on_wait: Callable[[LiveServer], None]
) -> None:
    """A running odin holds live per-env Reconcilers, BackingAws instances and
    an in-memory World; swapping the store out from under them would leave it
    reconciling against state it never read. Restore is a server-DOWN
    operation, full stop — no partial-liveness special cases to reason about.

    Liveness comes from `util.live_server`, which does NOT depend on who started
    the server and does NOT read anyone's command line: v0.7.0 tested only
    `.odin/pid` (written by `odin start` alone), so this refusal was inert for
    anyone running the app the way the README documents; v0.7.1 fixed that by
    grepping `ps` for `odin.server:create_app` and promptly refused a restore
    because the OPERATOR'S OWN SHELL had that string in its argv — on the
    disaster-recovery path, naming their shell as the thing to kill.

    Which is why the two rules here are asymmetric. A missed live server
    corrupts one store; a phantom live server blocks a restore, and the person
    running a restore has already lost something. So the evidence has to be a
    thing only a real server can produce, and when it's still there we WAIT for
    it — a stop the operator issued seconds ago is a server on its way out, not
    a reason to refuse — and when we refuse we say what we observed and how to
    override it."""
    if ignore or (server := live_server(root)) is None:
        return
    on_wait(server)
    remaining = await_server_exit(root, SHUTDOWN_GRACE)
    if remaining is None:
        return
    raise BackupError(
        f"odin is running ({remaining.detail}) — refusing to import into a live store. "
        f"Waited {SHUTDOWN_GRACE:.0f}s for it to exit and it is still holding this store. "
        f"Stop it with {remaining.how_to_stop}, then re-run `odin import` "
        "(restore is a server-down operation). If you are certain nothing is "
        "running, re-run with --ignore-live-server."
    )
