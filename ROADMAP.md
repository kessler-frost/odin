# Odin Roadmap

Odin (repo: odin): a local-first AWS-compatible cloud. A drag-drop canvas
where you design real AWS architectures; an agent (claude-agent-sdk)
translates canvas ↔ Terraform/OpenTofu both ways; **Simulate** runs a real
`tofu apply` against odin's own gateway, which fulfills the AWS API calls with
local substitutes (RustFS for S3, etc.) at full API compatibility; IAM
permissions drawn as edges are **enforced for real** by odin's own IAM engine;
Nebula is the network layer.

## North star — the source of truth

[NORTHSTAR.md](NORTHSTAR.md), set by the owner on 2026-07-22, governs every
architecture decision here — read it before proposing or accepting any
direction change. The project has pivoted its middle layer more than once
(Moto → MiniStack → own gateway); the destination hasn't moved. Test every
future decision against these points instead of re-deriving them:

1. **Odin is an infra tool first** — an AWS-compatible endpoint on your Mac:
   real AWS wire protocol, real AWS verbs, real IAM semantics. Anything built
   on top (a canvas-driven app layer, say) sits ON TOP of that, not instead of it.
2. **Real execution, always.** Every resource is a real local thing (RustFS,
   goaws, dynalite, Postgres, containers, Lima VMs) — never in-memory
   make-believe. Emulators/routers may front the wire protocol, but never
   hold the data.
3. **Edges = IAM permissions = the core UX.** Drawing an edge between two
   components IS granting access (AWS verbs), enforced for real by odin's own
   IAM engine at its gateway (real `AccessDenied`).
4. **Nebula is the network layer, IAM is the API layer.** Separate concerns,
   never conflated: the mesh firewall decides who can REACH whom; IAM decides
   who may CALL what.
5. **Dependencies are replaceable; the contract is not.** RustFS, goaws,
   dynalite, Postgres, MiniStack's models (as a design reference) — all
   implementation details behind odin's own AWS endpoint. Swapping one must
   never change what a user's Terraform or boto3 code sees.

## What's done and reused

- **Canvas UI** — ReactFlow drag/drop/resize/connect, config panel, env
  switcher, live status over WebSocket.
- **Real AWS-shaped substitutes** — RustFS (S3), goaws (SQS+SNS), dynalite
  (DynamoDB), real Postgres (RDS) — provisioned per env, supervised,
  crash-recovering, integration-tested today.
- **Runtime drivers** — Colima (containers) and Lima (VMs) behind one
  `RuntimeDriver` protocol, the execution substrate EC2/ECS/Lambda substitutes
  will run on.
- **Nebula fabric** — cert/lighthouse/config primitives, per-env networks,
  sticky IPs, `sg_rules_to_firewall` — the substrate for the network layer.
- **Spec Store** (append-only revisions), per-env isolation, the events/WS
  status pipeline, the integration-test harness, the `odin` CLI skeleton.
- **claude-agent-sdk brain machinery** — repurposed toward canvas↔IaC
  translation and TF generation; its typed-membrane pattern moves into the
  translation agent's tools.

## Roadmap (northstar-derived sequence)

- [ ] **Gateway + IAM enforcement.** SigV4 verification → (service, action,
  resource) classification → policy evaluation → forward to substitutes. The
  front door for the TF AWS provider, not just boto3 SDKs — IAM permissions
  drawn as canvas edges are evaluated here for real.
- [ ] **Canvas↔Terraform translation agent + Simulate + TF import.** The
  Claude Agent SDK translates canvas state to Terraform/OpenTofu and back;
  **Simulate** runs a real `tofu apply` through the gateway; import an
  existing TF project onto the canvas.
- [ ] **Service coverage expansion.** EC2 (real Lima VMs), ECS (containers),
  Lambda, ECR, VPC, and more — adopting MiniStack's per-service models as a
  design reference where they help, not as a runtime dependency. Anything not
  yet supportable gets recorded as unsupported, not silently dropped.
- [ ] **Nebula network layer.** Security groups, VPCs, and firewalls drawn on
  the canvas become real Nebula network primitives.
- [ ] **odin CLI as an agent control surface.** Lets a human's or an agent's
  (e.g. Claude Code) tooling drive the canvas and its configuration directly.
- [ ] **Packaging.** Bundle the external tools (colima, lima, uv, …) into one
  distributable.

## Deprecated 2026-07-22 (superseded by NORTHSTAR.md)

Everything below this line described **allfather**, a local-only "Railway,
but with a brain" app orchestrator — the product identity before the owner's
2026-07-22 pivot back to odin being an AWS-compatible core. The app-workload
layer it describes (service/dep/batch/llm node kinds, the memory-aware
scheduler, the per-kind probe registry, the claude-agent-sdk config-completion
brain) has been ripped from live code and parked at git tag
`app-layer-parked` — it may return as a layer on top of the AWS core later.
Kept below for history, not as current direction.

### Direction (2026-06-23): local-only pivot (superseded)

**allfather is going local-only.** We're dropping the ambition to maintain AWS /
cloud resources. The actual use cases are personal + friends/family, the local
machine is plenty, and the intelligence runs locally too — so there's no reason
to cater to cloud/AWS users.

What this means:
- **Drop the AWS-emulation story.** MiniStack (the embedded AWS control plane),
  the Pulumi-for-AWS infra layer we were mid-designing, the real-AWS↔local
  switching, and the whole infra-vs-app layering question — none of it.
  allfather is not an AWS tool. (MiniStack was also just friction.)
