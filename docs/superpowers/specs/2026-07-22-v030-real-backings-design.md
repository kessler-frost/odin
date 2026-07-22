# v0.3.0 — Real Backings: allfather sheds MiniStack

**Date:** 2026-07-22 · **Branch:** `develop` · **Target:** v0.3.0

## Direction (user-confirmed 2026-07-22)

Keep the AWS-shaped vocabulary — `s3` / `sqs` / `sns` / `dynamodb` / `rds`
nodes stay, because that's how the user thinks about these components — but
remove MiniStack entirely. Each kind is backed by a **real open-source
service** (permissive licenses only: Apache-2.0/MIT/BSD/MPL — no AGPL, no
proprietary "dev-only" licenses) run as a plain container by allfather's own
reconciler, so the backings integrate with the runtime, the World, and the
Nebula mesh like every other workload.

In scope for v0.3.0 (all user-confirmed):
1. **C1 — MiniStack removal + real backings** (this is the pivot)
2. **C2 — llm node `claude` backend** (Anthropic-compatible endpoint via
   `claude-agent-sdk`, model sonnet-5; switchable to any Anthropic-compatible
   URL, e.g. omlx/LM Studio later)
3. **C3 — Brain Toolbelt MCP** (candidate-only brain behind typed tools)
4. **C4 — M8 region-select debugging** ("what's wrong here?")
5. **C5 — Packaging** (pragmatic: `odin doctor` + one-command install)

Then: full browser e2e sweep, README rewrite verified by actually following
it, merge to `main`, tag v0.3.0, GitHub release.

## C1 — Real backings

### The backing model

- **One shared backing container per (env, service-kind)**, started lazily
  when the first node of that kind is provisioned:
  `allfather-aws-{svc}-{env}` with labels `allfather=1`, `allfather-env={env}`,
  `allfather-backing={svc}`. Dynamic host ports (`ports={svc_port: 0}`).
  Nodes are *resources inside* the backing (bucket / queue / topic / table) —
  dropping two s3 nodes means two buckets, not two object-store clusters.
- **rds stays per-node**: an rds node = its own Postgres container
  `allfather-rds-{env}-{id}` (an RDS instance IS a server). Direct spawn via
  the RuntimeDriver — no emulator in between. (Also fixes the latent cross-env
  container-name collision `ministack-rds-{id}` had.)
- **Per-env isolation** comes from per-env containers. The 12-digit
  account-scoping machinery (`account_for_env`) dies with MiniStack.

### Backing services (LOCKED 2026-07-22 — every row live-verified on Colima/arm64 with boto3)

| kind | backing | license (verified) | image | port | notes |
|---|---|---|---|---|---|
| s3 | RustFS | Apache-2.0 | `rustfs/rustfs:latest` (261MB) | 9000 | creds via `RUSTFS_ACCESS_KEY`/`RUSTFS_SECRET_KEY` (override defaults); boto3 needs path-style + region; probe `GET /health` |
| sqs + sns | goaws | MIT | `admiralpiett/goaws:v0.5.4` (22MB) — MUST pin v0.5.x (v0.4.x can't speak boto3's SQS JSON protocol) | 4100 (both) | ONE container serves both kinds; SNS→SQS delivery is in-process; needs a yaml config mounted from under `$HOME` (Colima only shares `$HOME` + `/tmp/colima` — `.odin/{env}/` qualifies) |
| dynamodb | Dynalite | Apache-2.0 | `node:alpine` + `npx -y dynalite --port 4567` (~20s cold start; no maintained image) | 4567 | verified create_table/put/get/query; NO TTL, NO Streams (accepted gaps); last commit 2025-09 |
| rds | PostgreSQL | PostgreSQL License | `postgres:16-alpine` | 5432 | per-node container (landed in C1 Task 1) |

Fallback (documented, not wired): SeaweedFS for s3 (Apache-2.0, list_buckets
quirk in anonymous mode), ElasticMQ for sqs-only (Apache-2.0).

