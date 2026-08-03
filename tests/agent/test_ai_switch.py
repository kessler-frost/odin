"""`ODIN_AI=0` — the one switch that turns OFF every model call odin can make.

THREE features can reach a model, and they do not share a shape: the refine pass
(`ODIN_TRANSLATE_REFINE`, opt-in), the debug agent (`ODIN_DEBUG_AGENT`,
opt-out), and canvas chat (NO per-feature flag at all). These tests pin that one
switch covers all three, that it also covers the path with NO per-feature gate
(`translate(stack)` with no cache), that nothing is constructed or awaited when
it is off, and that the deterministic compiler is completely unaffected.

The count is now PINNED rather than asserted in prose (`test_the_inventory_of
_model_reaching_modules_is_exactly_the_documented_three`). This docstring and
`agent/ai.py`'s both said "two features" for the whole life of the chat
feature -- the switch was always right, the inventory was not, and a prose
inventory has gone stale in this repo twice before.

`_NeverConstructed` is the load-bearing double: it raises from `__init__`, so
any test that passes it and still gets an answer proves no SDK client was ever
built -- no `claude` process, nothing to dial, nothing to hang on.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import re
from pathlib import Path

import pytest

from odin.agent import ai
from odin.agent import chat as chat_mod
from odin.agent import debugger as debugger_mod
from odin.agent import translate as translate_mod
from odin.iac.hcl import generate_tf
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
    deterministic compiler (`iac/hcl.py`), so `ODIN_AI=0` changes NOTHING
    about what gets applied."""
    stack = Stack(resources=(ResourceDesired(id="node", kind=kind),))
    monkeypatch.setenv("ODIN_AI", "0")
    off = await translate(stack, client_cls=_NeverConstructed)
    assert off.files == generate_tf(stack).files
    assert off.binary_files == generate_tf(stack).binary_files
    assert off.unsupported == generate_tf(stack).unsupported
    assert off.wiring_errors == generate_tf(stack).wiring_errors


# --- the inventory, pinned rather than described ----------------------------
#
# `agent/ai.py`'s docstring and this file's both said "exactly two features can
# talk to a model" for the whole life of the chat feature. The SWITCH was never
# wrong -- `chat.propose` checks `off_reason()` before constructing a client --
# but a reader auditing "what can call out?" was handed a list with a hole in
# it. So the list is derived from the source now.

_SRC = Path(__file__).resolve().parents[2] / "src" / "odin"

# module -> the flag that gates it BEFORE `ODIN_AI` gets a say. `None` means
# there is none, which is a real property of chat rather than an oversight: the
# whole argument for one master switch is that the per-feature flags do not
# share a shape.
MODEL_REACHING: dict[str, str | None] = {
    "agent/translate.py": "ODIN_TRANSLATE_REFINE",
    "agent/debugger.py": "ODIN_DEBUG_AGENT",
    "agent/chat.py": None,
}


