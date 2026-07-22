# C2 — llm `claude` backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** llm nodes get a `backend` field — default `claude` (no container: an Anthropic-compatible proxy on the odin server forwards to `claude-agent-sdk`, auth rides the user's Claude login), plus `endpoint` (external Anthropic-compatible base_url) and `container` (today's image path, unchanged).

**Architecture:** A `LlmProxyRegistry` lives on the app; the Reconciler registers/unregisters claude-backend llm nodes instead of running containers, and publishes `ANTHROPIC_BASE_URL` facts that containers can reach (`host.docker.internal:{ODIN_PORT}/llm/{env}/{id}`). A FastAPI route implements POST `/llm/{env}/{node_id}/v1/messages` (non-streaming) by flattening the Messages payload into one `claude_agent_sdk.query()` call. Spec: `docs/superpowers/specs/2026-07-22-v030-real-backings-design.md` §C2.

**Tech Stack:** FastAPI, claude-agent-sdk (`query`, `ClaudeAgentOptions`, model `claude-sonnet-5`).

## Global Constraints

- `uv` never pip; imports at top; Pathlib; minimal if/else + try/except.
- `claude-agent-sdk` only — never the `anthropic` package.
- Unit suite green after every task: `uv run pytest -q`. Integration tests marked `integration`.
- The SDK call must strip nested-session confusion exactly like `brain.py` does: `os.environ.pop("CLAUDECODE", None)` before `query()`, `allowed_tools=[]`.
- Default model: `claude-sonnet-5`; the node's `model` field overrides.
- Facts consumed by containers use `host.docker.internal`; the server port comes from the `ODIN_PORT` env var (default `4200`) — `odin start` already knows the port and must export it to the uvicorn subprocess.
- Commit after each task; do NOT push (controller pushes).

---

### Task 1: LlmProxyRegistry + the Anthropic-compatible route

**Files:**
- Create: `src/odin/llm/__init__.py` (empty), `src/odin/llm/proxy.py`
- Modify: `src/odin/server.py` (instantiate registry, mount route, expose on `app.state`)
- Test: `tests/llm/test_proxy.py` (new; add empty `tests/llm/__init__.py`)

**Interfaces:**
- Produces: `LlmProxyRegistry` with `register(env: str, node_id: str, model: str) -> None`, `unregister(env: str, node_id: str) -> None`, `get(env: str, node_id: str) -> str | None` (returns the model, None if unregistered), `base_url(env: str, node_id: str, host: str, port: int) -> str` (= `f"http://{host}:{port}/llm/{env}/{node_id}"`).
- Produces: `create_llm_router(registry, ask) -> APIRouter` where `ask: Callable[[str, str, str], Awaitable[str]]` is `(model, system, prompt) -> text`. The default ask (`claude_ask`) lives in `proxy.py` and calls `claude_agent_sdk.query()` exactly like `brain._fill` does (pop CLAUDECODE, `ClaudeAgentOptions(system_prompt=system or None, allowed_tools=[], model=model)`, concatenate `TextBlock`s).
- Consumes: `create_app(...)` gains keyword `llm_ask=None` (None → `claude_ask`); tests inject a fake.
- Route contract: `POST /llm/{env}/{node_id}/v1/messages` with Anthropic Messages body `{model?, system?, max_tokens?, messages: [{role, content}]}` →
  - 404 `{"error": "unknown llm node"}` if not registered;
  - 400 `{"error": "streaming not supported"}` if body has `"stream": true`;
  - else 200 `{"id": "msg_{node_id}", "type": "message", "role": "assistant", "model": <registered or body model>, "content": [{"type": "text", "text": <ask result>}], "stop_reason": "end_turn", "usage": {"input_tokens": 0, "output_tokens": 0}}`.
  - Prompt flattening: `content` may be a string or a list of `{type:"text",text}` blocks — join text blocks; multi-turn messages become `"{role}: {text}"` lines joined by newlines; the trailing user turn is included the same way. `system` may also be string-or-blocks; flatten identically.

- [ ] **Step 1: Write failing tests** — `tests/llm/test_proxy.py`:

