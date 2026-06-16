from __future__ import annotations

import time

import pytest

from odin.process import Daemon, run


async def test_run_captures_stdout():
    result = await run("echo", "hello world")
    assert result.ok
    assert "hello world" in result.stdout


async def test_run_nonzero_returncode_does_not_raise():
    result = await run("sh", "-c", "echo oops >&2; exit 3")
    assert not result.ok
    assert result.returncode == 3
    assert "oops" in result.stderr


async def test_run_missing_binary_raises():
    # A genuinely missing binary fails loudly (no silent degradation).
    with pytest.raises(FileNotFoundError):
        await run("odin-no-such-binary-xyz")


def test_daemon_start_stop():
    daemon = Daemon("sleep", "30")
    assert not daemon.running
    daemon.start()
    try:
        time.sleep(0.2)
        assert daemon.running
    finally:
        daemon.stop()
    assert not daemon.running


def test_daemon_stop_is_idempotent():
    daemon = Daemon("sleep", "30")
    daemon.start()
    daemon.stop()
    daemon.stop()  # second stop must not raise
    assert not daemon.running
