# NORTHSTAR — what odin is being built into

**Set by the owner on 2026-07-22.** This document governs. Every plan, task,
and architecture decision gets checked against it. The numbered directives
below preserve the owner's words (lightly cleaned for typos only) so they can
verify nothing drifted — treat them as the requirements of record.

## The vision in one paragraph

A local-first AWS: a drag-drop canvas where people design real AWS
architectures; an agent (claude-agent-sdk) translates canvas ↔
Terraform/OpenTofu code both ways; **Simulate** runs a real `tofu apply`
against odin's own gateway, which fulfills the AWS API calls with local
substitutes (RustFS for S3, etc.) at full API compatibility; IAM permissions
are drawn as edges and **enforced for real** by odin's own IAM engine; Nebula
provides the network layer (VPCs, security groups, firewalls); and an odin
CLI gives both humans and their agents (e.g. Claude Code) control of the
canvas and its configuration.

## The nine directives (owner's words)

### 1. Canvas + agent-generated IaC
> The UI and canvas that we've built where people can drag, drop, resize,
> connect different components, configure and do all sorts of stuff with
> these different AWS components via tofu/terraform code — and that code is
> generated via an agent (we'll be using the Claude Agent SDK) and it will
> also be using the odin CLI which I refer to later in here.

### 2. Intelligence = the translation layer, both directions
> The intelligence here is at the layer of translation from the canvas state
> into IaC as well as vice-versa, cuz I want an importing-from-a-TF-project
> mechanism too.

*Engineering note, as built (audit finding, 2026-07-24 — not a rewording of*
*the owner's words above, which stay verbatim per this doc's own charter):*
*both directions of this translation layer are DETERMINISTIC code*
*(`agent/hcl.py` canvas→TF, `agent/import_tf.py` TF→canvas) — that's what*
*makes them reliable and testable. `claude-agent-sdk` sits behind an*
*optional, off-by-default refine pass (`ODIN_TRANSLATE_REFINE`) over the*
*canvas→TF direction only: it may add comments/tags/unset arguments, and a*
*deterministic guardrail (resource-set equality + a value-fidelity check —*
*every argument the skeleton set must survive unchanged) rejects anything*
*else and falls back to the skeleton, so it is structurally incapable of*
*being the thing that decides what gets applied. Genuinely agent-shaped work*
*this layer doesn't do yet, and could: generating HCL for kinds with no*
*deterministic builder, least-privilege IAM policy synthesis, and import of*
*unmodeled resource types — roadmap items, not shipped behavior.*

### 3. Simulate = tofu apply through the gateway
> Next button I want is **Simulate** — which will generate the code and do
> `tf apply` — and this apply would go through our gateway which will create
> the necessary resources via our substitutions but with the same API
> compatibilities — like say an S3 resource needs to be created with a bucket
> named "my_bucket", that request will be adhered to via RustFS instead of
> actual S3 — this is why our gateway layer is one of the most important
> pieces, including the above translation layer I mentioned.

### 4. IAM via edges, enforced, with our own engine
> The way IAM should work is: if you connect two components in the canvas via
> an edge and it makes sense for them to have IAM permissions between them,
> then on the config panel there will be checkmarks for which permissions you
> wanna give those and in what directions — I guess sometimes there will be
> directions which are important but sometimes there won't be, depends on the
> components, so figure out how to do that. This IAM engine is something
> we'll probably be building our own if it's not existing, and I want **full
> enforcement** of this as well — this obviously includes compatibility with
> TF, which I guess will happen via our gateway.

### 5. Service coverage: adopt MiniStack's models where helpful
> If there are certain AWS services that we can't yet fully support, then
> first look into how MiniStack does it, and can we adopt that way in our own
> mechanisms/models — and if not then we just won't support it for now, but
> record it as an unsupported thing. For the most part the major AWS services
> — EC2, ECS, VPC, IAM, Lambda, ECR, etc. — we should definitely be able to
> support them without much trouble at all.

### 6. Nebula is the network layer
> Nebula will be used for firewalls, security groups, VPCs and similar
> things — so make sure we are handling that gracefully, as well as being
> aware of all of Nebula's features.

### 7. Cleanup + docs + memory hygiene
> Clean up everything else that we don't need apart from the things
> mentioned, and make sure we remove the appropriate things from all of the
> .md files in here (including README), and update the memories accordingly
> as well — while preserving our previous decisions but marking them as
> deprecated.

### 8. The odin CLI as a control surface
> The odin CLI that lets people's Claude Code (or whatever agent) as well as
> humans control the canvas + configurations + etc. of odin's UI.

### 9. This document
> Write down all of these EXACTLY in that .md doc as well so I can later
> check what I told you to do.

## What this reuses (already built and verified)

- **Canvas UI** — ReactFlow canvas, drag/drop/resize/connect, config panel,
  env switcher, live status over WebSocket.
- **The gateway** (PRD + de-risked prototypes, plan ready): SigV4
  verification → (service, action, resource) classification → policy
  evaluation → forward to substitutes. Becomes the front door for the TF AWS
  provider, not just boto3 SDKs.
- **Real substitutes**: RustFS (S3), goaws (SQS+SNS), dynalite (DynamoDB),
  real Postgres (RDS) — provisioned per env, supervised, crash-recovering,
  fully integration-tested today. MiniStack's per-service models are the
  reference for extending coverage (directive 5) — as designs to adopt, not
  a dependency.
- **Runtime drivers**: Colima (containers) and Lima (VMs) behind one
  protocol — the execution substrate for EC2/ECS/Lambda substitutes.
- **Nebula fabric**: cert/lighthouse/config primitives, per-env networks,
  sticky IPs, `sg_rules_to_firewall` — the substrate for directive 6.
- **Spec store** (append-only revisions), per-env isolation, the events/WS
  status pipeline, the integration-test harness, `odin` CLI skeleton
  (+ doctor/installer work).
- **claude-agent-sdk brain machinery** — repurposed toward directive 2
  (canvas ↔ IaC translation) and TF generation.

## Deprecated by this northstar (recorded, not yet all removed)

- **"No Terraform ever" (2026-06-21)** — REVERSED: tofu is back as the apply
  engine, now against our own gateway instead of Moto validate-only.
- **MiniStack as a runtime dependency** — stays removed (its models remain a
  design reference per directive 5).
- **"Local app orchestrator / Railway-like" as the product's identity** —
  odin is an AWS-compatible local cloud first. (Whether app-workload nodes
  survive as a layer on top: open question below.)
- Prior direction docs (ROADMAP's old milestones, the v0.3.0 run docs) to be
  annotated as superseded where they conflict — kept for history.

## Resolved with the owner (2026-07-22, same day)

1. **App-workload layer (service/dep/batch nodes, scheduler, supervision of
   user apps): RIPPED OUT for now.** Owner: "park all of that code somewhere
   … for right now I don't want anything to do with that." Parked in git
   history — tag `app-layer-parked` marks the last commit containing it;
   CLAUDE.md + memory note we may come back to it. The reconciler itself
   stays (it supervises the AWS substitutes); only user-app workload kinds
   leave.
2. **llm nodes: out for now.** May return later as a Bedrock-shaped
   substitute if ever.
3. **Typed config-completion (C3) / M8 region debugging (C4): both out for
   now** — the toolbelt's typed-membrane PATTERN is absorbed into the
   translation agent's tools (canvas ↔ TF), which is where the intelligence
   now lives. M8 revisited after the AWS core lands.
4. **EC2 instances = real Lima VMs** (old odin's proven approach: vzNAT,
   cloud-init, real SSH, joins Nebula). ECS tasks ride containers; Lambda
   substitute design comes with its service work.

## Amendments (owner, dated)

- **2026-07-22 (evening) — one button, named Apply.** Directive 3's button is
  renamed: "Simulate" → **Apply** (owner: "I'm liking the latter better").
  And it's the ONLY action button: no Destroy, no Reset — the canvas is the
  single source of truth, and Apply converges reality to it, deletions
  included (remove a node, Apply, it's gone; empty canvas + Apply = full
  teardown). Keep the UI uncrowded. Backend routes like `/destroy` may
  survive for tests/CLI, but the human surface is: draw → Apply.

## Non-negotiables carried forward

Local-first on one Mac (multi-Mac via self-hosted Nebula later) · real
execution, no mock-only modes · permissive licenses only (no AGPL) · uv, bun,
Colima · the AWS contract is odin's; every dependency behind it is
replaceable.
