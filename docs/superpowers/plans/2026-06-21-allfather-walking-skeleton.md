# ALLFATHER Walking Skeleton (S0–S3) — Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Evolve odin into the allfather walking skeleton — drop an `api` (app) node + a `db` (RDS) node on the canvas, wire `${{db.DATABASE_URL}}`, apply, and have allfather boot a real Postgres (via MiniStack's RDS API but allfather's own runner — no double-spawn), run a supervised app container that reads the live connection string, paint live status on the canvas, and auto-restart on crash.

**Architecture:** A Spec Store holds per-env `Stack` (desired) + `World` (observed) Pydantic docs. The LLM only writes candidate desired-state; a deterministic Reconciler `plan(Stack, World) → [Action]` drives reality through a Runtime driver; deterministic assertions decide health. MiniStack is embedded in-process as the AWS control plane with its container spawn rewired (via a `_docker` monkeypatch shim) through allfather's Runtime driver. **The app stays bootable at every step:** build the new spine alongside the old Moto/Tofu path, cut over in S2.

**Tech Stack:** Python 3.12+ (uv), FastAPI, Pydantic v2, claude-agent-sdk, forked MiniStack (ASGI3, embedded), Colima/nerdctl for real containers, React/ReactFlow UI (kept), pytest (asyncio_mode=auto).

## Global Constraints (every task inherits these)
- **Permissive licenses only** (Apache-2.0/MIT/BSD/MPL/ISC). Verify any new dep (incl. the MiniStack fork = MIT, `psycopg2-binary` = LGPL-with-exception → acceptable as a dynamically-linked binary; prefer it over GPL psycopg2-source).
- **Clean up after every build/test:** prune containers, VMs, scratch. Tests must tear down what they create. Limited disk.
- `uv` for Python, `bun` for JS, **Colima as the container runtime**. `python` (not `python3`).
- Pathlib for paths; imports at top of files; minimize if/else + try/except; structured I/O (Pydantic) over regex.
- The app must boot (`odin start --dev`) and `uv run pytest` must stay green after every task.
- Commit per task. Conventional commits. Co-Author trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Runtime decision for the skeleton (resolves the "Lima VM is heavy" review finding)
The Runtime driver's **single host = the local Colima container engine** (run containers directly via `nerdctl`/`docker` against Colima), NOT a per-node Lima VM. This is real containers, fast, and matches the user's "Colima as the Docker runtime" rule. odin's existing Lima-VM-per-EC2 path (`SimulationRunner`) becomes an *alternate* Runtime impl for VM-level isolation / remote hosts (a later milestone). The `RuntimeDriver` Protocol is written so the Colima impl and a future Lima impl are interchangeable.

## File structure
```
src/odin/spec/            NEW   — Stack/World/Changeset models + SpecStore (append-only revisions)
  models.py                     ResourceDesired, Edge, Ref, Stack, ResourceObserved, World, WorldDelta, Phase
  store.py                      SpecStore: get_stack/apply/current_world/write_world/subscribe
src/odin/runtime/         NEW   — Runtime driver port + Colima impl + container shim
  driver.py                     RuntimeDriver Protocol; ContainerSpec; RunHandle; HostFacts
  colima.py                     ColimaRuntime (nerdctl/docker against Colima)
  shim.py                       AllfatherDockerShim (duck-types docker client for MiniStack)
src/odin/fabric/          NEW   — addressing
  localhost.py                  LocalhostFabric: register(port)->fact, resolve(ref)->Address
src/odin/aws/             NEW   — MiniStack embed + bootstrap
  embed.py                      build_ministack_app(), in-process ASGI boto client, _docker monkeypatch
src/odin/reconcile/       NEW   — the control loop
  actions.py                    Action union (RunContainer, CreateMiniStackResource, StopContainer, NoOp)
  plan.py                       plan(Stack, World) -> [Action]   (pure, total, idempotent)
  reconciler.py                 Reconciler async loop + executor + inline assertions
  assertions.py                 http_ok(url), pg_ready(host,port)
src/odin/agent/           EVOLVE — re-skill prompt + add write_candidate tool
src/odin/server.py        EVOLVE — mount /aws, add /apply, cut over from Moto/Tofu in S2
ui/src/...                EVOLVE — WorldDelta consumption (StatusBadge/BottomPanel) + app/db node kinds
tests/spec, tests/runtime, tests/reconcile, tests/aws  NEW
```

---

