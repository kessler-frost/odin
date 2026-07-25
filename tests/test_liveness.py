"""Two field-test findings about the same guard, one release apart.

v0.7.0 (agent A, "B5"): the documented "import refuses to run while odin is up"
guardrail never fired. The engineer's server was up and healthy but `odin
status` said "Odin is not running", so `odin import` restored into a LIVE store,
exit 0, no warning. Cause: the check read only `.odin/pid`, which ONLY `odin
start` writes, while the README documents running the app as `uvicorn
odin.server:create_app --factory`.

v0.7.1 (field test 3): the fix for that scanned `ps` for the literal string
`odin.server:create_app` and refused an import because the engineer's own SHELL
had that string in its argv -- with no server running anywhere and port 4510
dead -- and told them to `kill 26940`, their own shell. Moving the server
command into a file made the identical import exit 0. The natural trigger is an
ops wrapper script that restores a backup and then starts the app: the start
command sits in its own argv. So the failure landed exactly on the
disaster-recovery path.

Hence the shape of this file. Liveness is evidence, never inference, and the two
directions are not symmetric: a missed live server corrupts one store, while a
phantom live server blocks a restore for someone who has already lost something.
So `test_a_process_that_merely_mentions_the_server_is_not_one` runs a REAL
process with the marker in its argv, and every positive test holds a REAL kernel
lock on the store -- no process text is parsed anywhere, and nothing is faked at
any seam.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from odin import backup, util
from odin.backup import BackupError, export_env, import_archive
from odin.server import _keep_store_lock

# The exact command line the README documents -- the one v0.7.1 grepped for.
UVICORN = "/x/.venv/bin/python -m uvicorn odin.server:create_app --factory --host 127.0.0.1 --port 4510"


def _store(tmp_path: Path) -> Path:
    root = tmp_path / ".odin"
    (root / "e").mkdir(parents=True)
    (root / "e" / "HEAD").write_text("rev")
    return root


@pytest.fixture
def held_lock():
    """A store lock held for the test, like a server holds it, released after."""
    locks: list[util.StoreLock] = []

    def hold(root: Path) -> util.StoreLock:
        locks.append(util.hold_store_lock(root))
        return locks[-1]

    yield hold
    for lock in locks:
        lock.release()


def test_no_server_no_refusal(tmp_path: Path):
    assert util.live_server(_store(tmp_path)) is None


def test_odin_start_path_is_detected_via_the_pidfile(tmp_path: Path):
    """Launch path A: `odin start` (background or dev) writes `.odin/pid`. It
    stays the first answer -- it is also what covers the second or two between
    `odin start` forking uvicorn and uvicorn's lifespan taking the store lock."""
    root = _store(tmp_path)
    (root / "pid").write_text(str(os.getpid()))
    server = util.live_server(root)
    assert server is not None and server.pid == os.getpid() and server.managed


def test_stale_pidfile_is_not_a_live_server(tmp_path: Path):
    root = _store(tmp_path)
    (root / "pid").write_text("999999")  # never alive
    assert util.live_server(root) is None


def test_uvicorn_path_is_detected_with_no_pidfile_at_all(tmp_path: Path, held_lock):
    """Launch path B: the command the README documents. No pidfile exists --
    the server itself is holding the store lock, which is what odin observes."""
    root = _store(tmp_path)
    held_lock(root)
    server = util.live_server(root)
    assert server is not None
    assert server.pid == os.getpid() and not server.managed
    assert "store lock" in server.detail and f"kill {os.getpid()}" in server.how_to_stop


def test_a_process_that_merely_mentions_the_server_is_not_one(tmp_path: Path):
    """FIELD TEST 3, the regression this file exists for. A real process whose
    argv contains `odin.server:create_app` -- the ops wrapper script that
    restores a backup and then starts the app, or the shell it runs in -- with
    nothing listening and no lock held. v0.7.1 called this a live server and
    told the operator to kill it."""
    root = _store(tmp_path)
    wrapper = subprocess.Popen(
        # A long-lived process carrying the marker in its argv and nothing else:
        # what an ops wrapper (`restore.sh` -> `odin import` -> start the app)
        # looks like to `ps` while the import is running.
        [sys.executable, "-c", "import time; time.sleep(30)", UVICORN],
        cwd=tmp_path,
    )
    try:
        listed = subprocess.run(["ps", "-xo", "pid=,command="], capture_output=True, text=True).stdout
        assert "odin.server:create_app" in listed, "the decoy must really be visible to a ps scan"
        assert util.live_server(root) is None
    finally:
        wrapper.kill()
        wrapper.wait()


