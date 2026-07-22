"""S2 -- the tofu runner: preflight, event streaming, failure tails, and the
per-env concurrency lock. Uses a small fake `tofu` shell script (never the
real binary) so these stay fast, deterministic unit tests; the real-tofu
round-trip lives in `test_tf_runner_e2e.py` (integration)."""
from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from odin.agent.hcl import TfProject
from odin.simulate.runner import SimulateBusy, TfResult, TfRunner, TofuNotInstalled


class RecordingWs:
    """A minimal `ConnectionManager` stand-in: just records every broadcast."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast(self, message: dict) -> None:
        self.messages.append(message)


def _write_fake_tofu(path: Path, script: str) -> Path:
    tofu = path / "tofu"
    tofu.write_text(f"#!/bin/sh\n{script}\n")
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC)
    return tofu


_INIT_OK = 'if [ "$1" = "init" ]; then echo "Initializing..."; exit 0; fi'

_APPLY_OK_TWO_LINES = _INIT_OK + '\nif [ "$1" = "apply" ]; then echo "line one"; echo "line two"; exit 0; fi'

_APPLY_FAILS = _INIT_OK + '\nif [ "$1" = "apply" ]; then echo "planning"; echo "boom: invalid resource"; exit 1; fi'

_APPLY_SLOW = _INIT_OK + '\nif [ "$1" = "apply" ]; then echo "starting"; sleep 0.4; echo "done"; exit 0; fi'


def _project() -> TfProject:
    return TfProject(files={"main.tf": 'provider "aws" {\n  region = "us-east-1"\n}\n'})


async def test_apply_raises_tofu_not_installed_when_which_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    runner = TfRunner(tmp_path)
    with pytest.raises(TofuNotInstalled):
        await runner.apply("default", _project(), 4266, "ak", "sk")


async def test_destroy_also_raises_tofu_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    runner = TfRunner(tmp_path)
    with pytest.raises(TofuNotInstalled):
        await runner.destroy("default", 4266, "ak", "sk")


async def test_successful_apply_streams_line_events_then_a_terminal_ok_event(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_OK_TWO_LINES)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    ws = RecordingWs()
    runner = TfRunner(tmp_path, ws=ws)

    result = await runner.apply("default", _project(), 4266, "ak", "sk")

    assert result.ok is True
    assert result.exit_code == 0
    apply_lines = [m["line"] for m in ws.messages if m.get("phase") == "apply" and "line" in m]
    assert apply_lines == ["line one", "line two"]
    terminal = [m for m in ws.messages if m.get("phase") == "apply" and "status" in m]
    assert terminal == [{"type": "tf", "env": "default", "phase": "apply", "status": "ok", "exit_code": 0}]
    init_terminal = [m for m in ws.messages if m.get("phase") == "init" and "status" in m]
    assert init_terminal == [{"type": "tf", "env": "default", "phase": "init", "status": "ok", "exit_code": 0}]


async def test_failed_apply_reports_failed_status_with_tail(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_FAILS)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    ws = RecordingWs()
    runner = TfRunner(tmp_path, ws=ws)

    result = await runner.apply("default", _project(), 4266, "ak", "sk")

    assert result.ok is False
    assert result.exit_code == 1
    assert result.tail == ("planning", "boom: invalid resource")
    terminal = next(m for m in ws.messages if m.get("phase") == "apply" and "status" in m)
    assert terminal["status"] == "failed"
    assert terminal["tail"] == ["planning", "boom: invalid resource"]


async def test_second_apply_while_first_is_running_raises_simulate_busy(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_SLOW)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    first = asyncio.create_task(runner.apply("default", _project(), 4266, "ak", "sk"))
    await asyncio.sleep(0.1)  # let the first call acquire the lock and start the subprocess

    with pytest.raises(SimulateBusy):
        await runner.apply("default", _project(), 4266, "ak", "sk")

    result = await first
    assert result.ok is True  # the busy rejection never disturbed the in-flight run


async def test_a_different_env_is_not_blocked_by_another_envs_lock(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_SLOW)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    first = asyncio.create_task(runner.apply("a", _project(), 4266, "ak", "sk"))
    await asyncio.sleep(0.1)

    # env "b" has its own lock -- must NOT raise SimulateBusy just because "a" is running.
    other = await runner.destroy("b", 4266, "ak", "sk")
    assert other.ok is True  # no workspace for "b" yet -> a clean no-op

    result = await first
    assert result.ok is True


async def test_destroy_with_no_prior_apply_is_a_noop_success(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _INIT_OK)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    result = await runner.destroy("default", 4266, "ak", "sk")

    assert result == TfResult(ok=True, exit_code=0)


async def test_status_reflects_last_result_and_running_flag(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_OK_TWO_LINES)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    assert runner.status("default")["running"] is False
    assert runner.status("default")["last"] is None

    await runner.apply("default", _project(), 4266, "ak", "sk")

    status = runner.status("default")
    assert status["running"] is False
    assert status["workspace_exists"] is True
    # tail is always carried on the result (status can show recent output
    # even on success); only the WS terminal EVENT omits it for a clean run.
    assert status["last"] == {"ok": True, "exit_code": 0, "tail": ["line one", "line two"]}
