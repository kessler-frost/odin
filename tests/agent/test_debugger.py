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
    # env-wide: belongs to no node, so it lands in `recent_tf` instead.
    {"type": "tf", "env": "dbg", "phase": "apply", "status": "ok", "exit_code": 0},
]

LOGS = {"api": "starting\nFATAL: config missing\n", "db": "database system is ready to accept connections\n"}


async def _logs(node: str) -> str:
    """`assemble_context` reads each node's tail with `await logs(node_id)`
    (v0.7.7: the real resolver shells out, natively async), so this stand-in is
    a coroutine function too -- a sync fake here would type-check the call site
    but stop matching the seam it replaces."""
    return LOGS.get(node, "")


async def _context(node_ids=("db", "api", "ghost"), stack=STACK, world=WORLD, events=EVENTS, logs=_logs) -> dict:
    return await assemble_context(stack, world, events, logs, list(node_ids))


# --- the assembler: shape ----------------------------------------------------


async def test_healthy_node_carries_desired_observed_events_and_logs():
    node = (await _context())["nodes"]["db"]
    assert node["desired"]["kind"] == "rds"
    assert node["desired"]["fields"]["engine"] == {"value": "postgres", "provenance": "user"}
    assert node["observed"]["phase"] == "healthy"
    assert node["observed"]["restarts"] == 0
    assert "ready to accept connections" in node["logs"]
    assert [e["type"] for e in node["events"]] == ["world_delta"]


async def test_crashed_node_carries_the_real_verdict_restarts_refs_and_crash_log():
    node = (await _context())["nodes"]["api"]
    assert node["observed"]["phase"] == "crashed"
    assert node["observed"]["verdict"] == "Essential container in task exited (exit 1)"
    assert node["observed"]["restarts"] == 3
    assert node["refs"] == [{"var": "DATABASE_URL", "target_id": "db", "target_attr": "DATABASE_URL"}]
    assert "FATAL: config missing" in node["logs"]


async def test_selected_node_absent_from_the_stack_is_included_with_desired_none():
    # The canvas can select a stale tile whose node was already removed --
    # it still gets a record (with no desired config), never a KeyError.
    node = (await _context())["nodes"]["ghost"]
    assert node["desired"] is None
    assert node["observed"] is None
    assert node["refs"] == [] and node["events"] == [] and node["logs"] == ""


async def test_env_wide_events_are_not_attributed_to_any_node():
    context = await _context()
    for node in context["nodes"].values():
        assert all(e["type"] != "tf" for e in node["events"])


# --- the assembler: the env-wide tofu section --------------------------------


def _tf_events(*, status: str = "failed", exit_code: int = 1, tail=("Error: creating S3 Bucket: BucketAlreadyOwned",)):
    """The exact two shapes `simulate/runner.py::TfRunner._run` broadcasts."""
    return [
        {"type": "tf", "env": "dbg", "phase": "init", "line": "OpenTofu has been successfully initialized!"},
        {"type": "tf", "env": "dbg", "phase": "init", "status": "ok", "exit_code": 0},
        {"type": "tf", "env": "dbg", "phase": "apply", "line": "aws_s3_bucket.assets: Creating..."},
        *({"type": "tf", "env": "dbg", "phase": "apply", "line": line} for line in tail),
        {"type": "tf", "env": "dbg", "phase": "apply", "status": status, "exit_code": exit_code, "tail": list(tail)},
    ]


async def test_a_failed_tofu_apply_reaches_the_context_env_wide():
    # THE most common "what's wrong here?": the user's apply just failed. That
    # evidence carries no resource_id, so before `recent_tf` it reached nothing.
    context = await _context(events=_tf_events())
    assert "apply: Error: creating S3 Bucket: BucketAlreadyOwned" in context["recent_tf"]
    assert context["recent_tf"][-1] == "tofu apply failed (exit 1)"


async def test_the_tf_section_is_env_level_not_per_node():
    context = await _context(events=_tf_events())
    for node in context["nodes"].values():
        assert node["events"] == []


async def test_a_successful_apply_still_reports_its_verdict():
    context = await _context(events=_tf_events(status="ok", exit_code=0, tail=()))
    assert context["recent_tf"][-1] == "tofu apply ok (exit 0)"


