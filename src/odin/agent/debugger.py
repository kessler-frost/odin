"""W2.9 / M8 -- "what's wrong here?": the one job the deterministic path
genuinely cannot do.

Everything else the agent layer touches is deterministic on purpose
(`agent/hcl.py` canvas->TF, `agent/import_tf.py` TF->canvas, with the refine
pass off by default and structurally unable to change what gets applied). This
module is the opposite case: given a region of the canvas, EXPLAIN in plain
English why it is broken. There is no deterministic function from (exit code,
StateReason, a Postgres connection error, 40 lines of container stdout) to
"your ECS task can't reach the database because the DATABASE_URL you wired
points at a node that never became healthy" -- that's a judgement over
evidence, and it's what the AI dependency is actually for.

Two halves, deliberately split:

1. `assemble_context` -- PURE. (stack, world, events, a logs callable,
   node_ids) -> a small JSON-able dict. No I/O, no SDK, fully testable, and
   the ONLY place that decides what the model is allowed to see. Two rules
   are load-bearing:
   - **Secrecy.** Env-var VALUES never enter the context (key names only), and
     any `FieldValue.sensitive` field (v0.6.0) is `[REDACTED]`. On top of
     that, every string in the assembled tree -- facts, verdicts, event text,
     the log tail -- is `scrub()`ed against `Stack.sensitive_values()`, because
     a real secret rides out on those surfaces too (an rds node's `facts`
     carry the full `DATABASE_URL`, password included, and a container is free
     to echo its own env into stdout). See tests/agent/test_debugger.py's leak
     test, the analogue of test_translate.py's own.
   - **Caps.** ~40 log lines and 10 events per node, 20 nodes, and every other
     string clipped -- the context has to stay small enough to be one cheap
     prompt, not a log dump.

2. `diagnose` -- ONE agent run whose only effect channel is the typed
   `report_diagnosis` tool (the house pattern from `agent/translate.py`: the
   agent NEVER writes files or state, it returns structured output odin
   materializes). Best-effort by design: no SDK, no credentials, a timeout, or
   any exception at all answers `{"answer": "agent unavailable", "suspects":
   []}` and the route returns 200 anyway -- the same discipline translate's
   fallback keeps.

Unlike translate's refine pass this is ON by default (`ODIN_DEBUG_AGENT=0`
turns it off). It has no drift risk to guard against: it only READS state and
returns prose, so the worst a bad answer costs is a wrong hunch, never a wrong
apply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any, TypedDict

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SdkMcpTool, create_sdk_mcp_server, tool

from odin.spec.models import REDACTED, FieldValue, Stack, World, scrub

log = logging.getLogger("odin.debugger")

_MODEL = "claude-sonnet-5"

# The caps. Each one is a real bound on the prompt, not a stylistic choice:
# a crash-looping container can emit thousands of log lines, an env's event
# log grows forever, and an ec2 node's `userData` or a lambda's `code` field
# is arbitrarily long.
MAX_NODES = 20
MAX_EVENTS = 10
MAX_LOG_LINES = 40
MAX_VALUE_CHARS = 200

_TRUNCATED = "... (truncated)"

# The honest answer whenever the agent didn't produce one -- SDK absent, no
# credentials, timeout, exception, or a run that never called the tool. Callers
# copy it (`dict(UNAVAILABLE)`) rather than returning the module-level object.
UNAVAILABLE: dict[str, Any] = {"answer": "agent unavailable", "suspects": []}
_DISABLED = "the failure-explanation agent is off (unset ODIN_DEBUG_AGENT to enable)"


def enabled() -> bool:
    """ON by default -- the deliberate difference from
    `translate.refine_enabled()`. That pass is optional decoration over an
    already-correct deterministic translation, so it opts IN; this one is the
    whole feature, and it cannot corrupt anything (read-only + prose out), so
    it opts OUT. `ODIN_DEBUG_AGENT=0` (or false/no/off) disables it. Read
    fresh on every call, same convention as translate's own env reads."""
    return os.environ.get("ODIN_DEBUG_AGENT", "1").strip().lower() not in ("0", "false", "no", "off")


def _default_timeout() -> float:
    """This pass IS on the request's critical path (a human is waiting on the
    answer), unlike translate's background refine -- so the budget is a UI
    budget, not a batch one. `ODIN_DEBUG_TIMEOUT` overrides it."""
    return float(os.environ.get("ODIN_DEBUG_TIMEOUT", "60"))


# --- 1. the context assembler (pure) -----------------------------------------


