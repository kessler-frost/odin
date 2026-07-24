# Odin — a local-first AWS-compatible cloud (repo: odin)

## Overview
[NORTHSTAR.md](../NORTHSTAR.md) (repo root) governs — read it first for the
full direction. In short: a drag-drop canvas where people design real AWS
architectures; an agent (claude-agent-sdk) translates canvas ↔ Terraform/
OpenTofu both ways; **Simulate** runs a real `tofu apply` against odin's own
gateway, which fulfills the AWS calls with local substitutes (RustFS for S3,
etc.) at full API compatibility; IAM permissions drawn as edges are enforced
for real; Nebula is the network layer. Today, live code covers the AWS
substitute layer (RDS/S3/SQS/SNS/DynamoDB applied for real per environment) —
the gateway, the translation agent, Simulate, and IAM enforcement are still
being built (see ROADMAP.md).

**PARKED (2026-07-22, tag `app-layer-parked`):** the app-workload layer —
service/dep/batch/llm node kinds, the memory-aware scheduler
(`reconcile/scheduler.py`), the per-kind probe registry (`reconcile/
probes.py`), and the claude-agent-sdk config-completion brain (`agent/
brain.py`, `agent/completion.py`) — was ripped from live code and lives at
that git tag. It may return as a layer on top of the AWS core later; until
then don't resurrect it from memory of the old "Railway-like" direction.

The even-older Moto/OpenTofu *validate* path is retired too — don't resurrect
that either; `tofu apply` returns as the **Simulate** button per NORTHSTAR.md,
not as the old validate-only flow.

## Tech Stack
- **Backend:** Python 3.12+ (uv), FastAPI + WebSocket, Pydantic.
- **AWS substitutes:** real per-env backing containers — RustFS (S3), goaws (SQS+SNS, one container serves both), dynalite (DynamoDB) — plus a real Postgres container per RDS node. Run through the same `RuntimeDriver` as everything else, no emulator holding fake state.
- **Runtime:** `ColimaRuntime` (containers directly on Colima — the default) and `LimaRuntime` (containers inside a Lima VM, VM isolation), both behind the `RuntimeDriver` protocol.
- **Translation agent:** parked for now (see above). `claude-agent-sdk` is still a dependency; it's being repurposed toward canvas↔Terraform translation next, not config-completion.
- **UI:** React 19 + ReactFlow + Tailwind v4 + Vite (`ui/`, `bun`). High-contrast dark industrial aesthetic.

## Architecture (`src/odin/`)
A Spec Store spine: the desired-state Stack is authored by the canvas (and,
later, the translation agent); deterministic code reconciles; deterministic
assertions verify.

- `spec/` — `models.py` (Stack=desired, World=observed, WorldDelta, provenance-tagged fields), `store.py` (append-only content-addressed per-env revisions + `list_envs`), `translate.py` (canvas → Stack; `${{node.attr}}` → Ref; kinds today: rds/s3/sqs/sns/dynamodb).
- `reconcile/` — `plan.py` (pure `plan(Stack,World)→[Action]`, total+idempotent; rds + the AWS-shaped PROVISIONED kinds), `reconciler.py` (the loop: observe → plan → execute, supervision, AWS env injection, per-env, gc), `assertions.py` (pg_ready — the rds health check), `actions.py`.
- `runtime/` — `driver.py` (protocol), `colima.py`, `lima.py`.
- `aws/` — `backings.py` (`BackingAws`: the real RustFS/goaws/dynalite backings — provision/exists/deprovision/gc/aws_env/facts, a host-side boto3 `client` for tests), `rds.py` (`PostgresRds`: rds nodes as direct Postgres containers).
- `fabric/` — resolve `${{node.VAR}}` from World facts. `localhost.py` (loopback, the default); `nebula.py` + `models.py` = the **self-hosted Nebula mesh fabric** for multi-Mac: `NebulaFabric` is a drop-in for `resolve` (the overlay IP rides in via facts), plus recovered nebula-cert/lighthouse primitives (one network per env, sticky overlay IPs) + `mesh_state`/`GET /mesh?env=` for a mesh UI. Nebula NOT Tailscale (you own the lighthouse, build a control plane on top). The old per-EC2 Nebula *Simulate* overlay was deleted; this host-level mesh is the new thing — don't confuse them.
- `compute/` — `lima_yaml.py`/`cloud_init.py`/`models.py`: the EC2-as-real-Lima-VM substrate for the next service-coverage phase (not wired into the reconciler yet).
- `agent/` — parked (just `__init__.py`); the canvas↔Terraform translation agent lands here next. `api/canvas.py`, `api/ws.py`, `server.py`.