async def test_the_failure_tail_is_not_duplicated_by_the_stream_lines_it_repeats():
    # `tail` IS the last lines of the same stdout already streamed as `line`
    # events, so re-adding them would spend the whole cap on a repeat.
    lines = (await _context(events=_tf_events()))["recent_tf"]
    assert lines.count("apply: Error: creating S3 Bucket: BucketAlreadyOwned") == 1


async def test_a_tail_entry_no_stream_line_carried_is_kept():
    # The timeout path: `TfRunner._run` appends a synthetic sentence to the
    # tail that was never emitted as a line event, and it is the whole reason
    # for the failure.
    timed_out = "tofu apply timed out after 600s -- process killed"
    context = await _context(events=[
        {"type": "tf", "env": "dbg", "phase": "apply", "line": "aws_instance.web: Still creating... [9m50s elapsed]"},
        {"type": "tf", "env": "dbg", "phase": "apply", "status": "failed", "exit_code": -9, "tail": [timed_out]},
    ])
    assert context["recent_tf"] == [
        "apply: aws_instance.web: Still creating... [9m50s elapsed]",
        f"apply: {timed_out}",
        "tofu apply failed (exit -9)",
    ]


async def test_the_tf_section_is_capped_at_the_last_twenty_lines():
    events = [{"type": "tf", "env": "dbg", "phase": "apply", "line": f"tf-line-{i}"} for i in range(200)]
    lines = (await _context(events=events))["recent_tf"]
    assert len(lines) == debugger.MAX_TF_LINES
    assert lines[0] == "apply: tf-line-180" and lines[-1] == "apply: tf-line-199"


async def test_a_very_long_tf_line_is_clipped_like_every_other_string():
    events = [{"type": "tf", "env": "dbg", "phase": "apply", "line": "z" * 9000}]
    line = (await _context(events=events))["recent_tf"][0]
    assert len(line) < 9000 and line.endswith("(truncated)")


async def test_an_env_with_no_tofu_output_carries_an_empty_section():
    assert (await _context(events=[]))["recent_tf"] == []
    # ...and an env whose only tf event is a clean apply says exactly that.
    assert (await _context())["recent_tf"] == ["tofu apply ok (exit 0)"]


async def test_no_secret_survives_the_tf_section():
    # `simulate/runner.py` already scrubs each line before it reaches
    # events.jsonl, but the assembler re-scrubs against the CURRENT Stack --
    # the two secret sets can differ (a Stack edited since the apply, a caller
    # that passed none), and this is the last place that can still catch it.
    events = [
        {"type": "tf", "env": "dbg", "phase": "apply", "line": f'  + password = "{PASSWORD}"'},
        {"type": "tf", "env": "dbg", "phase": "apply", "status": "failed", "exit_code": 1,
         "tail": [f"Error: invalid credentials for {PASSWORD}"]},
    ]
    context = await assemble_context(STACK, WORLD, events, _logs, ["db"])
    assert context["recent_tf"], "the section must exist for this test to mean anything"
    assert PASSWORD not in json.dumps(context)
    assert REDACTED in json.dumps(context["recent_tf"])


async def test_the_prompt_tells_the_agent_about_the_env_wide_tofu_evidence():
    prompt = debugger._prompt(await _context(events=_tf_events()), "what's wrong here?")
    assert "recent_tf" in prompt and "recent_tf" in debugger._SYSTEM
    assert "BucketAlreadyOwned" in prompt


# --- the assembler: drift (no key of its own, by design) ---------------------


async def test_a_drift_verdict_reaches_the_context_through_the_verdict_channel():
    """`reconcile/drift.py` landed after M8 and needs NO new key: the sweep's
    sentence is overlaid as the resource's `verdict` by
    `Reconciler._project_tf_owned` (and written into the store record, so
    `tf_status.project()` keeps projecting it after a restart). This pins that
    the existing `observed.verdict` path is what carries it."""
    reason = "VM odin-ec2-web deleted outside odin — re-Apply to recreate"
    world = World(env="dbg", resources=(
        ResourceObserved(id="web", kind="ec2", phase="crashed", verdict=reason),
    ))
    node = (await assemble_context(Stack(env="dbg"), world, [], _logs, ["web"]))["nodes"]["web"]
    assert node["observed"]["verdict"] == reason
    assert node["observed"]["phase"] == "crashed"