```python
"""The Anthropic-compatible llm proxy: registry + /v1/messages route."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from odin.llm.proxy import LlmProxyRegistry, create_llm_router


async def fake_ask(model: str, system: str, prompt: str) -> str:
    return f"[{model}] sys={system!r} saw: {prompt}"


def make_client() -> tuple[TestClient, LlmProxyRegistry]:
    registry = LlmProxyRegistry()
    app = FastAPI()
    app.include_router(create_llm_router(registry, fake_ask))
    return TestClient(app), registry


def test_unregistered_node_404s():
    client, _ = make_client()
    resp = client.post("/llm/default/brain/v1/messages",
                       json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 404


def test_messages_roundtrip_anthropic_shape():
    client, registry = make_client()
    registry.register("default", "brain", "claude-sonnet-5")
    resp = client.post("/llm/default/brain/v1/messages", json={
        "system": "be terse",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 64,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["model"] == "claude-sonnet-5"
    assert body["stop_reason"] == "end_turn"
    text = body["content"][0]["text"]
    assert "user: ping" in text and "be terse" in text


def test_content_blocks_and_multiturn_flatten():
    client, registry = make_client()
    registry.register("default", "brain", "claude-sonnet-5")
    resp = client.post("/llm/default/brain/v1/messages", json={
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
    })
    text = resp.json()["content"][0]["text"]
    assert "user: a" in text and "assistant: b" in text and "user: c" in text


def test_stream_true_rejected():
    client, registry = make_client()
    registry.register("default", "brain", "claude-sonnet-5")
    resp = client.post("/llm/default/brain/v1/messages",
                       json={"stream": True,
                             "messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 400


def test_registry_scopes_by_env_and_unregisters():
    registry = LlmProxyRegistry()
    registry.register("staging", "brain", "m")
    assert registry.get("staging", "brain") == "m"
    assert registry.get("default", "brain") is None
    registry.unregister("staging", "brain")
    assert registry.get("staging", "brain") is None
    assert registry.base_url("default", "b", "host.docker.internal", 4200) == \
        "http://host.docker.internal:4200/llm/default/b"
```

- [ ] **Step 2: Run to confirm failure** — `uv run pytest tests/llm/ -q` → ImportError.
- [ ] **Step 3: Implement `src/odin/llm/proxy.py`** per the interface block above. `claude_ask` mirrors `brain._fill`'s SDK usage (read `src/odin/agent/brain.py` first). The router handler: registered model lookup → 404 / stream check → 400 / flatten → `await ask(model, system_text, prompt)` → Anthropic-shaped dict.
- [ ] **Step 4: Wire into `create_app`** (`src/odin/server.py`): `llm_ask=None` kwarg; `registry = LlmProxyRegistry()`; `app.include_router(create_llm_router(registry, llm_ask or claude_ask))`; `app.state.llm_registry = registry`.
- [ ] **Step 5: Green + suite** — `uv run pytest tests/llm/ -q` then `uv run pytest -q`.
- [ ] **Step 6: Commit** — `feat(llm): Anthropic-compatible claude proxy (registry + /v1/messages route)`.

---

### Task 2: Reconciler support — `backend` field drives llm execution

**Files:**
- Modify: `src/odin/reconcile/reconciler.py` (llm branches in `_run_service`, `_observe_container` path, StopContainer, `_evictable_llms`), `src/odin/server.py` (pass registry + `ODIN_PORT` into reconcilers), `src/odin/__main__.py` (export `ODIN_PORT` to the server process in both start modes), `src/odin/reconcile/scheduler.py` (footprint: claude/endpoint-backend llm = 0.0)
- Test: `tests/reconcile/test_llm_backends.py` (new)

**Interfaces:**
- Consumes: `LlmProxyRegistry` from Task 1 (`register/unregister/base_url/get`).
- Produces: Reconciler gains `llm_registry=None, server_port: int = 4200` init kwargs. Behavior by llm `backend` field (default `"claude"` when absent — `spec/translate.py` unchanged, absence handled in reconciler + scheduler):
  - `claude`: RunContainer action → `registry.register(env, id, model)` (model = `fields["model"].value` or `"claude-sonnet-5"`), emit `healthy` with facts `{"ANTHROPIC_BASE_URL": registry.base_url(env, id, "host.docker.internal", server_port), "MODEL": model}`. No container. StopContainer → `registry.unregister` + prune. Observe: if registered → stays healthy (no per-tick SDK calls); if registry lost it (server restart) → re-register on next plan pass (plan sees phase from World: healthy → NoOp… so observe must emit `crashed` when `registry.get` is None for a phase-healthy claude llm, letting plan re-run it).
  - `endpoint`: requires `base_url` field; RunContainer → emit healthy with facts `{"ANTHROPIC_BASE_URL": base_url, "MODEL": model or ""}` after an HTTP probe of the URL succeeds (use the injected `http_ok` on `{base_url}/v1/models`, falling back to plain TCP is NOT needed — if probe fails, stay `starting`). StopContainer → prune only.
  - `container` (or legacy nodes with an `image` field and no `backend`): exactly today's path — treat `backend` default as `container` WHEN `image` is present and `backend` absent, so existing canvases keep working; otherwise default is `claude`.
  - Scheduler: `footprint()` returns 0.0 for llm resources whose backend is claude/endpoint; `_evictable_llms` excludes them.

