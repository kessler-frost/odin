"""W2.9 / M8 -- the region debugger: the pure context assembler and the
best-effort diagnosis pass.

`_FakeClient` drives the REAL `create_sdk_mcp_server`/`@tool` dispatch
(`mcp.server.lowlevel.Server.request_handlers`) with canned args instead of
spawning the Claude Code CLI -- the same seam `tests/agent/test_translate.py`
uses, so these tests exercise the exact tool registration `debugger.py` runs in
production rather than a reimplementation of it.
"""
from __future__ import annotations

import asyncio
import json

from mcp import types as mcp_types

from odin.agent import debugger
from odin.agent.debugger import assemble_context, diagnose
from odin.spec.models import REDACTED, FieldValue, Ref, ResourceDesired, ResourceObserved, Stack, World

# --- fixtures: one healthy node, one crashed node, one selected-but-absent ----

PASSWORD = "hunter2-not-in-any-prompt"

STACK = Stack(
    env="dbg",
    resources=(
        ResourceDesired(
            id="db", kind="rds",
            fields={
                "engine": FieldValue(value="postgres"),
                "password": FieldValue(value=PASSWORD, sensitive=True),
            },
        ),
        ResourceDesired(
            id="api", kind="ecs",
            fields={
                "image": FieldValue(value="busybox:latest"),
                "env": FieldValue(value={"DATABASE_URL": f"postgresql://odin:{PASSWORD}@127.0.0.1:5432/db",
                                         "LOG_LEVEL": "debug"}),
            },
            refs=(Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL"),),
        ),
    ),
)

WORLD = World(
    env="dbg",
    resources=(
        ResourceObserved(id="db", kind="rds", phase="healthy",
                         facts={"DATABASE_URL": f"postgresql://odin:{PASSWORD}@127.0.0.1:5432/db"}),
        ResourceObserved(id="api", kind="ecs", phase="crashed", restarts=3,
                         verdict="Essential container in task exited (exit 1)"),
    ),
)

EVENTS = [
    {"type": "world_delta", "env": "dbg", "resource_id": "db", "phase": "healthy"},
    {"type": "world_delta", "env": "dbg", "resource_id": "api", "phase": "crashed"},
    {"type": "log", "env": "dbg", "source": "api", "text": "FATAL: config missing", "level": "error"},
    {"type": "tf", "env": "dbg", "phase": "apply", "status": "ok", "exit_code": 0},  # env-wide: belongs to no node
]

LOGS = {"api": "starting\nFATAL: config missing\n", "db": "database system is ready to accept connections\n"}


def _logs(node: str) -> str:
    return LOGS.get(node, "")


def _context(node_ids=("db", "api", "ghost"), stack=STACK, world=WORLD, events=EVENTS, logs=_logs) -> dict:
    return assemble_context(stack, world, events, logs, list(node_ids))


# --- the assembler: shape ----------------------------------------------------


def test_healthy_node_carries_desired_observed_events_and_logs():
    node = _context()["nodes"]["db"]
    assert node["desired"]["kind"] == "rds"
    assert node["desired"]["fields"]["engine"] == {"value": "postgres", "provenance": "user"}
    assert node["observed"]["phase"] == "healthy"
    assert node["observed"]["restarts"] == 0
    assert "ready to accept connections" in node["logs"]
    assert [e["type"] for e in node["events"]] == ["world_delta"]


def test_crashed_node_carries_the_real_verdict_restarts_refs_and_crash_log():
    node = _context()["nodes"]["api"]
    assert node["observed"]["phase"] == "crashed"
    assert node["observed"]["verdict"] == "Essential container in task exited (exit 1)"
    assert node["observed"]["restarts"] == 3
    assert node["refs"] == [{"var": "DATABASE_URL", "target_id": "db", "target_attr": "DATABASE_URL"}]
    assert "FATAL: config missing" in node["logs"]


def test_selected_node_absent_from_the_stack_is_included_with_desired_none():
    # The canvas can select a stale tile whose node was already removed --
    # it still gets a record (with no desired config), never a KeyError.
    node = _context()["nodes"]["ghost"]
    assert node["desired"] is None
    assert node["observed"] is None
    assert node["refs"] == [] and node["events"] == [] and node["logs"] == ""


def test_env_wide_events_are_not_attributed_to_any_node():
    context = _context()
    for node in context["nodes"].values():
        assert all(e["type"] != "tf" for e in node["events"])


# --- the assembler: drift (no key of its own, by design) ---------------------


def test_a_drift_verdict_reaches_the_context_through_the_verdict_channel():
    """`reconcile/drift.py` landed after M8 and needs NO new key: the sweep's
    sentence is overlaid as the resource's `verdict` by
    `Reconciler._project_tf_owned` (and written into the store record, so
    `tf_status.project()` keeps projecting it after a restart). This pins that
    the existing `observed.verdict` path is what carries it."""
    reason = "VM odin-ec2-web deleted outside odin — re-Apply to recreate"
    world = World(env="dbg", resources=(
        ResourceObserved(id="web", kind="ec2", phase="crashed", verdict=reason),
    ))
    node = assemble_context(Stack(env="dbg"), world, [], _logs, ["web"])["nodes"]["web"]
    assert node["observed"]["verdict"] == reason
    assert node["observed"]["phase"] == "crashed"


def test_the_drift_crash_log_event_reaches_the_node_too():
    """The second copy, for free: `Reconciler._emit`'s crashed path broadcasts
    the verdict as a `type:"log"` event whose `source` is the node, which
    `_event_node` already attributes correctly."""
    reason = "container odin-lambda-dbg-fn removed outside odin — re-Apply to recreate"
    events = [{"type": "log", "env": "dbg", "source": "fn", "text": reason, "level": "error"}]
    node = assemble_context(Stack(env="dbg"), World(env="dbg"), events, _logs, ["fn"])["nodes"]["fn"]
    assert [e["text"] for e in node["events"]] == [reason]


def test_the_env_is_carried_and_duplicate_ids_collapse():
    context = _context(node_ids=("api", "api", "db"))
    assert context["env"] == "dbg"
    assert list(context["nodes"]) == ["api", "db"]


# --- the assembler: secrecy --------------------------------------------------


def test_env_var_values_are_reduced_to_key_names_only():
    fields = _context()["nodes"]["api"]["desired"]["fields"]
    assert fields["env"] == {"keys": ["DATABASE_URL", "LOG_LEVEL"], "provenance": "user"}
    assert "value" not in fields["env"]


def test_a_sensitive_field_value_is_redacted():
    assert _context()["nodes"]["db"]["desired"]["fields"]["password"]["value"] == REDACTED


def test_no_secret_value_survives_anywhere_in_the_assembled_context():
    # The leak test for THIS path (the analogue of test_translate.py's own):
    # a real secret rides out on more than the desired field it was typed
    # into -- an rds node's observed `facts` carry the whole DATABASE_URL,
    # password included, and a container is free to echo its own env into
    # stdout. Nothing that reaches the prompt may contain it.
    logs = {"api": f"connecting with PGPASSWORD={PASSWORD}\nFATAL: config missing\n"}
    events = [*EVENTS, {"type": "log", "env": "dbg", "source": "db", "text": f"password={PASSWORD}"}]
    context = assemble_context(STACK, WORLD, events, lambda n: logs.get(n, ""), ["db", "api"])
    assert PASSWORD not in json.dumps(context)
    assert REDACTED in json.dumps(context)


def test_the_prompt_itself_carries_no_secret():
    context = _context()
    prompt = debugger._prompt(context, "what's wrong here?")
    assert PASSWORD not in prompt
    assert "busybox:latest" in prompt  # non-sensitive evidence is still shown, unredacted


# --- the assembler: caps ----------------------------------------------------


def test_only_the_last_ten_events_per_node_are_kept():
    events = [{"type": "world_delta", "env": "dbg", "resource_id": "api", "seq": i} for i in range(25)]
    node = assemble_context(STACK, WORLD, events, _logs, ["api"])["nodes"]["api"]
    assert len(node["events"]) == debugger.MAX_EVENTS
    assert [e["seq"] for e in node["events"]] == list(range(15, 25))  # the LAST ten


def test_only_the_last_forty_log_lines_are_kept():
    text = "\n".join(f"line-{i}" for i in range(500))
    node = assemble_context(STACK, WORLD, [], lambda _n: text, ["api"])["nodes"]["api"]
    lines = node["logs"].splitlines()
    assert len(lines) == debugger.MAX_LOG_LINES
    assert lines[0] == "line-460" and lines[-1] == "line-499"


def test_a_long_field_value_is_clipped():
    stack = Stack(env="dbg", resources=(
        ResourceDesired(id="vm", kind="ec2", fields={"userData": FieldValue(value="x" * 5000)}),
    ))
    value = assemble_context(stack, World(env="dbg"), [], _logs, ["vm"])["nodes"]["vm"]["desired"]["fields"]["userData"]["value"]
    assert len(value) < 5000 and value.endswith("(truncated)")


def test_a_long_fact_value_is_clipped_too():
    world = World(env="dbg", resources=(
        ResourceObserved(id="api", kind="ecs", phase="crashed", facts={"logtail": "y" * 9000}),
    ))
    facts = assemble_context(STACK, world, [], _logs, ["api"])["nodes"]["api"]["observed"]["facts"]
    assert len(facts["logtail"]) < 9000 and facts["logtail"].endswith("(truncated)")


def test_beyond_the_node_cap_extra_ids_are_named_not_silently_dropped():
    ids = [f"n{i}" for i in range(debugger.MAX_NODES + 3)]
    context = assemble_context(Stack(env="dbg"), World(env="dbg"), [], _logs, ids)
    assert len(context["nodes"]) == debugger.MAX_NODES
    assert context["omitted_nodes"] == ids[debugger.MAX_NODES:]


# --- diagnose: the one typed effect channel + the fallback -------------------


class _FakeClient:
    """Test double for `ClaudeSDKClient` (the `translate.py` test seam,
    verbatim): records the prompt and, on `receive_response()`, drives the real
    MCP `call_tool` handler with `canned_args`."""

    canned_args: dict | None = None
    raises: Exception | None = None
    hang: bool = False

    def __init__(self, options) -> None:
        self.options = options

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        _PROMPTS.append(prompt)
        if self.raises is not None:
            raise self.raises
        if self.hang:
            await asyncio.sleep(30)

    async def receive_response(self):
        if self.canned_args is not None:
            server = self.options.mcp_servers["debugger"]["instance"]
            handler = server.request_handlers[mcp_types.CallToolRequest]
            await handler(mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name="report_diagnosis", arguments=self.canned_args),
            ))
        return
        yield  # pragma: no cover -- keeps this an async generator