async def test_the_drift_crash_log_event_reaches_the_node_too():
    """The second copy, for free: `Reconciler._emit`'s crashed path broadcasts
    the verdict as a `type:"log"` event whose `source` is the node, which
    `_event_node` already attributes correctly."""
    reason = "container odin-lambda-dbg-fn removed outside odin — re-Apply to recreate"
    events = [{"type": "log", "env": "dbg", "source": "fn", "text": reason, "level": "error"}]
    node = (await assemble_context(Stack(env="dbg"), World(env="dbg"), events, _logs, ["fn"]))["nodes"]["fn"]
    assert [e["text"] for e in node["events"]] == [reason]


async def test_the_env_is_carried_and_duplicate_ids_collapse():
    context = await _context(node_ids=("api", "api", "db"))
    assert context["env"] == "dbg"
    assert list(context["nodes"]) == ["api", "db"]


# --- the assembler: secrecy --------------------------------------------------


async def test_env_var_values_are_reduced_to_key_names_only():
    fields = (await _context())["nodes"]["api"]["desired"]["fields"]
    assert fields["env"] == {"keys": ["DATABASE_URL", "LOG_LEVEL"], "provenance": "user"}
    assert "value" not in fields["env"]


async def test_a_sensitive_field_value_is_redacted():
    assert (await _context())["nodes"]["db"]["desired"]["fields"]["password"]["value"] == REDACTED


async def test_no_secret_value_survives_anywhere_in_the_assembled_context():
    # The leak test for THIS path (the analogue of test_translate.py's own):
    # a real secret rides out on more than the desired field it was typed
    # into -- an rds node's observed `facts` carry the whole DATABASE_URL,
    # password included, and a container is free to echo its own env into
    # stdout. Nothing that reaches the prompt may contain it.
    logs = {"api": f"connecting with PGPASSWORD={PASSWORD}\nFATAL: config missing\n"}
    events = [*EVENTS, {"type": "log", "env": "dbg", "source": "db", "text": f"password={PASSWORD}"}]

    async def read(node: str) -> str:
        return logs.get(node, "")

    context = await assemble_context(STACK, WORLD, events, read, ["db", "api"])
    assert PASSWORD not in json.dumps(context)
    assert REDACTED in json.dumps(context)


async def test_the_prompt_itself_carries_no_secret():
    context = await _context()
    prompt = debugger._prompt(context, "what's wrong here?")
    assert PASSWORD not in prompt
    assert "busybox:latest" in prompt  # non-sensitive evidence is still shown, unredacted


# --- field test 2 finding #8: ids odin has never heard of --------------------


async def test_an_id_with_no_record_anywhere_is_flagged_unknown():
    context = await _context(node_ids=("db", "d1"))
    assert context["unknown_nodes"] == ["d1"]
    assert context["known_nodes"] == ["api", "db"]


async def test_an_observed_or_event_only_id_counts_as_known():
    # "Selected but not in the applied stack" is the deliberate desired-None
    # case, NOT an unknown id: World and the event log are records too.
    world = World(env="dbg", resources=(ResourceObserved(id="gone", kind="ecs", phase="crashed"),))
    events = [{"type": "access_denied", "env": "dbg", "resource_id": "principal"}]
    context = await assemble_context(Stack(env="dbg"), world, events, _logs, ["gone", "principal"])
    assert context["unknown_nodes"] == []
    assert context["nodes"]["gone"]["desired"] is None


async def test_the_refusal_names_every_unknown_id_and_the_labels_that_do_exist():
    context = await _context(node_ids=("d1", "e1"))
    answer = debugger.no_evidence_answer(context, ["d1", "e1"])
    assert answer["suspects"] == []
    assert "'d1'" in answer["answer"] and "'e1'" in answer["answer"]
    assert "no such node" in answer["answer"] and "env 'dbg'" in answer["answer"]
    assert "api, db" in answer["answer"]