**Node kinds today:** rds (a direct Postgres container) + s3/sqs/sns/dynamodb
(AWS-shaped resources in shared per-env backing containers). service/dep/
batch/llm are parked (see above); ec2/ecs/lambda and the rest of the AWS
catalog are unbuilt placeholders in the UI catalog pending service-coverage
work.

## Conventions
- **`bun`** (not npm/npx/yarn/pnpm); **`uv`** (not pip); **`python`** (not python3).
- Pathlib for paths; imports at top; minimize if/else + try/except; structured I/O (Pydantic) over regex.
- Permissive licenses only (Apache/MIT/BSD/MPL). Branch work on `develop`; merge to `main` only for releases (locally, no PRs), then push.
- Lima via `limactl` CLI; containers via Colima `docker` (default) or `nerdctl` in a Lima VM.

## Cleanup / Disk (limited headroom — clean up after EVERY heavy step)
- **Containers:** every test/run tears down its own; `docker ps -aq --filter label=odin=1 | xargs -r docker rm -f`. Tests use the `runtime` fixture's teardown.
- **Lima VMs:** the LimaRuntime VM is `odin-host`; integration tests delete it after. Never leave stray VMs (`limactl list -q`); delete by exact name (the user's own VMs like `veronica` are off-limits).
- **Misc:** prune `.odin/`, `.playwright-cli/*.yml`, `/tmp/*.png`, `__pycache__`, `.pytest_cache`, `.ruff_cache`.

## CLI / running
- `uv run uvicorn odin.server:create_app --factory --host 127.0.0.1 --port 4200` (the real app: reconciler + AWS backings in lifespan).
- `odin start` / `odin start --dev` (Vite :4200 + uvicorn :4201). Tests: `uv run pytest` (unit), `uv run pytest -m integration` (real Colima backings — slow).

## Status / lifecycle
- Canonical resource id = the node **label**. World phases: `pending` / `starting` / `healthy` / `crashed` are what's actually emitted today (rds + the PROVISIONED AWS kinds). `blocked` / `queued` / `running` / `done` / `evicted` / `error` remain in the `Phase` type for the parked workload layer and future gateway/Simulate error states — don't be surprised they're unreachable right now.
- Status is a one-way projection: drivers + assertions author facts → Reconciler emits `WorldDelta` → `ConnectionManager.broadcast` (WS) + append-only `.odin/<env>/world.json` + `events.jsonl`. The UI is a pure projection; `StatusBadge` maps phases to colors; deltas carry `env` (UI filters by the active env).

## Environments
Multiple named envs reconciled independently (`/apply?env=`, `/world?env=`, `/destroy?env=`, `/envs`); each env gets its own `BackingAws` + `PostgresRds` (containers named `odin-aws-<backing>-<env>` / `odin-rds-<env>-<id>`) → isolated AWS-shaped state per env. UI has an env field in the TopBar.

## UI Design Rules
- **Grid alignment:** 20px grid; node sections are multiples of 20px (header=40, single-line meta=20, two-line=40, button row=40). Snap to 20px; node positions/sizes multiples of 20.
- **High-contrast dark theme:** near-black backgrounds (#050508, #0a0a10), bright borders, neon per-type accents. Solid borders. Catalog nodes render via the generic `ServiceNode`; bespoke nodes (s3/dynamodb) have their own components in `ui/src/components/nodes/`.
- **Catalog:** `ui/src/lib/catalog.ts` — hand-curated AWS-shaped nodes (sqs/sns/rds live today; the rest are future-coverage placeholders per NORTHSTAR.md directive 5).
- **Z-index:** all catalog/bespoke nodes at 2; `elevateNodesOnSelect={false}`. Reference edges auto-drawn from `${{node.VAR}}` fields.
