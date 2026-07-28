"""`ODIN_AI=0` — the one switch that turns OFF every model call odin can make.

Two features can reach a model, each with its own flag pointing a different way
(`ODIN_TRANSLATE_REFINE` opt-in, `ODIN_DEBUG_AGENT` opt-out). These tests pin
that one switch covers both, that it also covers the path with NO per-feature
gate at all (`translate(stack)` with no cache), that nothing is constructed or
awaited when it is off, and that the deterministic compiler is completely
unaffected.

`_NeverConstructed` is the load-bearing double: it raises from `__init__`, so
any test that passes it and still gets an answer proves no SDK client was ever
built -- no `claude` process, nothing to dial, nothing to hang on.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from odin.agent import ai
from odin.agent import debugger as debugger_mod
from odin.agent import translate as translate_mod
from odin.agent.hcl import generate_tf
from odin.agent.translate import translate
from odin.spec.models import ResourceDesired, Stack
from odin.spec.store import rev_of

STACK = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
CONTEXT = {"env": "default", "nodes": [{"id": "uploads", "kind": "s3"}]}


class _NeverConstructed:
    def __init__(self, options) -> None:
        raise AssertionError("no SDK client may be constructed while ODIN_AI is off")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ODIN_AI", "ODIN_TRANSLATE_REFINE", "ODIN_DEBUG_AGENT"):
        monkeypatch.delenv(name, raising=False)


# --- the value table -------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_an_explicit_on_allows_model_calls(monkeypatch, value):
    """`""` and `"  "` left this list when the default flipped to off: an unset
    (or blank) variable now defers to the UI switch, which the test below covers.
    An explicit truthy value still forces model calls on regardless."""
    monkeypatch.setenv("ODIN_AI", value)
    assert ai.off_reason() is None, value


def test_unset_means_OFF_until_the_switch_is_turned_on(monkeypatch, tmp_path):
    """Owner decision, 2026-07-28: odin ships with model calls off.

    This asserted the opposite until then. A tool that phones a model the first
    time you press a button, without being asked, is not a default anyone chose
    -- so an unset `ODIN_AI` now defers to the UI switch, which starts off.
    """
    monkeypatch.delenv("ODIN_AI", raising=False)
    monkeypatch.setattr(ai, "STATE_FILE", tmp_path / "ai.json")

    reason = ai.off_reason()
    assert reason is not None
    assert "switch is off" in reason
    assert "ODIN_AI=1" in reason, "and it should name the way to override it without the UI"

    ai.set_runtime_enabled(True)
    assert ai.off_reason() is None

    ai.set_runtime_enabled(False)
    assert ai.off_reason() is not None


def test_an_explicitly_set_env_var_beats_the_switch(monkeypatch, tmp_path):
    """`ODIN_AI` is the ops override a CI job and `ODIN_AI=0 odin apply` rely on.
    A preference file that could silently overrule it would make the flag a
    suggestion."""
    monkeypatch.setattr(ai, "STATE_FILE", tmp_path / "ai.json")
    ai.set_runtime_enabled(True)
    monkeypatch.setenv("ODIN_AI", "0")
    assert ai.off_reason() is not None, "ODIN_AI=0 must win over a switch that is on"

    ai.set_runtime_enabled(False)
    monkeypatch.setenv("ODIN_AI", "1")
    assert ai.off_reason() is None, "ODIN_AI=1 must win over a switch that is off"


def test_the_switch_survives_a_restart(monkeypatch, tmp_path):
    """Held on disk, because a preference that reset every time the server
    restarted would be a nag -- odin restarts often during development."""
    monkeypatch.delenv("ODIN_AI", raising=False)
    monkeypatch.setattr(ai, "STATE_FILE", tmp_path / "ai.json")
    ai.set_runtime_enabled(True)
    assert ai.runtime_enabled() is True  # a fresh read of the file, no cache


def test_an_unreadable_state_file_reads_as_OFF(monkeypatch, tmp_path):
    """Fail closed. A corrupt preference must not enable model calls."""
    state = tmp_path / "ai.json"
    state.write_text("{not json")
    monkeypatch.setattr(ai, "STATE_FILE", state)
    assert ai.runtime_enabled() is False


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "OFF"])
def test_explicit_falsy_values_turn_every_model_call_off(monkeypatch, value):
    monkeypatch.setenv("ODIN_AI", value)
    reason = ai.off_reason()
    assert reason is not None and "ODIN_AI" in reason


def test_an_unrecognised_value_disables_calls_loudly_rather_than_silently_allowing_them(monkeypatch, caplog):
    """The direction that matters. `ODIN_REAP_EC2_VMS` guards a destructive
    reaper, so anything it does not recognise leaves the safety net ON; here
    the safety net IS "do not call a model", so `ODIN_AI=fasle` must not
    quietly leave the calls enabled -- it stops them and says why."""
    monkeypatch.setenv("ODIN_AI", "fasle")
    with caplog.at_level(logging.WARNING, logger="odin.agent"):
        reason = ai.off_reason()
    assert reason is not None and "fasle" in reason and "not a recognised value" in reason
    assert any("not a value I recognise" in record.getMessage() for record in caplog.records)


def test_refuse_if_off_raises_before_anything_is_built(monkeypatch):
    monkeypatch.setenv("ODIN_AI", "0")
    with pytest.raises(ai.AiDisabled) as raised:
        ai.refuse_if_off()
    assert "ODIN_AI" in str(raised.value)


def test_refuse_if_off_is_a_no_op_when_the_switch_is_on(monkeypatch):
    monkeypatch.setenv("ODIN_AI", "1")
    ai.refuse_if_off()  # must not raise


# --- it wins over both per-feature flags -----------------------------------


def test_it_overrides_the_refine_opt_in(monkeypatch):
    # The baseline is explicit since the default flipped to off: the point here
    # is that ODIN_AI=0 beats a feature's OWN opt-in, which needs the feature to
    # be genuinely on first.
    monkeypatch.setenv("ODIN_AI", "1")
    monkeypatch.setenv("ODIN_TRANSLATE_REFINE", "1")
    assert translate_mod.refine_enabled() is True
    monkeypatch.setenv("ODIN_AI", "0")
    assert translate_mod.refine_enabled() is False


def test_it_overrides_the_debug_agents_on_by_default(monkeypatch):
    monkeypatch.setenv("ODIN_AI", "1")
    assert debugger_mod.enabled() is True  # M8 is ON once model calls are allowed
    monkeypatch.setenv("ODIN_AI", "0")
    assert debugger_mod.enabled() is False


def test_the_debug_agent_names_the_switch_that_actually_stopped_it(monkeypatch):
    """Naming `ODIN_DEBUG_AGENT` while `ODIN_AI=0` is what really held it back
    would send the user to a flag that changes nothing."""
    monkeypatch.setenv("ODIN_AI", "0")
    monkeypatch.setenv("ODIN_DEBUG_AGENT", "1")
    reason = debugger_mod.disabled_reason()
    assert "ODIN_AI" in reason and "ODIN_DEBUG_AGENT" not in reason


# --- nothing is attempted, on any path -------------------------------------


async def test_the_uncached_translate_path_makes_no_call_either(monkeypatch):
    """The gap this switch closes structurally: `translate()` with no cache runs
    the SDK pass with NO `refine_enabled()` check at all. `ODIN_AI=0` still has
    to stop it, which is why the refusal lives at the client boundary."""
    monkeypatch.setenv("ODIN_AI", "0")
    result = await translate(STACK, client_cls=_NeverConstructed)  # cache=None
    assert result.refined is False
    assert result.files == generate_tf(STACK).files
    assert "ODIN_AI" in " ".join(result.notes)


async def test_the_cached_path_starts_no_background_task(monkeypatch):
    monkeypatch.setenv("ODIN_AI", "0")
    monkeypatch.setenv("ODIN_TRANSLATE_REFINE", "1")  # opted in, and still no call
    cache = translate_mod.TranslateCache()
    result = await translate(STACK, client_cls=_NeverConstructed, cache=cache)
    assert result.refined is False
    assert "ODIN_AI" in " ".join(result.notes)
    assert cache._tasks == {} and not cache._refining(rev_of(STACK))


async def test_the_debug_agent_answers_its_honest_unavailable_without_calling_anything(monkeypatch):
    """The existing "agent unavailable" story, reused -- and it comes back
    immediately rather than after the 90s timeout, because nothing is awaited."""
    monkeypatch.setenv("ODIN_AI", "0")
    answer = await asyncio.wait_for(
        # the COROUTINE, not an awaited result -- awaiting it here would run
        # `diagnose` to completion before `wait_for` ever saw it, making the
        # 5s bound (this test's whole point) vacuous.
        debugger_mod.diagnose(CONTEXT, "what's wrong here?", client_cls=_NeverConstructed),
        timeout=5.0,
    )
    assert "ODIN_AI" in answer["answer"]
    assert answer["suspects"] == []


# --- and the deterministic compiler is untouched ---------------------------


@pytest.mark.parametrize("kind", ["s3", "sqs", "sns", "dynamodb", "rds", "vpc", "lambda", "ecs"])
async def test_the_canvas_to_terraform_compiler_is_identical_with_ai_off(monkeypatch, kind):
    """The reassuring fact, pinned per kind: canvas -> Terraform is a
    deterministic compiler (`agent/hcl.py`), so `ODIN_AI=0` changes NOTHING
    about what gets applied."""
    stack = Stack(resources=(ResourceDesired(id="node", kind=kind),))
    monkeypatch.setenv("ODIN_AI", "0")
    off = await translate(stack, client_cls=_NeverConstructed)
    assert off.files == generate_tf(stack).files
    assert off.binary_files == generate_tf(stack).binary_files
    assert off.unsupported == generate_tf(stack).unsupported
    assert off.wiring_errors == generate_tf(stack).wiring_errors
