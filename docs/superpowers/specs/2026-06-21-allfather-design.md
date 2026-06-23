# ALLFATHER — Design Spec

**Status:** Approved design (brainstorm complete 2026-06-21). Implementation begins on branch `allfather`.
**Repo:** `odin` (consolidation target). `clockwork` and `styx` are reference patterns only — no code dependency.
**License:** Apache-2.0. Every dependency must be permissive (Apache-2.0 / MIT / BSD / MPL / ISC).

---

## 1. Vision

ALLFATHER is a Mac-native, visualizable, intelligence-backed orchestration system. Mental model: **"Railway, but local-first on a single Mac, with an AI operator."** You drop applications and infrastructure onto a visual canvas; an AI places and configures them; allfather's own control loop runs and supervises them *for real* on the Mac; status streams back to the canvas live.

It is built by evolving the existing `odin` codebase (Python + React/ReactFlow). It is **not** a greenfield rewrite — almost every organ already exists in embryo. We re-shape two organs (the one-shot Orchestrator becomes a continuous Reconciler; the Moto/OpenTofu validate path becomes an embedded MiniStack + the control loop) and keep the rest.

### Goals
- Drop app + infra nodes on a canvas; AI completes config + proposes placement; apply runs them for real, locally.
- Run all four workload classes: long-running services, batch/agentic jobs, dev/data dependencies, local LLM inference (via `omlx`).
- Continuous supervision: restart on crash, re-place on host failure, memory-aware scheduling on one 48GB host.
- Live, trustworthy status on the canvas (status is a strict projection of observed reality).
- Single Mac now; multi-Mac fleet over a self-hosted Nebula mesh later (additive, behind ports).

### Non-goals (explicitly out of scope)
- Real AWS-IAM policy *enforcement* (no permissive engine exists; building one is engineer-months). IAM is *modeled* + AI-sanity-checked; real boundaries come from the substrate.
- Multi-tenancy, billing, regions, RBAC, horizontal autoscaling, public-internet TLS. On 48GB the AI schedules-and-evicts to fit memory; it does not scale out.
- Being a high-fidelity AWS test double for rehearsing exact cloud deploys.

---

## 2. Locked decisions (do not relitigate)