## S0 — Embed MiniStack + prove the spawn-rewire (additive; app still boots)

### Task S0.1: Add the MiniStack fork + deps; confirm in-process embed
**Files:** Modify `pyproject.toml`; Create `src/odin/aws/embed.py`, `tests/aws/test_embed.py`.
**Interfaces — Produces:** `build_ministack_app() -> ASGIApp` (sets env before import, returns `ministack.app.app`); `ministack_boto_client(service: str)` → an in-process boto3 client whose endpoint is the embedded app via `httpx.ASGITransport`.

- [ ] **Step 1 — Decide the fork source.** Fork `github.com/ministackorg/ministack` → `kessler-frost/ministack` (or pin upstream `git+https://github.com/ministackorg/ministack@<tag>`). Add to `pyproject.toml` deps: the ministack ref, `psycopg2-binary>=2.9`, `httpx>=0.27`. Run `uv sync`. (Confirm MIT license in the fork's LICENSE before pinning.)
- [ ] **Step 2 — Write the failing test** `tests/aws/test_embed.py::test_embed_lists_services`: set env, `app = build_ministack_app()`, drive an in-process STS `GetCallerIdentity` via ASGITransport, assert it returns an Account. Run → FAIL (module not present).
- [ ] **Step 3 — Implement `embed.py`:** set `os.environ` (`MINISTACK_HOST=localhost`, `MINISTACK_ACCOUNT_ID`, `RDS_BASE_PORT`) **before** `import ministack.app`; expose `build_ministack_app()` returning the module `app`; build `ministack_boto_client()` using boto3 with a custom endpoint backed by `httpx.ASGITransport` (or botocore's `before-send` to route to the ASGI app). **Do NOT trigger lifespan.**
- [ ] **Step 4 — Run → PASS.** **Step 5 — Commit** `feat(aws): embed forked MiniStack as in-process AWS control plane`.

### Task S0.2: AllfatherDockerShim + ColimaRuntime (minimal)
**Files:** Create `src/odin/runtime/shim.py`, `src/odin/runtime/colima.py`, `tests/runtime/test_colima.py`.
**Interfaces — Produces:**
- `ColimaRuntime.run_container(ContainerSpec) -> RunHandle` (runs `nerdctl run -d` / `docker run -d` against Colima, returns `RunHandle{id, name}`); `.stop(name)`, `.remove(name)`, `.status(name) -> str`, `.host_port(name, container_port) -> int`.
- `AllfatherDockerShim(runtime: ColimaRuntime)` exposing the docker-client surface MiniStack calls: `.containers.run(image, environment=, ports=, name=, labels=, **_) -> _ShimContainer`; `_ShimContainer` has `.id`, `.status` (mapped from runtime), `.reload()`, `.attrs`, `.remove()`, `.stop()`.

- [ ] **Step 1 — Failing test** `test_colima_runs_and_reports`: `rt.run_container(ContainerSpec(image="postgres:16", env={"POSTGRES_PASSWORD":"x"}, ports={5432:0}))`, poll `rt.status(name)` → `"running"`, `rt.host_port(name,5432)` > 0; teardown removes it. (Marked `integration` — needs Colima.)
- [ ] **Step 2 — Run → FAIL.**
- [ ] **Step 3 — Implement `ColimaRuntime`** as thin `nerdctl`/`docker` subprocess calls (reuse `odin.process.run`); label every container `allfather=1` + `allfather.name=<name>` (NOT `ministack=…`, to avoid MiniStack's reaper). Implement the shim mapping `containers.run` → `runtime.run_container` with a `_ShimContainer` whose `.status` returns a value never in `{exited,dead,removing}` while booting.
- [ ] **Step 4 — Run → PASS** (with Colima). **Step 5 — Commit** `feat(runtime): Colima container runtime + MiniStack docker shim`.

### Task S0.3: Prove no-double-spawn end-to-end (the make-or-break)
**Files:** Modify `src/odin/aws/embed.py` (add `install_rds_spawn_rewire(runtime)`); Create `tests/aws/test_rds_rewire.py`.
**Interfaces — Produces:** `install_rds_spawn_rewire(runtime: ColimaRuntime) -> None` (eagerly `import ministack.services.rds as rds; rds._docker = AllfatherDockerShim(runtime)`).

- [ ] **Step 1 — Failing integration test** `test_rds_create_boots_real_postgres`: install rewire; in-process boto `rds.create_db_instance(DBInstanceIdentifier="appdb", Engine="postgres", ...)`; poll `describe_db_instances` until `DBInstanceStatus=="available"`; read `Endpoint.Address/Port`; assert a real `psycopg2.connect()` to `127.0.0.1:port` succeeds. Teardown: stop+remove the Postgres, assert 0 leftover `allfather=1` containers.
- [ ] **Step 2 — Run → FAIL.**
- [ ] **Step 3 — Implement the rewire** + whatever shim fields the RDS path needs (`_docker_image_for_engine` honored, `host_port` binding to 5432, `.reload().status` liveness). Set `RDS_PUBLIC_ENDPOINT`/no `DOCKER_NETWORK` so the endpoint resolves to `{MINISTACK_HOST, host_port}`.
- [ ] **Step 4 — Run → PASS:** real Postgres boots via allfather's runner, MiniStack reports it available, psycopg2 connects. **This proves the highest-risk seam.** Verify `odin start --dev` still boots (Moto/Tofu path untouched).
- [ ] **Step 5 — Commit** `feat(aws): prove RDS->real Postgres via allfather runner (no double-spawn)`.

---

## S1 — The spine alongside the old path (no cutover)

### Task S1.1: Spec Store models (Stack / World / WorldDelta)
**Files:** Create `src/odin/spec/models.py`, `tests/spec/test_models.py`.
**Interfaces — Produces:** frozen Pydantic: `Phase` (Literal: `pending|starting|healthy|crashed|idle|queued|running|done|evicted|blocked|error`); `Provenance` (`user|ai|default`); `Field(value, provenance)`; `ResourceDesired(id, kind, fields: dict[str,Field], placement_hint: str|None, refs: list[Ref])`; `Ref(var, target_id, target_attr)`; `Edge(src, dst, kind, perms)`; `Stack(env, rev, resources, edges, refs)`; `ResourceObserved(id, backing, phase, facts: dict, verdict: str|None, restarts: int)`; `World(env, resources)`; `WorldDelta(env, resource_id, phase, facts, verdict)`.
- [ ] Steps: failing test (construct a Stack with one app + one rds ResourceDesired, round-trip JSON, assert provenance preserved) → models → PASS → commit `feat(spec): Stack/World/WorldDelta Pydantic models`.

### Task S1.2: SpecStore (append-only content-addressed revisions)
**Files:** Create `src/odin/spec/store.py`, `tests/spec/test_store.py`.
**Interfaces — Produces:** `SpecStore(root: Path)`: `apply(env, Stack) -> rev (sha256 of canonical JSON)` writing `.odin/<env>/stacks/<rev>.json` + updating `.odin/<env>/HEAD`; `get_stack(env, rev=None) -> Stack`; `current_world(env) -> World`; `write_world(env, World)`; `apply_delta(env, WorldDelta) -> World`. Canonical JSON = `model_dump_json` with sorted keys. No GC (skeleton).
- [ ] Steps: failing test (apply two stacks → two revs + HEAD points at the second; get_stack(rev) returns the first; world round-trips) → impl → PASS → commit `feat(spec): append-only SpecStore with content-addressed revisions`.

### Task S1.3: RuntimeDriver Protocol + facts/stats
**Files:** Create `src/odin/runtime/driver.py`; Modify `src/odin/runtime/colima.py` (implement the Protocol); `tests/runtime/test_driver.py`.
**Interfaces — Produces:** `class RuntimeDriver(Protocol)`: `ensure_host() -> HostFacts`; `run_container(ContainerSpec) -> RunHandle`; `stop(name)`, `remove(name)`; `facts(name) -> ContainerFacts{phase, host_port, ...}`; `stats(name) -> {cpu, ram}`. `ColimaRuntime` implements it.
- [ ] Steps: failing test (`isinstance`-via-Protocol structural check + `stats()` returns numeric cpu/ram for a running container) → implement `stats()` via `nerdctl stats --no-stream --format json` → PASS → commit `feat(runtime): RuntimeDriver protocol + stats sampling`.

### Task S1.4: Localhost Fabric (host-port facts only)
**Files:** Create `src/odin/fabric/localhost.py`, `tests/fabric/test_localhost.py`.
**Interfaces — Produces:** `LocalhostFabric`: `register(node_id, host_port) -> Endpoint`; `resolve(ref: Ref, world: World) -> str` (reads the target's `facts['endpoint']` from World; raises `Unresolved` if the target isn't healthy). No Nebula, no `.local`.
- [ ] Steps: failing test (resolve `${{db.DATABASE_URL}}` from a World where db is healthy with an endpoint fact → returns `postgres://…`; unhealthy db → `Unresolved`) → impl → PASS → commit `feat(fabric): localhost host-port resolver`.

### Task S1.5: WorldDelta UI migration (StatusBadge/BottomPanel)
**Files:** Modify `ui/src/components/nodes/StatusBadge.tsx`, `ui/src/components/BottomPanel.tsx`, `src/odin/api/ws.py` (helper to emit `WorldDelta`); `tests/api/test_ws_worlddelta.py`.
- [ ] Steps: failing test (broadcasting a `WorldDelta` appends to events.jsonl + is retrievable) → add a `world_delta` event type the UI consumes (map `phase` → badge color/label, keeping back-compat with old `resource_*` until S2 cutover) → `bun run build` clean → PASS → commit `feat(ui): consume WorldDelta phases on status tiles`.

---

## S2 — Minimal Reconciler (service-only, single-host) + cut over

### Task S2.1: Action union + pure plan()
**Files:** Create `src/odin/reconcile/actions.py`, `src/odin/reconcile/plan.py`, `tests/reconcile/test_plan.py`.
**Interfaces — Produces:** `Action` = `RunContainer(id, spec) | CreateMiniStackResource(id, service, params) | StopContainer(id, name) | NoOp`; `plan(stack: Stack, world: World) -> list[Action]` — **pure, total, idempotent.** Logic (skeleton scope): for each desired resource not healthy in World → emit its create/run Action (rds → CreateMiniStackResource then RunContainer gated on rds healthy; app → RunContainer gated on its refs resolvable). For each observed resource absent from desired → StopContainer (prune). Unchanged+healthy → NoOp.
- [ ] Steps (TDD, the most important tests):
  - `test_empty_world_plans_creates`: desired {app, db}, empty world → plan emits CreateMiniStackResource(db) + (app gated → not yet).
  - `test_idempotent_when_healthy`: desired == world all healthy → all NoOp.
  - `test_app_gated_on_db`: db not healthy → app RunContainer NOT emitted; db healthy + app endpoint resolvable → app RunContainer emitted.
  - `test_prune_extra`: world has a resource not in desired → StopContainer emitted.
  - `test_restart_on_crash`: app phase=crashed in world → RunContainer (restart) emitted.
  - → implement `plan()` → all PASS → commit `feat(reconcile): pure total plan(Stack,World)->[Action]`.

### Task S2.2: Assertions (inline, the two the skeleton needs)
**Files:** Create `src/odin/reconcile/assertions.py`, `tests/reconcile/test_assertions.py`.
**Interfaces — Produces:** `async http_ok(url) -> bool`; `async pg_ready(host, port, user, password, db) -> bool` (psycopg2 connect). M4 later generalizes to a per-kind registry.
- [ ] Steps: failing tests (http_ok against a local stub; pg_ready against the S0 Postgres) → impl → PASS → commit `feat(reconcile): http + postgres readiness assertions`.

### Task S2.3: Reconciler loop + executor
**Files:** Create `src/odin/reconcile/reconciler.py`, `tests/reconcile/test_reconciler.py`.
**Interfaces — Produces:** `Reconciler(spec_store, runtime, fabric, ministack, ws)`: `async tick(env)` (read Stack+World, `plan()`, execute each Action, run its assertion, write WorldDelta); `start()/stop()` (async loop on tick + a watch on container exit); supervision: a crashed fact → next tick restarts (backoff). `blocked` phase + per-ref timeout so a never-healthy dep fails loudly.
- [ ] Steps: failing test with a **fake runtime** (in-memory, no Colima) (`test_reconciler_brings_app_up_after_db`, `test_reconciler_restarts_crashed`, `test_blocked_times_out`) → impl → PASS → commit `feat(reconcile): Reconciler loop with supervision + ref-gating`.

### Task S2.4: Agent re-skill — completion prompt + write_candidate tool
**Files:** Modify `src/odin/agent/prompt.py` (new `build_completion_prompt`), `src/odin/agent/client.py` (add `write_candidate` MCP tool to the in-process server + allowlist; keep read-only `read_world`/`read_stack`); `tests/agent/test_completion.py`.
**Interfaces — Produces:** `OdinAgent.complete(stack) -> Stack` (fills `None`/missing fields, tags `provenance=ai`, user values win); the MCP tool surface restricted to `write_candidate`/`read_stack`/`read_world` (no apply/start tools).
- [ ] Steps: failing test (a Stack with an under-specified rds → `complete()` fills engine/port/etc., user-set fields untouched, all filled tagged ai) → re-skill prompt + tool → PASS → commit `feat(agent): schema completion via write_candidate tool (Brain Toolbelt seed)`. **Clear `.odin/agent_session_id`** in the test/run harness so the new prompt takes effect.

### Task S2.5: Cut over — /apply route, retire Moto/Tofu
**Files:** Modify `src/odin/server.py` (mount `/aws`, add `create_apply_router(reconciler)`, remove MotoEngine/TofuRunner/`create_validate_router`/`create_deploy_router` wiring + the tofu lifespan); delete/retire `simulator/engine.py`, `terraform/`; Modify `ui` TopBar (Validate/Simulate/Destroy → Apply/Destroy); `tests/api/test_apply.py`.
- [ ] Steps: failing test (`POST /apply` with a 2-node canvas drives the Reconciler against the fake runtime → World shows both healthy) → wire create_app to the new spine, drop the old routers + Moto lifespan, mount `/aws` → `uv run pytest` green (delete/adjust the now-obsolete tofu/moto tests) → `odin start --dev` boots on the new path → commit `feat(server): cut over to the Reconciler + embedded AWS; retire Moto/Tofu`.

---

## S3 — Run the slice end-to-end (UI + playwright)

### Task S3.1: app + db node kinds in the canvas
**Files:** Modify `src/odin/resources.py` (add `app`/`service` + ensure `rds` carries kind/backing), `ui/src/lib/catalog.ts` + a `ServiceNode`/`AppNode`, `ui/src/lib/refs.ts` (NEW — parse `${{node.VAR}}` in a config field into a Ref edge).
- [ ] Steps: add the two node kinds + the `${{…}}` ref parser (auto-draws an edge like `iam.ts` does for IAM); `bun run build` clean; a UI unit test for the ref parser; commit `feat(ui): app + rds node kinds and reference-variable edges`.

### Task S3.2: End-to-end playwright run + restart + teardown
**Files:** `tests/e2e/` (playwright-cli driven), update `docs/SCENARIOS.md`.
- [ ] Steps (via the playwright-cli skill, against `odin start --dev`):
  - Drop `api` (image, e.g. a tiny app that reads `DATABASE_URL` and serves 200) + `db` (rds), set `api.env.DATABASE_URL = ${{db.DATABASE_URL}}`, Apply.
  - Assert: db tile → healthy (real Postgres up), api tile → healthy (after db), CPU/RAM strip painted.
  - Kill the api container (`nerdctl rm -f`), assert the tile flips crashed→starting→healthy (auto-restart).
  - Destroy → assert 0 `allfather=1` containers, clean teardown.
  - Record the run in `docs/SCENARIOS.md`. Commit `test(e2e): walking-skeleton slice via playwright + restart + teardown`.

---

## Milestone roadmap (post-skeleton; not bite-sized here — re-plan per milestone)
- **M1** — full Brain Toolbelt (`place`/`propose_changeset`/`review_iam`) + Changeset model + staged-diff UI + atomic Apply.
- **M2** — Service-Backend decision generalized; remaining real-container services (ElastiCache→Redis, ECS, EKS→k3s, Lambda→RIE, …) via per-module `_docker` monkeypatches; repo-path build (`nerdctl build`).
- **M3** — Scheduler bin-pack (numeric mem/cpu/gpu footprints) + batch FIFO queue + omlx load/evict (license-checked).
- **M4** — Assertion Engine per-kind probe registry; full supervision/re-place.
- **M5** — Catalog codegen from MiniStack's service list → ~55-service UI parity.
- **M6** — Environments as cheap copies + per-env account-id namespacing + runner name/port scoping + config-as-code.
- **M7** — Lima-VM Runtime impl (VM isolation), Nebula/`*.local` Fabric + `apply_edge_policy`, Tailscale fabric, multi-Mac memberlist/raft.

## Acceptance for the walking skeleton
1. `uv run pytest` green (unit + the new spine tests). 2. `odin start --dev` boots on the new Reconciler path (no Moto/Tofu). 3. The S3 playwright slice passes: api+db apply → real Postgres + supervised app reading the live `DATABASE_URL`, live tiles, auto-restart, clean teardown (0 containers/VMs/orphans). 4. No license violations; no leftover build/test artifacts.