async def test_a_request_with_one_real_id_is_worth_a_model_call():
    context = await _context(node_ids=("db", "d1"))
    assert debugger.no_evidence_answer(context, ["db", "d1"]) is None


async def test_an_empty_selection_is_an_env_wide_question_not_a_refusal():
    # `node_ids: []` + a failed apply in `recent_tf` is a legitimate
    # "what's wrong with this environment?" -- there is nothing to refuse.
    context = await _context(node_ids=())
    assert debugger.no_evidence_answer(context, []) is None


async def test_the_refusal_says_so_plainly_when_the_env_has_nothing_applied():
    context = await assemble_context(Stack(env="dbg"), World(env="dbg"), [], _logs, ["d1"])
    answer = debugger.no_evidence_answer(context, ["d1"])
    assert "no applied nodes at all" in answer["answer"]


def test_the_prompt_tells_the_agent_to_call_out_unknown_ids():
    assert "unknown_nodes" in debugger._SYSTEM


# --- field test 2 finding #6: credentials odin ISSUED, not ones it was given --

ISSUED_ACCESS = "AKODINFAKEFAKEFAKEFA"
ISSUED_SECRET = "fake-issued-secret-000000000000000000000"


async def test_a_gateway_issued_credential_is_scrubbed_from_the_context():
    """`Stack.sensitive_values()` can NEVER contain a gateway-issued key: it is
    built from canvas-authored fields, and these are minted by
    `gateway/keys.py::KeyStore.issue`. Until now the only thing keeping a
    workload's live credentials out of the prompt was the 200-char clip -- and
    in the real verdict field test 2 measured, the access key began at
    character 235. `extra_secrets` closes that by name instead of by luck."""
    world = World(env="dbg", resources=(
        ResourceObserved(
            id="api", kind="ecs", phase="crashed",
            verdict=f"docker run odin-ecs-dbg-web failed: AWS_SECRET_ACCESS_KEY={ISSUED_SECRET}",
            facts={"logtail": f"boot with AWS_ACCESS_KEY_ID={ISSUED_ACCESS}"},
        ),
    ))
    async def read(_node: str) -> str:
        return f"exported AWS_SECRET_ACCESS_KEY={ISSUED_SECRET}"

    context = await assemble_context(
        STACK, world, [], read, ["api"],
        extra_secrets=frozenset({ISSUED_ACCESS, ISSUED_SECRET}),
    )
    dumped = json.dumps(context)
    assert ISSUED_SECRET not in dumped and ISSUED_ACCESS not in dumped
    assert REDACTED in dumped
    assert "docker run odin-ecs-dbg-web failed" in dumped  # the diagnostic survives


async def test_a_secret_is_scrubbed_before_the_clip_not_after():
    """Ordering matters: clipping FIRST can cut a secret in half, and the
    surviving prefix is no longer a substring `scrub` can match -- a partial
    credential is still a leak."""
    long_verdict = "x" * (debugger.MAX_VALUE_CHARS - 10) + ISSUED_SECRET
    world = World(env="dbg", resources=(
        ResourceObserved(id="api", kind="ecs", phase="crashed", verdict=long_verdict),
    ))
    context = await assemble_context(
        STACK, world, [], _logs, ["api"], extra_secrets=frozenset({ISSUED_SECRET}),
    )
    assert ISSUED_SECRET[:10] not in json.dumps(context)


# --- the assembler: caps ----------------------------------------------------


async def test_only_the_last_ten_events_per_node_are_kept():
    events = [{"type": "world_delta", "env": "dbg", "resource_id": "api", "seq": i} for i in range(25)]
    node = (await assemble_context(STACK, WORLD, events, _logs, ["api"]))["nodes"]["api"]
    assert len(node["events"]) == debugger.MAX_EVENTS
    assert [e["seq"] for e in node["events"]] == list(range(15, 25))  # the LAST ten


async def test_only_the_last_forty_log_lines_are_kept():
    text = "\n".join(f"line-{i}" for i in range(500))

    async def read(_node: str) -> str:
        return text

    node = (await assemble_context(STACK, WORLD, [], read, ["api"]))["nodes"]["api"]
    lines = node["logs"].splitlines()
    assert len(lines) == debugger.MAX_LOG_LINES
    assert lines[0] == "line-460" and lines[-1] == "line-499"