def _clip(value: Any) -> Any:
    """Bound one scalar. Non-strings (a port number, a restart count, a bool)
    pass through untouched -- they can't be unbounded."""
    if not isinstance(value, str) or len(value) <= MAX_VALUE_CHARS:
        return value
    return value[:MAX_VALUE_CHARS] + _TRUNCATED


def _sanitize(value: Any, secrets: frozenset[str]) -> Any:
    """One deep walk that enforces both invariants on every string in the
    tree: clipped to `MAX_VALUE_CHARS`, then scrubbed of every known-sensitive
    raw value. Applied to the whole assembled node record (desired fields,
    observed facts, verdicts, events) so no new surface can be added later
    that silently skips redaction."""
    if isinstance(value, str):
        return scrub(_clip(value), secrets)
    if isinstance(value, dict):
        return {str(k): _sanitize(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, secrets) for v in value]
    return value


def _field(fv: FieldValue) -> dict[str, Any]:
    """One desired field, as the model may see it.

    A `sensitive` field (v0.6.0's `FieldValue.sensitive`, defaulted from the
    field NAME by `is_sensitive_field_name`) shows as `[REDACTED]`; a
    dict-valued field is an env/variable block, so only its KEY NAMES go in --
    knowing that `DATABASE_URL` is set is what the diagnosis needs, its value
    never is. Every other value goes in as-is (clipped by `_sanitize`)."""
    if fv.sensitive:
        return {"value": REDACTED, "provenance": fv.provenance}
    if isinstance(fv.value, dict):
        return {"keys": sorted(str(k) for k in fv.value), "provenance": fv.provenance}
    return {"value": fv.value, "provenance": fv.provenance}


def _desired(stack: Stack, node_id: str) -> dict[str, Any] | None:
    resource = next((r for r in stack.resources if r.id == node_id), None)
    if resource is None:
        return None
    return {"kind": resource.kind, "fields": {name: _field(fv) for name, fv in resource.fields.items()}}


def _refs(stack: Stack, node_id: str) -> list[dict[str, str]]:
    resource = next((r for r in stack.resources if r.id == node_id), None)
    if resource is None:
        return []
    return [{"var": r.var, "target_id": r.target_id, "target_attr": r.target_attr} for r in resource.refs]


def _observed(world: World, node_id: str) -> dict[str, Any] | None:
    observed = world.get(node_id)
    if observed is None:
        return None
    return {
        "phase": observed.phase, "facts": dict(observed.facts),
        "verdict": observed.verdict, "restarts": observed.restarts,
    }


def _event_node(event: dict) -> str | None:
    """Which node an event belongs to. `world_delta`/`access_denied` carry
    `resource_id`; the crash `log` message the reconciler pushes carries the
    node in `source` (api/ws.py + reconciler.py's `_log_message`). An env-wide
    event (a `tf` apply line) belongs to no node and is deliberately left out
    -- per-node caps are what keep this context small."""
    node = event.get("resource_id") or event.get("source")
    return node if isinstance(node, str) else None


def _events_for(events: list[dict], node_id: str) -> list[dict]:
    return [e for e in events if _event_node(e) == node_id][-MAX_EVENTS:]


def _log_tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-MAX_LOG_LINES:])


def assemble_context(
    stack: Stack, world: World, events: list[dict], logs: Callable[[str], str], node_ids: list[str],
) -> dict[str, Any]:
    """The pure half. `logs(node_id) -> str` is the caller's resolver (in
    production `api/debug.py` wraps the wave-1 `/logs` per-kind resolution,
    which already knows how to find an ec2 VM's journal, an ecs service's task
    containers, a lambda's RIE container, or an rds/backing container).

    A selected node absent from the Stack still gets a record, with
    `desired: None` -- the canvas can select a stale tile whose node was
    already removed, and its last observed state + logs are exactly what
    explains where it went. Beyond `MAX_NODES` the extra ids are named in
    `omitted_nodes` rather than silently dropped."""
    secrets = stack.sensitive_values()
    unique = list(dict.fromkeys(node_ids))
    selected, omitted = unique[:MAX_NODES], unique[MAX_NODES:]
    nodes = {
        node_id: {
            **_sanitize({
                "desired": _desired(stack, node_id),
                "refs": _refs(stack, node_id),
                "observed": _observed(world, node_id),
                "events": _events_for(events, node_id),
            }, secrets),
            # Logs are line-capped rather than char-clipped (40 lines of real
            # stdout IS the evidence), so they're scrubbed on their own.
            "logs": scrub(_log_tail(logs(node_id)), secrets),
        }
        for node_id in selected
    }
    return {"env": stack.env, "nodes": nodes, "omitted_nodes": omitted}