1. **Language:** Python backend + React/ReactFlow frontend. Single repo = `odin`.
2. **Two layers on one canvas:** INFRA nodes (the "where" — AWS-shaped: VPC/subnet/security-group/host + the ~55 AWS services MiniStack emulates) + APP nodes (the "what" — user services, batch/agentic jobs). The AI places app nodes onto infra and completes config.
3. **Workloads (all four):** services (long-running, supervised), batch/agentic jobs (run-to-completion), dev/data deps (Postgres/Redis/etc.), local LLM inference via `omlx` (memory-aware load/evict).
4. **Control model:** allfather owns a desired-state spec + a **continuous reconciler/control loop** (not one-shot IaC apply). Nomad-shaped; Nomad itself is out (BUSL).
5. **Scope:** single Mac now (bin-pack one host's mem/CPU/GPU); a `Node` abstraction with `localhost` as its only impl now; multi-Mac over a self-hosted Nebula mesh (you own the lighthouse — private network, build a control plane/UI on top; chosen over Tailscale) is a later additive milestone (then memberlist/raft, both MPL).
6. **Brain:** reuse odin's claude-agent-sdk agent + clockwork's "schema-native completion" pattern (under-specified Pydantic models; LLM fills missing fields; **user values always win**; track which fields the AI filled). LLM **generates + places + proposes changesets + sanity-checks IAM**; deterministic code **reconciles**; deterministic **assertions verify** (the LLM never verifies).
7. **AWS emulation = MiniStack-central.** MiniStack (Python, MIT, ~55 services) is **forked + embedded in-process** (`ministack.app:app`, a bare ASGI3 callable, mounted in allfather's FastAPI app). Its container-spawn step is **rewired to allfather's Runtime driver** so its containers join allfather's World. 8 services spawn real containers (RDS→Postgres/MySQL, ElastiCache→Redis, ECS→Docker tasks, EKS→k3s, Lambda→RIE, Glue, MWAA, OpenSearch); the rest are in-memory control-plane.
8. **IAM:** no enforcement engine. Modeled on the canvas (edges-with-permissions) + AI sanity-check (least-privilege / blast-radius). Real boundaries = container namespaces/capabilities/seccomp + overlay-network firewall rules (odin's `sg_rules_to_firewall` already does SG→Nebula).
9. **Runtime:** odin's existing `SimulationRunner` (real Lima VMs + nerdctl containers + per-network Nebula overlay, clean teardown) is generalized into the Runtime driver. Runtime drivers (lima/nerdctl now; apple-container, linux later) and the Fabric (localhost/`*.local` now; self-hosted Nebula mesh later) are **platform-gated plugin seams**.
10. **UX = Railway minus the cloud:** spatial canvas primary view; live status painted on tiles (state + CPU/RAM/GPU + log tail); reference variables `${{node.VAR}}` that auto-draw edges; staged-changes/changeset model (review a diff, apply atomically — pairs with the AI proposing the changeset); environments as cheap experiment copies; config-as-code layered over the GUI.

### The six settled design decisions
1. **Spec format:** append-only content-addressed whole-canvas JSON revisions per env under `.odin/<env>/` (defer SQLite). Gives free changeset diffs, environment-as-fork-the-head-rev, crash-safe replay.
2. **App packaging:** image reference for the skeleton → repo-build via `nerdctl build` (odin's `ContainerManager.build_image` already exists) by M2 → buildpack auto-detect later.
3. **Reference variables:** reconcile-time **late-binding**, gated on the dependency's assertion passing (compile-time breaks on first boot).
4. **Brain invocation:** **hybrid** — the agent may reason/stream text, but the *only* state it can produce is via typed MCP tools (`write_candidate` / `place` / `review_iam`); `read_*` tools are read-only. Satisfies the structured-I/O rule + the "LLM only writes candidates" invariant.
5. **MiniStack env model:** one embedded instance, **namespace AWS state per-env** inside it (fork change, scheduled M6). Cheap on 48GB, enables side-by-side environments.
6. **Batch/LLM vs service scheduling:** **one Reconciler loop, per-kind policy** — service = level-trigger-always-heal, batch = run-to-completion-retire, llm = memory-managed-evictable. Keeps a single total, testable `plan()` core.

---

## 3. Architecture

### 3.0 Corrections from spec review (read first)
Ground-truth fixes an implementer must know (verified 2026-06-21 against real code):
- The MCP `@tool` decorators + `create_sdk_mcp_server` + the `allowed_mcp_tools` allowlist live in **`agent/client.py`** (not `mcp/tools.py`). `mcp/tools.py` is the plain `OdinTools` backing class. The Brain Toolbelt evolves `agent/client.py`.
- **Reference variables `${{node.VAR}}` are NEW**, not a reuse. `iam.ts` only auto-detects IAM edges from node-*type* pairs (`detectEdgeTypes`); there is no `${{…}}` parsing anywhere today.
- The **Status Spine is EVOLVED, not KEPT**: `ConnectionManager`/`events.jsonl`/the registry are reused, but collapsing the emitted messages into one `WorldDelta` schema + adding `metric_sample`/`log_line` is real work (owned by S1). The actual emitted types today are `resource_draft|error|live|removed|validated|validating` (+ `simulating`/`simulated` from the Simulate runner); `StatusBadge` also styles `deploying`.
- **Schema-native completion is NEW.** `build_suggest_defaults_prompt` is a free-text prompt asking for a JSON array (text-parsed), not Pydantic/tool-structured output. It is a reasonable *seed* only.
- **Catalog counts:** backend `RESOURCE_SPECS` = 25 entries; `ui/src/lib/catalog.ts` = 19 (the drift is real). Codegen makes **backend `RESOURCE_SPECS` canonical** and generates `catalog.ts`. "~55" is MiniStack's full set (the M5 target), derived from MiniStack's real backend table, not a fixed number.
- **`get_instance_type` cannot seed the bin-packer directly:** it returns EC2 sizes with memory as a *string* (`"1GiB"`) and no GPU. The Scheduler needs new numeric `(mem,cpu,gpu)` footprint fields on the Schema Registry — an adaptation, not a drop-in.
- **`omlx`** is an unresolved dependency contract: pin a version, **verify it is permissively licensed** (§7), and define its load/evict/endpoint interface before M3. Treated as "an OpenAI-compatible local model endpoint" until then.
- **`World` authorship vs emission:** drivers + the Assertion Engine *author* facts/verdicts; the Reconciler is the sole *serializer/emitter* to the Status Spine — it never originates a fact. The Brain's re-placement output is a *candidate* that goes through the normal apply path; the Scheduler holds final placement authority (the membrane never leaks an LLM write into the World).

### 3.1 The spine and the invariant

A **Spec Store** holds, per environment, two frozen Pydantic documents plus proposed changes:
- **Stack** — desired state (written by the Canvas API and the Brain).
- **World** — observed state (written *only* by drivers + the Assertion Engine).
- **Changeset** — a proposed diff awaiting review/apply.

**The invariant that creates every clean seam (enforced structurally, not by convention):**

> The LLM only ever writes a *candidate* Changeset into desired state. Deterministic code reads `(Stack, World)` and emits idempotent Actions through narrow driver ports. Deterministic Assertions read the World and decide health. **The LLM never applies, starts, stops, or verifies.**

Enforcement mechanism: odin already has an in-process MCP server (`@tool` + `create_sdk_mcp_server`) with an `allowed_mcp_tools` allowlist. The Brain's tools are read-only over World/Stack/HostFacts and write-only into a candidate Changeset. The allowlist *is* the audited capability surface; if a `start`/`apply` tool ever leaked into it, the invariant would silently die.

**Second load-bearing rule — one container-spawn authority:** allfather's Runtime driver is the *only* code that runs a container. MiniStack keeps its per-service knowledge (which image/creds/config, the `CreateDBInstance`→Postgres trigger) but its container-spawn primitive is **rewired (overridden from an allfather bootstrap) to call allfather's Runtime driver and register the container in the World.** So an "RDS node" and a "user Postgres node" become the same Postgres container via the same run path — nothing double-spawns, the World is one coherent ledger, and AWS-service containers inherit supervision, memory budgeting, the overlay, live tiles, and AI monitoring for free.

### 3.2 Layer diagram

```
 CANVAS UI (ReactFlow — KEPT)  ─ pure projection of WORLD + staged diff
   │ edits → candidate Changeset                 ▲ WorldDelta stream (WS)
   ▼                                             │
 Canvas API ──►   S P E C   S T O R E   ◄── Status Spine (events.jsonl + WS)
                  Stack(desired) │ World(observed) │ Changeset
        ┌─────────────┼───────────────┬────────────────┐
        ▼             ▼               ▼                 ▼
      BRAIN       RECONCILER      SCHEDULER       ASSERTION ENGINE
   (LLM, writes  (plan(Stack,    (bin-pack mem/   (port open? HTTP 200?
    CANDIDATE     World)→Actions, cpu/gpu, batch   model loaded? →
    only)         supervise)      queue, LLM evict) Verdict→World)
                      │
        ┌─────────────┼──────────────────┐
        ▼             ▼                  ▼
     RUNTIME       FABRIC         SERVICE-BACKEND ──► MiniStack embed (/aws, forked)
     (lima+        (localhost+     decision: control-    ~47 in-mem control-plane
     nerdctl;      *.local now;    plane(no-op) vs        + 8 real-container svcs
     apple@)       nebula@)        container→Runtime      (spawn rewired→Runtime)
```

### 3.3 Components

Legend: **KEPT** (reuse ~verbatim) · **EVOLVED** (reshape existing) · **RETIRED** · **NEW**.

| Component | Status | Role | Evolves from |
|---|---|---|---|
| **Canvas UI** | KEPT | ReactFlow canvas; INFRA + APP render layers; live status tiles; `${{node.VAR}}` ref edges auto-drawn; staged-Changeset diff review + atomic Apply | `Canvas.tsx`, `ConfigPanel`, `Sidebar`, `BottomPanel` (loads `/events`), `StatusBadge` (consumes any `resource_*`), `iam.ts` auto-edge |
| **Spec Store** | EVOLVED→NEW | Per-env Stack + World + Changeset; atomic `apply`; World subscription; append-only content-addressed revisions under `.odin/<env>/` | `ResourceRegistry` (`registry.json`)→World; `canvas.json`→Stack; registry `save()` pattern; **new:** provenance tags, Changeset, per-env lineage |
| **Schema Registry** | EVOLVED | ONE typed catalog of every node kind (~55 MiniStack services + 6 infra nodes + 4 workload kinds); each an under-specified Pydantic model with field provenance + memory_footprint + backing hint; **codegen** kills the `RESOURCE_SPECS`↔`catalog.ts` drift | `resources.py` `RESOURCE_SPECS`; replaces hand-maintained `catalog.ts` |
| **Canvas API** | EVOLVED | Canvas edits → candidate Changeset; environments as cheap copies | `create_canvas_router` (`/canvas`); retires `create_validate_router` |
| **Brain** | EVOLVED | The only LLM unit. COMPLETE (fill missing fields, user wins, tag `provenance=ai`), PLACE (propose host bin-pack — a hint), CHANGESET (author a reviewable diff), IAM SANITY-CHECK (findings). Never reconciles/verifies | persistent `ClaudeSDKClient`, session persistence, `_send_and_stream`, `build_suggest_defaults_prompt` as the completion seed; re-skill `prompt.py` HCL→completion |
| **Brain Toolbelt** | EVOLVED | The structural AI/deterministic membrane: read-only `read_world`/`read_stack`/`read_host_facts`/`check_feasibility`, write-only `write_candidate`/`place`/`review_iam`. No tool can apply/start/stop | `mcp/tools.py` `@tool` + `allowed_mcp_tools`; retires `validate_infrastructure` (tofu) |
| **Reconciler** | EVOLVED | The Nomad-shaped core. Long-lived async loop; pure `plan(Stack, World)→[Action]` that is **total + idempotent** (crash-safe, fixture-testable); supervises, restarts/re-places, advances batch queue, drives LLM load/evict | rewrites `orchestrator.py` core; keeps `_prune_stale` (delete-extra), per-node status mapping, `_broadcast` |
| **Scheduler / Placement Binder** | NEW | Deterministic bin-packer over (mem,cpu,gpu) on one host; owns the batch FIFO queue + LLM load/evict policy. Brain proposes, Scheduler is the authority that validates feasibility | new; footprint seeds from `compute/models` `get_instance_type` |
| **Assertion Engine** | NEW | Read-only health verification: container Up *and* port open *and* HTTP 200; LLM model loaded; MiniStack describe ready. The verifier the LLM may not be | new; replaces the role tofu diagnostics played |
| **Runtime Driver port** | EVOLVED | Platform-gated plugin seam; the ONLY code that shells out to limactl/nerdctl/container. `ensure_host`/`run_container`/`stop`/`exec`/`facts()`/`stats()` | `SimulationRunner` + `VmManager` + `ContainerManager` + `cloud_init` + `lima_yaml` → the `lima` impl; **new:** `stats()` sampling |
| **Fabric Driver port** | EVOLVED | Inter-node addressing + real security boundaries (overlay firewall). `register`/`resolve(ref)`/`apply_edge_policy`/`facts()` | `NebulaManager` + `VpcOverlay` + `network/models` → overlay impl; `sg_rules_to_firewall` IS `apply_edge_policy`; **new:** localhost + `*.local` resolver |
| **Service-Backend decision** | EVOLVED | For each AWS-shaped node, decide backing: MiniStack control-plane (no-op at runtime) vs real container (→ Runtime driver). Prevents double-spawn | `SimulationRunner.SERVICE_CONTAINERS` (s3→RustFS, rds→Postgres, sqs→ElasticMQ already mapped) |
| **MiniStack Embed** | NEW (RETIRES Moto/Tofu) | Forked MiniStack ASGI app mounted at `/aws` as the emulated AWS control plane; its spawn rewired to the Runtime driver | replaces `simulator/engine.py` (MotoEngine), `terraform/runner.py` (TofuRunner) |
| **Status Spine** | KEPT | Strict one-way projection: driver facts → World → typed `WorldDelta` → WS broadcast + append-only `events.jsonl`. UI has zero status write-path | `ConnectionManager` (WS + events.jsonl), `ResourceRegistry` as World, `{type}_{label}` key, `broadcast()`; collapse ad-hoc `resource_*` messages into one `WorldDelta` schema |

### 3.4 Data model

- **Stack** = `{env, rev, resources: [ResourceDesired], edges: [Edge], refs: [Ref]}`. `ResourceDesired = {id, kind, fields (each provenance-tagged user|ai|default), placement_hint?, refs}`. Whole-canvas declarative desired state (inherits odin's "rewrite the full graph" model), content-addressed, append-only. Environments are independent Stack lineages; a cheap copy = fork the head rev.
- **World** = `{env, resources: [ResourceObserved]}`. `ResourceObserved = {id, backing, phase (pending|starting|healthy|crashed|idle|queued|running|done|evicted|error), facts (cpu/ram/gpu/endpoint/logtail), verdict, restarts}`. Written only by driver facts + Assertion verdicts.
- **Changeset** = a proposed delta to the Stack (add/remove/wire nodes, filled fields), reviewed as a diff, applied atomically.
- Per-kind field models come from the Schema Registry: `ServiceSpec{image|repo, ports, env, health}`, `BatchSpec{image, cmd, inputs}`, `DepSpec{ministack_service, image}`, `LlmSpec{model, ctx, ttl, footprint}`, plus AWS infra specs.

### 3.5 The Reconciler

A single long-lived asyncio loop woken by (a) a Stack apply, (b) a periodic tick, (c) runtime watch events (container exited). Its one pure core:

```
plan(desired: Stack, observed: World) -> [Action]
```

`Action` is a typed union: `EnsureHost`, `RunContainer`, `StopContainer`, `CreateMiniStackResource`, `ApplyEdgePolicy`, `EnqueueBatch`, `LoadModel`, `EvictModel`, `NoOp`. `plan()` is **total + idempotent** — re-running on unchanged `(desired, observed)` yields only `NoOp`s — which makes the loop crash-safe (restart re-derives from the Spec Store) and trivially fixture-testable. A deterministic executor runs each Action, then runs its Assertion before marking phase.

**Per-kind lifecycle policy (one loop, no separate controllers):**
- **service** — level-triggered toward healthy; crash ⇒ `RunContainer` (restart with backoff), or re-place if the host is gone (the one place the loop re-invokes the Brain, for fresh placement).
- **batch** — `queued → running → done`; the Scheduler holds a FIFO queue gated by capacity; completion retires the job; a crashed batch reports error and frees its slot (no auto-restart).
- **llm** — memory-managed; `LlmSpec.footprint` feeds the Scheduler; under memory pressure `Scheduler.evict_plan()` picks victims (idle-LRU, never-evict-pinned) and `plan()` emits `EvictModel`/`LoadModel`. A "crashed" LLM under pressure is an intentional eviction, not a failure.

Placement is deterministic first-fit-decreasing over `(mem,cpu,gpu)` on one host inventory; the Brain's proposal seeds preferred hosts but the Scheduler has final authority and rejects infeasible hints (the staged-changeset UX surfaces "AI proposed X, packer chose Y, here's why").

### 3.6 MiniStack integration

MiniStack is vendored as a fork (`kessler-frost/ministack`, MIT) under `src/odin/ministack/` (or a pinned `pip install git+...@sha`) and **imported, not subprocessed**. Its bare ASGI3 callable `ministack.app:app` is configured from env at import (one global instance/process), exposed two ways:
- `app.mount("/aws", ministack_app)` inside odin's `create_app()` for browser/boto3 traffic.
- An in-process `httpx.AsyncClient(transport=ASGITransport(app=ministack_app))` for allfather's own boto-shaped calls (zero network).

This directly replaces `simulator/engine.py` (MotoEngine subprocess) and `terraform/runner.py` (TofuRunner); the Moto/OpenTofu validate path is retired wholesale.

Two seams keep MiniStack opaque upstream: (1) an AWS-API endpoint for clients; (2) a one-time feed of its service catalog into the Schema Registry so every emulated service becomes a canvas node kind (codegen drives the ~25→~55 UI parity automatically).

**Spawn rewire (mechanism confirmed by spec review, 2026-06-21):** MiniStack's container spawning is *scattered* across 8 service modules (no central helper). Each module has a private `_get_docker()` → module-global `_docker` (lambda_svc and opensearch use `_docker_client`) and calls `<client>.containers.run(...)` inline. We override **from an allfather bootstrap, without editing service files**: eagerly `import ministack.services.rds as rds` then set `rds._docker = AllfatherDockerShim()`. The shim duck-types the `docker` client surface MiniStack uses (`.containers.run(image, environment, ports, name, labels, …) → obj` with `.id`/`.status`/`.reload()`/`.attrs`/`.remove()`/`.stop()`); inside `run()` allfather's Runtime driver boots the real container (Lima/nerdctl) and the shim reports liveness, so MiniStack's control-plane bookkeeping (instance dict, status flip, endpoint) runs unchanged and no real Docker is touched. Patch *after* importing each module (lazy import resets the global). This is N per-service monkeypatches, one per real-container service we support — not one seam.

**Embedding rules (review-confirmed):**
- **Set env before import:** `MINISTACK_HOST`, `MINISTACK_ACCOUNT_ID` before `import ministack.app`; `RDS_BASE_PORT` etc. before the first RDS request. `MINISTACK_HOST` is frozen into regexes at import.
- **Do NOT run MiniStack's lifespan in-process.** Its startup/shutdown calls `_stop_docker_containers()` which force-removes any container labelled `ministack=rds|ecs|…` via a real `docker.from_env()` — it would reap allfather's containers. `httpx.ASGITransport` does not run lifespan by default; keep it that way (the per-request path works without it). Use non-colliding container labels in the shim regardless.
- **Deps:** the `docker` SDK and `psycopg2` are NOT MiniStack base deps. The shim removes the need for a real docker daemon, but MiniStack's RDS readiness probe imports `psycopg2` to auth-probe the booted Postgres; add `psycopg2-binary` to allfather's venv or readiness degrades to a flaky 1s TCP check.
- **Per-env (M6):** control-plane state is already account-scoped (a distinct 12-digit access key → isolated state, ARNs, uniqueness checks), so map env → synthetic account id. BUT real container names (`ministack-rds-{db_id}`) and the global host-port counter are NOT env-scoped — allfather must namespace names/ports inside the runner shim.

A weekly GitHub Action keeps the fork synced (merge upstream + pytest + open a PR only on conflict); allfather pins to a fork **commit/tag** the CI bumps and asserts the `_docker` monkeypatch targets exist at startup (fail fast on drift — MiniStack moves daily).

### 3.7 Networking and references

Nodes address each other *only* through resolved references, never hardcoded endpoints. A `${{node.VAR}}` on the canvas becomes a typed Ref edge in the Stack (auto-drawing the visual edge, preserving odin's `iam.ts` auto-detect UX). At reconcile time the **Fabric driver** (not the LLM) resolves: `resolve(ref) → Address` appropriate to the active fabric. localhost impl: each container gets a host port recorded as a World fact + a friendly `<node>.local` on the per-network Nebula overlay (reused from `VpcOverlay` + `NebulaManager`). A consumer never knows whether it's talking to localhost, `.local`, or (later) a tailnet name.

**Ordering:** `${{db.DATABASE_URL}}` can only resolve once `db` has chosen creds/port, so the reconcile plan **gates** `api`-start on `db`'s assertion passing. An unresolved ref is a deterministic assertion failure surfaced on the tile, never a silent empty env var.

IAM/SG edges are dual-purpose: modeled on the canvas (data for the Brain's sanity-check) and compiled by the Fabric driver into real overlay firewall rules (`sg_rules_to_firewall`). There is no simulated `AccessDenied`; the UX must make "modeled, not enforced" explicit.

### 3.8 Status spine

Strict one-way projection so the canvas can never disagree with reality. Drivers (Runtime + Fabric + Service-Backend) are the sole producers of observed facts; the Assertion Engine annotates each with a Verdict; the Reconciler is the single emitter. Every World mutation is a typed `WorldDelta` on two sinks odin already has: the WebSocket `ConnectionManager.broadcast` (live tile repaint) and append-only `events.jsonl` (durable replay; the `BottomPanel` already backfills from `/events`). Cleanup: collapse `resource_validating/validated/live/error/draft/simulating` into one `WorldDelta` schema with lifecycle phases. New signals (`metric_sample` cpu/ram/gpu each tick from Runtime `stats()`, `log_line` tailed from Runtime logs) ride the same channel.

---

## 4. Build plan

**Build principle (from spec review): the app stays bootable at every step.** The new spine is built *alongside* the old Moto/Tofu path, then cut over in S2 — never a dead window. Each step is a thin vertical slice with no forward dependency. Deferred out of the skeleton: Changeset (→M1), Nebula/`*.local` fabric + `apply_edge_policy` (→M7), the general Service-Backend control-plane-vs-container decision (→M2; S0 hardcodes RDS→Postgres), the Scheduler/bin-pack/batch/llm branches (→M3). The detailed, step-by-step plan lives in `docs/plans/` (writing-plans output).

### Walking skeleton (build first — proves the make-or-break no-double-spawn integration)
- **S0 — Embed MiniStack additively + prove the spawn-rewire in isolation.** Add the forked MiniStack dep + `psycopg2-binary` + `docker` SDK types; mount `ministack.app:app` at `/aws` in `create_app()` (additive — **do not** remove Moto/Tofu yet; set `MINISTACK_HOST`/`MINISTACK_ACCOUNT_ID` before import; never run its lifespan). Build a minimal `AllfatherDockerShim` + a thin runner that boots a real Postgres (reuse `ContainerManager`/`VmManager`). Monkeypatch `rds._docker = shim` from a bootstrap. Acceptance (a standalone integration test, app still boots normally): an in-process boto RDS `CreateDBInstance` → real Postgres up via allfather's runner → `DescribeDBInstances` returns an `available` endpoint reachable at `127.0.0.1:host_port`.
- **S1 — Add the spine alongside the old path (no cutover).** Stack + World Pydantic docs + the Spec Store (append-only canonical-JSON revisions under `.odin/<env>/`, sha256 content-address + a `HEAD` file, no GC in the skeleton); `World` subsumes `ResourceRegistry`'s role but both coexist for now. A minimal `RuntimeDriver` Protocol (`ensure_host`/`run_container`/`stop`/`facts`/`stats`) with a Lima impl wrapping `SimulationRunner`/`VmManager`/`ContainerManager` (+ new `stats()` cpu/ram sampling). A localhost `Fabric` that records each container's host port as a World fact (no Nebula, no `.local`). Define the single `WorldDelta` schema + lifecycle phases and update `StatusBadge`/`BottomPanel` to consume it (this migration is owned here). Old orchestrator untouched.
- **S2 — Minimal Reconciler (service-only, single-host) + cut over.** `plan(Stack, World) → [Action]` handling exactly `RunContainer(app)` and `CreateMiniStackResource + RunContainer(rds)` — **no Scheduler, no queue, no evict, no batch/llm branches** (total+idempotent over just these). Two inline assertions land here (app `HTTP 200`, Postgres `pg_isready`) — M4 later generalizes them into a per-kind registry. Re-skill the agent: a new completion prompt (replacing the HCL `build_system_prompt`) + a typed `write_candidate` MCP tool (the start of the Brain Toolbelt). `${{db.DATABASE_URL}}` resolved at reconcile-time by the Fabric (localhost host-port), gated on db's assertion with a `blocked` phase + timeout (never-healthy fails loudly). Add a `/apply` route that drives the Reconciler; **now** retire the Moto/Tofu/`/validate`/deploy routers (the Reconciler replaces them, so the app stays bootable). Clear `.odin/agent_session_id` so the new prompt takes effect.
- **S3 — Run the slice end-to-end (UI + playwright).** Drop `api` + `db` on the canvas, wire `${{db.DATABASE_URL}}`, apply; the app container on a Lima host reads the live Postgres; healthy badge + CPU/RAM tile painted via `ConnectionManager`; kill the container → the Reconciler restarts it; destroy → clean teardown (0 VMs, 0 containers, 0 orphan processes).

### Milestones (additive, behind already-proven ports; several parallelizable)
- **M1** — Brain as full candidate-only producer behind the Brain Toolbelt MCP: `place` + `propose_changeset` + `review_iam`; staged-changeset diff UI + atomic Apply.
- **M2** — Service-Backend container backing for the remaining 7 real-container AWS services; repo-path build via `nerdctl build`.
- **M3** — Scheduler bin-pack + batch FIFO queue + omlx load/evict (memory-aware) on the single 48GB host.
- **M4** — Assertion Engine per-kind probes + full supervision/restart/re-place.
- **M5** — Catalog codegen from MiniStack's service list → ~55-service UI parity (pure data, parallelizable).
- **M6** — Environments as cheap copies + per-env MiniStack namespacing + config-as-code precedence.
- **M7** (later, no core change) — new Runtime/Fabric impls: apple-container CLI, self-hosted Nebula mesh fabric (NOT Tailscale — see §3.7), multi-Mac memberlist/raft.
- **M8 — Region-select debugging ("what's wrong here?")** (UX, no core change) — drag a selection rectangle over a canvas region → context menu ("Debug this" / "What's wrong here?" / "Fix this part" / free-form ask) → a region-scoped agent AUTO-GATHERS the enclosed nodes + edges and, for each, its World state (phase/facts/verdict/restarts) + recent events/logs + relevant Stack fields, then investigates/fixes from there. Reuses the existing Cmd+drag selection; the new parts are the menu + a context-assembler that turns a selection into the agent prompt. Goal: the user points at a region instead of describing it — far less back-and-forth.

M1, M5, and M2's per-service work are independently dispatchable to parallel agents once S0–S3 lands.

---

## 5. Testing strategy

- **Unit:** `plan(Stack, World)→[Action]` is a pure function — fixture-test every lifecycle transition (create, restart, re-place, batch retire, llm evict, no-op idempotence). Assertions and the Spec Store are pure/IO-thin and unit-tested.
- **Integration:** the Service-Backend + Runtime path (RDS create → real Postgres up → endpoint back to MiniStack), ref resolution + ordering gate, teardown leaves 0 VMs / 0 containers / 0 orphan processes.
- **End-to-end (playwright-cli):** drive the real UI — drag nodes, wire `${{db.DATABASE_URL}}`, AI complete, apply, watch tiles go starting→healthy, kill a container → auto-restart, destroy → clean teardown. **Re-validate the earlier `docs/SCENARIOS.md` cases against the new MiniStack + control-loop architecture.**
- **Cleanup discipline:** every test tears down its VMs/containers; prune build artifacts; never leave bloat (limited disk).

---

## 6. Risks

- **No-double-spawn integration (S0)** is the highest-risk seam — proven first, on purpose.
- **MiniStack spawn-override point** depends on MiniStack internals (one helper vs scattered) — confirm at M2; fallback is centralize-and-upstream.
- **Reconciler correctness** — mitigated by the total/idempotent pure `plan()` + fixture tests.
- **Memory pressure on 48GB** with multiple real containers + LLMs — the Scheduler's evict policy is load-bearing; test under pressure.
- **Fork drift** — auto-sync CI + additive-only modifications keep it manageable.

---

## 7. Constraints (standing)

- Permissive licenses only (Apache-2.0 / MIT / BSD / MPL / ISC). No GPL/AGPL/BUSL/SSPL.
- Clean up after every build/test (limited disk) — prune containers, VMs, build artifacts, scratch.
- User-facing copy: plain voice, no em dashes, no generic claims.
- `uv` for Python, `bun` for JS, Colima as the Docker runtime where Docker is needed.
