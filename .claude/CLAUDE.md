# allfather — AI-operated orchestration canvas (repo: odin)

## Overview
Drop apps + AWS resources onto a visual canvas; an AI completes their config; a
continuous control loop runs them **for real** on the Mac (real containers, a
real embedded AWS control plane) and supervises them; live status streams back.
"Railway, but local-first on one Mac, with an AI operator." Users draw, not code.

The old Moto/OpenTofu *validate* path was retired (branch `allfather`). Do not
resurrect Terraform/Moto/HCL — the system runs real workloads now.

## Tech Stack
- **Backend:** Python 3.12+ (uv), FastAPI + WebSocket, Pydantic.
- **AWS control plane:** **MiniStack** (`ministack` on PyPI) embedded in-process (a uvicorn thread, `lifespan="off"`); its container spawn is rewired to allfather's runtime via a `_docker` monkeypatch.
- **Runtime:** `ColimaRuntime` (containers directly on Colima — the default) and `LimaRuntime` (containers inside a Lima VM, VM isolation), both behind the `RuntimeDriver` protocol.
- **Brain:** `claude-agent-sdk` (one-shot `query`) fills blank config + reviews IAM; best-effort, defaults cover failure.
- **UI:** React 19 + ReactFlow + Tailwind v4 + Vite (`ui/`, `bun`). High-contrast dark industrial aesthetic.

## Architecture (`src/odin/`)
A Spec Store spine with a strict invariant: **the LLM only writes a candidate
desired-state; deterministic code reconciles; deterministic assertions verify.**

- `spec/` — `models.py` (Stack=desired, World=observed, WorldDelta, provenance-tagged fields), `store.py` (append-only content-addressed per-env revisions + `list_envs`), `translate.py` (canvas → Stack; `${{node.attr}}` → Ref).
- `reconcile/` — `plan.py` (pure `plan(Stack,World)→[Action]`, total+idempotent), `reconciler.py` (the loop: observe → plan → execute, supervision, ref-gating, AWS env injection, per-env), `scheduler.py` (memory-aware admission + LLM eviction), `probes.py` (Assertion Engine: per-kind health), `assertions.py`, `actions.py`.
- `runtime/` — `driver.py` (protocol), `colima.py`, `lima.py`, `shim.py` (the MiniStack `_docker` lookalike).
- `aws/` — `embed.py` (run MiniStack in-process + per-env account scoping), `rds.py`, `provision.py` (S3/SQS/SNS/DynamoDB), `catalog_gen.py` (codegen the canvas catalog from MiniStack's services).
- `fabric/localhost.py` — resolve `${{node.VAR}}` from World facts. `agent/` — `brain.py` (claude_complete, review_iam), `completion.py` (merge + ai_diff). `api/canvas.py`, `api/ws.py`, `server.py`.

**Node kinds:** service (HTTP-supervised), dep (any container, e.g. Redis), batch (run-to-completion), llm (omlx, memory-managed/evictable), rds + s3/sqs/sns/dynamodb (AWS). The 8 MiniStack real-container services + workloads share ONE spawn authority (the Runtime driver) — never double-spawned.

## Conventions
- **`bun`** (not npm/npx/yarn/pnpm); **`uv`** (not pip); **`python`** (not python3).
- Pathlib for paths; imports at top; minimize if/else + try/except; structured I/O (Pydantic) over regex.
- Permissive licenses only (Apache/MIT/BSD/MPL). Branch work on `allfather`; never merge to `main` without asking.
- Lima via `limactl` CLI; containers via Colima `docker` (default) or `nerdctl` in a Lima VM.

## Cleanup / Disk (limited headroom — clean up after EVERY heavy step)
- **Containers:** every test/run tears down its own; `docker ps -aq --filter label=allfather=1 | xargs -r docker rm -f`. Tests use the `runtime` fixture's teardown.
- **Lima VMs:** the LimaRuntime VM is `allfather-host`; integration tests delete it after. Never leave stray VMs (`limactl list -q`); delete by exact name (the user's own VMs like `veronica` are off-limits).
- **Misc:** prune `.odin/`, `.playwright-cli/*.yml`, `/tmp/*.png`, `__pycache__`, `.pytest_cache`, `.ruff_cache`.

## CLI / running
- `uv run uvicorn odin.server:create_app --factory --host 127.0.0.1 --port 4200` (the real app: embedded MiniStack + reconciler in lifespan).
- `odin start` / `odin start --dev` (Vite :4200 + uvicorn :4201). Tests: `uv run pytest` (unit), `uv run pytest -m integration` (real Colima/MiniStack/Claude — slow).

## Status / lifecycle
- Canonical resource id = the node **label**. World phases: pending / starting / healthy / blocked / crashed / queued / running / done / evicted / error.
- Status is a one-way projection: drivers + assertions author facts → Reconciler emits `WorldDelta` → `ConnectionManager.broadcast` (WS) + append-only `.odin/<env>/world.json` + `events.jsonl`. The UI is a pure projection; `StatusBadge` maps phases to colors; deltas carry `env` (UI filters by the active env).

## Environments
Multiple named envs reconciled independently (`/apply?env=`, `/world?env=`, `/destroy?env=`, `/envs`); each scoped to a distinct 12-digit MiniStack account → isolated AWS state in one embed. UI has an env field in the TopBar.

## UI Design Rules
- **Grid alignment:** 20px grid; node sections are multiples of 20px (header=40, single-line meta=20, two-line=40, button row=40). Snap to 20px; node positions/sizes multiples of 20.
- **High-contrast dark theme:** near-black backgrounds (#050508, #0a0a10), bright borders, neon per-type accents. Solid borders. Catalog nodes render via the generic `ServiceNode`; bespoke nodes (vpc/subnet/ec2/lambda/s3/sg/dynamodb) have their own components in `ui/src/components/nodes/`.
- **Catalog:** `ui/src/lib/catalog.ts` (hand-curated workload + key AWS nodes) merges `catalog.generated.ts` (47 AWS services auto-generated from MiniStack — regenerate with `python -m odin.aws.catalog_gen`).
- **Z-index:** VPC=0, Subnet=1, leaf=2; `elevateNodesOnSelect={false}`. No ReactFlow parent-child — containment is spatial + z-index. Reference edges auto-drawn from `${{node.VAR}}` fields.