_PROMPTS: list[str] = []


def _client_with(canned_args: dict | None = None, raises: Exception | None = None, hang: bool = False) -> type:
    return type("FakeClient", (_FakeClient,), {"canned_args": canned_args, "raises": raises, "hang": hang})


PAYLOAD = {
    "answer": "the api task exits immediately: its command prints FATAL: config missing and returns 1.",
    "suspects": [{"node_id": "api", "reason": "exit 1 with 'FATAL: config missing' on every task"}],
}


async def test_the_reported_diagnosis_comes_back_verbatim():
    result = await diagnose(_context(), "what's wrong here?", client_cls=_client_with(PAYLOAD), timeout=5)
    assert result == PAYLOAD


async def test_the_context_reaches_the_prompt():
    _PROMPTS.clear()
    await diagnose(_context(), "why is api down?", client_cls=_client_with(PAYLOAD), timeout=5)
    assert "why is api down?" in _PROMPTS[-1]
    assert "Essential container in task exited (exit 1)" in _PROMPTS[-1]
    assert "FATAL: config missing" in _PROMPTS[-1]


async def test_an_sdk_error_falls_back_to_agent_unavailable():
    result = await diagnose(_context(), "?", client_cls=_client_with(raises=RuntimeError("no CLI")), timeout=5)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_a_timeout_falls_back_to_agent_unavailable():
    result = await diagnose(_context(), "?", client_cls=_client_with(hang=True), timeout=0.05)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_a_run_that_never_calls_the_tool_falls_back():
    result = await diagnose(_context(), "?", client_cls=_client_with(None), timeout=5)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_a_malformed_report_is_rejected_by_the_typed_membrane_itself():
    # Not defensive parsing -- the `report_diagnosis` SCHEMA is the contract.
    # A report whose suspects aren't {node_id, reason} objects never reaches
    # the collector at all, so the run falls back honestly instead of
    # returning half-understood data.
    payload = {"answer": "unclear", "suspects": ["api", {"reason": "no id"}]}
    result = await diagnose(_context(), "?", client_cls=_client_with(payload), timeout=5)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_extra_keys_on_a_suspect_are_dropped_rather_than_breaking_the_response():
    # The membrane allows extra properties through; `normalize_suspects` is
    # what keeps `api/debug.py`'s `Suspect(**s)` from raising on them.
    payload = {"answer": "a", "suspects": [{"node_id": "api", "reason": "r", "confidence": "high"}]}
    result = await diagnose(_context(), "?", client_cls=_client_with(payload), timeout=5)
    assert result == {"answer": "a", "suspects": [{"node_id": "api", "reason": "r"}]}


async def test_it_is_on_by_default_and_ODIN_DEBUG_AGENT_0_turns_it_off(monkeypatch):
    assert debugger.enabled() is True  # unlike translate's refine pass
    monkeypatch.setenv("ODIN_DEBUG_AGENT", "0")
    assert debugger.enabled() is False
    result = await diagnose(_context(), "?", client_cls=_client_with(PAYLOAD), timeout=5)
    assert result["suspects"] == [] and "off" in result["answer"]


def test_the_timeout_is_overridable(monkeypatch):
    monkeypatch.setenv("ODIN_DEBUG_TIMEOUT", "7.5")
    assert debugger._default_timeout() == 7.5
