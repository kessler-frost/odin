# C3 — Brain Toolbelt MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Brain becomes a candidate-only producer behind typed MCP tools — `propose_changeset` (config completion), `report_findings` (stack security review, replacing IAM review), `report_diagnosis` (used by M8) — instead of free-text JSON scraping.

**Architecture:** One in-process MCP server (`create_sdk_mcp_server` from claude-agent-sdk) built per call with closure collectors; `ClaudeSDKClient` runs the conversation; the ONLY effect channel is the collected tool payloads, which flow into the existing deterministic `merge_completion` (user values always win, `provenance="ai"`). Free-text JSON parsing (`_extract_json`, regex) dies. Spec: `docs/superpowers/specs/2026-07-22-v030-real-backings-design.md` §C3.

**Tech Stack:** claude-agent-sdk ≥0.1.39 (`tool`, `create_sdk_mcp_server`, `ClaudeSDKClient`, `ClaudeAgentOptions`).

## Global Constraints

- `claude-agent-sdk` only; `uv` never pip; imports at top; minimal branching.
- The brain stays best-effort/off the critical path: ANY SDK failure → empty result → stack applies as-is (existing behavior, keep the log lines).
- User values always win; AI fields tagged `provenance="ai"` — `merge_completion` is untouched.
- Unit suite green after every task (`uv run pytest -q`); real-SDK tests marked `integration`.
- Commit per task; don't push.

---

### Task 1: The toolbelt runner (`agent/toolbelt.py`)

**Files:**
- Create: `src/odin/agent/toolbelt.py`
- Test: `tests/agent/test_toolbelt.py` (new)

**Interfaces:**
- Produces: `async run_toolbelt(prompt: str, system: str, tools: list, timeout: float = 120.0) -> None` — builds `create_sdk_mcp_server(name="toolbelt", tools=tools)`, runs `ClaudeSDKClient` with `ClaudeAgentOptions(system_prompt=system, mcp_servers={"toolbelt": server}, allowed_tools=[f"mcp__toolbelt__{t.name}" for t in tools], model="claude-sonnet-5")`, sends the prompt, drains the response (`os.environ.pop("CLAUDECODE", None)` first, like `brain.py` does). It returns nothing — effects land in the tool closures.
- Produces: `make_tool(name: str, description: str, schema: dict, collector: Callable[[dict], None])` — wraps claude-agent-sdk's `@tool` so callers build collecting tools in one line; the handler stores `args` via `collector(args)` and returns `{"content": [{"type": "text", "text": "recorded"}]}`.
- Testability seam: `run_toolbelt` accepts `_client_cls=ClaudeSDKClient` kwarg; unit tests pass a fake client class that "calls" given tools with canned args (invoke the tool handlers directly) — asserting the collector plumbing without the SDK.

- [ ] **Step 1:** Read `src/odin/agent/brain.py` (current SDK usage) and the claude-agent-sdk docs for custom tools: `uv run python -c "import claude_agent_sdk, inspect; print(inspect.signature(claude_agent_sdk.create_sdk_mcp_server)); print(inspect.signature(claude_agent_sdk.tool))"` — confirm exact signatures before writing code (the SDK is installed).
- [ ] **Step 2:** Failing tests: a fake client class records the options it got (assert `allowed_tools` lists `mcp__toolbelt__propose_changeset`, model `claude-sonnet-5`, system prompt passthrough) and directly invokes a made tool's handler with `{"changes": [{"node_id": "db", "field": "port", "value": 5432}]}` — assert the collector saw it and the handler returned the "recorded" content shape.
- [ ] **Step 3:** Implement; **Step 4:** `uv run pytest tests/agent/test_toolbelt.py -q` green, suite green. **Step 5:** Commit `feat(agent): toolbelt runner — typed MCP membrane for the brain`.

---

### Task 2: `claude_complete` + `review_stack` on the toolbelt

**Files:**
- Modify: `src/odin/agent/brain.py` (rewrite `_fill`→gone; `claude_complete` uses one toolbelt run for ALL gaps; add `review_stack(stack) -> list[str]`; delete `review_iam`, `_extract_json`, `_IAM_SYSTEM`, the regex import)
- Modify: `src/odin/server.py` (`/review-iam` route → `/review`, calling `review_stack`; keep response shape `{"findings": [...], "env": env}`)
- Modify: `ui/src/components/TopBar.tsx` (the Review button hits `/review`; check `ui/src` for `review-iam` references: `grep -rn "review-iam" ui/src`)
- Test: `tests/agent/test_brain.py` (update: fake client class injected through `claude_complete(stack, _client_cls=...)`; existing integration test updated), `tests/api/test_apply.py` (route rename if referenced)

**Interfaces:**
- `claude_complete(stack: Stack, _client_cls=None) -> Stack`: builds ONE prompt covering every gap from `needs_completion(stack)` (resource id, kind, already-set fields, missing fields) + the tool `propose_changeset` with schema `{"changes": [{"node_id": str, "field": str, "value": any}]}`; collector accumulates into `{rid: {field: value}}` filtering to (a) rids actually in gaps, (b) fields actually missing — then `merge_completion`. No tool call → unchanged stack.
- `review_stack(stack: Stack, _client_cls=None) -> list[str]`: prompt = per-resource summary (id, kind, image/port/env KEYS only — never env values, they may hold secrets) + tool `report_findings` schema `{"findings": [str]}`; returns the collected findings (empty on failure/none). System prompt: security reviewer for a LOCAL single-host orchestrator — exposed ports, plaintext creds in field values, unpinned/latest images, privileged-looking commands.

- [ ] **Step 1:** Failing unit tests: (a) complete: fake client invokes propose_changeset with a value for a missing field + a value for a USER-SET field → merged stack has the missing one with `provenance="ai"` and the user one untouched; (b) no tool calls → stack unchanged; (c) SDK raise → stack unchanged (caught, logged); (d) review: fake client reports two findings → returned; prompt contains env KEY but not env VALUE (assert a sentinel secret value does not appear in the prompt the fake captured); (e) `/review` route wired (TestClient, `create_app(..., complete=False)` — route works with an injected fake via `app.state` seam if needed; simplest: unit-test `review_stack` directly and keep the route test to a 200 + shape check with the brain mocked by dependency injection through `create_app(review_fn=...)` — add that kwarg mirroring `complete_fn`).
- [ ] **Step 2-4:** RED → implement → GREEN + suite. UI: `cd ui && bunx tsc --noEmit && bun run build`.
- [ ] **Step 5:** Integration (real SDK, marked): apply a canvas with an rds node with NO fields → completed stack has ai-provenance username/password/port; `review_stack` on a stack with `password` field value `"admin"` and a service publishing port 80 returns ≥1 finding. Run: `uv run pytest -m integration tests/agent/test_brain.py -q`.
- [ ] **Step 6:** Commit `feat(agent): brain speaks only through the toolbelt (propose_changeset / report_findings)`.
