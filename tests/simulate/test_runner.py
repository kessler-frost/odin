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

_APPLY_LEAKS_A_SECRET = _INIT_OK + (
    '\nif [ "$1" = "apply" ]; then echo "planning"; echo "password = hunter2"; exit 0; fi'
)

_APPLY_LEAKS_A_SECRET_ON_FAILURE = _INIT_OK + (
    '\nif [ "$1" = "apply" ]; then echo "password = hunter2"; echo "boom"; exit 1; fi'
)

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


# --- field test 2 finding B6: a wedged DESTROY is bounded, and says why ----

_DESTROY_WEDGED = _INIT_OK + '\nif [ "$1" = "destroy" ]; then echo "Destroying..."; sleep 30; exit 0; fi'
_INIT_WEDGED = 'if [ "$1" = "init" ]; then echo "Initializing..."; sleep 30; exit 0; fi'


def test_destroy_timeout_defaults_below_the_apply_timeout(monkeypatch):
    monkeypatch.delenv("ODIN_TOFU_DESTROY_TIMEOUT", raising=False)
    monkeypatch.delenv("ODIN_TOFU_TIMEOUT", raising=False)
    assert runner_mod._default_destroy_timeout() < runner_mod._default_tofu_timeout()
    assert runner_mod._default_destroy_timeout() == 300.0


def test_destroy_timeout_reads_its_own_env_var(monkeypatch):
    monkeypatch.setenv("ODIN_TOFU_DESTROY_TIMEOUT", "77")
    assert runner_mod._default_destroy_timeout() == 77.0


async def test_a_wedged_destroy_is_killed_and_names_the_likely_cause(tmp_path, monkeypatch):
    """Field-test 2 finding B6: `odin destroy` on a RESTORED env (which boots no
    backing containers) was killed by hand at 8m26s with no progress. Nothing
    was broken about the timeout -- 8m26s is 506s, under the 600s
    `ODIN_TOFU_TIMEOUT` -- but 600s per phase is not a bound anyone waits out,
    and the failure said nothing about the real cause: every AWS call the
    destroy makes 503s (no backing is running) and the provider retries each
    one ~25 times with backoff."""
    _write_fake_tofu(tmp_path, _DESTROY_WEDGED)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    (tmp_path / "default" / "tf").mkdir(parents=True)
    ws = RecordingWs()
    runner = TfRunner(tmp_path, ws=ws, timeout=30, destroy_timeout=0.3)

    start = time.monotonic()
    result = await runner.destroy("default", 4266, "ak", "sk")
    elapsed = time.monotonic() - start

    assert elapsed < 5, "the destroy should have been killed, not left to sleep out its 30s"
    assert result.ok is False
    tail = " ".join(result.tail)
    assert "timed out" in tail
    assert "backing" in tail, tail
    assert "odin apply" in tail, tail  # the documented recovery, named
    assert runner.status("default")["running"] is False


async def test_the_destroy_budget_covers_init_too(tmp_path, monkeypatch):
    """`_init_then` runs `init` FIRST with its own full budget, so a
    per-phase-only bound made the worst case 2x the number anyone was told.
    The destroy budget is a single deadline across both phases."""
    _write_fake_tofu(tmp_path, _INIT_WEDGED)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    (tmp_path / "default" / "tf").mkdir(parents=True)
    runner = TfRunner(tmp_path, timeout=30, destroy_timeout=0.3)

    start = time.monotonic()
    result = await runner.destroy("default", 4266, "ak", "sk")
    elapsed = time.monotonic() - start

    assert elapsed < 5, "init must be bounded by the destroy budget, not the apply timeout"
    assert result.ok is False


async def test_a_normal_destroy_is_unaffected_by_the_destroy_budget(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _INIT_OK + '\nif [ "$1" = "destroy" ]; then echo "gone"; exit 0; fi')
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    (tmp_path / "default" / "tf").mkdir(parents=True)
    runner = TfRunner(tmp_path, timeout=30, destroy_timeout=30)
    result = await runner.destroy("default", 4266, "ak", "sk")
    assert result.ok is True
    assert result.exit_code == 0
    assert result.timed_out is False


# --- field test 5 (MED): "it timed out" is a claim, so it needs a signal ---