NOT allowed (verified): MinIO/Garage/Scylla-Alternator (AGPL),
amazon/dynamodb-local (proprietary "DynamoDB Local License Agreement"),
LocalStack (the emulator pattern we're escaping).

Cross-cutting verified facts: boto3 respects per-service
`AWS_ENDPOINT_URL_{S3,SQS,SNS,DYNAMODB}` env vars; goaws queue URLs carry a
non-resolving host (harmless — boto3 dials the endpoint override); per-env
isolation is container-level (one backing set per env).

### Code changes (ground truth from 2026-07-22 read-through)

- `src/odin/aws/embed.py` — **DELETE** (MiniStack server, account scoping,
  `aws_container_env`, boto client). `CONTAINER_HOST = "host.docker.internal"`
  moves to `odin/runtime/colima.py` (the runtime owns how containers reach the
  host).
- `src/odin/runtime/shim.py` — **DELETE** (the `_docker` monkeypatch).
- `src/odin/aws/catalog_gen.py` — **DELETE** (+ `tests/aws/test_catalog_gen.py`).
- `src/odin/aws/rds.py` — **REWRITE**: `PostgresRds` with the same interface
  the Reconciler already consumes (`create_db(id, user, pw)`, `delete_db(id)`,
  `endpoint(id) -> (host, port) | None`, `container_name(id)`), implemented
  directly on the RuntimeDriver (postgres image, dynamic host port, env-scoped
  names).
- `src/odin/aws/provision.py` — **REWRITE**: `BackingAws` with the same
  interface as `MiniStackAws` (`provision(service, name, ...)`,
  `exists(service, name)`, `deprovision(service, name)`) plus:
  - `ensure_backing(service)` — start/reuse the backing container, wait
    healthy;
  - `endpoints() -> dict[str, str]` — container-reachable endpoint per running
    backing (feeds injection);
  - `gc(active_kinds: set[str])` — stop backing containers whose kind has no
    desired nodes left (called by the reconciler after execute; replaces
    nothing — MiniStack never needed gc because it was in-process);
  - sns `provision` accepts subscription targets (queue names) derived from
    Stack edges sns-node → sqs-node, and subscribes queue(s) to the topic.
- `src/odin/reconcile/actions.py` — rename `CreateMiniStackResource` →
  `ProvisionResource` (same shape: `id`, `service`).
- `src/odin/reconcile/plan.py` — rename import; PROVISIONED loop unchanged
  except: a provisioned kind that was healthy but whose resource/backing is
  gone re-enters `crashed` → recreate (supervision parity).
- `src/odin/reconcile/reconciler.py`:
  - `_observe`: PROVISIONED kinds observed in `("starting", "healthy")` (not
    just starting); healthy→`crashed` when `exists()` fails. Emit facts on
    healthy: s3 `{BUCKET, endpoint}`, sqs `{QUEUE_URL, endpoint}`, sns
    `{TOPIC_ARN, endpoint}`, dynamodb `{TABLE, endpoint}` (endpoint =
    container-reachable `http://host.docker.internal:{port}`).
  - `_execute` ProvisionResource: rds → `_rds.create_db` (unchanged); others →
    `_aws.provision(service, id, subscriptions=...)` for sns.
  - after the action loop each tick: `self._aws.gc({kinds desired})`.
  - `aws_env` injection: replace the single `AWS_ENDPOINT_URL` with boto3's
    native per-service vars — `AWS_ENDPOINT_URL_S3` / `_SQS` / `_SNS` /
    `_DYNAMODB` for backings that are running, plus
    `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION`.
    Built per-tick from `BackingAws.endpoints()` (not frozen at startup).
- `src/odin/server.py` — lifespan drops `start_ministack` /
  `install_rds_spawn_rewire` / `stop_ministack`; `_make_reconciler` wires
  `PostgresRds(env, runtime)` + `BackingAws(env, runtime)`; `embed` param
  renamed `backings` (same default True, same test ergonomics).
- `pyproject.toml` — drop `ministack`; keep `boto3` (provisioning client);
  description updated.
- UI: delete `ui/src/lib/catalog.generated.ts`; drop its import+spread in
  `catalog.ts` (L10, L243); fix TopBar tooltip (L153). Remove
  ec2/lambda/vpc/subnet/sg from palette + their bespoke components (non-
  runnable decorations post-MiniStack; keeping them would be false
  advertising).
- **Access edges stay** (standing decision, 2026-06-21 design spec §IAM +
  `fabric/nebula.py:41-42`): edges-with-permissions are the access-graph
  model, generalized from the old ec2/lambda pairs to **workload→resource**
  (service/batch/llm → s3/sqs/sns/dynamodb/rds). Modeled + AI-reviewed today,
  explicitly "modeled, not enforced" in the UX; ENFORCEMENT compiles them to
  Nebula overlay firewall rules via the preserved `sg_rules_to_firewall` when
  M7 multi-Mac activates (single-host has no inter-host traffic to police).
  `iam.ts` is generalized, not deleted.
- `review_iam` → `review_stack` (see C3): reviews BOTH the access graph
  (least-privilege / blast-radius on permission edges — the old review_iam
  semantics, kept) AND config hygiene (exposed ports, plaintext secrets,
  risky images).

### Tests (from the coverage map)

- DELETE: `tests/aws/test_embed.py`, `tests/aws/test_catalog_gen.py`.
- REWIRE (same intent, new backing): `test_provision_e2e.py` (s3 node →
  bucket exists via boto3 against RustFS), `test_rds_rewire.py` →
  `test_rds_postgres.py` (psycopg2 `SELECT 1` — the make-or-break),
  `test_skeleton_e2e.py`, `test_multikind_e2e.py`, `test_aws_usable_e2e.py`
  (container `aws s3 mb` with `AWS_ENDPOINT_URL_S3` → host sees bucket).
- LIGHT RENAMES: `test_plan.py`, `test_reconciler.py`, `test_apply.py`
  (`ProvisionResource`, container names, per-service endpoint injection).
- `tests/api/test_environments.py`: replace `account_for_env` assertions with
  per-env backing isolation (same bucket name in two envs → two containers,
  two buckets).
- NEW (closing the zero-coverage gap): sqs / sns / dynamodb each get (a) a
  unit path and (b) one real integration test; plus one **showcase e2e**:
  s3 + sqs + sns (edge sns→sqs) + dynamodb + rds + service, publish to topic →
  message lands in queue — proving backing interconnection.

## C2 — llm `claude` backend

`llm` nodes gain a `backend` field (default **`claude`**):
- `claude`: no container. The odin server mounts an Anthropic-compatible
  proxy at `/llm/{node_id}/v1/messages` forwarding to `claude-agent-sdk`
  (model `claude-sonnet-5` by default, `model` field overrides) — auth rides
  the user's existing Claude Code login. Facts:
  `ANTHROPIC_BASE_URL=http://host.docker.internal:{server_port}/llm/{id}`,
  `MODEL`. Phase: healthy once the route is live (a self-probe request checks
  the SDK is reachable). Eviction/scheduler: zero memory footprint.
- `container`: today's path unchanged (any image serving an OpenAI/Anthropic
  endpoint; `/v1/models`-or-TCP probe; memory-managed + evictable).
