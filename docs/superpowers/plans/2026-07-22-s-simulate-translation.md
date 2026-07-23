# S — Simulate + Translation Implementation Plan (northstar directives 1-3)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Prereq: gateway G1-G5 landed (docs/superpowers/plans/2026-07-22-c15-gateway.md). Ground truth for every provider behavior claim: docs/superpowers/research/research-tofu-provider.md (verified live: OpenTofu 1.12.3, aws provider 5.100.0, full apply→zero-drift plan→destroy on s3/sqs/sns/sns-subscription/dynamodb).

**Goal:** The Simulate button: the translation agent turns the canvas into clean Terraform; `tofu apply` runs through odin's gateway onto the substitutes; TF-project import goes the other way (TF → canvas).

**Architecture:** Simulate = compile + apply, supervised like everything else. The gateway grows a small synthesized control-plane (identity, tags, attributes, delete-confirms — the calls the substitutes can't serve; per-env, stateless where possible, tiny JSON sidecar stores where not). The agent emits PORTABLE TF (no endpoints in HCL); odin injects `AWS_ENDPOINT_URL=<gateway>` + a generated `override.tf` (path-style, skips, creds) at run time.

## Global Constraints

- Portable TF only: agent-emitted HCL contains no endpoints, no skip flags, no creds — those live in odin's generated `override.tf` + env vars (research §1 has the verified block; `skip_requests_validation` does NOT exist — exactly the four verified `skip_*`).
- Provider pinned `~> 5.0` in generated TF; tofu binary via brew (doctor checks it; do not vendor).
- tofu runs with `AWS_ENDPOINT_URL=http://127.0.0.1:{gateway_port}` under an OPERATOR principal per env (full allow within the env) — create-path actions (CreateQueue/CreateTopic/CreateBucket/CreateTable + name-carrying reads) must classify under this principal (G2 left them None → extend classify for operator flows; workload principals keep the v1 use-only posture).
- TF workspace per env: `.odin/{env}/tf/` (main.tf agent-owned, override.tf odin-owned, state local). Never commit state; `.gitignore` covers `.odin/`.
- All tofu invocations stream stdout/stderr lines into the events pipeline (WS) tagged `{type: "simulate", env, phase: plan|apply|destroy}`.
- claude-agent-sdk with typed tools (the toolbelt membrane pattern) — the agent NEVER writes files directly; it returns structured output odin materializes.

---

### Task S1: gateway synthesized control-plane (research build-order items 2-5)

**Files:** `src/odin/gateway/synth.py` (new), extend `app.py` routing, `src/odin/gateway/stores.py` (per-env JSON sidecar under `.odin/{env}/gateway/`), tests.

Implement, per research §§3-5 captured surfaces: STS `GetCallerIdentity` (env-stable 12-digit account id — reuse a hash like the old account_for_env; enables dropping 2 skip flags later, keep flags for now); per-env TAG store serving SQS `ListQueueTags/TagQueue/UntagQueue`, SNS `ListTagsForResource/TagResource/UntagResource`, DynamoDB `ListTagsOfResource/TagResource/UntagResource`; SNS `GetTopicAttributes/SetTopicAttributes` store (goaws's biggest hole — research lists the exact attribute set TF expects); SQS delete-confirmation shim + `GetQueueAttributes` attr echo; SNS subscription `GetSubscriptionAttributes/Unsubscribe` NotFound fidelity; CreateQueue response host rewrite (goaws advertises `us-east-1.goaws.com:4100` — rewrite to the gateway's own host:port so TF state stays clean). Synth answers are evaluated AFTER policy (denied is denied); only the operator principal reaches create/tag paths in v1.

### Task S2: the tofu runner

**Files:** `src/odin/simulate/runner.py`, `src/odin/simulate/workspace.py`, server routes `POST /simulate?env=` `POST /simulate/destroy?env=` `GET /simulate/status?env=`, doctor gains a tofu check, tests (unit with a fake tofu; one integration running real tofu against the full gateway+substitutes stack — the research main.tf is the fixture).

Workspace materialization (main.tf from the translation output, override.tf generated, `tofu init -input=false` cached provider), apply/destroy with `-auto-approve -input=false -no-color`, line-streamed events, exit-code → env-level `simulate_failed` event with the tail. Concurrency: one simulate at a time per env (asyncio lock); reconciler keeps supervising the substitutes it already owns — document the boundary: canvas Apply provisions via reconciler (today's path), Simulate provisions via tofu→gateway; BOTH converge on the same backings, and `/destroy` still tears everything.

### Task S3: the translation agent (canvas → TF)

**Files:** `src/odin/agent/translate.py` (the agent returns via a typed `emit_terraform` tool: `{files: [{path, content}], notes: [str]}`), `src/odin/agent/hcl.py` (deterministic skeleton generation for the 5 supported kinds — the agent refines/annotates; determinism first, intelligence on top), route `POST /translate?env=` returning the TF for review, tests (deterministic skeleton unit-tested exhaustively; agent path integration-marked).

Canvas mapping: s3→aws_s3_bucket, sqs→aws_sqs_queue, sns→aws_sns_topic, sns→sqs edge→aws_sns_topic_subscription (+ queue policy note), dynamodb→aws_dynamodb_table (hash key from node config), rds→aws_db_instance is OUT of Simulate v1 (no RDS API in the gateway yet — keep rds on the reconciler path; record as unsupported in Simulate, per northstar directive 5 honesty rule).

### Task S4: TF import (TF → canvas)

**Files:** `src/odin/agent/import_tf.py`, route `POST /import-tf` (multipart or path), tests.

Two modes, research-verified: (a) parse an existing project's state/HCL for the supported resource types → canvas nodes+edges (deterministic); (b) live import: `import {}` blocks + `tofu plan -generate-config-out` against the gateway (research Q4 verified this works against custom endpoints). Unsupported resource types in the source project → listed in the response `unsupported: [...]`, never silently dropped (directive 5).

### Task S5: Simulate UI + real acceptance

**Files:** TopBar Simulate button (+ Import), a simulate progress panel fed by the WS events, `tests/simulate/test_simulate_e2e.py`.

Acceptance (real, integration-marked): canvas with s3+sqs+sns(+edge)+dynamodb → POST /translate → returned TF matches golden skeleton → POST /simulate → tofu applies through gateway → all four exist via host-side clients → `tofu plan` rerun = zero drift (the research bar) → simulate/destroy cleans → import mode (b) round-trips a bucket created out-of-band. Browser pass via playwright-cli: draw → Simulate → watch events → verify → destroy.