async def test_a_real_deadline_kill_sets_timed_out(tmp_path, monkeypatch):
    """The runner is the only frame that KNOWS -- it is the thing that sent the
    signal -- so it is the frame that reports it. Against a real subprocess
    really killed by `_run`'s own timeout branch, not a fabricated result."""
    _write_fake_tofu(tmp_path, _DESTROY_WEDGED)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    (tmp_path / "default" / "tf").mkdir(parents=True)
    runner = TfRunner(tmp_path, timeout=30, destroy_timeout=0.3)
    result = await runner.destroy("default", 4266, "ak", "sk")
    assert result.timed_out is True
    assert result.exit_code < 0


async def test_an_external_kill_is_a_negative_exit_code_but_not_a_timeout(tmp_path, monkeypatch):
    """The proxy the route used to infer a timeout from -- `exit_code < 0` --
    is ambiguous, and this is the ambiguity, produced for real: tofu kills
    ITSELF with SIGKILL, so the result is exit -9 with no timeout anywhere near
    it. The field test hit this as an external `kill -9` 0.87s into a destroy
    that was then reported as a 300-SECOND deadline expiry, sending the operator
    to tune `ODIN_TOFU_DESTROY_TIMEOUT` for something unrelated."""
    _write_fake_tofu(
        tmp_path,
        _INIT_OK + '\nif [ "$1" = "destroy" ]; then echo "Destroying..."; kill -9 $$; fi',
    )
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    (tmp_path / "default" / "tf").mkdir(parents=True)
    runner = TfRunner(tmp_path, timeout=30, destroy_timeout=30)
    result = await runner.destroy("default", 4266, "ak", "sk")
    assert result.exit_code == -9, "the ambiguous proxy: an external kill looks exactly like a deadline kill"
    assert result.timed_out is False
    assert not any("timed out" in line for line in result.tail)


# --- security finding #3: tofu's own log output is scrubbed for secrets ---


async def test_secrets_are_scrubbed_from_streamed_line_events(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_LEAKS_A_SECRET)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    ws = RecordingWs()
    runner = TfRunner(tmp_path, ws=ws)

    result = await runner.apply("default", _project(), 4266, "ak", "sk", secrets=frozenset({"hunter2"}))

    assert result.ok is True
    apply_lines = [m["line"] for m in ws.messages if m.get("phase") == "apply" and "line" in m]
    assert apply_lines == ["planning", "password = [REDACTED]"]
    assert not any("hunter2" in line for line in apply_lines)


async def test_secrets_are_scrubbed_from_the_failure_tail(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_LEAKS_A_SECRET_ON_FAILURE)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    ws = RecordingWs()
    runner = TfRunner(tmp_path, ws=ws)

    result = await runner.apply("default", _project(), 4266, "ak", "sk", secrets=frozenset({"hunter2"}))

    assert result.ok is False
    assert "hunter2" not in " ".join(result.tail)
    assert "password = [REDACTED]" in result.tail
    terminal = next(m for m in ws.messages if m.get("phase") == "apply" and "status" in m)
    assert "hunter2" not in " ".join(terminal["tail"])


async def test_no_secrets_given_leaves_output_untouched(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_LEAKS_A_SECRET)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    result = await runner.apply("default", _project(), 4266, "ak", "sk")  # no secrets= given

    assert result.ok is True
    assert result.tail == ("planning", "password = hunter2")


async def test_secrets_are_scrubbed_from_destroy_output_too(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _INIT_OK + '\nif [ "$1" = "destroy" ]; then echo "password = hunter2"; exit 0; fi')
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    # A prior apply is required for destroy to actually run tofu (else it's the no-workspace no-op).
    await runner.apply("default", _project(), 4266, "ak", "sk")
    result = await runner.destroy("default", 4266, "ak", "sk", secrets=frozenset({"hunter2"}))

    assert "hunter2" not in " ".join(result.tail)


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


# --- field test 3: `plan` -- the safe drift check ------------------------
#
# The finding: checking for drift meant hand-running `tofu plan` in
# `.odin/<env>/tf`, where main.tf carries NO endpoint (it is deliberately
# portable) -- so a plan run without `AWS_ENDPOINT_URL` talks to REAL AWS.
# `TfRunner.plan` runs it through the very same machinery apply uses, so the
# endpoint cannot be gotten wrong.