def test_a_server_on_another_store_does_not_block_this_one(tmp_path: Path, held_lock):
    """The second-instance case the ROADMAP explicitly blesses ("a separate
    working directory with its own store") must stay restorable. The lock is
    per-store by construction, so this is structural rather than a heuristic."""
    root = _store(tmp_path)
    elsewhere = tmp_path / "elsewhere" / ".odin"
    held_lock(elsewhere)
    assert util.live_server(root) is None


def test_a_lock_file_nobody_holds_is_not_a_live_server(tmp_path: Path):
    """The stale-evidence direction: the lock file outlives the server (it is a
    file), the LOCK does not (the kernel drops it when the process dies, `kill
    -9` included). A restore after a crash must not be blocked by leftovers."""
    root = _store(tmp_path)
    util.hold_store_lock(root).release()
    assert (root / util.STORE_LOCK_NAME).is_file()
    assert util.live_server(root) is None


def test_deleting_the_lock_file_lets_two_processes_hold_one_store(tmp_path: Path, held_lock):
    """WHY `odin clean --all` now refuses while a server is up (v0.7.4).

    An flock lives on the INODE, not on the name, so unlinking the file
    releases nothing -- and that is precisely what makes it dangerous. This
    reproduces both consequences, with real kernel locks and no fakes:

      1. odin goes BLIND: `live_server` finds no lock file, so `odin status`
         says "not running" and `odin import` restores into a live store --
         the v0.7.0 bug, re-created by hand.
      2. EXCLUSION IS LOST: the next `hold_store_lock` creates a NEW inode and
         takes an uncontended lock on it, so two processes each hold "the"
         store lock at the same time. Both would reconcile the same envs'
         real containers.
    """
    root = _store(tmp_path)
    first = held_lock(root)
    lock_path = root / util.STORE_LOCK_NAME
    assert util.live_server(root) is not None            # a server is demonstrably up

    lock_path.unlink()                                    # what `clean --all`'s rmtree did

    assert first.fd >= 0                                  # the first holder still has it open
    assert util.live_server(root) is None, "odin can no longer see its own live server"
    second = held_lock(root)
    assert second.fd != first.fd
    # The proof: the second take SUCCEEDED, on a different inode, while the
    # first is still held. Under the real lock file this is impossible --
    # `test_uvicorn_path_is_detected_with_no_pidfile_at_all` is the same call
    # refusing. Two servers, one store.
    assert lock_path.read_text().strip() == str(os.getpid())


def test_the_holder_puts_its_own_lock_file_back_after_a_deletion(tmp_path: Path, held_lock):
    """The other half of the fix (field test 4). `odin clean --all` refusing
    stops odin doing this to itself; `reassert` covers every deleter odin does
    NOT control -- a stray script, an impatient `rm -rf .odin` -- so the window
    in which odin lies about its own liveness is one watchdog interval instead
    of the rest of the server's life."""
    root = _store(tmp_path)
    lock = held_lock(root)
    old_fd = lock.fd
    (root / util.STORE_LOCK_NAME).unlink()
    assert util.live_server(root) is None                  # the blind window, measured

    assert lock.reassert() is False                        # False = a repair was needed
    assert lock.fd != old_fd                               # a new inode, taken and stamped
    server = util.live_server(root)
    assert server is not None and server.pid == os.getpid()
    # ...and the repaired lock really excludes: this is the whole point.
    probe = os.open(root / util.STORE_LOCK_NAME, os.O_RDWR)
    try:
        assert util._flock(probe) is False
    finally:
        os.close(probe)
    assert lock.reassert() is True                          # idempotent: nothing left to do


def test_reassert_refuses_to_steal_a_lock_file_someone_else_already_owns(tmp_path: Path, held_lock):
    """The one case it must NOT repair: a second server got there first, so
    the file on disk is genuinely theirs. Report it (the caller warns) rather
    than take it -- and keep our own fd until we know we have the new one."""
    root = _store(tmp_path)
    lock = held_lock(root)
    (root / util.STORE_LOCK_NAME).unlink()
    squatter = held_lock(root)                              # "the second server"
    ours = lock.fd

    assert lock.reassert() is False
    assert lock.fd == ours, "our own lock must not be dropped for one we could not take"
    assert squatter.fd != ours
    assert util.live_server(root) is not None                # the squatter is the one on record


