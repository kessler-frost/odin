"""S2 -- the tofu runner: preflight, event streaming, failure tails, and the
per-env concurrency lock. Uses a small fake `tofu` shell script (never the
real binary) so these stay fast, deterministic unit tests; the real-tofu
round-trip lives in `test_tf_runner_e2e.py` (integration)."""
from __future__ import annotations

import asyncio
import stat
import time
from pathlib import Path

import pytest

from odin.agent.hcl import TfProject
from odin.simulate import runner as runner_mod
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

# A wedged apply (release finding #3): sleeps far longer than any test's own
# short timeout, so a real kill -- not just the process finishing on its
# own -- is what makes the test complete quickly.
_APPLY_WEDGED = _INIT_OK + '\nif [ "$1" = "apply" ]; then echo "starting"; sleep 30; echo "should never print"; exit 0; fi'


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


# --- release finding #3: a hard subprocess timeout -----------------------


def test_default_tofu_timeout_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("ODIN_TOFU_TIMEOUT", "42")
    assert runner_mod._default_tofu_timeout() == 42.0


def test_default_tofu_timeout_falls_back_to_600_seconds(monkeypatch):
    monkeypatch.delenv("ODIN_TOFU_TIMEOUT", raising=False)
    assert runner_mod._default_tofu_timeout() == 600.0


async def test_wedged_apply_is_killed_at_the_timeout_and_reported_failed(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_WEDGED)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    ws = RecordingWs()
    runner = TfRunner(tmp_path, ws=ws, timeout=0.3)

    start = time.monotonic()
    result = await runner.apply("default", _project(), 4266, "ak", "sk")
    elapsed = time.monotonic() - start

    assert elapsed < 5, "the process should have been killed, not left to sleep out its 30s"
    assert result.ok is False
    assert any("timed out" in line for line in result.tail)
    terminal = next(m for m in ws.messages if m.get("phase") == "apply" and "status" in m)
    assert terminal["status"] == "failed"
    assert any("timed out" in line for line in terminal["tail"])
    # the lock is released -- a wedged, killed run must not wedge the env forever
    assert runner.status("default")["running"] is False


async def test_a_fast_apply_is_unaffected_by_a_short_default_timeout(tmp_path, monkeypatch):
    # sanity: the timeout wraps the WHOLE subprocess, not just idle time --
    # a normal fast run under a generous timeout must behave exactly as before.
    _write_fake_tofu(tmp_path, _APPLY_OK_TWO_LINES)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path, timeout=30)

    result = await runner.apply("default", _project(), 4266, "ak", "sk")

    assert result.ok is True
    assert result.exit_code == 0


# --- owner directive B3: a bounded -parallelism, not tofu's own default 10 --


def test_default_parallelism_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("ODIN_TOFU_PARALLELISM", "8")
    assert runner_mod._default_parallelism() == 8


def test_default_parallelism_falls_back_to_4(monkeypatch):
    monkeypatch.delenv("ODIN_TOFU_PARALLELISM", raising=False)
    assert runner_mod._default_parallelism() == 4


def test_tf_runner_reads_the_parallelism_env_var_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_TOFU_PARALLELISM", "9")
    assert TfRunner(tmp_path)._parallelism == 9


_RECORD_APPLY_AND_DESTROY_ARGS = (
    _INIT_OK
    + '\nif [ "$1" = "apply" ] || [ "$1" = "destroy" ]; then echo "$@" >> "$RECORDED_ARGS_FILE"; exit 0; fi'
)


async def test_apply_and_destroy_pass_the_configured_parallelism_flag(tmp_path, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("RECORDED_ARGS_FILE", str(args_file))
    _write_fake_tofu(tmp_path, _RECORD_APPLY_AND_DESTROY_ARGS)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path, parallelism=2)

    apply_result = await runner.apply("default", _project(), 4266, "ak", "sk")
    assert apply_result.ok is True
    destroy_result = await runner.destroy("default", 4266, "ak", "sk")
    assert destroy_result.ok is True

    lines = args_file.read_text().splitlines()
    assert any(line.startswith("apply") and "-parallelism=2" in line for line in lines)
    assert any(line.startswith("destroy") and "-parallelism=2" in line for line in lines)


async def test_init_args_never_carry_the_parallelism_flag(tmp_path, monkeypatch):
    # `-parallelism` is not a valid `tofu init` flag -- a fake tofu that
    # rejects any unexpected init arg proves it's never passed there.
    script = (
        'if [ "$1" = "init" ]; then\n'
        '  if [ "$#" -gt 2 ]; then echo "unexpected init arg: $@"; exit 1; fi\n'
        '  echo "Initializing..."; exit 0\n'
        "fi\n"
        'if [ "$1" = "apply" ]; then echo "ok"; exit 0; fi'
    )
    _write_fake_tofu(tmp_path, script)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path, parallelism=2)

    result = await runner.apply("default", _project(), 4266, "ak", "sk")
    assert result.ok is True