_PLAN_RECORDS_ENV_AND_ARGS = (
    _INIT_OK
    + '\nif [ "$1" = "plan" ]; then echo "$@ | $AWS_ENDPOINT_URL | $AWS_ACCESS_KEY_ID" >> "$RECORDED_ARGS_FILE"; exit 0; fi'
)
_PLAN_CHANGES_LINE = 'if [ "$1" = "plan" ]; then echo "Plan: 1 to add"; exit 2; fi'
_PLAN_NO_CHANGES_LINE = 'if [ "$1" = "plan" ]; then echo "No changes."; exit 0; fi'
_PLAN_CHANGES = _INIT_OK + "\n" + _PLAN_CHANGES_LINE
_PLAN_ERRORS = _INIT_OK + '\nif [ "$1" = "plan" ]; then echo "Error: no valid credential sources"; exit 1; fi'


async def test_plan_injects_the_gateway_endpoint_and_operator_credentials(tmp_path, monkeypatch):
    """The whole point: a plan through odin can never reach real AWS, because
    the endpoint and credentials are injected exactly as they are for apply."""
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("RECORDED_ARGS_FILE", str(args_file))
    _write_fake_tofu(tmp_path, _PLAN_RECORDS_ENV_AND_ARGS)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path, parallelism=3)

    result = await runner.plan("default", _project(), 4266, "ak", "sk")

    assert result.ok is True
    recorded = args_file.read_text()
    assert "-detailed-exitcode" in recorded
    assert "-parallelism=3" in recorded
    assert "http://127.0.0.1:4266" in recorded
    assert "| ak" in recorded


async def test_plan_with_changes_exits_two_and_is_still_a_successful_run(tmp_path, monkeypatch):
    """`-detailed-exitcode`: 2 means "changes present", NOT a failed run."""
    _write_fake_tofu(tmp_path, _PLAN_CHANGES)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    ws = RecordingWs()
    runner = TfRunner(tmp_path, ws=ws)

    result = await runner.plan("default", _project(), 4266, "ak", "sk")

    assert result.exit_code == 2
    assert result.ok is True
    terminal = next(m for m in ws.messages if m.get("phase") == "plan" and "status" in m)
    assert terminal["status"] == "ok"
    assert terminal["exit_code"] == 2


async def test_plan_error_is_a_failure_with_a_tail(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _PLAN_ERRORS)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    result = await runner.plan("default", _project(), 4266, "ak", "sk")

    assert result.ok is False
    assert result.exit_code == 1
    assert "Error: no valid credential sources" in " ".join(result.tail)


async def test_plan_never_overwrites_the_last_apply_on_status(tmp_path, monkeypatch):
    """A plan is a read: `odin tf status` must keep reporting the last real
    apply/destroy, not a drift check someone ran afterwards."""
    _write_fake_tofu(tmp_path, _APPLY_OK_TWO_LINES + "\n" + _PLAN_CHANGES_LINE)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    await runner.apply("default", _project(), 4266, "ak", "sk")
    after_apply = runner.status("default")["last"]
    await runner.plan("default", _project(), 4266, "ak", "sk")

    assert runner.status("default")["last"] == after_apply


async def test_plan_secrets_are_scrubbed_from_its_output(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _INIT_OK + '\nif [ "$1" = "plan" ]; then echo "password = hunter2"; exit 2; fi')
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    result = await runner.plan("default", _project(), 4266, "ak", "sk", secrets=frozenset({"hunter2"}))

    assert "hunter2" not in " ".join(result.tail)
    assert "password = [REDACTED]" in result.tail


async def test_plan_while_an_apply_is_running_raises_simulate_busy(tmp_path, monkeypatch):
    _write_fake_tofu(tmp_path, _APPLY_SLOW + "\n" + _PLAN_NO_CHANGES_LINE)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    runner = TfRunner(tmp_path)

    first = asyncio.create_task(runner.apply("default", _project(), 4266, "ak", "sk"))
    await asyncio.sleep(0.1)

    with pytest.raises(SimulateBusy):
        await runner.plan("default", _project(), 4266, "ak", "sk")

    assert (await first).ok is True


async def test_plan_raises_tofu_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    runner = TfRunner(tmp_path)
    with pytest.raises(TofuNotInstalled):
        await runner.plan("default", _project(), 4266, "ak", "sk")


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
