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
*(`iac/hcl.py` canvas→TF, `iac/import_tf.py` TF→canvas) — that's what*
*makes them reliable and testable. `claude-agent-sdk` sits behind an*
*optional, off-by-default refine pass (`ODIN_TRANSLATE_REFINE`) over the*
*canvas→TF direction only: it may add comments/tags/unset arguments, and a*
*deterministic guardrail (resource-set equality + a value-fidelity check —*
*every argument the skeleton set must survive unchanged) rejects anything*
*else and falls back to the skeleton, so it is structurally incapable of*
*being the thing that decides what gets applied.*

*SHIPPED (W2.9/M8, 2026-07-25) — the agent-shaped job that is now real:*
*plain-English failure explanation. `agent/debugger.py` + `POST /agent/debug`*
*take a selected region of the canvas and answer "what's wrong here?" from*
*real evidence — each node's desired config, refs, observed phase, crash*
*verdict, recent events and log tail — with per-node suspects. It is the one*
*place the AI is load-bearing (there is no deterministic function from an exit*
*code plus log lines to a cause), and it is safe to be load-bearing there:*
*read-only, prose out, secrets and env-var values redacted before the prompt,*
*and an honest "agent unavailable" whenever the SDK can't run. ON by default*
*(`ODIN_DEBUG_AGENT=0` disables), unlike the refine pass above.*

*Genuinely agent-shaped work this layer still doesn't do, and could:*
*generating HCL for kinds with no deterministic builder, least-privilege IAM*
*policy synthesis, and import of unmodeled resource types — roadmap items, not*
*shipped behavior.*

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

- **2026-07-30 (owner observation, recorded as the governing principle) —
  deterministic first; intelligence only where no function exists.** The owner,
  reading this document back against what got built: *"I feel we have found a
  deterministic way of doing all of those things without intelligence right?
  And we'll only involve intelligence where needed."*

  That is what happened, and it is a better outcome than the plan. Directive 2
  said an agent translates canvas ↔ Terraform; both directions are
  deterministic code (`iac/hcl.py`, `iac/import_tf.py`), with the SDK behind
  an off-by-default refine pass a guardrail can reject outright — structurally
  incapable of deciding what gets applied. The same pattern then repeated
  everywhere it was tested: edge semantics are a pair table with a
  mutation-tested ratchet, not a model's opinion; IAM is compiled from the
  applied file, not inferred; the reconciler is `plan(Stack, World) -> [Action]`,
  total and idempotent.

  So the rule, stated once here rather than rediscovered per feature:
  **where a deterministic function exists, write it — reserve intelligence for
  the places where one provably does not.** Three such places are known today.
  Explaining a failure is the only one SHIPPED (`agent/debugger.py`): there is no
  function from an exit code plus log lines to a cause, and it is safe to be
  load-bearing there because it is read-only, prose out, secrets redacted, and
  honest when the SDK cannot run. The unbuilt ones are HCL for kinds with no
  builder, least-privilege policy synthesis, and import of unmodeled types — plus
  RANKING among legal edge meanings once a pair is ambiguous, where the table
  still decides what is legal and the model only chooses between those.

  The test that keeps this honest: if a model's output is load-bearing, ask what
  a deterministic checker would look like. If one is possible, it belongs in the
  code and the model belongs behind it.

  *Follow-through (2026-08-03): the two translators MOVED out of `agent/` into*
  *`src/odin/iac/` because of this amendment.* They had never imported
  `claude_agent_sdk` — they lived under `agent/` only because directive 2 said
  "an agent translates" and the directory got named for the intent before the
  implementation turned out deterministic. But a directory name is read as a
  claim, and `agent/hcl.py` told every reader a model writes their
  infrastructure. `agent/` now holds only what actually calls one (`chat.py`,
  `debugger.py`, `translate.py`'s refine pass, `ai.py`), the dependency runs one
  way (`agent` → `iac`, never back), and
  `tests/test_iac_is_deterministic.py` fails the build on either leak — because
  this paragraph cannot.

- **2026-08-06 (owner, recorded as an OPEN QUESTION — not yet a directive) —
  who is odin for, and how AWS should its face be?** Raised while sizing odin
  against Brainboard/Cloudcraft (canvas tools for professional infra teams
  deploying to real cloud accounts): *"whether we should rely on aws keywords
  that heavily, as I'm looking to target odin and perhaps build it for the
  home lab users who are somewhat different than professional infra
  engineers? Especially when intelligence backends are becoming increasingly
  local like self hosted models."*

  Unpacked, three threads to test future decisions against:
  1. **Audience: home-lab users, not (only) professional infra engineers.**
     Home-labbers want running services on their own hardware with no cloud
     bill — which is what odin already does. The commercial canvas-to-cloud
     tools all assume real cloud credentials; nobody found so far serves the
     local-first quadrant. This reframes them as neighbors, not competitors.
  2. **Surface language: how much AWS vocabulary to wear.** The question is
     about the FACE (keywords, catalog names, marketing), not the contract —
     directive 1's AWS wire protocol at the gateway is what makes real
     Terraform providers work and stays. Open: whether the canvas could speak
     in plainer nouns ("object storage", "queue", "database", "function")
     with AWS-compat as the implementation detail underneath, so a home-lab
     user who has never opened the AWS console isn't repelled by SQS/SNS/EBS
     jargon. AWS-shaped names would remain the resource kinds' identity in
     Stack/HCL either way.
  3. **Self-hosted intelligence fits this audience.** Home-labbers
     increasingly run local models; odin's agent layer (`agent/ai.py` master
     switch) currently assumes `claude_agent_sdk`. If the audience shifts,
     "bring your own local model" becomes a natural expectation for the
     chat/debugger/refine surfaces — same shape as odin substituting local
     backings for AWS services, applied to the intelligence layer itself.

  Not decided; nothing in the codebase moves on this yet. Recorded so the
  thought survives, and so the tension with directive 1's framing ("an
  AWS-compatible endpoint on your Mac") is confronted deliberately rather
  than drifted past.