async def test_a_long_field_value_is_clipped():
    stack = Stack(env="dbg", resources=(
        ResourceDesired(id="vm", kind="ec2", fields={"userData": FieldValue(value="x" * 5000)}),
    ))
    context = await assemble_context(stack, World(env="dbg"), [], _logs, ["vm"])
    value = context["nodes"]["vm"]["desired"]["fields"]["userData"]["value"]
    assert len(value) < 5000 and value.endswith("(truncated)")


async def test_a_long_fact_value_is_clipped_too():
    world = World(env="dbg", resources=(
        ResourceObserved(id="api", kind="ecs", phase="crashed", facts={"logtail": "y" * 9000}),
    ))
    facts = (await assemble_context(STACK, world, [], _logs, ["api"]))["nodes"]["api"]["observed"]["facts"]
    assert len(facts["logtail"]) < 9000 and facts["logtail"].endswith("(truncated)")


async def test_beyond_the_node_cap_extra_ids_are_named_not_silently_dropped():
    ids = [f"n{i}" for i in range(debugger.MAX_NODES + 3)]
    context = await assemble_context(Stack(env="dbg"), World(env="dbg"), [], _logs, ids)
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
    result = await diagnose(await _context(), "what's wrong here?", client_cls=_client_with(PAYLOAD), timeout=5)
    assert result == PAYLOAD


async def test_the_context_reaches_the_prompt():
    _PROMPTS.clear()
    await diagnose(await _context(), "why is api down?", client_cls=_client_with(PAYLOAD), timeout=5)
    assert "why is api down?" in _PROMPTS[-1]
    assert "Essential container in task exited (exit 1)" in _PROMPTS[-1]
    assert "FATAL: config missing" in _PROMPTS[-1]


async def test_an_sdk_error_falls_back_to_agent_unavailable():
    result = await diagnose(await _context(), "?", client_cls=_client_with(raises=RuntimeError("no CLI")), timeout=5)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_a_timeout_falls_back_to_agent_unavailable():
    result = await diagnose(await _context(), "?", client_cls=_client_with(hang=True), timeout=0.05)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_a_run_that_never_calls_the_tool_falls_back():
    result = await diagnose(await _context(), "?", client_cls=_client_with(None), timeout=5)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_a_malformed_report_is_rejected_by_the_typed_membrane_itself():
    # Not defensive parsing -- the `report_diagnosis` SCHEMA is the contract.
    # A report whose suspects aren't {node_id, reason} objects never reaches
    # the collector at all, so the run falls back honestly instead of
    # returning half-understood data.
    payload = {"answer": "unclear", "suspects": ["api", {"reason": "no id"}]}
    result = await diagnose(await _context(), "?", client_cls=_client_with(payload), timeout=5)
    assert result == {"answer": "agent unavailable", "suspects": []}


async def test_extra_keys_on_a_suspect_are_dropped_rather_than_breaking_the_response():
    # The membrane allows extra properties through; `normalize_suspects` is
    # what keeps `api/debug.py`'s `Suspect(**s)` from raising on them.
    payload = {"answer": "a", "suspects": [{"node_id": "api", "reason": "r", "confidence": "high"}]}
    result = await diagnose(await _context(), "?", client_cls=_client_with(payload), timeout=5)
    assert result == {"answer": "a", "suspects": [{"node_id": "api", "reason": "r"}]}


async def test_it_is_on_by_default_and_ODIN_DEBUG_AGENT_0_turns_it_off(monkeypatch):
    assert debugger.enabled() is True  # unlike translate's refine pass
    monkeypatch.setenv("ODIN_DEBUG_AGENT", "0")
    assert debugger.enabled() is False
    result = await diagnose(await _context(), "?", client_cls=_client_with(PAYLOAD), timeout=5)
    assert result["suspects"] == [] and "off" in result["answer"]


def test_the_timeout_is_overridable(monkeypatch):
    monkeypatch.setenv("ODIN_DEBUG_TIMEOUT", "7.5")
    assert debugger._default_timeout() == 7.5