def _imports_the_sdk(path: Path) -> bool:
    """A real `import claude_agent_sdk`, found by AST rather than by substring.

    Substring-matching the module NAME is the same defect as the guard this
    file's siblings just lost (`test_policies_from_applied_iam.py`): it counts
    any mention, so `agent/ai.py` naming the SDK in a docstring registered as a
    fourth caller. An `ast.Import`/`ast.ImportFrom` node cannot be a comment.
    """
    return any(
        (isinstance(node, ast.Import) and any(a.name.split(".")[0] == "claude_agent_sdk" for a in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "claude_agent_sdk")
        for node in ast.walk(ast.parse(path.read_text()))
    )


def test_the_inventory_of_model_reaching_modules_is_exactly_the_documented_three():
    """Every module that imports `claude_agent_sdk` can spawn the real `claude`
    CLI. Adding a fourth without listing it here fails, which is the point: the
    prose said two while there were three."""
    importers = {str(path.relative_to(_SRC)) for path in _SRC.rglob("*.py") if _imports_the_sdk(path)}
    assert importers == set(MODEL_REACHING), (
        "the set of modules that can reach a model changed. Update MODEL_REACHING "
        "here, `agent/ai.py`'s docstring and this file's -- and make sure the new "
        f"one is gated: {sorted(importers ^ set(MODEL_REACHING))}"
    )


@pytest.mark.parametrize("module,flag", sorted(MODEL_REACHING.items()))
def test_each_features_own_flag_is_where_the_inventory_says_it_is(module, flag):
    """Pins the SHAPE the master switch exists because of: two per-feature flags
    pointing opposite ways, and one feature with none at all."""
    source = (_SRC / module).read_text()
    if flag is not None:
        assert flag in source, f"{module} no longer names {flag}"
        return
    assert not re.search(r'os\.environ\.get\(\s*"ODIN_\w*(REFINE|AGENT)"', source), (
        f"{module} grew a per-feature flag -- record it in MODEL_REACHING"
    )


async def test_chat_constructs_no_client_when_the_switch_is_off(monkeypatch, tmp_path):
    """The third feature, behaviourally -- the half the stale inventory hid.

    `_NeverConstructed` raises from `__init__`, so getting an answer at all
    proves no SDK client was built: no `claude` process, nothing to dial. The
    note names the switch, so the user is told WHY rather than handed a silent
    empty proposal.
    """
    monkeypatch.setenv("ODIN_AI", "0")
    monkeypatch.setattr(ai, "STATE_FILE", tmp_path / "ai.json")
    canvas = {"nodes": [{"id": "n1", "type": "s3", "data": {"label": "uploads"}}], "edges": []}
    result = await asyncio.wait_for(
        chat_mod.propose(canvas, "add a queue", client_cls=_NeverConstructed), timeout=5.0,
    )
    assert result.canvas == canvas
    assert result.changes == []
    assert "ODIN_AI" in result.note


# --- the one-caller invariant, which was PROSE -------------------------------
#
# `agent/chat.py`'s comment argues that chat needs no `ai.refuse_if_off()`
# because `propose`'s `disabled_reason()` gate covers every path that can reach
# the SDK -- and that argument holds "for one reason only: `_run_agent` has
# exactly ONE caller". That is a load-bearing claim kept in a comment, which in
# this repo is a claim that goes stale: the module-level inventory two functions
# up said "exactly two" while there were three, and the thread inventory in
# CLAUDE.md described a state that had not existed for a week. Prose cannot fail
# a build, so the invariant is pinned here instead.
#
# Counted by AST for the same reason `_imports_the_sdk` is: a substring count of
# "_run_agent" scores its own definition and the three comment lines that
# discuss it, which is how you get a guard that passes at four callers.


def _call_sites(path: Path, name: str) -> list[int]:
    """Line numbers where `name` is CALLED -- not defined, not mentioned."""
    tree = ast.parse(path.read_text())
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    )


def test_run_agent_still_has_exactly_one_caller_or_chat_needs_the_boundary_check():
    """The safety argument for chat having no `refuse_if_off` is reachability.

    A second caller can reach `ClaudeSDKClient` without passing `propose`'s
    `disabled_reason()`, which turns `ODIN_AI=0` from a guarantee into a
    coincidence. If this fails, the fix is NOT to update the number: it is to
    call `ai.refuse_if_off()` at the top of `_run_agent` and delete the comment's
    one-caller clause.
    """
    callers = _call_sites(_SRC / "agent/chat.py", "_run_agent")
    assert len(callers) == 1, (
        f"_run_agent now has {len(callers)} call sites (lines {callers}). The comment in "
        "agent/chat.py above the SDK half says one caller is what makes the missing "
        "ai.refuse_if_off() safe -- add the boundary check rather than editing this number."
    )


def test_the_one_caller_is_reached_through_the_gate_that_is_claimed_to_cover_it():
    """Names the gate, so deleting it fails here rather than silently."""
    source = (_SRC / "agent/chat.py").read_text()
    assert "disabled_reason()" in source, (
        "agent/chat.py no longer calls disabled_reason() -- that IS chat's ODIN_AI gate"
    )