# --- 2. the diagnosis (one agent run, one typed effect channel) ---------------


class SuspectInput(TypedDict):
    node_id: str
    reason: str


class ReportDiagnosisInput(TypedDict):
    answer: str
    suspects: list[SuspectInput]


_SYSTEM = (
    "You explain infrastructure failures to the person who drew the architecture. "
    "You are given odin's own evidence for a few selected canvas nodes: each node's "
    "desired configuration, its references to other nodes, its observed phase, facts "
    "and crash verdict, its recent events, and a tail of its real logs. "
    "Answer in plain English, in a few sentences, and ground every claim in that "
    "evidence -- quote the exit code, the verdict, or the log line that shows it. "
    "If the evidence does not explain the failure, say so plainly instead of "
    "guessing. Secrets and env-var values are redacted by design; never ask for them "
    "and never treat their absence as the problem. Name as suspects only nodes that "
    "appear in the evidence. Call report_diagnosis exactly once."
)


def make_report_tool(collector: list[dict]) -> SdkMcpTool:
    """The ONE typed tool the agent may call -- the only effect channel out of
    the run, exactly as `translate.make_emit_tool` is for the refine pass.
    `collector` accumulates each call's raw args."""

    @tool("report_diagnosis", "Report the plain-English diagnosis and the per-node suspects.", ReportDiagnosisInput)
    async def report_diagnosis(args: ReportDiagnosisInput) -> dict:
        collector.append(dict(args))
        return {"content": [{"type": "text", "text": "recorded"}]}

    return report_diagnosis


def _prompt(context: dict, question: str) -> str:
    return (
        f"Question: {question}\n\n"
        "Evidence (odin's desired + observed state for the selected canvas nodes; "
        "secret values and env-var values are redacted, key names only):\n"
        f"```json\n{json.dumps(context, indent=2, default=str)}\n```\n\n"
        "Explain what is wrong here, then call report_diagnosis exactly once."
    )


async def _run_agent(prompt: str, options: ClaudeAgentOptions, client_cls: type) -> None:
    async with client_cls(options=options) as client:
        await client.query(prompt)
        async for _ in client.receive_response():
            pass


def normalize_suspects(raw: Any) -> list[dict[str, str]]:
    """Coerce whatever the tool reported into the response shape. The values
    are the agent's own, verbatim -- this only guarantees the two keys exist
    as strings so the route's response model can never fail to serialize."""
    if not isinstance(raw, list):
        return []
    return [
        {"node_id": str(item.get("node_id", "")), "reason": str(item.get("reason", ""))}
        for item in raw if isinstance(item, dict)
    ]


async def diagnose(
    context: dict, question: str, client_cls: type = ClaudeSDKClient, timeout: float | None = None,
) -> dict[str, Any]:
    """One agent run over `context`; returns `{"answer": str, "suspects":
    [{"node_id", "reason"}]}`.

    `client_cls` is the same `ClaudeSDKClient`-shaped seam translate uses, so
    tests drive the REAL MCP tool dispatch with canned args instead of
    spawning the Claude Code CLI. EVERY failure mode -- disabled, SDK missing,
    unauthenticated, timeout, a run that never called the tool -- returns an
    honest fallback answer instead of raising, because the route must never
    500 for agent reasons."""
    if not enabled():
        return {"answer": _DISABLED, "suspects": []}

    os.environ.pop("CLAUDECODE", None)  # avoid nested-Claude-Code confusion (translate.py precedent)
    collected: list[dict] = []
    server = create_sdk_mcp_server(name="debugger", tools=[make_report_tool(collected)])
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM,
        model=_MODEL,
        mcp_servers={"debugger": server},
        allowed_tools=["mcp__debugger__report_diagnosis"],
    )
    try:
        await asyncio.wait_for(
            _run_agent(_prompt(context, question), options, client_cls),
            timeout=timeout if timeout is not None else _default_timeout(),
        )
    except Exception:
        log.exception("debug agent SDK pass failed for env %s", context.get("env"))
        return dict(UNAVAILABLE)
    if not collected:
        log.warning("debug agent produced no diagnosis for env %s", context.get("env"))
        return dict(UNAVAILABLE)
    payload = collected[-1]
    return {"answer": str(payload.get("answer", "")), "suspects": normalize_suspects(payload.get("suspects"))}