- **Everything is a local container / process.** A "database" is just a Postgres
  container (a `dep` node); a cache is a Redis container; a queue is a real
  local broker (NATS/Redis) — run + supervised by the reconciler directly, with
  no AWS abstraction in front of them.
- **allfather = a local-first, AI-operated orchestrator** for your Mac (Railway/
  Compose, but with a brain, fully local). Workloads: app services,
  dependencies, batch jobs, local LLMs. Intelligence: local (omlx / local models).
- **The palette** eventually sheds the AWS nodes (VPC/EC2/S3/SQS/RDS/…) and keeps
  the local primitives (app, dependency, job, LLM, + local volumes / networks).

> The pre-allfather history (Moto/OpenTofu validate, the old Lima+Nebula
> per-EC2 "Simulate" overlay) was **retired and deleted** — 21 source modules +
> 30 test files removed. Do not resurrect Terraform/Moto/HCL or that old
> per-VM Nebula overlay. NOTE: this is distinct from the **self-hosted Nebula
> mesh fabric** (`fabric/nebula.py`) — a host-level mesh that IS the chosen
> multi-Mac direction. Different thing; don't re-strip it.

### Done (superseded)

#### Walking skeleton (S0–S3)
- [x] Spec Store spine — Stack (desired) + World (observed) + append-only, content-addressed, per-env revisions
- [x] Pure `plan(Stack, World) → [Action]` (total + idempotent) + the Reconciler loop (observe → plan → execute, supervision, ref-gating)
- [x] MiniStack embedded in-process as the AWS control plane; its container spawn rewired to allfather's runtime (one spawn authority, no double-spawn)
- [x] `ColimaRuntime` behind a `RuntimeDriver` protocol; localhost fabric resolving `${{node.VAR}}` from World facts
- [x] api + RDS→real-Postgres slice, proven end-to-end (headless + browser)

#### Milestones
- [x] **M1 — Brain:** `claude_complete` fills blank config (AI-tagged, user values win, best-effort); IAM review
- [x] **M1-UX — staged changeset:** `POST /preview` returns the AI's proposed diff before Apply; Preview button; `POST /review-iam`
- [x] **M2 — workloads:** all 4 kinds — service (HTTP-supervised), dep (any container, e.g. Redis), batch (run-to-completion), llm — plus AWS usable *by* app containers (injected endpoint/creds)
- [x] **M3 — Scheduler:** memory-aware admission (queue over budget) + idle-LLM eviction for higher-priority work
- [x] **M4 — Assertion Engine:** per-kind health probe registry (http / tcp / `/v1/models` / pg / exit-code), injectable
- [x] **M5 — UI parity:** catalog codegen from MiniStack's service registry (47 generated AWS nodes)
- [x] **M6 — environments:** independent per-env reconcilers, each scoped to a distinct MiniStack account (isolated AWS state); `/envs`; UI env switcher
- [x] **M7 (single-host) — Lima runtime:** `LimaRuntime`, a second `RuntimeDriver` impl running workloads inside a Lima VM (VM isolation); unit + real-VM integration
- [x] AWS resource provisioning from canvas nodes (S3/SQS/SNS/DynamoDB created in the embed on Apply)
- [x] **Nebula mesh fabric foundation** (`fabric/nebula.py`) — recovered cert/lighthouse/config primitives (one network per env, sticky overlay IPs) + `NebulaFabric` (a verified drop-in for the `resolve` seam) + a `mesh_state` read model and `GET /mesh?env=` for a future mesh UI. The cross-Mac *activation* (host overlay IP → World facts, World replication, placement) is M7 below.

### Roadmap (superseded)

- [ ] **M8 — Region-select debugging ("what's wrong here?")** — drag a selection rectangle over a canvas region → context menu ("Debug this" / "What's wrong here?" / "Fix this part" / free-form ask) → a region-scoped agent auto-gathers the enclosed nodes + edges and, for each, its World state (phase/facts/verdict/restarts) + recent events/logs + relevant Stack fields, then investigates or fixes from there. Reuses the existing Cmd+drag selection; new parts are the menu + a context-assembler that turns a selection into the agent prompt. **Point at a region instead of describing it — far less back-and-forth.**
- [ ] **M7 (multi-Mac) — the fleet:** a **self-hosted Nebula mesh** fabric (you own the lighthouse — runs in your private network, programmable, a control-plane/UI can be built on top; chosen over Tailscale, whose SaaS coordination would limit that) + multi-Mac membership (memberlist/raft) + apple-container runtime. The Nebula fabric foundation (cert/lighthouse/config primitives + the `NebulaFabric` resolve seam) is reinstated under `fabric/nebula.py`; cross-Mac placement is the deferred part. Additive, no core change.
- [ ] **Brain Toolbelt MCP:** make the Brain a candidate-only producer behind a typed `place` + `propose_changeset` + `review_iam` MCP membrane (stricter than today's best-effort completion).
- [ ] **MiniStack real-container backings** for the remaining stateful AWS services (ElastiCache→Redis, etc.) so apps use them for real, not just the API.
- [ ] **Packaging:** bundle the external tools (colima, lima, uv, …) into one distributable.

### Testing (superseded)
- [x] pytest suite: 80 unit + 9 integration (real Colima/MiniStack/Lima/Claude, marker-gated)
- [x] Browser e2e via playwright (skeleton + full-breadth scenarios)
- [ ] Broader end-to-end scenario coverage as milestones land
