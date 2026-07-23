"""Filesystem write helpers shared across odin's sidecar persistence.

Every mutable JSON/text sidecar under `.odin/` (spec revisions, HEAD,
world.json, gateway JsonStores, credential keys, the Nebula overlay, the
saved canvas) is rewritten WHOLESALE on every mutation -- a crash mid-write,
or a concurrent reader opening the path at the wrong instant, must never
observe a truncated/partial file. `os.replace` is atomic on POSIX for a
same-filesystem rename, so every writer below stages the new content in a
sibling temp file and renames it into place instead of writing the target
path directly.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Write `text` to `path` atomically.

    Stages the content in a temp file in the SAME directory as `path` (so
    the final `os.replace` is a same-filesystem rename, never a cross-device
    copy), optionally `chmod`s it BEFORE the replace (a credential file must
    never be briefly world-readable between create and chmod), then swaps it
    in with one atomic rename. A crash or a concurrent read at any point
    before the replace still sees the OLD file, fully intact -- nothing ever
    observes a half-written one. On any failure the temp file is cleaned up
    rather than left behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if mode is not None:
            tmp_path.chmod(mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