- [ ] **Step 1: Write failing tests** with the existing fakes pattern from `tests/reconcile/test_reconciler.py` (FakeRuntime/FakeStore — read that file first; reuse its helpers by import if importable, else mirror). Cases: (a) claude llm apply → healthy, zero `rt.runs`, facts carry `ANTHROPIC_BASE_URL` ending `/llm/default/brain` and port 4321 when `server_port=4321`; (b) model field override respected; (c) destroy → unregistered + pruned; (d) registry wiped (simulate `registry.unregister`) while World says healthy → next tick emits crashed then re-registers → healthy; (e) endpoint backend: with `http_ok` fake returning True → healthy with the given base_url; (f) legacy llm node with only `image` still runs a container (rt.runs grows); (g) scheduler: claude llm admits at 0 footprint and is not in eviction candidates.
- [ ] **Step 2: Confirm failures**, **Step 3: implement**, **Step 4: suite green** (commands as in Task 1).
- [ ] **Step 5: Commit** — `feat(llm): backend field — claude (proxy), endpoint (external), container (legacy)`.

---

### Task 3: Real integration test (a real sonnet-5 answer through an applied llm node)

**Files:**
- Test: `tests/llm/test_llm_e2e.py` (new)

- [ ] **Step 1: Write the test** (marked integration; needs Claude auth + Colima NOT required):

```python
"""E2E: an applied llm node (backend=claude) answers a real /v1/messages call."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

CANVAS = {"nodes": [{"id": "n1", "type": "llm",
                     "data": {"label": "brain", "backend": "claude"}}],
          "edges": []}


def test_llm_node_answers_for_real(tmp_path):
    app = create_app(store=SpecStore(tmp_path), embed=False, complete=False)
    with TestClient(app) as client:
        assert client.post("/apply", json=CANVAS).status_code == 200
        # llm claude backend goes healthy without containers; poll /world
        for _ in range(30):
            world = client.get("/world").json()
            phases = {r["id"]: r["phase"] for r in world["resources"]}
            if phases.get("brain") == "healthy":
                break
        assert phases.get("brain") == "healthy"
        resp = client.post("/llm/default/brain/v1/messages", json={
            "messages": [{"role": "user",
                          "content": "Reply with exactly the word: pong"}],
            "max_tokens": 16,
        }, timeout=httpx.Timeout(120))
        assert resp.status_code == 200
        assert "pong" in resp.json()["content"][0]["text"].lower()
```

(If `create_app`'s backing kwarg has been renamed by the C1 switchover by the time this runs, use the renamed kwarg — check `server.py`.)

- [ ] **Step 2: Run it for real** — `uv run pytest -m integration tests/llm/test_llm_e2e.py -q` → 1 passed (a real sonnet-5 call, ~5-30s).
- [ ] **Step 3: Commit** — `test(llm): real sonnet-5 e2e through an applied llm node`.

---

### Task 4: UI — llm node config exposes backend/model/base_url

**Files:**
- Modify: `ui/src/lib/catalog.ts` (llm entry fields: add `backend` (default `claude`), `model`, `base_url`; sublabel copy), `ui/src/components/ConfigPanel.tsx` only if field rendering needs a select for backend (keep it a plain text field if selects don't exist yet — YAGNI).
- Test: `cd ui && bunx tsc --noEmit && bun run build` (the UI has no unit-test rig; type-check + build is the gate).

- [ ] **Step 1:** Read the llm entry in `ui/src/lib/catalog.ts` and one bespoke fields example in `ConfigPanel.tsx`'s `fieldsForType`.
- [ ] **Step 2:** Add the three fields with sensible placeholder copy (`claude | endpoint | container`, `claude-sonnet-5`, `http://localhost:1234/v1`). Default data: `backend: 'claude'`.
- [ ] **Step 3:** `cd ui && bunx tsc --noEmit && bun run build` → clean.
- [ ] **Step 4: Commit** — `feat(ui): llm node backend/model/base_url config`.