- `endpoint`: passthrough to an external `base_url` (e.g. LM Studio / omlx on
  another port); facts publish it; health = HTTP probe.

Consumers wire `${{mybrain.ANTHROPIC_BASE_URL}}` exactly like DATABASE_URL.

## C3 — Brain Toolbelt MCP

Rework `agent/brain.py` from free-text JSON scraping to **candidate-only,
tool-mediated output** using `ClaudeSDKClient` + `create_sdk_mcp_server`:
- `propose_changeset(changes: [{node_id, field, value}])` — the ONLY way the
  brain affects config; deterministic `merge_completion` still applies
  user-wins + `provenance="ai"`.
- `review_stack(findings: [str])` — replaces `review_iam` (IAM died with
  MiniStack); reviews exposed ports/secrets/images.
- (M8 reuses the same membrane for `report_diagnosis`.)
`/review-iam` route → `/review`; UI button copy follows. Best-effort behavior
preserved: SDK failure → stack applies as-is.

## C4 — M8 region-select debugging

- UI: existing Cmd+drag selection → floating "Ask the operator" menu
  (Debug this / What's wrong here? / free-form) → `POST /agent/debug
  {env, node_ids, question}` → answer panel.
- Server: context assembler gathers, per selected node: Stack fields
  (values + provenance), World (phase/facts/verdict/restarts), last events
  from `events.jsonl`, `runtime.logs(id, tail=40)` — then one `query()` call
  with a diagnosis system prompt; structured result
  `{answer, suspects: [{node_id, reason}]}` via the C3 membrane.

## C5 — Packaging (pragmatic scope)

- `odin doctor`: checks colima/docker, lima (optional), bun (dev only),
  claude CLI + auth, disk headroom; prints exact fix commands.
- `scripts/install.sh` (README one-liner): brew-installs missing tools, then
  `uv tool install git+…@latest`.
- Not in scope: vendoring colima/lima binaries into a single artifact
  (platform-fiddly, low value vs. brew).

## Release train

`develop` (continuous pushes) → README rewrite verified by following it
fresh (multiple runs) → CI fix (drop dead `pytest -m tofu` step) → version
0.3.0 in pyproject + description → merge to `main` locally → push → tag
`v0.3.0` → GitHub release (release.yml builds wheel + bundles UI +
fast-forwards `latest`).
