"""`ODIN_AI=0` — the one switch that turns OFF every model call odin can make.

WHY IT IS ONE SWITCH. odin has THREE features that can talk to a model, and
they do NOT have a uniform shape -- which is the whole argument:

  * the canvas→Terraform refine pass (`agent/translate.py`) --
    `ODIN_TRANSLATE_REFINE`, opt-IN, off by default;
  * "what's wrong here?" (`agent/debugger.py`) -- `ODIN_DEBUG_AGENT`,
    opt-OUT, ON by default;
  * canvas chat (`agent/chat.py`) -- NO per-feature flag at all; its
    `disabled_reason()` is `ai.off_reason()` and nothing else.

All three go through `claude-agent-sdk`, which spawns the real `claude` CLI. A
user who wants "no model calls, ever" would have had to know two flags, know
which way each one points, notice that the third feature has none, and keep
knowing as features are added. That is not a switch, it is a research project --
so this one sits UNDER all of them, and under the SDK boundary itself
(`refuse_if_off`), which is what makes the flagless third feature safe.

This paragraph said "exactly two" until v0.8.18, after chat had shipped. The
SWITCH was never wrong -- `chat.propose` checks `off_reason()` before
constructing a client -- only the inventory, and an inventory in prose has gone
stale in this repo twice. `tests/agent/test_ai_switch.py` now pins it -- by
finding every module that imports `claude_agent_sdk` and by proving no client is
CONSTRUCTED for any of the three when the switch is off, the way
`tests/test_thread_inventory.py` pins the thread list. A prose inventory cannot
fail a build.

WHAT IT IS NOT. It does not touch the canvas↔Terraform translation, which is a
deterministic compiler (`iac/hcl.py`): with `ODIN_AI=0` a canvas still
compiles to the same Terraform, `tofu apply` still runs, IAM edges are still
enforced, and every substrate still works. The refine pass is optional
decoration over an already-correct translation -- turning it off costs
comments and tags, never correctness. Nothing else in odin has ever asked a
model anything (audited: no `api.anthropic.com`, no OpenAI, no local
inference endpoint, no `ANTHROPIC_*` read anywhere in src/).

THE VALUE RULES, and why they are not `ODIN_REAP_EC2_VMS`'s exactly. That flag
guards a destructive reaper, so only explicit false-y values may disable it and
anything else keeps the safety net ON. Here the safety net IS the disabled
state -- "do not call a model" -- so the same PRINCIPLE (a typo must not
silently disarm what the user asked for) points the other way:

  * unset, or empty              -> ON  (odin's default behaviour, unchanged)
  * `1` / `true` / `yes` / `on`  -> ON  (explicitly)
  * `0` / `false` / `no` / `off` -> OFF
  * anything else                -> OFF, loudly, naming the value

So `ODIN_AI=fasle` does NOT quietly leave the model calls enabled; it stops
them and says it did not understand, which is the direction that cannot
surprise anyone who set it on purpose. Nothing is cached at import -- read
fresh on every call, the same convention `refine_enabled` and
`debugger.enabled` already follow, so a flip takes effect on the next call
with no restart.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from odin.settings import AiSettings, env_name, settings
from odin.util import atomic_write_text

log = logging.getLogger("odin.agent")

# The name, from the one place that owns it (`odin/settings.py`), because every
# sentence below quotes it back at the user -- a message naming a variable the
# reader does not honour is the exact failure `env_name` exists to prevent.
ENV_VAR = env_name(AiSettings, "enabled")
_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")


# Where the UI switch's answer lives. The env var stays the authoritative ops
# override; this is the product-facing preference underneath it.
STATE_FILE = Path(".odin") / "ai.json"


def runtime_enabled() -> bool:
    """Has someone turned the AI on in the UI? OFF until they do.

    Owner decision, 2026-07-28: odin ships with model calls OFF and a switch in
    the top bar. A tool that phones a model the first time you press a button,
    without being asked, is not a default anyone chose.

    Persisted rather than held in memory: a preference that reset on every
    server restart would be a nag, and odin restarts often during development.
    """
    try:
        return bool(json.loads(STATE_FILE.read_text()).get("enabled", False))
    except (OSError, ValueError):
        return False


def set_runtime_enabled(enabled: bool) -> bool:
    """Turn model calls on or off, and return what it now is."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_FILE, json.dumps({"enabled": bool(enabled)}), mode=0o600)
    return bool(enabled)


def off_reason() -> str | None:
    """Why no model call may happen, or None if they are allowed.

    A sentence, not a bool, because every degradation path has to be able to
    SAY why -- `POST /agent/debug`'s answer, `/translate`'s notes and the log
    line all quote this.

    Precedence: an explicitly SET `ODIN_AI` wins, because it is the ops switch a
    CI job or a `ODIN_AI=0 odin apply` relies on and it must not be silently
    overridden by a preference file. With it unset, the UI switch decides, and
    its default is off.

    The value is a `str` in `AiSettings`, not a `bool`, and this function is
    why: FOUR outcomes come out of it, not two, and the fourth (an
    unrecognised value -> no model calls, plus a warning naming the value) is
    the one a pydantic `bool` would turn into a startup crash instead.
    """
    raw = settings.ai.enabled.strip()
    value = raw.lower()
    if value in _OFF:
        return f"{ENV_VAR}={raw} — every model call is disabled (unset it, or set {ENV_VAR}=1, to allow them)"
    if value in _ON:
        return None
    if value == "":
        if runtime_enabled():
            return None
        return (
            "the AI switch is off — turn it on in the top bar, or set "
            f"{ENV_VAR}=1 to allow model calls without it"
        )
    log.warning(
        "%s=%r is not a value I recognise, so odin is making NO model calls at all. Use %s=0 "
        "(or false/no/off) to disable them deliberately, or %s=1 to allow them.",
        ENV_VAR, raw, ENV_VAR, ENV_VAR,
    )
    return (
        f"{ENV_VAR}={raw} is not a recognised value, so odin is making no model calls at all "
        f"(use {ENV_VAR}=0 to disable them deliberately, or {ENV_VAR}=1 to allow them)"
    )


class AiDisabled(RuntimeError):
    """Raised at the SDK boundary when a model call was attempted with the
    switch off."""


def refuse_if_off() -> None:
    """The BOUNDARY check, at the one place each agent constructs a client.

    The per-feature gates above it are what make the switch quiet and honest;
    this is what makes it complete. `translate()` has an uncached path that
    runs the SDK pass with NO `refine_enabled()` check at all (a unit-test
    seam today, one forgotten `cache=` away from being a production caller),
    and any future agent will have the same shape unless the refusal lives
    where the client is built. Checked BEFORE `create_sdk_mcp_server` and
    before the client exists, so nothing is spawned, nothing dials out, and
    nothing can hang waiting."""
    reason = off_reason()
    if reason is not None:
        raise AiDisabled(reason)
