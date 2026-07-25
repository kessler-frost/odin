# Odin Roadmap

Odin (repo: odin): a local-first AWS-compatible cloud. A drag-drop canvas
where you design real AWS architectures; a deterministic translator turns the
canvas into Terraform/OpenTofu and back; **Apply** — the one action button —
runs a real `tofu apply` against odin's own gateway, which fulfills the AWS
API calls with local substitutes (RustFS for S3, etc.) at full API
compatibility; IAM permissions drawn as edges are **enforced for real** by
odin's own gateway; Nebula is the network layer.

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
- **claude-agent-sdk** — an optional, off-by-default refine pass over the
  deterministic canvas→TF translation (`ODIN_TRANSLATE_REFINE`), fenced by a
  guardrail that rejects any change to the architecture or to a value the
  canvas set. The agent-shaped work it could genuinely own — HCL for kinds
  with no builder, least-privilege policy synthesis, plain-English failure
  explanation (M8) — is roadmap, not shipped.

## Roadmap (northstar-derived sequence)

- [x] **Gateway + IAM enforcement.** DONE 2026-07-22 (G1–G5 + synthesized
  control-plane): SigV4 verification (incl. S3 body-hash cross-check) →
  (service, action, resource) classification → edge-compiled policy
  evaluation → forward to substitutes (re-signed for RustFS), with
  protocol-correct AccessDenied per service, STS identity, tag/attribute
  stores, and ~2ms added latency. Proven by real-container acceptance tests
  (edges grant; absence denies; foreign envs deny; a container crossed the
  boundary via aws-cli) — re-proven live for the 0.4.0 release (fine-grained
  per-action allow/deny against a running gateway; see README's
  [Edges are IAM](README.md#edges-are-iam)).
- [x] **Canvas↔Terraform translation + Apply-runs-tofu + TF import.** DONE
  2026-07-23 (S1–S5): `tofu apply` through the gateway (operator principal),
  the single **Apply** button (apply → translate → tofu, one call), the
  agent-refined translation pass with a deterministic fallback when the
  refinement fails a portability guardrail, the live Terraform code panel
  (previews the *current* unsaved canvas, not the last-applied one), and TF
  import (`/import-tf`: HCL text or live-state → canvas nodes) are all live.
  Terraform-owned resources' status is projected back into `/world` so every
  node's badge reflects reality regardless of which path provisioned it.
- [x] **Service coverage expansion.** DONE 2026-07-23 (V1–V5, sequence per
  the captured provider surfaces in `docs/superpowers/research/
  research-coverage.md`): the gateway owns each service's model, bound to a
  real substrate —
  1. VPC / Subnet / Security Groups → Nebula (`IpPermissions` compiles
     directly to `sg_rules_to_firewall`'s input)
  2. IAM control-plane CRUD (roles/policies onto odin's policy store) + ECR
     (CNCF `registry:2`, Apache-2.0 — real `docker push` verified)
  3. EC2 as real Lima VMs (the flagship; boot ~50–60s, the provider's
     pending→running waiter absorbs it; zero-drift)
  4. Lambda (real AWS RIE container, Apache-2.0; apply ~6s, invoke ~40ms)
  5. ECS (real Colima containers; scale up/down re-applies cleanly)

  **v1 limits, recorded rather than hidden** (northstar directive 5's honesty
  rule):
  - Lambda: inline code only, `$LATEST` only — no S3-deployed packages,
    versions, or aliases.
  - ECS: no `network_configuration` (awsvpc/Fargate-style ENIs — odin's tasks
    are `launch_type = "EC2"` / `network_mode = "bridge"`, which need none);
    a task that dies between API calls isn't auto-replaced until the next
    Apply reconciles the service. Generated services set
    `wait_for_steady_state = true` with a bounded `timeouts.create` (v0.5.4,
    finding #3) so a bad image / crash-on-start fails apply fast and honestly
    instead of silently "succeeding" with a service that never runs — the
    trade-off is that a genuinely slow FIRST image pull that exceeds the
    timeout also fails apply (a retry re-uses the now-cached image). (Fixed: a
    `tags` block on `aws_ecs_service` now plans zero-drift — the gateway stores
    the full tag set and echoes it back, with
    `TagResource`/`UntagResource`/`ListTagsForResource` modeled.)
  - SNS→SQS live-edit: FIXED (v0.5.0) — adding a subscription edge to an
    already-healthy topic lands on the next Apply via the reconciler's
    observe pass (proven by real fanout to both queues).
  - RDS stays off Terraform — the reconciler's real Postgres container, not
    a `tofu`-managed resource, until an RDS gateway model lands.
  - RDS endpoint reachability is per-consumer: a CONTAINER consumes
    `${{db.DATABASE_URL}}` (`host.docker.internal`); an EC2 (Lima VM) consumer
    must use `${{db.DATABASE_URL_VM}}` (`host.lima.internal`), since a Lima VM
    can't resolve the container-host alias. odin publishes BOTH facts (v0.5.4,
    finding #5); picking the right one per consumer type is manual — automatic
    ref-routing by consumer kind is deferred.
  - Security groups don't gate RDS or the other backing containers (goaws/
    RustFS/dynalite/Postgres): those run as HOST containers, not Nebula mesh
    members, so a drawn `db-sg` is decorative and DB access rides the raw host
    port, ungoverned. SG enforcement applies only to EC2 VMs on the mesh — and
    even there v1 compiles the firewall from the VPC's DEFAULT SG, not an
    instance's assigned security group. An instance's assigned SG IS reflected
    in DescribeInstances for zero-drift re-apply (v0.5.4, finding #2), but does
    not yet gate that VM's mesh traffic.
  - Single local server by design: `ODIN_GATEWAY_PORT` overrides the embedded
    gateway's port, but there is no supported way to run two servers against
    the same CWD-relative `.odin` store (the second binds-conflicts on the
    gateway port and would resume/reconcile the first's envs). Run a second
    instance only from a separate working directory with its own store.
  - Nebula: single-host mesh is REAL end-to-end — a real host lighthouse
    process (`fabric/nebula.py::LighthouseManager`) and a real `nebula`
    daemon inside every VPC-joined EC2 VM, the VPC's compiled SG firewall
    baked into its config. The lighthouse needs NO host privileges at all:
    it only ever coordinates (tells mesh members where to find each other),
    never carries their traffic, so it runs with `tun: disabled: true` —
    plain unprivileged `nebula`, no root, no sudo, no one-time setup
    (empirically verified: an unprivileged process with that flag starts
    and binds its UDP port; the same config without it dies immediately
    with "operation not permitted"). Only the VMs join the actual data
    plane, running `nebula` as root INSIDE the VM (systemd) — that costs
    the user nothing, since it's a VM they already own outright. **Real
    finding:** stock Lima `vz` NATs every VM into its OWN isolated address
    space — there is NO VM-to-VM underlay path at all (confirmed live: a
    raw ping between two VMs' vzNAT addresses is 100% loss, before nebula
    is even involved), so cross-VM mesh traffic routes THROUGH the
    lighthouse acting as a relay (`relay: {am_relay: true}` on the
    lighthouse, `relay: {use_relays: true}` on every VM) rather than
    direct — still fully unprivileged, since relaying is opaque encrypted
    UDP forwarding between two peers already handshaken with the lighthouse,
    needing no tun device either. The live overlay proof
    (`tests/simulate/test_nebula_mesh_e2e.py`) boots two real VMs and
    proves a real VM-to-VM ping (via the relay) plus a real
    SG-rule-filtered connection — the host itself has no overlay presence
    to test from. Cross-Mac reachability (a second machine's mesh) is still
    open — see M7.
- [x] **CloudWatch Logs — the log sink (W2.1).** DONE 2026-07-24: the `logs`
  node is real. `aws_cloudwatch_log_group` is a full gateway model
  (Create/Delete/DescribeLogGroups, Put/DeleteRetentionPolicy, tag CRUD →
  zero-drift plans) plus the DATA plane (CreateLogStream, PutLogEvents,
  GetLogEvents, FilterLogEvents, DescribeLogStreams), and the substrates ship
  their real output INTO it: a Lambda invoke's RIE container tail →
  `/aws/lambda/{fn}`, ECS task containers → `/ecs/{service}` on every sweep.
  So `odin logs <node>` (and `odin logs --group /aws/lambda/foo`) reads ONE
  place regardless of kind, and an IAM edge drawn to a log group is what lets
  a workload read its own lines (proven end-to-end: the function's own creds
  read the line it printed; a principal with no edge gets a real
  AccessDenied).

  **v1 limits, recorded rather than hidden:**
  - Storage is a per-env JSON sidecar (`.odin/{env}/gateway/logsctl.json`),
    dev-scale by design: each group keeps at most 10 000 events in a ring
    buffer (appending past the cap drops the OLDEST events, never the
    newest). A real backing store lands only if volume demands it.
  - `filterPattern` is a plain SUBSTRING match; CloudWatch's full
    filter-pattern grammar (JSON selectors, space-delimited field positions,
    term composition) is not modeled — an unmodeled construct simply doesn't
    match rather than being reinterpreted.
  - Substrate shipping is a bounded `docker logs --tail` read per
    invoke/sweep, deduped by a per-stream line cursor: repeated sweeps never
    duplicate a line, but a burst LARGER than that tail window loses its
    oldest lines (no continuous log streaming daemon in v1).
  - One stream per real container, named after it — not AWS's
    `{date}/[$LATEST]{requestId}` convention.
  - Metric filters, subscription filters, Logs Insights queries, export
    tasks and `logGroupClass` variants beyond STANDARD are unsupported.
  - Substrate ingestion auto-creates a missing group (like real Lambda), and
    an explicit `CreateLogGroup` then ADOPTS that group instead of failing
    `ResourceAlreadyExists` — a deliberate deviation so Apply always
    converges after an invoke-before-you-drew-it.
- [x] **Secrets Manager + SSM Parameter Store — where secrets live (W2.4).**
  DONE 2026-07-25: the `secret` and `ssm` nodes are real. Both are full
  gateway models — the control plane the TF provider drives (secret
  create/describe/update/delete + `ListSecrets` + tag CRUD +
  `GetResourcePolicy`; parameter put/delete/describe + tag CRUD → zero-drift
  plans) *and* the value plane (`GetSecretValue`, `PutSecretValue`,
  `UpdateSecretVersionStage`; `GetParameter`, `GetParameters`,
  `GetParametersByPath`). The canvas label IS the secret/parameter name, so
  an IAM edge drawn to one of these nodes is exactly what lets a workload
  read the value — and a principal without that edge gets a real
  AccessDenied.

  **v1 limits, recorded rather than hidden:**
  - No KMS at all. A secret's value and an SSM `SecureString` are stored
    CLEARTEXT in a per-env JSON sidecar
    (`.odin/{env}/gateway/secretsctl.json`, `.odin/{env}/gateway/ssmctl.json`)
    written `0600`; `KmsKeyId`/`KeyId` are accepted, stored and echoed back
    for Terraform fidelity and encrypt NOTHING. The protection is the file
    mode and the machine boundary — see SECURITY.md's Secrets section.
  - `DeleteSecret` is IMMEDIATE: `RecoveryWindowInDays` is accepted and
    ignored, there is no recovery window and no `RestoreSecret`. A deliberate
    deviation, and the thing that makes an empty-canvas Apply followed by a
    re-Apply converge instead of wedging on "scheduled for deletion" — the
    generated HCL says it out loud with `recovery_window_in_days = 0`.
  - Secret ARNs carry no random 6-character suffix (`...:secret:name`, not
    `...:secret:name-AbCdEf`), so they're deterministic per env.
  - Versioning covers AWSCURRENT/AWSPREVIOUS plus arbitrary labels via
    `UpdateSecretVersionStage`. Rotation is NOT modeled (`RotateSecret` is an
    unmodeled action, `RotationEnabled` is always false), nor are replica
    regions (`AddReplicaRegions` is accepted and ignored).
  - A secret RESOURCE POLICY can't be authored — `GetResourcePolicy` always
    answers "there is no policy" rather than storing an inert document that
    would look enforced. Access is granted by IAM edges instead, which the
    gateway enforces for real.
  - `ListSecrets` filters match as case-insensitive SUBSTRINGS; AWS's own
    word-prefix semantics and its `!` negation are not modeled, and an
    unrecognized filter key matches nothing (fails closed).
  - SSM keeps only the CURRENT version of a parameter — `Version` still
    increments on every overwrite (so terraform sees a real change), but
    `GetParameterHistory` and version LABELS (`LabelParameterVersion`,
    selectors) are not modeled. Parameter POLICIES are stored and echoed back,
    and nothing expires or notifies on them. The `Advanced` and
    `Intelligent-Tiering` tiers behave exactly like `Standard`.
  - `GetParametersByPath`/`DescribeParameters` don't paginate (`NextToken` is
    never emitted; `MaxResults` truncates), and an unrecognized
    `ParameterFilters` key/option matches nothing (fails closed).
  - IAM authorization gap shared by both: a call carrying a LIST of names is
    authorized against the FIRST one only, so a batch
    `GetParameters(Names=[a, b])` passes with an edge to `a` alone. The
    single-name reads a workload actually makes (`GetParameter`,
    `GetSecretValue`) are exact. Same bounded gap the ecr/ecs classifiers
    already carry.
- **Recorded as UNSUPPORTED for now** (northstar directive 5's honesty rule):
  ALB/ELBv2, EKS, CloudFormation, autoscaling, and RDS-via-Terraform (rds
  nodes stay on the reconciler path until an RDS API model lands).
- [x] **Nebula network layer (single-host), fully activated.** Security
  groups and VPCs drawn on the canvas compile to real Nebula network +
  firewall primitives (`fabric/nebula.py::sg_rules_to_firewall`,
  `ensure_network`) AND run for real: the host runs the env's lighthouse
  process, every VPC-joined EC2 VM runs a real `nebula` daemon carrying the
  compiled SG firewall, and `GET /mesh?env=` reports live lighthouse status.
  Proven by a real overlay `ping` plus a real SG-rule-filtered TCP
  connection (`tests/simulate/test_nebula_mesh_e2e.py`). The multi-Mac
  half — a second machine joining the SAME mesh — is deferred; see M7 below.
- [x] **odin CLI as an agent control surface.** DONE 2026-07-24 (v0.5.0):
  `odin canvas get/set`, `apply`, `world`, `envs`, `events`, `translate`,
  `import-tf`, `tf status/destroy`, `destroy`, `keys issue` — a thin client
  over the HTTP API with `-o json` for machine consumption, proven by an
  all-CLI session (set → translate → apply → healthy world → destroy).
  Lets a human's or an agent's
  (e.g. Claude Code) tooling drive the canvas and its configuration directly —
  process control (`start`/`stop`/`status`/`clean`/`doctor`) plus the full
  canvas surface (`canvas get/set`, `translate`, `apply`, `world`, `envs`,
  `events`, `tf`, `import-tf`, `destroy`, `keys issue`), all with `-o json`.
- [x] **Packaging (pragmatic scope).** DONE 2026-07-24 (v0.5.0):
  `scripts/install.sh` (one command: brew tools + colima up + odin + doctor)
  and `odin doctor` (toolchain checks with exact fixes, disk headroom,
  `--prebake` for the dynalite image). Full binary vendoring into one
  distributable.
- [ ] **M7 (multi-Mac) — the fleet.** The single-host half is DONE (see
  above: a real lighthouse + real per-VM daemons + a real ping/SG-filter
  proof, all on one Mac). What remains is genuinely cross-machine: a second
  Mac's host joining the SAME env's mesh (today's lighthouse only binds
  `0.0.0.0:4242` locally reachable via this Mac's own vzNAT bridge — a real
  external/LAN-reachable underlay address plus multi-Mac membership and
  cross-machine placement are still open). Additive, no core change.

## Deprecated 2026-07-22 (superseded by NORTHSTAR.md)

Everything below this line described **odin**, a local-only "Railway,
but with a brain" app orchestrator — the product identity before the owner's
2026-07-22 pivot to an AWS-compatible core. The app-workload
layer it describes (service/dep/batch/llm node kinds, the memory-aware
scheduler, the per-kind probe registry, the claude-agent-sdk config-completion
brain) has been ripped from live code and parked at git tag
`app-layer-parked` — it may return as a layer on top of the AWS core later.
Kept below for history, not as current direction.

### Direction (2026-06-23): local-only pivot (superseded)

**odin is going local-only.** We're dropping the ambition to maintain AWS /
cloud resources. The actual use cases are personal + friends/family, the local
machine is plenty, and the intelligence runs locally too — so there's no reason
to cater to cloud/AWS users.

What this means:
- **Drop the AWS-emulation story.** MiniStack (the embedded AWS control plane),
  the Pulumi-for-AWS infra layer we were mid-designing, the real-AWS↔local
  switching, and the whole infra-vs-app layering question — none of it.
  odin is not an AWS tool. (MiniStack was also just friction.)
- **Everything is a local container / process.** A "database" is just a Postgres
  container (a `dep` node); a cache is a Redis container; a queue is a real
  local broker (NATS/Redis) — run + supervised by the reconciler directly, with
  no AWS abstraction in front of them.
- **odin = a local-first, AI-operated orchestrator** for your Mac (Railway/
  Compose, but with a brain, fully local). Workloads: app services,
  dependencies, batch jobs, local LLMs. Intelligence: local (omlx / local models).
- **The palette** eventually sheds the AWS nodes (VPC/EC2/S3/SQS/RDS/…) and keeps
  the local primitives (app, dependency, job, LLM, + local volumes / networks).

> The earlier history (Moto/OpenTofu validate, the old Lima+Nebula
> per-EC2 "Simulate" overlay) was **retired and deleted** — 21 source modules +
> 30 test files removed. Do not resurrect Terraform/Moto/HCL or that old
> per-VM Nebula overlay. NOTE: this is distinct from the **self-hosted Nebula
> mesh fabric** (`fabric/nebula.py`) — a host-level mesh that IS the chosen
> multi-Mac direction. Different thing; don't re-strip it.

### Done (superseded)

#### Walking skeleton (S0–S3)
- [x] Spec Store spine — Stack (desired) + World (observed) + append-only, content-addressed, per-env revisions
- [x] Pure `plan(Stack, World) → [Action]` (total + idempotent) + the Reconciler loop (observe → plan → execute, supervision, ref-gating)
- [x] MiniStack embedded in-process as the AWS control plane; its container spawn rewired to odin's runtime (one spawn authority, no double-spawn)
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