async def test_the_servers_watchdog_restores_the_lock_without_anyone_asking(tmp_path: Path, held_lock):
    """`reassert` is only worth having if something runs it. This is the loop
    the app's lifespan starts -- driven at a test interval, but otherwise the
    production code path, including its cancellation."""
    root = _store(tmp_path)
    lock = held_lock(root)
    (root / util.STORE_LOCK_NAME).unlink()
    assert util.live_server(root) is None

    watchdog = asyncio.create_task(_keep_store_lock(lock, interval=0.01))
    await asyncio.sleep(0.1)
    watchdog.cancel()

    server = util.live_server(root)
    assert server is not None and server.pid == os.getpid()


def test_a_second_holder_is_refused_while_the_lock_file_is_intact(tmp_path: Path, held_lock):
    """The control for the test above: with the file left alone, the kernel
    hands the store to exactly one holder. `_flock` returning False is the ONLY
    thing that means "somebody else has it"."""
    root = _store(tmp_path)
    held_lock(root)
    probe = os.open(root / util.STORE_LOCK_NAME, os.O_RDWR)
    try:
        assert util._flock(probe) is False   # EWOULDBLOCK: refused, definitively
    finally:
        os.close(probe)


def test_an_unlockable_store_does_not_block_a_restore(tmp_path: Path):
    """The fail-open rule, at the one place the OS's answer is interpreted.
    v0.7.1 shelled out to `lsof` and treated "no lsof" as "assume it's ours",
    so a machine without lsof could not restore at all. Nothing shells out now,
    and anything short of a definite EWOULDBLOCK answers "nothing was proven"."""
    fd = os.open(tmp_path, os.O_RDONLY)
    os.close(fd)
    assert util._flock(fd) is True  # EBADF -- unknowable, so never a refusal


def test_import_refuses_the_uvicorn_launch_path(
    tmp_path: Path, held_lock, monkeypatch: pytest.MonkeyPatch
):
    """B5's exact scenario still refuses, with a fix `odin stop` cannot give --
    and the pid it names is the one the KERNEL says holds the store."""
    monkeypatch.setattr(backup, "SHUTDOWN_GRACE", 0.5)  # a server that stays is not worth 20s here
    root = _store(tmp_path)
    archive = tmp_path / "e.tar.gz"
    export_env(root, "e", archive)

    held_lock(root)
    with pytest.raises(BackupError) as exc:
        import_archive(archive, root, env="restored")
    message = str(exc.value)
    assert f"kill {os.getpid()}" in message and "--ignore-live-server" in message
    assert not (root / "restored").exists()


def test_import_waits_out_a_server_that_is_shutting_down(tmp_path: Path):
    """The timing trap the field hit: uvicorn with the reconciler in its
    lifespan takes >6s to let go, so `odin stop; sleep 6; odin import` legitimately
    still saw the old server and the refusal "felt arbitrary". A restore now
    waits for the store to be released instead of refusing on a stopwatch."""
    root = _store(tmp_path)
    archive = tmp_path / "e.tar.gz"
    export_env(root, "e", archive)

    lock = util.hold_store_lock(root)
    threading.Timer(1.0, lock.release).start()
    waited: list[util.LiveServer] = []
    started = time.monotonic()
    result = import_archive(archive, root, env="restored", on_wait=waited.append)
    assert result.env == "restored" and (root / "restored" / "HEAD").is_file()
    assert waited, "the caller is told it is waiting, never a silent stall"
    assert 0.5 < time.monotonic() - started < util.SHUTDOWN_GRACE


def test_ignore_live_server_is_the_escape_hatch(tmp_path: Path, held_lock):
    """Whatever else this guard gets wrong, it must never be the last word on
    someone's restore."""
    root = _store(tmp_path)
    archive = tmp_path / "e.tar.gz"
    export_env(root, "e", archive)
    held_lock(root)
    result = import_archive(archive, root, env="restored", ignore_live_server=True)
    assert (root / "restored" / "HEAD").read_text() == "rev" and result.env == "restored"
