# C4 — M8 Region-Select Debugging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drag-select canvas nodes → "Ask the operator" menu → a region-scoped agent gets each node's desired state, observed state, recent events, and logs, and answers "what's wrong here?" with per-node suspects.

**Architecture:** A pure context assembler (`agent/debugger.py`) turns (store, runtime, events, env, node_ids) into a compact JSON context; one toolbelt run (C3's `run_toolbelt` + a `report_diagnosis` tool) produces `{answer, suspects}`; `POST /agent/debug` serves it; the UI shows a floating menu over the current ReactFlow selection and renders the result in a side panel. Spec: `docs/superpowers/specs/2026-07-22-v030-real-backings-design.md` §C4.

**Tech Stack:** FastAPI, claude-agent-sdk via `agent/toolbelt.py` (C3 Task 1 — must land first), React/ReactFlow.

## Global Constraints

- Depends on C3 Task 1 (`run_toolbelt`, `make_tool`) — same membrane, no free-text parsing.
- Best-effort: SDK failure → `{"answer": "agent unavailable", "suspects": []}` with a logged exception; the route never 500s for agent reasons.
- Env-var VALUES never enter the agent prompt (keys only) — same secrecy rule as C3.
- Log tails capped (40 lines/node) and events capped (last 10/node) — the context must stay small.
- Unit suite green per task; real-SDK test marked `integration`. Commit per task; don't push.

---

### Task 1: Context assembler + `/agent/debug` route

**Files:**
- Create: `src/odin/agent/debugger.py`
- Modify: `src/odin/server.py` (route; wire with store/runtime/ws_manager already on `app.state`)
- Test: `tests/agent/test_debugger.py` (new)

**Interfaces:**
- Produces: `assemble_context(stack, world, events: list[dict], logs: Callable[[str], str], node_ids: list[str]) -> dict` — per node id: `{"desired": {field: {"value": ..., "provenance": ...}} with env VALUES redacted to key list, "refs": [...], "observed": {"phase", "facts", "verdict", "restarts"} or None, "events": last 10 events for that node, "logs": logs(node_id) or ""}`. Nodes not in the stack are included with `{"desired": None}` (the UI may select stale tiles).
- Produces: `async diagnose(context: dict, question: str, run=run_toolbelt) -> dict` — one toolbelt run with tool `report_diagnosis` schema `{"answer": str, "suspects": [{"node_id": str, "reason": str}]}`; returns the collected payload or the fallback `{"answer": "agent unavailable", "suspects": []}`.
- Route: `POST /agent/debug` body `{"env": "default", "node_ids": [...], "question": "what's wrong here?"}` → `{"env", "answer", "suspects"}`. Events come from `ws_manager.get_events(env)`; logs via `runtime.logs(node_id, tail=40)` wrapped so a missing container yields `""` (runtime.logs already returns "" / raises? read `colima.py` and match its behavior).

- [ ] **Step 1:** Failing unit tests: (a) assembler shape for one healthy + one crashed + one unknown node from a hand-built Stack/World/events/fake-logs (assert env values redacted, event/log caps enforced); (b) `diagnose` with a fake `run` that invokes the tool with a canned payload → returned verbatim; fake `run` that raises → fallback shape; (c) route: TestClient + fakes → 200 with answer/suspects (wire fakes via `create_app` state or kwargs — mirror how `/apply` tests fake things in `tests/api/test_apply.py`, read it first).
- [ ] **Step 2-4:** RED → implement → GREEN + `uv run pytest -q`.
- [ ] **Step 5:** Commit `feat(agent): region debug — context assembler + /agent/debug`.

---

### Task 2: UI — selection menu + diagnosis panel

**Files:**
- Create: `ui/src/components/RegionAsk.tsx`
- Modify: `ui/src/components/Canvas.tsx` (track ReactFlow selection via `onSelectionChange`; render `RegionAsk` when ≥1 node selected)
- Test gate: `cd ui && bunx tsc --noEmit && bun run build`

**Interfaces / contract:**
- `RegionAsk` props: `{selectedIds: string[], env: string}`. Renders a small fixed-position pill bar (bottom-center of the canvas viewport, above the canvas, z-40, matches the dark industrial theme + 20px grid) with: "What's wrong here?" button, a free-form input + Ask button. On submit: POST `/agent/debug` with `{env, node_ids: selectedIds, question}` (question = the button's canned text or the input). While pending: button shows a subtle busy state (no spinner libraries). Result renders in a right-side panel (reuse the app's existing panel styling conventions — read `ConfigPanel.tsx` for classes): the answer text, then suspects as a list of `node_id — reason` rows.
- Node ids on the canvas = the node LABEL (`data.label`), matching how `translate.py` keys resources — send labels, not ReactFlow ids. Selection → labels via the selected nodes' `data.label`.
- Keep all styling consistent with existing components (near-black bg, bright border, neon accent); 20px grid multiples.

- [ ] **Step 1:** Read `Canvas.tsx` (how panels/topbar are laid out, how env is known — TopBar owns the env field; find where env state lives and thread it), `ConfigPanel.tsx` (panel styling), then implement.
- [ ] **Step 2:** `cd ui && bunx tsc --noEmit && bun run build` → clean.
- [ ] **Step 3:** Manual browser check happens in the controller's e2e phase (playwright) — not this task's gate.
- [ ] **Step 4:** Commit `feat(ui): region-select ask — menu + diagnosis panel (M8)`.

---

### Task 3: Real integration test

**Files:**
- Test: `tests/agent/test_debug_e2e.py` (new)

- [ ] **Step 1:** Marked `integration`, needs Colima + Claude: apply a canvas with a service node whose image is deliberately broken (`image: "busybox"`, `command: ["false"]` — it will crash-loop to `crashed`) plus a healthy dep (`redis:7-alpine`); wait for `crashed`; POST `/agent/debug` for both nodes asking "what's wrong here?"; assert 200, non-empty `answer`, and that some suspect names the crashing node. Teardown: `/destroy`, assert no `allfather=1` containers remain.
- [ ] **Step 2:** `uv run pytest -m integration tests/agent/test_debug_e2e.py -q` → passed.
- [ ] **Step 3:** Commit `test(agent): region-debug e2e — the agent fingers the crashing node`.