- **2026-08-06 (from the same landscape review, recorded as an OPTION under
  directive 5 — not adopted) — post-LocalStack emulators (MiniStack, Floci)
  as candidate LONG-TAIL BACKINGS behind the gateway.** Context: LocalStack
  archived its OSS repo and account-walled the product (2026-03); the vacuum
  filled with MIT-licensed emulators — `ministackorg/ministack` (~4k stars;
  RDS = real Postgres containers, EKS = real k3s: odin's real-substrate
  philosophy independently reinvented) and `floci-io/floci` (~18k stars, 69
  services, Docker-backed tier for the heavy ones). Neither has a canvas,
  IaC generation/import, or IAM enforcement (MiniStack README: "IAM policies
  are stored but not enforced"; Floci: "credentials can be any non-empty
  values") — they are the substrate layer commoditizing, not competitors on
  odin's axes.

  The option: for kinds odin will never hand-build a real substrate for,
  fulfill the API behind odin's OWN gateway by proxying to one of these
  containers — the same shape as goaws/dynalite today, with sixty services
  in one container. Odin keeps the contract, SigV4, IAM enforcement, canvas
  and translation; the emulator community does the long-tail API surface.
  MIT both sides, so the permissive-only rule holds.

  Relation to the deprecation above ("MiniStack as a runtime dependency —
  stays removed"): that entry removed MiniStack as the MIDDLE LAYER — the
  thing holding the AWS contract itself (the Moto → MiniStack → own-gateway
  arc). This option puts an emulator BEHIND the gateway as one more
  replaceable backing, the position directive 5 and the non-negotiable
  ("every dependency behind [the contract] is replaceable") already
  contemplate. Not a reversal — but near enough that adoption must be
  deliberate and per-kind, never by drift.

  Two conditions if ever adopted, both owed to the honesty rules: (1) a kind
  backed by an emulator is DRAWN as emulated — in `docs/limits.md` and the
  architecture diagram (rule 2b: an unreal boundary drawn as real is the
  diagram lying); (2) "real execution, no mock-only modes" keeps meaning
  what it says for the core kinds — the emulator path is long-tail only,
  and each kind's entry records what actually holds its state.

- **2026-08-06 (owner, DECIDED) — intelligence in the UI is visual proposal,
  not chat: "vision is the communication language and not chatting
  primarily."** Refines directives 1 and 4 and carries the 2026-07-27
  directive ("canvas and navigating things around IS the language of odin")
  into the intelligence layer rather than amending it away. The model:

  1. **The AI proposes; the user's click is the drawing.** Proposals (ghost
     edges, pending config completions) are UI-only overlay state — never
     written to the Stack, invisible to Apply, gone on dismiss. Accepting
     routes through the SAME creation path as the equivalent manual action,
     so "edges are drawn by the user, never by odin" stays true in code, not
     just in spirit. Accepted items carry provenance
     "agent-proposed, user-accepted" (`spec/models.py` provenance fields),
     permanently distinguishable from hand-drawn work.
  2. **"Accept all" exists, but odin's edges are PERMISSION GRANTS
     (directive 4), so bulk-accept is a bulk permissioning action.** For
     IAM-granting edges, accept-all must first show the grant summary it is
     about to make — how many edges, which actions, which directions, in the
     same vocabulary as the per-edge checkmarks — so one click approves it
     informed. Reference-only edges (plain `${{node.attr}}` wiring) may
     accept-all freely.
  3. **Completions are VISIBLE, attributed, and pending until accepted.**
     Config-panel proposals render as visibly-pending values with a
     one-line why, per the precedent `docs/intelligence-layer.md` already
     sets for containment ("show what containment decided and why"). The
     gesture invariant extends to intelligence verbatim: nothing silently
     rewrites what a person authored, and nothing accepted becomes
     indistinguishable from what the person typed.
  4. **The 2026-07-30 division of labor is unchanged**: the pair table
     decides what an edge MAY mean; the model only ranks among legal
     meanings and drafts values; deterministic code decides what gets
     applied. Chat stays the auxiliary channel for what has no spatial
     representation (the debugger's "why is this crashed?" — failure causes
     are not drawable).

  Bookkeeping owed when this is built: CLAUDE.md's edge-origin inventory
  ("Canvas.tsx produces edges in exactly two places") gains a third place —
  the accept handler — and must be updated in the same change, for the same
  reason that line exists at all.

## Non-negotiables carried forward

Local-first on one Mac (multi-Mac via self-hosted Nebula later) · real
execution, no mock-only modes · permissive licenses only (no AGPL) · uv, bun,
Colima · the AWS contract is odin's; every dependency behind it is
replaceable.
