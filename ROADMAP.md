# allfather Roadmap

allfather: a Mac-native, AI-operated orchestration canvas (repo: odin, branch
`allfather`). Drop apps + AWS resources, the AI completes config, a control loop
runs them for real on Colima/Lima with an embedded MiniStack AWS control plane.

> The pre-allfather history (Moto/OpenTofu validate, the old Lima+Nebula
> per-EC2 "Simulate" overlay) was **retired and deleted** — 21 source modules +
> 30 test files removed. Do not resurrect Terraform/Moto/HCL or that old
> per-VM Nebula overlay. NOTE: this is distinct from the **self-hosted Nebula
> mesh fabric** (`fabric/nebula.py`) — a host-level mesh that IS the chosen
> multi-Mac direction (see M7 below). Different thing; don't re-strip it.

## Done

### Walking skeleton (S0–S3)
- [x] Spec Store spine — Stack (desired) + World (observed) + append-only, content-addressed, per-env revisions
- [x] Pure `plan(Stack, World) → [Action]` (total + idempotent) + the Reconciler loop (observe → plan → execute, supervision, ref-gating)
- [x] MiniStack embedded in-process as the AWS control plane; its container spawn rewired to allfather's runtime (one spawn authority, no double-spawn)
- [x] `ColimaRuntime` behind a `RuntimeDriver` protocol; localhost fabric resolving `${{node.VAR}}` from World facts
- [x] api + RDS→real-Postgres slice, proven end-to-end (headless + browser)

### Milestones
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

## Roadmap

- [ ] **M8 — Region-select debugging ("what's wrong here?")** — drag a selection rectangle over a canvas region → context menu ("Debug this" / "What's wrong here?" / "Fix this part" / free-form ask) → a region-scoped agent auto-gathers the enclosed nodes + edges and, for each, its World state (phase/facts/verdict/restarts) + recent events/logs + relevant Stack fields, then investigates or fixes from there. Reuses the existing Cmd+drag selection; new parts are the menu + a context-assembler that turns a selection into the agent prompt. **Point at a region instead of describing it — far less back-and-forth.**
- [ ] **M7 (multi-Mac) — the fleet:** a **self-hosted Nebula mesh** fabric (you own the lighthouse — runs in your private network, programmable, a control-plane/UI can be built on top; chosen over Tailscale, whose SaaS coordination would limit that) + multi-Mac membership (memberlist/raft) + apple-container runtime. The Nebula fabric foundation (cert/lighthouse/config primitives + the `NebulaFabric` resolve seam) is reinstated under `fabric/nebula.py`; cross-Mac placement is the deferred part. Additive, no core change.
- [ ] **Brain Toolbelt MCP:** make the Brain a candidate-only producer behind a typed `place` + `propose_changeset` + `review_iam` MCP membrane (stricter than today's best-effort completion).
- [ ] **MiniStack real-container backings** for the remaining stateful AWS services (ElastiCache→Redis, etc.) so apps use them for real, not just the API.
- [ ] **Packaging:** bundle the external tools (colima, lima, uv, …) into one distributable.

## Testing
- [x] pytest suite: 80 unit + 9 integration (real Colima/MiniStack/Lima/Claude, marker-gated)
- [x] Browser e2e via playwright (skeleton + full-breadth scenarios)
- [ ] Broader end-to-end scenario coverage as milestones land
