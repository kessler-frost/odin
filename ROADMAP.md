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
  (DynamoDB), real Postgres (RDS, `tofu`-managed since W2.7) — per env,
  supervised, crash-recovering, integration-tested today.
- **Runtime drivers** — Colima (containers) and Lima (VMs) behind one
  `RuntimeDriver` protocol, the execution substrate EC2/ECS/Lambda substitutes
  will run on.
- **Nebula fabric** — cert/lighthouse/config primitives, per-env networks,
  sticky IPs, `sg_rules_to_firewall` — the substrate for the network layer.
- **Spec Store** (append-only revisions), per-env isolation, the events/WS
  status pipeline, the integration-test harness, the `odin` CLI skeleton.
- **claude-agent-sdk** — two uses, deliberately different in kind. (1) An
  optional, off-by-default refine pass over the deterministic canvas→TF
  translation (`ODIN_TRANSLATE_REFINE`), fenced by a guardrail that rejects any
  change to the architecture or to a value the canvas set. (2) **SHIPPED
  (W2.9/M8): plain-English failure explanation** — `agent/debugger.py` +
  `POST /agent/debug` answer "what's wrong here?" for a selected region from
  real evidence (desired config, refs, phase, crash verdict, events, log tail),
  with per-node suspects. ON by default (`ODIN_DEBUG_AGENT=0` disables) because
  it only reads state and returns prose; secrets and env-var values are
  redacted before the prompt; an unavailable SDK answers `agent unavailable`
  rather than failing. Limits: evidence is capped at 40 log lines and 10 events
  per node and 20 nodes, so a cause that scrolled out of that window won't be
  in the answer. **`ODIN_AI=0` turns BOTH of these off at once** (`agent/ai.py`)
  — one switch for every model call in the process, checked at the SDK boundary
  as well as at each feature's own flag, and an unrecognised value disables
  them too rather than silently allowing a call. Everything else stays: the
  canvas↔Terraform translation is a deterministic compiler and is completely
  unaffected. Still roadmap, not shipped: HCL for kinds with no builder,
  least-privilege policy synthesis, import of unmodeled resource types.

## Roadmap (northstar-derived sequence)

- [x] **Gateway + IAM enforcement.** DONE 2026-07-22 (G1–G5 + synthesized
  control-plane): SigV4 verification (incl. S3 body-hash cross-check) →
  (service, action, resource) classification → applied-IAM policy
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
  6. ElastiCache (wave 2, W2.8): `aws_elasticache_cluster` → a real
     `redis:7-alpine` container per cluster, the published port advertised as
     the cluster's node endpoint and published as `REDIS_URL`/`REDIS_URL_VM`
     World facts; single-node redis only (see the limits below)

  **v1 limits, recorded rather than hidden** (northstar directive 5's honesty
  rule):
  - Lambda: `$LATEST` only — no S3-deployed packages, versions, or aliases.
    Code may be a single pasted file OR a whole directory (`sourceDir`, v0.8.14)
    whose tree is packaged as-is; dependencies are whatever you vendored into
    that directory, because odin runs no package manager and fetches nothing at
    apply time (docs/limits.md).
  - ECR: an `ecr ↔ ecs` edge sets the service's image as of v0.8.15 (a real
    `${aws_ecr_repository.<n>.repository_url}:latest` interpolation, since the
    registry's port is minted per env and can't be typed in advance); a
    hand-typed `image` still wins. Proven end to end 2026-07-29
    (`tests/simulate/test_ecr_image_edge_e2e.py`): a real `docker push` to the
    published `repositoryUri`, the local tag then DELETED, and the ECS task
    still comes up running that image — so the pull really reached the
    `registry:2` container, which answers the one link `ecr.py`'s docstring had
    never claimed (it only ever said the address works for a host-side CLI, not
    for the daemon inside Colima). Push BEFORE applying the service: odin
    builds and pushes nothing, so a combined apply asks ECS for a tag that does
    not exist yet. `ecr ↔ lambda` sets nothing and says so —
    odin's Lambda substrate is a zip in an RIE container, not a container
    image. And the ECR *permissions* the catalog offers
    (`ecr:GetAuthorizationToken`, `ecr:BatchGetImage`) are enforced by nothing:
    neither has a gateway handler, and image-layer traffic dials the
    `registry:2` container directly without passing through the gateway at all.
  - ECS: no `network_configuration` (awsvpc/Fargate-style ENIs — odin's tasks
    are `launch_type = "EC2"` / `network_mode = "bridge"`, which need none);
    a task that dies between API calls isn't auto-replaced until the next
    Apply reconciles the service. Generated services set
    `wait_for_steady_state = true` with a bounded `timeouts` block (v0.5.4,
    finding #3) so a bad image / crash-on-start fails apply fast and honestly
    instead of silently "succeeding" with a service that never runs — the
    trade-off is that a genuinely slow FIRST image pull that exceeds the
    timeout also fails apply (a retry re-uses the now-cached image). That guard
    covered CREATE but was **inert on UPDATE** until v0.7.1 (field test 2,
    finding B1): terraform-provider-aws's steady-state waiter keys on
    `len(deployments) == 1 && desiredCount == runningCount`, and a
    revision-blind `runningCount` counted the STALE tasks at the instant
    UpdateService returned, so a typo'd image tag reported `applied / tf: ok`
    in 2.3s while every healthy task was destroyed. `runningCount` (service and
    PRIMARY deployment alike) now counts only tasks on the service's CURRENT
    task definition — real ECS's own definition of a deployment's
    runningCount — so a bad-image update fails apply inside `timeouts.update`
    and the real `docker` error naming the image is carried on the deployment's
    `rolloutStateReason`, an ECS service event, the task's `stoppedReason` and
    the node's World verdict
    (`tests/simulate/test_ecs_bad_image_update_e2e.py`).
    **Deliberate deviation, recorded:** real AWS's SERVICE-level runningCount
    also counts draining old-revision tasks, but it distinguishes them with a
    SECOND deployment record, which odin does not model — with one deployment
    record, current-revision-only is the sole self-consistent choice.
    **The outage this used to cost, and the fix (v0.7.2, field test 3).** Until
    v0.7.2 odin stopped every stale task BEFORE launching a single replacement.
    Measured, not estimated — three healthy tasks serving HTTP 200, one typo'd
    image tag, sampled every 2 seconds: **the service went from 3 tasks to 0
    about four seconds after the apply started**, 108 consecutive samples at
    zero with every port refusing, and **the operator was told nothing for
    ~59 more seconds** while CI showed "running" against a service that was
    100% down. Nothing self-healed: 90+ further seconds of watching left the
    world `crashed`, and only another Apply brought it back. One typo, total
    outage in four seconds, no signal for a minute, down indefinitely.
    That was previously deferred as a genuine dichotomy — `minimumHealthyPercent
    = 100` would keep the old tasks serving, but a revision-blind projection
    would then report `healthy` while the deployment was dead, "a worse lie
    than the outage". **The dichotomy was false**, because the B1 fix above had
    already added per-task current-revision accounting: "N tasks serving the
    PREVIOUS revision, the new deployment failed" is computable from what odin
    already stores. So both halves now ship together — the scheduler surges
    first and retires second, honoring a real per-service
    `deploymentConfiguration` (`ecsctl._retire_stale` / `_serving_floor`,
    emitted explicitly as `deployment_minimum_healthy_percent = 100` /
    `deployment_maximum_percent = 200`), and the World projection is
    revision-aware, so a service left serving its previous revision reads
    **`error`** — never `healthy`, never `crashed` — with a verdict like
    `2 tasks serving the previous revision; deployment of <image> failed: <why>`.
    Sampled proof with real containers
    (`tests/simulate/test_ecs_failed_update_keeps_serving_e2e.py`): across a
    62.4s failed apply, **3 tasks and 3 HTTP 200s on every single sample —
    outage window zero seconds** — and a good update still applies in 4.6s,
    briefly running 6 tasks (the 200% surge), all of them serving.
    The apply FAILS loudly either way, so CI
    stops instead of scoring the outage green — but note it takes **two**
    guards to make that sentence true, and v0.7.1 shipped only the first.
    `wait_for_steady_state` is evaluated **only when tofu actually updates the
    resource**, so field test 3 found the hole: any apply tofu sees as a NO-OP
    (a re-apply on the already-broken service; an edit that only touches the
    launch-time `env` map, which is deliberately not in the task definition)
    skipped the check and reported `applied / tf: ok` at 0-of-3 tasks,
    reproducibly. FIXED in v0.7.2 by odin's own post-apply verification —
    `ecsctl.wait_for_steady_services`, run by `/apply-full` after its
    convergence pass, which fails the apply naming the service, running-vs-
    desired, and the real underlying reason
    (`tests/simulate/test_ecs_noop_apply_outage_e2e.py`). Bounded by the same
    60s budget as `timeouts.update` (`ODIN_ECS_STEADY_TIMEOUT` overrides), and
    it returns the moment nothing is left pending, so a healthy apply pays one
    store read. (Fixed: a
    **The blind window is CLOSED (v0.7.3).** `/apply-full` used to hold the
    reconciler's tick lock for the whole tofu run, freezing `/world` at its
    last pre-apply reading for ~60s. `hold()` now suspends the reconciler's
    ACTIONS (plan/execute, gc, the policy push, the prune) while leaving
    OBSERVATION running, so state changes are visible AS THEY HAPPEN: the
    honest reading arrived at t=62.3s before the fix (after the apply had
    already returned) and arrives at **t=4.1s** after it, re-measured
    independently by field test 4 at ~3s. Sixty observation ticks over a
    steady projection emit zero deltas, so nothing flaps. **Residual:** a replacement that takes longer than
    `ecsctl._ROLLOUT_STABILIZE_SECONDS` to crash is counted as serving and the
    old revision is retired anyway (real ECS behaves the same for a service
    with no health check configured). (Fixed: a
    `tags` block on `aws_ecs_service` now plans zero-drift — the gateway stores
    the full tag set and echoes it back, with
    `TagResource`/`UntagResource`/`ListTagsForResource` modeled.)
  - **DURING an apply, `/world` is part live and part cache — which part, and
    for how long** (field test 4, P4-4; this limit previously existed only as a
    source comment, which is precisely what northstar directive 5 forbids).
    Everything `tf_status.project()` computes from the gateway's own records is
    LIVE on every observation tick (~1s) — that is the blind window closed
    above — and so is the real state of every **ECS task**, because the
    projection re-runs `ecsctl.sweep_tasks` on each of those ticks and that
    sweep now recognises a container that has VANISHED (`absent`), not just one
    that exited. **Field test 5 put lambda and rds on that same live footing**
    (v0.7.5): every projection tick, in an apply or not, runs one bulk
    `docker ps` of container STATES (`reconcile/drift.py::live_verdicts`), so a
    function's RIE container or a database's Postgres container that is gone,
    `exited` (a `docker kill`) or `paused` reads `crashed` — with the real
    reason, and with the dead database's `DATABASE_URL` withheld — on the very
    next tick rather than on the sweep cadence. That check is READ-ONLY, which
    is what makes it safe to run mid-apply; and /apply-full runs the same read
    itself before reporting, so an apply never answers `applied` off a record
    another loop refreshes on a cadence. What is NOT live:
    - **ec2** — the Lima VM, checked by a bulk `limactl list`, still
      cache-bound during an apply;
    - **rds's `pg_ready`** — the one failure docker's own state cannot see (the
      container is running and Postgres inside it has wedged) is still found
      only by the cadence sweep, and is still REPORTED rather than written into
      the record, so it does not fail an apply.
    For those an in-flight apply reads the LAST SWEEP'S CACHE
    (`reconcile/reconciler.py::_project_tf_owned(act=False)`), deliberately and
    not as an oversight: a sweep does not merely look, it CORRECTS records
    (`mark_instance_terminated` / `mark_function_failed` /
    `rdsctl.mark_instance_failed`) off a sample taken while tofu is pulling
    images and booting VMs — the busy-daemon hazard `reconcile/drift.py`'s
    confirm-before-correcting note describes, where a false `failed` needs a
    human Apply to undo. **How long it can be stale:** the rest of the apply,
    plus up to one sweep cadence after it returns — `ODIN_DRIFT_SWEEP_TICKS`,
    default 10 ticks ≈ 10s at the production 1s poll — because the cadence
    counter does not advance while suspended and the tick both routes run right
    after the hold does not force a sweep. Field test 4 measured exactly that
    shape before the ECS half was closed: a container removed 20s into a 63.4s
    apply was still counted at 3-of-3 for 57s, 14 of them after the apply had
    returned. Drift reported BEFORE the apply keeps being reported throughout —
    the cache is stale, never empty. **Also suspended for the same
    "don't act mid-apply" reason:** the observe pass for the PROVISIONED kinds
    (s3/sqs/sns/dynamodb — it re-subscribes SNS topics, so it is not read-only,
    and it would inspect the very backing tofu is creating inside), and the
    World PRUNE of a label tofu destroyed; both resume on the tick right after.
    **If you need certainty about an EC2 VM, don't read `/world` mid-apply** —
    wait for the apply to return and give it one sweep (~10s), or ask the
    substrate directly: `odin doctor`, `docker ps`, `limactl list` and a `psql`
    connection all bypass this cache entirely. (lambda and rds no longer belong
    in that sentence — see the live half above.)
    (Pinned by `tests/reconcile/test_reconciler.py`'s
    `test_a_task_container_removed_mid_apply_is_seen_within_one_tick` and
    `test_a_deleted_ec2_vm_is_NOT_seen_until_the_apply_releases`.)
  - SNS→SQS live-edit: FIXED (v0.5.0) — adding a subscription edge to an
    already-healthy topic lands on the next Apply via the reconciler's
    observe pass (proven by real fanout to both queues).
  - **Every odin-generated SNS→SQS subscription sets
    `raw_message_delivery = true`, including on an import round trip where the
    source didn't have it.** Stated here because it CHANGES DELIVERY SEMANTICS:
    the queue receives the published body verbatim instead of SNS's JSON
    envelope, so a consumer that parses `Message` out of an envelope will not
    find one. It is deliberate and load-bearing rather than cosmetic — the
    reconciler's own `provision()` path subscribes with
    `Attributes={"RawMessageDelivery": "true"}` (`aws/backings.py`) and goaws
    honours it, so dropping it from the generated HCL would make a
    tofu-applied stack deliver differently from an `/apply`-provisioned one.
    A source `.tf` that wants the envelope has no way to say so today; the
    round trip will add the attribute back (v0.7.1, field test U3).
  - ElastiCache (W2.8): **single node, redis only.** `aws_elasticache_cluster`
    is real — a `redis:7-alpine` container per cluster, its published port
    advertised as the cluster's node endpoint, zero-drift re-plan — but
    `num_cache_nodes` must be 1 and `engine` must be `redis`; anything else is
    a real `InvalidParameterValue`, never a silent collapse to one node. That
    was only true of the APPLY path until v0.7.6 — `import-tf` reached the same
    fixed values by rewriting them, so a three-node memcached cluster became
    single-node redis without a word. It now warns per substituted argument,
    which is what makes the sentence above true from both directions. No
    replication groups, no cluster mode, no memcached (which would need a
    different substrate and a `ConfigurationEndpoint` odin doesn't emit), no
    snapshots/parameter groups beyond the metadata the provider reads back.
    `node_type` is accepted verbatim and maps to nothing real (every cluster
    gets the same fixed container memory cap) — it exists so the HCL
    round-trips, not because odin sizes anything from it.
  - **ElastiCache IAM edges gate the CONTROL plane only.** Redis's own wire
    protocol is not SigV4-signed and carries no AWS identity, so `GET`/`SET`
    traffic never reaches odin's gateway at all — exactly as on real AWS. An
    `elasticache` edge therefore grants Describe/Modify/Delete/tags and
    nothing more; whether a workload can actually *reach* the cache is a
    network question (security groups), and per the limit above those don't
    yet gate host containers either. Endpoint reachability is per-consumer the
    same way RDS's is: a container consumes `${{cache.REDIS_URL}}`
    (`host.docker.internal`), an EC2 (Lima VM) consumer must use
    `${{cache.REDIS_URL_VM}}` (`host.lima.internal`) — and "consumes" is
    literal since v0.7.1: see **Canvas wiring** below. Both are the raw
    published host port and **neither is SG-gated** (see the RDS "which fact
    is gated" note below); ElastiCache publishes no mesh fact at all, so a
    cache currently has no gated path.
  - **An UNSCOPED list/describe call is denied even with an edge drawn.** A
    workload with an `rds` edge can call
    `DescribeDBInstances(DBInstanceIdentifier="db")` but NOT bare
    `DescribeDBInstances()`; likewise `DescribeCacheClusters(CacheClusterId=…)`
    versus `DescribeCacheClusters()`. Both services behave identically, and
    the deny happens in `app.py` before either model is reached.
    **The mechanism, precisely** (re-verified against the code in v0.7.1, and
    worth stating because the intuitive explanation is not quite what
    happens): a call that names no resource classifies to the LITERAL resource
    `"*"` (`classify.py::_rds_resource` / `_classify_elasticache`), while an
    IAM edge compiles to a statement naming one literal node label
    (`policy.py::compile_policies`). Wildcards are expanded on the STATEMENT
    side only, so `"db"` does not match the string `"*"` and default-deny
    applies. The same fallback is why `tofu` is never blocked: the operator's
    statement really is `*`/`*`, which matches anything including `"*"`.
    **The denial names the action but never the resource** —
    `User is not authorized to perform: rds:DescribeDBInstances` — which is
    exactly why it reads as a contradiction: you are told the action you hold
    an edge for was denied, with no hint that the resource resolved to `"*"`.
    That, not the policy decision, is what cost a field tester a confusing
    hour. Written down rather than fixed: naming the resource is the intended
    usage, and the endpoint you actually want is also published as a World
    fact (`${{db.DATABASE_URL}}` and friends).
  - RDS is Terraform-managed (W2.7 — this used to read "RDS stays off
    Terraform"): an `rds` node compiles to `aws_db_instance`, and the gateway's
    own RDS model (`gateway/models/rdsctl.py`) fulfils CreateDBInstance with
    the same real `odin-rds-{env}-{id}` Postgres container, gating `available`
    on a real `pg_ready` connection. Its remaining limits:
    - **Postgres only.** Selecting mysql/mariadb is declined with a reason
      rather than silently handed a Postgres container (which is what the old
      reconciler path did).
    - **A node's label must be a valid RDS identifier** (lowercase letters/
      digits, single hyphens, starts with a letter) — the provider validates
      `identifier` client-side, so a label like `app_db` is declined at build
      time with the fix instead of failing inside tofu. Existing canvases with
      such labels must change the node's `data.label` (the decline message
      names that field, not "rename the node" -- a CLI author has no rename
      gesture, only JSON).
    - **`allocated_storage` / `instance_class` are metadata**: they round-trip
      faithfully (clean plans, faithful imports) but resize nothing — a local
      container has the host's disk and no instance sizing.
    - **No snapshots at all.** `skip_final_snapshot = true` is always emitted
      (without it `tofu destroy` refuses, breaking "empty canvas = full
      teardown"), and there is no DescribeDBSnapshots/CreateDBSnapshot surface.
    - **No cpu/ram facts.** The reconciler used to attach live `docker stats`
      to an rds node every tick; the TF-owned projection is a pure store read,
      so those two numbers are gone from an rds node's World facts (every other
      fact is unchanged). `odin logs`/`docker stats` still show them.
    - **A crash's log tail isn't attached to the WorldDelta** — the verdict
      names the container and its real exit code, and the log body is one
      `odin logs` away, rather than being read inside the projection.
    - The instance's **master password is stored** in
      `.odin/{env}/gateway/rdsctl.json` (0600) — the DATABASE_URL fact is built
      from it and the drift probe authenticates with it; the Stack revision on
      disk already holds the same value. `ModifyDBInstance` applies a password
      change for real (`ALTER USER`), so the published fact never lies.
  - RDS endpoint reachability is per-consumer: a CONTAINER consumes
    `${{db.DATABASE_URL}}` (`host.docker.internal`); an EC2 (Lima VM) consumer
    must use `${{db.DATABASE_URL_VM}}` (`host.lima.internal`), since a Lima VM
    can't resolve the container-host alias. odin publishes BOTH facts (v0.5.4,
    finding #5); picking the right one per consumer type is manual — automatic
    ref-routing by consumer kind is deferred.
    - **WHICH FACT IS GATED — read this before choosing one.**
      `DATABASE_URL` and `DATABASE_URL_VM` are the SAME raw published host
      port, reached by two different host aliases, and **security groups do
      NOT gate either of them**. `DATABASE_URL_MESH` is the overlay address,
      and it is **the only one a drawn security group governs**. So on a
      canvas with a VPC, a VM that a `db-sg` correctly refuses on the mesh
      still reaches the identical Postgres through `DATABASE_URL_VM` — field
      test 2 MEDIUM-5, where the fact named after the consumer type was the
      one that defeated the security group. **If the env has a mesh, a VM
      consumer should use `${{db.DATABASE_URL_MESH}}`**; `_VM` remains
      published (removing it would break existing canvases, and it is the
      right answer for an env with no VPC drawn) but it is the ungoverned
      path, deliberately, for the same additive reason as the host port
      itself. The same applies verbatim to `REDIS_URL_VM` — and ElastiCache
      has no mesh fact at all yet, so on a VM there is currently no gated
      path to a cache.
    - **The published host port is NOT stable across recreation.** It is
      ephemeral (`ports={5432: 0}`), so a `docker kill` + recovery Apply
      mints a new one (observed: 33363 → 33371) and both `DATABASE_URL` and
      `DATABASE_URL_VM` change with it. Anything baked in at VM boot time
      from `_VM` silently points at nothing after a database recovers, and
      there is no re-injection path — so re-Apply the consumer, or use
      `DATABASE_URL_MESH`, whose overlay IP and port (5432) are BOTH sticky
      across recreation. Stabilising the host port is not planned: the mesh
      address is the stable one by design.
  - Security groups: what IS enforced (W2.6) and what is not.
    - **EC2 VMs**: an instance's ASSIGNED security groups gate its overlay
      traffic — the UNION of their compiled rules is its nebula firewall
      (falling back to the VPC default SG only when it has none, as real AWS
      does), and its sg ids ride into its nebula CERT as groups, so an
      SG-to-SG rule matches by identity. Proven with two real VMs whose only
      difference is their drawn SG: one port allowed, the same port refused
      on the other, and a third port allowed in the refusing direction to
      rule out a dead tunnel (`tests/simulate/test_ec2_assigned_sg_e2e.py`).
      - **Editing a group's RULES reaches instances that are already
        running** — this used to be false, and silently so (field test 2
        HIGH-1: Apply said `applied`, the gateway had the new rule, and the
        running VM kept its boot-time firewall forever, so two VMs in one
        drawn group enforced different rules on the wire). Every Apply now
        re-renders each running instance's nebula config and, when it really
        changed, pushes it and makes the daemon adopt it: a **SIGHUP** for a
        firewall-only edit — nebula reloads firewall rules in place, so live
        tunnels are never dropped — and a restart for anything a reload
        cannot cover (only a moved lighthouse port does that, and there the
        tunnel is already dead). Unchanged rules cost one local file
        comparison: no `limactl`, no signal. Proven on two real VMs with the
        previously-blocked port probed before and after on the SAME instance,
        `NRestarts=0` and an unchanged `ActiveEnterTimestamp` across the edit
        (`tests/simulate/test_sg_edit_propagation_e2e.py`).
      - **Moving an instance BETWEEN groups reaches it too, and REVOKING is
        the case that matters.** For one release it did not, silently: an
        instance's group MEMBERSHIP lives in its nebula CERTIFICATE, not in
        its config, so the re-render above could never see a group move.
        Field test 3 HIGH-1 measured the consequence in the worst direction —
        `web1` was taken OUT of the group `db-sg` admits, Apply returned
        `applied` with exit 0 and zero warnings, and web1 went on reaching
        the database on the wire. A security control that fails open and
        reports success.
        Every Apply now compares each running instance's CURRENT groups
        against the ones odin last landed on it and, when they differ,
        re-issues its certificate (same sticky overlay IP, so nothing
        published goes stale), lands it on the VM and **RESTARTS** the
        daemon. A restart, never a SIGHUP: nebula reloads a firewall in
        place, but every peer caches the certificate of each tunnel it holds
        open, so only a re-handshake makes a new identity real — dropping the
        old tunnels is the entire point here, unlike a rule edit.
        **No recreate**: same VM, same instance id, same address.
        Proven on the wire with two real VMs and a real Postgres, in the
        hardest shape of the edit — `web-sg` and `admin-sg` carry IDENTICAL
        rules, so the move changes not one byte of web1's config and only the
        certificate can close the path (`tests/simulate/
        test_sg_membership_revoke_e2e.py`): web1→db refused after the move,
        web1→admin1:22 still answering at the same instant to prove the
        overlay is alive, and the path re-opening when web1 is put back.
        Unchanged membership costs one local file read — no `nebula-cert`,
        no `limactl`, no signal — and a reordering of the same groups is not
        a change.
      - **A revoke also reaches a connection that is ALREADY OPEN** (field
        test 4). New connections were already refused the moment the peer
        re-handshakes — measured at 0.11s, before Apply returns — but an open
        flow used to survive: field test 4 held a real TCP session to the
        database across a revoke, pushed a genuine Postgres startup packet
        down it, and the server ANSWERED. That is nebula's design, not a bug
        in it: its firewall keeps a conntrack entry per flow and re-validates
        it only when its OWN ruleset version changes, never when a peer's
        certificate does (the shipped binary's own diagnostics say so:
        `keeping old conntrack entry, does match new ruleset` vs `dropping
        old conntrack entry, does not match new ruleset`, and
        `firewall rulesVersion has overflowed, resetting conntrack`).
        So the lever is the ADMITTING member's own reload — and
        `reloadFirewall` counts a reload only if the `firewall` config
        section really changed, which a no-op reload does not. Measured
        against the shipped 1.10.3 binary, four SIGHUPs at one daemon:
        an identical config logs `No firewall config change detected`; adding
        one key nebula ignores (`firewall.odin_membership_revision`) logs
        `New firewall has been installed ... rulesVersion=1` with
        `firewallHashes` EQUAL to `oldFirewallHashes`; identical again is a
        no-op again. Equal rule hashes with a new ruleset version is exactly
        the no-op-but-versioned reload this needs, with no invented rule that
        could accidentally permit something. odin renders that key as a digest
        of the env's whole membership roster
        (`ec2compute.membership_revision`), so it moves when, and only when,
        some member's certificate groups move — and it rides inside the
        firewall block, so every member adopts it by SIGHUP, never a restart.
        Both member kinds are covered: an EC2 VM (`systemctl kill -s HUP`) and
        a database, whose sidecar now reloads in place (`docker kill -s HUP`)
        instead of being replaced. Proven on the wire
        (`tests/simulate/test_sg_revoke_drops_open_flow_e2e.py`): a real
        session held open across the revoke timed out on its next genuine
        protocol packet, while a still-permitted port on the SAME database
        over the SAME tunnel answered `connection refused` at the same
        instant, the database's own log shows the equal-hash reload, and its
        sidecar came through with the same container id, same start time and
        `RestartCount=0`.
      - **What that still does not mean.** It is not instant: the flow dies
        when the Apply's mesh passes finish (12.8s in the measurement above,
        longer on a loaded machine). And it depends on an ordering — the
        admitting member re-checks the flow against the certificate it
        CURRENTLY holds for the peer, so the peer must already have
        re-handshaked under the new one. odin enforces that (moved members are
        re-certified, restarted and poked into re-handshaking first, then
        admitting members reload, then databases — `ensure_instance_mesh`
        orders its own loop by `membership_changed`, and server.py runs it
        before `ensure_db_mesh`). If a poke fails, the admitting member can
        re-validate against the stale certificate, stamp the flow current, and
        that one flow survives until it closes or nebula's
        `firewall.conntrack` timeouts expire it; no later Apply revisits it,
        because the membership has not changed again by then.
      - **A membership change that cannot be applied FAILS the Apply.**
        `refresh_nebula` still never raises (mesh wiring must not fail an
        instance boot), but a `failed` is no longer a log line under a green
        light: `ensure_instance_mesh` raises `MeshRefreshFailed` naming every
        VM that did not adopt its groups, what is still open because of it,
        and what to do. A worse-but-honest message beats a green light on an
        unchanged firewall.
    - **RDS**: the Postgres container is a REAL mesh member (a nebula
      companion container shares its network namespace, so the stock upstream
      image answers on an overlay IP), gated by the SG its canvas node names
      in `securityGroups` — which reaches the gateway as the
      `aws_db_instance`'s `vpc_security_group_ids`, exactly like an EC2
      instance's, since W2.7 put the database on Terraform. A VM in `web-sg`
      reaches it; one that isn't is
      refused (`tests/simulate/test_sg_gates_backing_e2e.py`, plus the
      container-level `tests/aws/test_backing_mesh_e2e.py` where a real
      `psql` succeeds for the in-group member and times out for the other).
      The DB publishes the gated address as `${{db.DATABASE_URL_MESH}}` /
      `endpoint_mesh`, alongside (not instead of) the host-reachable pair.
    - **A `*_MESH` fact is now VERIFIED before it is published**, because for
      one release it wasn't: every health probe in odin dials the published
      HOST port, so a mesh endpoint that had been dead for minutes was still
      advertised beside a `healthy` badge (field test 2 HIGH-2/B8, twice, from
      two different causes). `reconcile/mesh_health.py` checks, on a sweep
      cadence (`ODIN_MESH_SWEEP_SECONDS`, default 30s; failures re-checked
      every 5s), that the env's lighthouse is alive, that the resource's mesh
      sidecar is running IN THE CURRENT container's network namespace, and
      that the overlay address itself answers — the last via one bounded
      `nc -z` run from inside the member's own namespace, the only rootless
      place a check can stand (the Mac is deliberately not a data-plane mesh
      member). When it fails, the `*_MESH` facts are WITHHELD and the resource
      reports `crashed` with the reason; the host path is untouched. It costs
      nothing for a resource that publishes no mesh fact. What it does NOT
      prove: that a specific peer is allowed in — that's the SG's job, and a
      refusal there is policy, not a fault.
    - **A mesh restart no longer leaves a window where the address is
      advertised but silent.** Field test 3 MED-2 measured it: ~10s (broken
      17:50:12, restored 17:50:22) after a sidecar restart during which
      `/world` said `healthy` and published the `*_MESH` fact while the peer's
      probe timed out — enough that the engineer's first security-group probe
      failed for BOTH VMs and read as "SG gating is broken". The cause is
      nebula behaving correctly, not a bug: the peer keeps sending into the
      tunnel that just died, the restarted member answers `recv_error`, and
      the peer deliberately ignores the first few of those before tearing the
      stale tunnel down — which, at a TCP probe's 1s/2s/4s retransmit cadence,
      is about ten seconds of a silently dead path. The member that restarted
      now MOVES FIRST instead: one bounded packet toward each peer's overlay
      address (`fabric/nebula.py::rehandshake_script`), which forces a fresh
      handshake and, in the same instant, replaces that peer's cached tunnel
      AND its cached certificate for us. That is also what makes a re-issued
      certificate take effect in one round trip rather than "eventually".
      Paid only on a real restart — an unchanged member never reaches it, and
      an env with no mesh never pays a millisecond. `mesh_health`'s own
      caveat still stands and is unchanged: probing from inside a member's own
      namespace cannot observe peer-side staleness, so this closes the window
      at the source rather than detecting it.
    - **One lighthouse port per ENV, not per machine.** It used to be a single
      fixed 4342, so the second env to start a lighthouse lost the bind, its
      `nebula` exited 1 with only a log line, and its whole mesh silently
      never worked. Each env now allocates its own port from 4342–4441
      (recorded in its `overlay.json`, reported by `GET /mesh` as
      `lighthouse_port`, embedded in every member's `static_host_map`), skips
      ports other envs in the store hold, and moves itself if the recorded one
      has since been taken. `ODIN_LIGHTHOUSE_PORT` pins it. Still rootless.
      A move reaches every member: sidecars re-join on the changed config, and
      already-running VMs are pushed the new `static_host_map` and RESTARTED
      by the same Apply-time refresh the SG-edit fix added (a restart, not a
      SIGHUP, because nebula does not reload that section — and a member whose
      lighthouse moved has no working tunnel to lose).
    - **...and teardown really stops it.** For one release every
      apply/destroy cycle leaked a live lighthouse and one held port, and the
      minimal canvas that did it had no EC2 at all — a VPC plus a single S3
      bucket (field test 3 HIGH-A). `odin destroy` reported "destroyed" and
      deleted `.odin/<env>/nebula/`, taking with it the pidfile that was the
      only way to name the process still running against it; three orphans
      were measured on `*:4343`/`*:4344`/`*:4345`, one 8m20s old. It did NOT
      leak on an env with VMs, which was the tell: the only stop was
      `ec2compute._finish_terminate`'s "last VM leaves", which an env without
      VMs never reaches. Now `_delete_vpc` stops the lighthouse BEFORE
      deleting its directory (ordering is the fix — `ensure_stopped` finds the
      process through the pidfile in there), and
      `fabric/nebula.py::reap_orphaned_lighthouses` is the startup backstop
      for one that leaked earlier or for a crash between those two steps. It
      identifies a leak by EVIDENCE, never by name: the process's own
      `-config` argument must point inside this store's root at a
      `lighthouse-config.yml` that no longer exists, so a live env's
      lighthouse, another odin store's, and a user's own `nebula` can none of
      them match. Proven by three real apply/destroy cycles on that exact
      canvas: zero surviving `nebula` processes, the port bindable again after
      each, and every cycle reusing 4342 rather than walking up the range
      (`tests/simulate/test_lighthouse_no_leak_e2e.py`).
    - **EC2 nodes publish addresses too** (they published nothing before):
      `${{web1.PRIVATE_IP}}` (host-reachable, ungated) and
      `${{web1.MESH_IP}}` (the SG-gated overlay address, sticky across
      recreation). `MESH_IP` is held to the same standard as `*_MESH` above —
      withheld when the env's lighthouse is down. For a VM that lighthouse
      check is ALL that is verified: its nebula is a systemd unit inside a
      Lima VM, and a `limactl shell` per VM per sweep is not a tick's price.
    - **An INTERRUPTED apply no longer strands VMs nothing can reclaim.**
      Field test 3 HIGH-B, and the one a user hits by closing their laptop:
      `kill -9` on tofu mid-apply (equally Ctrl-C, or an OOM) leaves tofu's
      state empty while the VMs it already created keep Running. `odin
      destroy` then answered `destroyed / tf ok` in 1.7s with three real VMs
      up and `/world` still listing seven resources — and a second destroy,
      the empty-canvas Apply, and a server restart all spared them, because
      `reap_orphaned_vms` builds its "expected" set from the very store that
      still claimed them. Reality and the store disagreed and the store was
      trusted. Only `limactl delete` by hand worked.
      Two fixes, because there are two moments. **`/destroy` now reclaims
      directly**: destroy is unambiguous about intent, so anything the
      gateway store still claims is deleted by exact name and forgotten —
      instances (`ec2compute.reclaim_env_instances`) and the VPC/subnet/SG
      records the same interruption stranded (`ec2net.purge_env`, which also
      stops the lighthouse those records were keeping alive). If a VM cannot
      be deleted, destroy REFUSES to say `destroyed` and names it. **And
      startup reclaims what an older odin left**: `reclaim_tf_forgotten_vms`
      takes tofu's own state as the second witness — an instance the store
      claims and the state has forgotten can never be reached by any
      terraform operation again, so it is deleted and forgotten. Read
      strictly: a state file that is missing or empty is NO evidence and
      reclaims nothing.
      Proven by SIGKILLing the real `tofu` process mid-apply (identified by
      its working directory, so nothing else on the machine can be touched)
      and then requiring a supported command to clean up
      (`tests/simulate/test_interrupted_apply_reclaim_e2e.py`).
    - **RESIDUAL GAP, stated plainly: the raw host port is still open and
      SGs do NOT gate it.** Mesh membership is ADDITIVE — every backing keeps
      its published Docker port, because the gateway forwards AWS calls to
      it, odin's own health probes use it, host-side clients (tests, psql,
      boto3) use it, and `${{node.VAR}}` facts publish it. So anything that can
      already dial `127.0.0.1:<published port>` (any process on your Mac, any
      container that can reach the host, **and any EC2 Lima VM via
      `host.lima.internal:<published port>` — i.e. exactly what the
      `DATABASE_URL_VM` fact hands a VM consumer**) still reaches Postgres
      with no SG in the path. Only the overlay path is governed. Closing the host path
      would mean making the Mac itself a data-plane mesh member, i.e. a host
      `tun` device, i.e. root/sudoers — rejected (see the Nebula bullet).
      On a local-first single-Mac tool the host is already inside the trust
      boundary; the SG story is honest about gating traffic BETWEEN drawn
      resources, not about sandboxing your own machine.
    - The AWS non-VPC backings (RustFS/goaws/dynalite/registry) join the mesh
      with certs + overlay IPs but nebula's allow-all firewall: real AWS
      doesn't SG-gate S3/SQS/SNS/DynamoDB/ECR either (IAM and endpoint policy
      do — the gateway's job, NORTHSTAR directive 4). Their shared-container
      shape also means gating would be per-CONTAINER, never per-resource.
    - Backing mesh membership is inert unless the canvas drew a VPC (no CA →
      no join), needs `NET_ADMIN` + `/dev/net/tun` INSIDE the sidecar
      container (a container capability Colima grants unprivileged — no sudo,
      no host change), and `ODIN_BACKING_MESH=0` turns it off.
    - **Egress rules are ENFORCED as of v0.8.17** — this line used to read
      "not modeled yet: egress rules (nebula gets allow-all outbound
      regardless of what a canvas draws)". `sg_rules_to_firewall` now compiles
      `IpPermissionsEgress` into nebula's `outbound`, and
      `ec2net._compiled_firewall` stops filtering the egress half of the store
      away. MEASURED on a real mesh (`tests/simulate/
      test_sg_egress_gates_e2e.py`): a member whose group's only egress rule is
      `tcp:6000` reaches tcp:6000 on a peer and gets **nothing** from tcp:5432
      on that same peer in the same instant; revert the compiler and the same
      probe returns `pong5432`. AWS's default is untouched — an empty
      `egressRules` field keeps the seeded allow-all, so an existing canvas
      admits the same packets. What it gates is OVERLAY traffic only; see
      `docs/limits.md` for what that does and does not cover.
    - **…and enforced ON A REAL EC2 VM as of v0.8.18**, which was a separate
      claim: v0.8.17's proof was container-level and said so ("no real Lima VM
      ever wore a restricted egress … that is an inference, not a
      measurement"). A VM reaches nebula by a wholly different route — config
      via `limactl shell … sudo tee`, daemon under systemd as root — so "same
      bytes" was not "same effect". MEASURED (`tests/simulate/
      test_sg_egress_gates_vm_e2e.py`), two real VMs whose groups differ ONLY
      in `egressRules`: `locked → peer:6000` (its own rule) returns `HTTP/1.0
      200 OK` and `locked → peer:5432` returns `REFUSED timed out`, while
      `peer → locked:5432` returns `HTTP/1.0 200 OK` in the same run — the
      control that rules out "no VM can reach that port". Revert the compiler
      and the blocked cell alone turns into `HTTP/1.0 200 OK`.
      Two things that fell out of it, both in `docs/limits.md`: a restricted
      egress also drops the member's own ICMP **re-handshake poke**
      (`rehandshake_script`), so `_converge` is no longer unconditional; and
      what egress does NOT gate is now measured rather than reasoned — the same
      locked VM still reached the host over vzNAT and got HTTP 200 from
      `https://example.com`.
    - Not modeled yet: NACLs and an SG's self-reference (`self = true`).
      ICMP is a narrower gap than this line used to claim. `icmp:-1:...` still
      can't be drawn — `ingressRules` takes a numeric port or an `8000-8100`
      range, and a bare `-1` is neither; pinned by `test_sg_port_ranges.py::
      test_a_bare_minus_one_port_is_still_declined`, since a range grammar is
      exactly the change that could have made this stale by accident. But ICMP
      as such IS drawable, which the old wording denied. Measured against the
      range grammar: `icmp:-1:0.0.0.0/0` → declined with the format reason;
      `icmp:0:0.0.0.0/0` and `icmp:8:0.0.0.0/0` → both drawn, and both compile
      to the IDENTICAL `proto: icmp, port: any`, because `_PORTLESS_PROTOCOLS`
      discards the port. That is the right AWS semantics (an SG's ICMP
      type/code lives in the port fields and nebula has no such granularity)
      but it is a quiet footgun worth naming: **the number you write after
      `icmp:` is ignored**, so `icmp:8` means all ICMP, not echo-request.
      Verified on the wire by the VM test above, where the rule gates real
      pings in both directions — an `icmp:0` rule admits an echo REQUEST
      (type 8), which it could not do if the port were honoured.
  - Single local server by design: `ODIN_GATEWAY_PORT` overrides the embedded
    gateway's port, but there is no supported way to run two servers against
    the same CWD-relative `.odin` store (the second binds-conflicts on the
    gateway port and would resume/reconcile the first's envs). Run a second
    instance only from a separate working directory with its own store.
    - **`ODIN_REAP_EC2_VMS=0` is what makes that second instance actually
      safe** (v0.7.1, field test U7). On startup odin deletes every Lima VM
      named `odin-ec2-*` that no env's store expects — and a second instance's
      store expects NONE of the first's, so it would reap them all. Until
      v0.7.1 the only seam was the `create_app(reap_ec2_vms=False)` keyword,
      so the documented "run a second instance" advice meant bypassing the
      `odin` CLI with a hand-written factory wrapper. The variable is read
      inside the reaper itself (`gateway/models/ec2compute.py`), so it holds
      for `odin start`, a bare `uvicorn`, and any other caller alike.
      Accepts `0`/`false`/`no`/`off`; **anything else, including unset or a
      typo, leaves the reaper ON** — a safety net you disabled by mistyping
      is not a safety net.
    - **What you give up:** the crash-recovery backstop. The reaper exists
      for the window where odin dies between `vm.delete` succeeding and the
      store update landing; with it off, that leftover VM stays on disk
      burning memory and disk until you `limactl delete` it by hand. It is
      the ONLY thing the reaper does — it never touches a VM some env's store
      still expects, and only ever considers names matching odin's own
      `odin-ec2-` convention, so your own Lima VMs were never at risk either
      way. Leave it ON unless a second odin (or another agent's VMs) shares
      the machine.
  - Nebula: single-host mesh is REAL end-to-end — a real host lighthouse
    process (`fabric/nebula.py::LighthouseManager`) and a real `nebula`
    daemon inside every VPC-joined EC2 VM, the VPC's compiled SG firewall
    baked into its config. The lighthouse needs NO host privileges at all:
    it only ever coordinates (tells mesh members where to find each other),
    never carries their traffic, so it runs with `tun: disabled: true` —
    plain unprivileged `nebula`, no root, no sudo, no one-time setup
    (empirically verified: an unprivileged process with that flag starts
    and binds its UDP port; the same config without it dies immediately
    with "operation not permitted"). The data-plane members are the VMs
    (running `nebula` as root INSIDE the VM via systemd — that costs the user
    nothing, since it's a VM they already own outright) and, since W2.6, the
    per-env BACKING containers: a nebula companion container shares the
    backing's network namespace, so an unmodified upstream image
    (postgres/RustFS/goaws/dynalite/registry) answers on an overlay IP. That
    needs `NET_ADMIN` + `/dev/net/tun` inside the sidecar CONTAINER — a
    container capability, not a host privilege (verified live on stock
    Colima). The macOS host itself is still NOT a data-plane member, by
    design: a host tun device would need root. **Real
    finding:** stock Lima `vz` NATs every VM into its OWN isolated address
    space — there is NO VM-to-VM underlay path at all (confirmed live: a
    raw ping between two VMs' vzNAT addresses is 100% loss, before nebula
    is even involved), so cross-VM mesh traffic routes THROUGH the
    lighthouse acting as a relay (`relay: {am_relay: true}` on the
    lighthouse, `relay: {use_relays: true}` on every VM) rather than
    direct — still fully unprivileged, since relaying is opaque encrypted
    UDP forwarding between two peers already handshaken with the lighthouse,
    needing no tun device either. **Second real finding (W2.6):** Lima
    automatically forwards every port a guest listens on to the HOST's
    127.0.0.1 — including each EC2 VM's own `nebula` on UDP 4242, so
    `limactl hostagent` HOLDS host 127.0.0.1:4242 (seen with lsof, right
    next to the lighthouse's own socket). Colima maps
    `host.docker.internal` onto the host loopback, so every BACKING
    container's handshake to the lighthouse was being delivered into a VM
    instead: container↔VM mesh traffic could not work at all while any EC2
    VM existed, though VM↔VM (which rides the vzNAT address, never
    loopback) was fine. Lima's own `portForwards: ignore` does not suppress
    it for UDP (the rule lands in the instance's effective config and
    `limactl` binds the port anyway), so the host lighthouse now listens on
    its OWN port, 4342 (`fabric/nebula.py::LIGHTHOUSE_PORT`) — nothing in
    any guest listens there, so nothing can forward it out from under us,
    including a user's own unrelated Lima VMs. The live overlay proof
    (`tests/simulate/test_nebula_mesh_e2e.py`) boots two real VMs and
    proves a real VM-to-VM ping (via the relay) plus a real
    SG-rule-filtered connection — the host itself has no overlay presence
    to test from. Cross-Mac reachability (a second machine's mesh) is still
    open — see M7.
- [x] **Canvas wiring — a node's `env` actually reaches its container.** An
  `ecs` or `lambda` node's `env` map (static entries plus
  `${{producer.ATTR}}` references) is delivered into the REAL container. Until
  v0.7.1 the two bullets above ("a container consumes `${{cache.REDIS_URL}}`",
  "a CONTAINER consumes `${{db.DATABASE_URL}}`") were **not achievable**: both
  field-test agents confirmed the map was silently dropped — `spec/translate.py`
  parsed it and lifted refs onto `ResourceDesired.refs`, but `agent/hcl.py`
  emitted no `environment` block at all, `fabric.resolve` had no production
  caller, and the container came up with the four `AWS_*` vars and nothing else.
  So you could provision the whole production stack with no canvas-driven way
  to hand the app its connection strings. Proven end to end by
  `tests/simulate/test_ecs_env_wiring_e2e.py`: a real ECS task container speaks
  a real Postgres SSLRequest and a real Redis `PING` to the addresses its
  canvas `env` refs resolved to.
  - **Injected at container LAUNCH, not through the generated HCL**
    (`gateway/wiring.py`), on the same seam that already injects the workload's
    issued gateway credentials, keyed off the `odin:node` tag. A resolved
    `DATABASE_URL` carries the database password, so putting it in
    `container_definitions`/`environment` would write it in plaintext into
    `main.tf` AND `terraform.tfstate`; it would also drift on every plan (the
    value embeds a Docker-assigned host port) and freeze a stale port into
    state. Facts come from the gateway's own live records, not from World,
    because World is not written until the reconciler's next tick — after the
    apply that creates both nodes.
  - **Ordering** comes from a real `depends_on` on the consumer's resource —
    the one thing an interpolated value would have given for free — so it
    carries no values.
  - **A ref that cannot be resolved fails honestly**, never an empty string: the
    ECS task goes STOPPED / the Lambda `State: Failed` with a reason naming the
    variable, the producer and what the producer does publish, which surfaces as
    a `crashed` node with that verdict AND (since the service stays short of
    desired) a FAILED apply. A ref naming a node that isn't on the canvas at all
    is reported in the apply response's `unsupported` at build time.
  - **v1 limits, recorded rather than hidden:** there is still no `env` editor
    in the UI's ConfigPanel for an ecs node — the map must be authored in the
    canvas JSON (`odin canvas set`). Only `rds`, `elasticache`, `alb` and
    `ec2` publish facts the injector resolves, so only those can be referenced
    from `env`. **`ec2` was the one that had to be added afterwards**: the
    mesh work published `${{web1.PRIVATE_IP}}` / `${{web1.MESH_IP}}` into
    World and the wiring work built the injector IN PARALLEL, coordinating
    only on the names — so for one release the facts demonstrably existed and
    no workload could consume them. The injector now applies the projection's
    own gates verbatim (running-only, `odin:node`-tagged, and
    `reconcile/mesh_health.py` deciding whether `MESH_IP` survives), so a ref
    resolves exactly when `/world` shows that fact — and a withheld `MESH_IP`
    (dead lighthouse) fails the ref honestly rather than injecting an empty or
    stale address. Values are read at LAUNCH,
    so editing a node's `env` only reaches a task once that task is replaced
    (real ECS behaves the same way — a taskdef change forces a new deployment);
    a re-Apply that changes nothing tofu can see will not restart it.
    `ResourceDesired.refs` does not record whether a ref came from `env` or from
    a top-level field, so a top-level `${{...}}` field also arrives as an env
    var named after that field.
  - **Not built: an ECS `command`.** The canvas has no `command` field for
    `ecs` and `hcl.py::_ecs_container_definitions` emits only
    `name`/`image`/`essential`/`portMappings`, so a task always runs its
    image's own entrypoint (`compute/tasks.py` would honour a `command` in a
    task definition, but nothing on the canvas path ever puts one there —
    locked by `test_ecs_container_definitions_never_carry_a_command`). Worth
    adding alongside the missing `env` editor if a real canvas needs to
    override an entrypoint; SECURITY.md's "what odin executes" section must be
    updated in the same change.
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

  **v0.8.15 — the drawn group is now the one that receives.** Those two
  destinations are derived from the workload and read from nowhere, so a Log
  Group tile drawn to a lambda used to produce TWO groups: the drawn one, which
  the policy granted `logs:PutLogEvents` on, and `/aws/lambda/{fn}`, which got
  every line. The drawn one stayed empty forever unless its label happened to
  coincide. An `aws_cloudwatch_log_group` whose node is edged to a lambda/ecs
  node is now created under that workload's destination name, and the policy's
  `Resource` follows it — `docs/limits.md` records the two costs (a workload
  that hardcodes the *label* in its own PutLogEvents call, and an import that
  re-labels the node).

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
- [x] **ALB — a load balancer backed by a REAL reverse proxy (W2.5).**
  DONE 2026-07-25: the `alb` node is real. One canvas node expands to
  `aws_lb` + `aws_lb_target_group` + `aws_lb_listener`, the gateway answers
  the whole `elasticloadbalancing:*` surface the TF provider drives (create/
  describe/delete for all three, load-balancer and target-group and LISTENER
  attributes, `ModifyTargetGroup`, `RegisterTargets`/`DeregisterTargets`/
  `DescribeTargetHealth`, and the ARN-only tag API), and the substrate is an
  actual **nginx:alpine container per load balancer** whose upstreams are the
  target group's registered targets. Draw a network edge from an `alb` to an
  `ecs` node and that service's tasks become the targets — registered by
  odin's ECS model as each task launches, exactly as real ECS's own service
  scheduler does it. Proven end to end: two ECS tasks behind one ALB, `curl`
  the LB's real published port for a 200, `docker rm -f` one task, the LB
  keeps serving 200s from the survivor, `tofu plan -detailed-exitcode` clean,
  empty-canvas Apply tears everything down
  (`tests/simulate/test_alb_tf_e2e.py`).

  *nginx, not Caddy* (both permissive, so licence wasn't the tiebreaker):
  ~10MB image, a config that's one `server` line per target, reload is a
  plain SIGHUP (so `docker kill -s HUP` — no `docker exec` seam and no
  admin-API dance; Caddy v2 dropped v1's signal reload), and
  `proxy_next_upstream` gives request-level failover, which is the behaviour
  that makes the kill-one-task proof work.

  **v1 limits, recorded rather than hidden:**
  - **Health checks are PASSIVE.** Open-source nginx has no active upstream
    checking (`health_check` with a URI/interval is NGINX Plus), so the target
    group's `HealthCheckPath` is **not polled by the proxy**. The honest
    mapping is `max_fails=1` + `fail_timeout` ← `HealthCheckIntervalSeconds`:
    one failed real request takes a target out of rotation for one interval.
    `DescribeTargetHealth` therefore answers from a REAL odin-performed HTTP
    GET against the target's real address on that path (compared to
    `Matcher.HttpCode`), never from an invented "healthy" — a refused
    connection reports `unhealthy` with the actual error.
  - **The reachable address is not the DNS name.** odin publishes the proxy on
    a DYNAMIC host port (a fixed 80 would collide across load balancers and
    across envs), and `DNSName` has nowhere to put a port. So `DNSName` is
    `127.0.0.1` and the real endpoint lives where a port belongs: the
    `ALB_ENDPOINT` World fact on the canvas node, and an odin-only
    `odin.endpoint.url` load-balancer attribute.
  - HTTP only: no HTTPS, no ACM certificates, no `SslPolicy`, no ALPN, no
    mutual TLS. `aws_lb_listener_certificate` is unmodeled.
  - One listener per load balancer is what the canvas authors (the model
    itself supports several, each published on its own host port; adding or
    removing one RECREATES the proxy container, since Docker can't change a
    live container's published ports — a target change is a zero-downtime
    SIGHUP reload instead).
  - **Default action only, and only `forward`.** No `aws_lb_listener_rule`, no
    path/host routing, no weighted or sticky target groups. `redirect`,
    `fixed-response` and `authenticate-*` actions are REFUSED with a real
    `ValidationError` rather than accepted and silently not served.
  - `application` type only. A `network` LB (NLB) reports itself unsupported
    on Apply instead of quietly becoming an ALB; `internal = true` always
    (odin has no internet gateway to be internet-facing through).
  - Targets: `ecs` and — since v0.8.15 — `ec2`. An alb→`ec2` edge emits a real
    `aws_lb_target_group_attachment` naming `aws_instance.<n>.id`, which is the
    form the model's generic `RegisterTargets` resolves through the EC2-compute
    store to the VM's real address. That resolution had never actually run:
    `_instance_address` read `private_ip_address`/`public_ip_address`, keys
    `ec2compute` has never written (its record carries `private_ip`/
    `public_ip`), and the one test covering it fabricated a record with the key
    the reader wanted. Nothing on the canvas could produce an `i-…` target, so
    it was never caught in the field either. Both are fixed; the test now boots
    an instance through ec2compute's own RunInstances.
    PROVEN END TO END 2026-07-29 (`tests/simulate/test_alb_ec2_target_e2e.py`),
    which this path had never been: a real `tofu apply` boots a real Lima VM
    (192.168.64.2), a real nginx container fronts it, and a GET on the load
    balancer's published port returns 200 with the VM's own bytes — apply
    including the VM boot, 66s. Re-injecting the old keys fails it on the
    rendered nginx config (`server i-994e52be19f90b93a:80`), so the test reads
    the real defect and not a fabricated one. It also settles a question
    nothing had asked: a container on Colima CAN reach a Lima VM's vzNAT
    address.
    A task target is `(host.docker.internal, its real published host port)`:
    the honest local analogue of an `ip` target for a bridge-mode container,
    not a fiction about instance ids.
    A `lambda` target is still REFUSED, with the reason: nginx dials host:port
    upstreams, and a lambda needs HTTP translated into the RIE's invoke
    envelope — the same shim `apigateway → lambda` needs, built once there.
  - Cross-zone/AZ behaviour, access logs, WAF, deletion protection and every
    other load-balancer attribute are STORED AND ECHOED for zero-drift
    fidelity and do nothing. AWS's own "an ALB needs ≥2 subnets in different
    AZs" validation is not enforced (the canvas gives one containing subnet).
  - `tofu apply` spends ~60s on `aws_lb` creation regardless of how fast the
    real container comes up: that's terraform-provider-aws's own fixed
    pre-poll delay in its `LoadBalancerActive` waiter, tuned for real AWS's
    multi-minute provisioning. Nothing odin returns can shorten it.
  - The proxy container is NOT covered by W2.2's drift sweep yet — `docker rm`
    it out of band and the load balancer still reports `active` until the next
    Apply re-converges it.
- **ONE account id, everywhere** — `000000000000` (`aws/backings.py::ACCOUNT`).
  `sts:GetCallerIdentity` reports exactly the account that appears inside every
  ARN odin builds, so the ordinary workload pattern (ask STS who you are, build
  an ARN from the answer) builds an ARN odin recognises. FIXED in v0.7.1: STS
  used to answer with a per-env sha256-derived id
  (`gateway/synth.py::account_for_env`, e.g. `561031708110`) while every ARN
  used `ACCOUNT`, so that pattern silently produced unmatchable ARNs (v0.7.0
  field test, U6). Unified toward `ACCOUNT` rather than making ARNs per-env
  because nothing in odin needs per-env account ids — envs are already isolated
  by their own stores and backing containers — and ~15 modules bake `ACCOUNT`
  into ARNs. The TF provider never noticed either way
  (`skip_requesting_account_id = true`); only workload STS callers did.
- **Drift is checked with `odin tf plan`, never by hand.** The generated
  `main.tf` under `.odin/<env>/tf` is PORTABLE by design (real AWS Terraform —
  the translation guardrail forbids `endpoints`/`localhost` in it), and the
  endpoint + operator credentials are injected by the runner at invoke time.
  So a hand-run `tofu plan` in that directory **talks to real AWS** — a field
  engineer's first attempt did exactly that and came back with a genuine
  `UnrecognizedClientException` from Amazon; on a machine with real
  credentials in the environment it would have planned against the real
  account (v0.7.2, field test 3; field test 2's U8 asked for this first).
  Portability was kept and the safe path was made the obvious one instead:
  `POST /tf/plan` / `odin tf plan` run through the same machinery `/tf/apply`
  uses (same workspace, same injected `AWS_ENDPOINT_URL`, same per-env
  OPERATOR credentials, same lock, same secret scrubbing) with
  `tofu plan -detailed-exitcode`, and every materialized workspace now carries
  a `README.md` that says the hazard out loud where someone would `cd`.
  - **Exit codes are the product**: 0 no changes, 2 changes present, 1 a real
    error or refusal — and 3, not 2, for an unreachable server, because 2
    already means drift here and a down odin must not read as a clean
    detection. (`odin tf plan` is the only command that deviates from the
    repo-wide 0/1/2 contract, deliberately.)
  - **Read-only**: no `-out` plan file, no `wiring.stage`, no Stack commit,
    and it is NOT recorded on `odin tf status`'s last-run cache — a drift
    check must not make the last real apply look like it went differently.
    It DOES regenerate `main.tf`/`override.tf` from the current canvas first
    (the same files an apply regenerates), which is what makes "changes
    present" mean "the canvas and the env disagree".
- **A failed Lambda invoke reports `FunctionError`, as real AWS does.**
  `aws lambda invoke` on a handler that raises used to come back
  `StatusCode: 200` with no `FunctionError` — the documented AWS way to
  detect a failed invoke — so a CI job scored a crashing function as a
  success (v0.7.2, field test 3; same failure shape as the exit-0-during-an-
  outage bug: the truth was available and the success signal didn't reflect
  it). **Cause:** the real RIE does not send `X-Amz-Function-Error` at all. A
  raised handler answers `200 OK` with the error document as the BODY and no
  header; an import failure or runtime exit answers `502` with the same
  shape. odin read only that header, so the value was always None — and with
  it `last_invocation_error`, the World verdict's own field (v0.7.1), which
  is fed from the same value and was therefore also silently dead. odin's
  fake RIE in the unit tests obligingly sent the header real RIE never sends.
  `compute/functions.py::_function_error` now reads the invocation outcome
  off the response RIE actually gives (non-200, or a body carrying BOTH
  `errorType` and `errorMessage`), one signal feeding both the
  `x-amz-function-error` response header the SDK parses and the durable
  record. Always `Unhandled`: RIE collapses AWS's Handled/Unhandled
  distinction, so odin reports the value an uncaught handler exception gets
  on real AWS rather than inventing a difference it cannot observe. Proven by
  `tests/simulate/test_lambda_failure_e2e.py` — real RIE containers, a real
  gateway, and boto3's own `FunctionError` as the assertion.
- **`tofu` runs are BOUNDED, and a wedged destroy says why.** `init`/`apply`
  each get `ODIN_TOFU_TIMEOUT` (default 600s); `destroy` gets a smaller
  WHOLE-CALL deadline, `ODIN_TOFU_DESTROY_TIMEOUT` (default 300s, `init`
  included), after which the process GROUP is killed and the failure tail names
  the cause plus the recovery (v0.7.1, field test 2 finding B6 — a destroy on a
  restored env was killed by hand at 8m26s with no progress).
  - **Why a destroy used to wedge, and the real fix:** `tofu destroy` has to
    REACH the backings its resources live in (an s3 bucket is deleted by a real
    DeleteBucket forwarded to RustFS), but `/destroy` never booted them. With no
    registered backing port the gateway answers every call with a real
    `503 ServiceUnavailable`, which aws-sdk-go-v2 treats as retryable: ~25
    attempts with exponential backoff per call, none of which prints anything on
    tofu's stdout — hence a silent hang. `/destroy` now runs the same
    `ensure_backings` phase `/apply-full` does, so **destroy-first on a restored
    env just works** instead of needing a manual Apply first. It runs inside the
    reconciler's `hold()`, spanning ensure + the whole destroy + the empty-Stack
    commit, so (a) no tick's gc can stop a backing mid-destroy and (b) the very
    first tick afterwards gc's every backing the ensure started — nothing is
    left running (`tests/api/test_apply.py`, with a destroy deliberately slower
    than the poll interval).
  - **A DOWN backing is no longer an authorization failure.** It fires its own
    `on_unavailable` seam and lands as a `backing_unavailable` event (with the
    service that is down and the recovery), instead of a `backing-unavailable`
    `access_denied` — which was protocol-wrong (the policy check has already
    passed; the answer is a 503) and polluted the exact stream a security review
    reads for real denials.
- **Recorded as UNSUPPORTED for now** (northstar directive 5's honesty rule):
  EKS, CloudFormation, autoscaling, and KMS (the `kms` catalog node is an
  unbacked placeholder — no substitute, no gateway model, and as of W2.6 it
  advertises no IAM actions either, since a `kms:Encrypt` permission could
  never be enforced or even reached). (ALB/ELBv2 was on this list until W2.5
  and RDS-via-Terraform until W2.7 — `aws_lb` and `aws_db_instance` are real
  now; see the ALB and RDS limits above for what's still missing inside them.)
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
- [x] **Backup/restore (W2.3) — control plane, not data.** `odin export` /
  `odin import` tar one env's `.odin/<env>/` (Stack lineage + HEAD,
  `world.json`, `keys.json`, the gateway's synth stores + lambda zips, the tofu
  workspace INCLUDING `terraform.tfstate`; `tf/.terraform/`'s provider cache is
  the one deliberate exclusion). Both work with the server DOWN, which is the
  whole point. What that means precisely:
  - **Data-plane state is NOT in the archive.** Restore + Apply gives you fresh
    backing containers matching the archived desired state: a bucket comes back
    empty, an RDS table you created is gone.
  - **One asymmetry worth knowing** (v0.7.1, field test): **CloudWatch
    log-group events SURVIVE** an export → wipe → import cycle, because odin's
    log sink is a control-plane sidecar (`gateway/logsctl.json`) rather than a
    backing container's volume. So log history persists while bucket objects
    don't — the opposite of what the "control plane, not data" rule leads you
    to expect for anything that looks like data.
  - **Guardrails:** refuses an existing env dir without `--force`, refuses any
    absolute/`..`/link member, and refuses to run while odin is up — as of
    v0.7.1 detected however the server was started (`odin start` OR a bare
    `uvicorn odin.server:create_app`), because the pidfile-only check was inert
    for the launch path the README itself documents.
  - **The archive is credential-grade**: `keys.json` and every canvas secret in
    cleartext. Written `0600`, every member stored `0600`, and a restore masks
    off group/other bits so it can only ever tighten a store's modes.
- [x] **Packaging (pragmatic scope).** DONE 2026-07-24 (v0.5.0):
  `install.sh` (one command: brew tools + colima up + odin + doctor)
  and `odin doctor` (toolchain checks with exact fixes, disk headroom,
  `--prebake` for the dynalite image). Full binary vendoring into one
  distributable.
- [x] **Pre-apply admission control.** `/apply-full` and `/tf/apply` both
  estimate the canvas's memory footprint and check free disk BEFORE spawning a
  single container or VM, and reject with a `409` whose message names the real
  numbers (`reconcile/admission.py`). A 3 x t3.medium canvas is refused in ~1s
  with nothing booted and no env directory created.
  - **Two DISJOINT pools, because the substrates are** (fixed v0.7.1, field
    test 2 finding MEDIUM-9 — everything used to be charged against Colima's
    VM, so a 48 GiB Mac was told "4.0 GiB budget (5.8 GiB total on this
    host)" and a 5 x t3.micro canvas was wrongly refused):
    - CONTAINER pool — `rds`/`ecs`/`lambda`/`elasticache`/`alb` plus the shared
      `s3`/`sqs`/`sns`/`dynamodb` backing containers, charged against the
      container runtime's own MemTotal (`docker info`, i.e. Colima's VM), and
      the rejection says exactly that rather than "this host".
    - HOST/VM pool — an `ec2` node is a REAL Lima VM allocated by
      Virtualization.framework from the Mac's RAM and consumes zero Colima
      memory, so it is charged against, and quoted against, real host memory
      (`os.sysconf` — stdlib, no new dependency, no subprocess).
  - **The budget** is 70% of each pool's TOTAL memory (not free memory).
    Overrides: `ODIN_MEMORY_BUDGET_MIB` (container pool, absolute MiB),
    `ODIN_VM_MEMORY_BUDGET_MIB` (VM pool), `ODIN_MIN_DISK_GIB` (free-disk
    floor, default 10 GiB — the same figure `odin doctor` checks).
  - **What it charges:** ec2 = the exact `INSTANCE_TYPES` memory;
    rds/ecs/lambda/elasticache/alb = a fixed per-node figure equal to that
    substrate's own real container memory cap; s3/sqs/sns/dynamodb = once per
    ENV, not per node (they share one backing container);
    vpc/subnet/sg/iam_role/ecr = zero.
  - **It also refuses an env name too long to make a Lima VM name** (v0.7.1,
    found while building the mesh proof). An `ec2` node becomes a real Lima VM
    called `odin-ec2-{env}-{instance-id}`, and `limactl` refuses any instance
    whose SSH control-socket path (`$LIMA_HOME/<vm>/ssh.sock.<16 digits>`)
    would reach `UNIX_PATH_MAX=104` bytes. Past a certain env length EVERY
    boot in that env failed — reliably, which is the good part — with a raw
    limactl error naming a socket path and a Lima constant, ~60s into a boot
    that was never going to work. Apply now refuses up front, naming the env,
    its length, the actual limit and both ways out (rename, or a shorter
    `LIMA_HOME`). **The limit is DERIVED, not hardcoded**
    (`compute/instances.py::max_env_name_len`), because it is machine-specific:
    it is 104 less `$LIMA_HOME` — whose default `~/.lima` moves with the
    username's length — less the separators, Lima's socket filename, odin's
    `odin-ec2-` prefix and the 19-character instance id. That works out to **22
    on a `/Users/fimbulwinter/.lima` home**, exactly the figure the mesh work
    measured by hand; a longer home name gives a shorter limit. A canvas with
    no `ec2` node is never blocked by it — nothing else mints a VM name.
  - **v1 limits, recorded rather than hidden:** an unknown total for either
    pool (Colima not running; `os.sysconf` unanswered) SKIPS that pool's check
    instead of printing a confident wrong number. It is a STATIC per-canvas
    estimate and does not look at memory actually in use, so two envs can each
    pass and jointly overcommit — cross-env accounting would mean summing every
    other env's applied Stack, which is not wired. `odin doctor` reports
    nothing about memory, so the ceiling is not discoverable before an Apply
    hits it, and it hardcodes its own disk floor rather than honouring
    `ODIN_MIN_DISK_GIB`.
- [ ] **M7 (multi-Mac) — the fleet. DEFERRED: do not build unless the owner
  explicitly asks for it** (owner call, 2026-07-28). It is the one item that
  cannot be honestly finished on this machine: a genuine cross-machine handshake
  needs a SECOND Mac, so anything built here could only be unit-tested and would
  ship claiming a capability nobody had verified. That is the exact shape the
  honesty rules exist to prevent, so it waits for a real two-machine setup
  rather than being simulated into looking done.

  The single-host half is DONE (see
  above: a real lighthouse + real per-VM daemons + a real ping/SG-filter
  proof, all on one Mac). What remains is genuinely cross-machine: a second
  Mac's host joining the SAME env's mesh (today's lighthouse only binds
  `0.0.0.0:4242` locally reachable via this Mac's own vzNAT bridge — a real
  external/LAN-reachable underlay address plus multi-Mac membership and
  cross-machine placement are still open). Additive, no core change.

## The intelligence layer — the canvas IS the language

**Owner's framing, in their words, and the reason this section exists:**

> *"canvas and navigating things around IS the language of odin and not chatting
> with a bot to update things around - that we'll add later too but this is a
> separate thing."*

> *"More intelligent placement like when I expand the ec2 box and put an ecs box
> inside it, that means I want ecs on ec2 - which is a valid thing instead of
> fargate right? and the configuration and stuff updates accordingly if needed
> but things like name and stuff remains as is."*

> *"if that kind of stuff can be done without intelligence then that's great too
> but I believe the intelligence layer would be needed anyways cuz of the intent
> detection and acting on user's events since canvas IS the language."*

So the ordering principle for everything below: **do it deterministically where
a gesture has exactly one honest meaning, and reach for the intelligence layer
where the job is INTENT DETECTION — reading what a person meant by an action on
the canvas and acting on it.** Containment implying a launch type is the
deterministic end (one gesture, one meaning, a lookup table). Inferring intent
from adjacency, grouping, or a sequence of edits is the other end, and that is
what the intelligence layer is actually for.

Neither end is a chat box. The chat/agent surface is listed last on purpose: it
is an ADDITION to the canvas language, never a replacement for it, and building
it first would answer a question nobody asked.

These four were LOST once already: a range-based edit to this file deleted them
while replacing a neighbouring entry, and they were only noticed when the owner
asked whether they had been forgotten. Recovered from 8a0c89e. Do not use
index-to-index deletion on this file.

- [x] **Containment changes configuration, not just labels — DONE.** Drawing an
  ecs box inside an ec2 box now places that workload's tasks INSIDE that
  instance, end to end:

      canvas gesture   ->  `host` stamped by lib/containment.ts (strict full-rect)
      spec             ->  carried into the ecs resource's fields
      terraform        ->  placement_constraints { type = "memberOf",
                             expression = "attribute:odin.instance == api-server" }
      gateway          ->  ecsctl reads AWS's own placementConstraints shape
      runtime          ->  TaskRuntime(LimaRuntime(vm="odin-ec2-prod-api-server"))

  **It is PLACEMENT, not a launch-type label.** The owner's example said "ecs on
  ec2 instead of fargate", and the honest finding is that odin already emits
  `launch_type = "EC2"` unconditionally and has NO Fargate substrate -- so
  flipping that label would have claimed a distinction odin cannot back. Where
  the task actually runs is the real difference, and an EC2 node is a real Lima
  VM, so it can be made true.

  Three things it needed:
  * `LimaRuntime.VM` was a class constant, pointing every caller at the shared
    `odin-host`. Now per-instance -- the change the design doc predicted would
    be the unlocking one.
  * an expanded EC2 box did not SURVIVE a reload (a leaf's stored height is
    dropped and re-derived from content), so the instance snapped back and the
    workload fell outside it. `EXPANDABLE_KINDS` keeps a height the user chose
    without giving every instance a default one.
  * the node SAYS what happened -- it reads `on api-server`. An inference the
    user cannot see is a trap, not a language.

  The owner's invariant holds and is tested: name, image, count and port all
  survive being placed, and dragging back out clears the placement rather than
  leaving a stale claim.

  Verified live: expanded instance at 248..448, workload at 318..400 inside it,
  node reads `ECS | web | DRAFT | tasks: 2 | on api-server`, `host` persisted.

- [x] **IAM edges across the whole catalog.** Turned out broader than this entry
  claimed -- s3, dynamodb, sqs, sns, rds, logs, secret, ssm and elasticache all
  had real vocabularies already. What was genuinely missing was **lambda, ecr and
  ecs**, now added: a workload can be granted `lambda:Invoke`, ECR login and
  repository reads, and ECS task control.

  **CORRECTED 2026-07-29, and the correction is the same bug this entry is
  about.** It said "the ECR image-pull actions", and said rds "had a real
  vocabulary already". Four offered actions could never bite locally:
  `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` and
  `ecr:BatchCheckLayerAvailability` have no handler in
  `gateway/models/ecr.py::_HANDLERS` and describe a DATA plane that `docker
  push`/`pull` reaches on the `registry:2` container's own published port,
  bypassing the gateway entirely; and `rds-db:connect` -- the FIRST action the
  rds tile offered -- names IAM database authentication, which odin does not
  implement, in a service prefix `classify.py` can never emit.

  The fix is the DEFAULT/TICKABLE split, and getting there took two agents
  disagreeing. `edge-registry` drew it for ecr and `connection-edge` had deleted
  the actions outright; the split is better, and the reason is portability: a
  drawn permission becomes a real `aws_iam_role_policy`, and taken to Amazon
  these are exactly the verbs an image pull and IAM DB auth need. So they stay
  TICKABLE and stop being PRE-TICKED, because a default is what odin ticks FOR
  you and must not assert a protection odin has not got. `rds`'s default is now
  `rds:DescribeDBInstances`; `ecr`'s is `GetAuthorizationToken`.

  Why the ratchet below did not catch them, which is worth more than the fix:
  ECR is a TARGET-derived service, so the test can only check the SERVICE
  prefix -- correctly, since any op a real SDK sends is emittable -- and
  "is there a handler" is a second question it never asked;
  `tests/gateway/test_ecr_vocabulary_has_handlers.py` asks it now, both ways.
  `rds-db:connect` was on an explicit `NOT_API_ACTIONS` whitelist, excused as
  naming a data-plane connection "which the mesh firewall gates". The first half
  was true and **the second was a guess**: that firewall is compiled from
  SECURITY GROUPS and has never read an IAM statement. The entry stays -- its
  portability reason is real -- but with the reason it actually has, in a file
  whose whole purpose is making decorative permissions impossible.

  The find that mattered more than the vocabulary: **AWS's spelling is not
  necessarily odin's.** The gateway classifies an invoke as `lambda:Invoke`
  (`classify.py::_LAMBDA_ROUTES`), not AWS's `lambda:InvokeFunction`. Measured
  against the real evaluator:

      evaluate([lambda:InvokeFunction], action=lambda:Invoke) -> False
      evaluate([lambda:Invoke],         action=lambda:Invoke) -> True

  So offering the AWS name would have produced an edge that draws, applies,
  reports success and grants NOTHING -- a decorative permission, across a
  TS/Python boundary where nothing type-checks either side.

  `tests/gateway/test_iam_vocabulary_is_enforceable.py` now pins every action the
  UI can put on an edge against what the classifier can actually emit (66 cases
  as of 2026-07-29, measured; it was 60 before EventBridge was added forwards and
  the four decorative actions were removed).
  Mutation-tested both ways: planting `lambda:InvokeFunction` fails it, and so
  does inventing a service the gateway never dispatches on. Two false positives
  in the checker itself were fixed first -- it read only one of the two dispatch
  spellings (reporting sqs as unclassified) and matched `nginx:alpine` out of a
  node's defaultData as though it were a grant.

- [x] **Every box persists its size, not just containers** (owner question,
  2026-07-28: *"it's not just ec2's box right? All the boxes and their exact
  positions and Heights and weights should be persisted if it exists right?"*).
  Correct, and it was not the case. Positions and widths always persisted;
  HEIGHTS were dropped on load for every kind except containers, so resizing an
  S3 or Lambda box silently snapped back.

  The rule outlived its reason. Heights were dropped because an older build
  baked `measured.height` into the saved canvas and froze every box at its
  first-render size; `sizeForSave` stopped writing measurements, which fixed
  that at the source. After that, dropping heights on load could only discard
  DELIBERATE resizes -- and it had just been caught doing exactly that to an
  expanded EC2 box, taking a workload's placement with it.

  Now every kind keeps a stored size. The frozen-boxes guarantee is enforced
  entirely on the save side and tested there: no matter how often a node is
  measured and re-saved, a height nobody chose is never written. Verified live
  across three kinds at once (260x140, 300x160, 240x120 all preserved through a
  reload). One caveat stated rather than guarded: a canvas written before the
  save-side fix may carry a baked height, which now renders instead of being
  re-derived -- visible, and one drag from fixed.

- [x] **Placement's four costs: all four closed.** `docs/intelligence-layer.md`
  named them when placement was designed, and shipping it did not address them.

  * **Ordering — CLOSED.** Nothing sequenced an ecs node behind its ec2 node,
    and a container cannot launch into a VM that is not up. A placed service now
    emits `depends_on = [aws_instance.<host>]`, so tofu owns the ordering and it
    is visible in `plan` rather than being an invisible sleep in the gateway.
    Combines correctly with ref-derived dependencies.
  * **Failure meaning — CLOSED.** "the VM is not up" and "the task failed" read
    identically before: both surfaced as a raw `limactl shell ... failed`. They
    need OPPOSITE responses from a person -- bring the instance back, versus fix
    the workload -- so a placed task's failure now names the instance and says
    the workload can only run once it is up, while keeping the underlying cause.
    An unplaced task's message is byte-for-byte unchanged.
  * **Capacity — CLOSED.** An EC2 node is a real Lima VM sized by its instance
    type (t3.micro -> 1GiB) and a placed task gets a real memory cap (512 MiB by
    default), so "three services of two tasks each inside a t3.micro" is
    arithmetic odin cannot honour. `spec/capacity.py` sums the demand per
    instance and /apply-full refuses with a 409 BEFORE anything is built --
    beside the wiring guard, naming the instance, what was asked, what it has,
    and what to do about it. The alternative was never "it works": it was
    OOM-killed containers minutes later, reported as task failures that say
    nothing about the instance being too small.
    Deliberately NOT a scheduler -- one sum per instance, checked once. It
    reserves 256 MiB for guest kernel/containerd overhead and says so in the
    message rather than hiding the fudge inside the comparison. A canvas that
    places nothing pays nothing.
  * **Naming — CLOSED as documentation, which is what it was.** `LimaRuntime`
    means two things depending on its `vm`: odin's shared VM-isolation runtime
    mode (`odin-host`) and one specific EC2 instance
    (`odin-ec2-<env>-<label>`). Its class docstring now states both, side by
    side, with the two constructor calls that produce them -- and
    `DEFAULT_VM` is named for the shared one so the default cannot be mistaken
    for "no VM". A type-level split would buy nothing today: the behaviour is
    identical and only the target differs. Revisit if a third meaning appears.

- [ ] **Placement that infers intent from geometry.** The general form of the
  first item: what a person expresses by putting one thing inside, next to, or
  overlapping another. Containment is the first and clearest case; adjacency
  and grouping follow. Each inference must be reversible by the opposite
  gesture and must never destroy authored values.

  **INSIDE is done and legible (v0.8.2).** One rule covers every case — the
  inner node's full rect within the outer's, compared `<=`, deepest container
  wins — and it compiles: a subnet's `vpc_id`, an SG's `vpc_id`, an EC2's
  `subnet_id`, and an ECS service's real `placement_constraints { memberOf }`.
  Reversibility is not an aspiration: `withContainment` drops the three derived
  keys and re-adds only what geometry still supports, so dragging back out
  clears the claim, and every other field passes through untouched
  (`containment.test.ts` pins both directions). v0.8.2 closed the last gap in
  it — `host` was the one stamp odin ACTED on and never showed, so a box
  dragged a few pixels short of "fully inside" looked identical to one that
  landed. It is now a read-only field on the ECS tile, and a test asserts no
  containment stamp anywhere is editable (typing one would be a second source
  of truth the next drag silently discards).

  **NEXT TO and OVERLAPPING are deliberately NOT built**, on the grounds that
  kept the edge-type selector unbuilt until 2026-07-29: there is nothing yet for
  them to mean. (The selector's grounds expired when a pair acquired a second
  honest meaning and the ambiguity ratchet fired. Adjacency has acquired none.)
  Overlapping is already answered — partial containment is OUTSIDE, decided by
  the owner on 2026-07-28 — and adjacency has no unambiguous reading in odin's
  model today, because every peer relationship it might stand for (IAM grant,
  network reach, SG membership) is already carried by an EDGE, which says which
  one it is. Inventing a meaning for "near" would also break this item's own
  rule in the worst way: it would make MOVING a node change infrastructure,
  with no gesture that reads as the opposite of "near".

  The trigger for building it: a gesture users actually perform whose intent is
  unambiguous and which no edge already expresses. That is a real observation
  about how people draw, not something to reason out in advance.

- [x] **The chat/agent surface — BUILT (v0.8.5), direct-editing since v0.8.6.**
  `odin chat "..."` changes the canvas; `--dry-run` previews.

  **Owner correction, 2026-07-28:** v0.8.5 returned a proposal to confirm, and
  the owner's call was that the CANVAS is the review surface — the edit should
  appear where you are already looking, and undo is what takes it back. That
  turned out to need no new mechanism at all: `Canvas.tsx` records history from
  a `[nodes, edges]` effect, not from the drag handlers, so a WebSocket-driven
  `setNodes` already lands on the undo stack exactly like a drag. VERIFIED in a
  real browser — the edge appears in an open tab (0 -> 1) and Cmd-Z reverses it
  (1 -> 0). I had presented this as a design tradeoff; it was not one.

  It still never APPLIES: editing the drawing is reversible, building from it is
  not. The save goes through the same `POST /canvas` the UI uses, so there is no
  privileged path for an agent-authored canvas. The conversation is remembered
  per env in server memory, bounded at 12 turns, cleared by a restart,
  `--clear`, or *Clear Agent Session* in the `···` menu — never touching the
  canvas, because reverting work you may have built on since is a far worse
  surprise than a stale conversation.

  **It proposes OPERATIONS, not a rewritten canvas.** Canvas-in-canvas-out is
  unauditable — a dropped node, a rewritten password and a rename all arrive as
  the same opaque blob, and diffing cannot recover intent. An op list can be
  validated one at a time against the real catalog, shown as a sentence per
  change, and makes an unrequested edit impossible by construction: there is no
  op that says "and also touch this". The owner's *"things like name and stuff
  remains as is"* is enforced literally — `set_field` may not write `label`, and
  renaming is its own op whose description says it DESTROYS and recreates.

  **Three defects found only by running the real thing**, each invisible to the
  unit tests that were already green:
  - the SDK returned `ops` as a JSON **string**, not a list. The reader iterated
    it character by character, found no dicts, and reported "the agent proposed
    nothing" — while the agent had proposed exactly the right edge. Honesty rule
    1 in a new costume: a reader wired to a shape the signal does not arrive in.
  - an empty tool call printed a blank line and exited 0, which is
    indistinguishable from success; and a `reply` claiming "Added a read-access
    edge" could stand alone with zero ops behind it. odin now always states
    whether anything would change, so prose cannot imply an action it did not
    take.
  - "odin does not model that operation" was reported for an op odin models
    perfectly whose ARGUMENTS were wrong (`node` where the schema says `label`),
    hiding the only actionable detail. The two failures are now distinct, and the
    one observed field alias is accepted.

## v0.8.15 — the canvas stops lying about what a line means, and triggers fire

**Event delivery, end to end.** Three sources, each proven against real
containers rather than fakes: a scheduled EventBridge rule invoked a real RIE
container **60.5s after `PutTargets`**, exactly once per period (a per-tick bug
would have shown ~60); `sqs → lambda` through real goaws, message in, `Records`
envelope out, message really gone after — plus the redrive half, where removing
the function's container leaves the message undeleted and goaws redelivers it;
and S3 notifications synthesized from traffic the gateway already sees, with
`tofu apply` succeeding and the follow-up `plan -detailed-exitcode` clean.

S3 had to be synthesized rather than forwarded, and that was a measurement not a
preference: RustFS **rejects every notification ARN form with `InvalidArgument`
and stores the configuration anyway**, so an apply failed, the next plan read the
config back through GET and reported no drift, and nothing ever fired. Three
answers, all different, none right. odin answers the call itself now.

Refused rather than half-shipped, because each would have applied clean, planned
clean, and never fired: `EventPattern` rules, `cron(...)` schedules, non-Lambda
EventBridge targets, non-SQS event sources. `PutEvents` still returns a named
error instead of `{"FailedEntryCount": 0}`.

**Six edge types instead of "everything is IAM".** The `iam_role → workload`
edge was completely inert — typed `network`, read by nobody — so the canvas
showed one role while the gateway enforced another, silently and permanently.
`logs → workload` shipped to a hardcoded `/aws/lambda/{fn}` while the group you
drew sat empty forever. `ecr → ecs` granted a pull and let a text box decide the
image. Each now authors the thing its permission was for.

**The connection edge, and the selector it made real.** `rds → ecs` writes
`DATABASE_URL` from the gesture. It also created odin's first genuinely ambiguous
pairs — eight of them — because in AWS a workload wired to an endpoint may also
need a grant on it. The picker is multi-select, not exclusive.

**Four from the limits queue:** `odin env rm`, non-destructive RDS repair on a
named volume, multi-file Lambda packaging, and SG egress with ARN-portable
policies that stay enforceable locally.

**What I would defend hardest: six new ratchets.** Guards that fire, replacing
prose that ages.
- The ambiguity ratchet the ROADMAP had claimed for a month and which **did not
  exist** — `iam.test.ts` iterated no pairs, and the only trace was a comment
  asserting it stayed green. Written, mutation-tested, and it fired for real the
  same day the connection edge landed, naming all eight pairs.
- A cross-language ratchet pinning the UI's edge registry against the Python
  builders, which caught `albTargetTypes` being one merge behind and printed
  which side and which line.
- A doc-count ratchet, after a *measured* number in a file no build reads rotted
  within a day of being written.
- The ECR vocabulary pinned both directions, after a comment claimed a test
  proved an action enforceable and that test's own docstring says it cannot.
- A cadence ratchet failing the build if any file assigns `ODIN_DISPATCH_TICKS`.
- **`bun test` in CI**, which had never run: 103 UI tests were gating nothing.

**Bugs found by verifying rather than reviewing.** `_instance_address` read
`private_ip_address`/`public_ip_address` — keys `ec2compute` has never written —
so it returned `None` for every real instance, and its one test fabricated a
record with the key the reader wanted. The dispatcher counted a **mid-redeploy**
function as a failed delivery attempt, so a redeploy slower than five ticks
dropped every notification enqueued during it *and* reported `GIVING UP` — a lost
event with a false verdict on top. `synth_error` had no `s3` branch while
`_respond` always had, so an S3 error came back as JSON botocore parses as XML.
An IPv6 CIDR's colons made `split(":")` produce five fields and silently
discarded an entire security group.

**The gate, stated as measured.** Two consecutive full integration runs, each
**82 passed / 1 failed**, a DIFFERENT single test each time, and each failure
passes in isolation: `test_rds_tf_e2e` (waiting for a drift sweep to mark a
killed database `failed`) then `test_debug_e2e` (waiting for a crash verdict).
Both are cadence-dependent assertions, and a 59-minute suite booting real VMs on
a machine also running agents is exactly the condition under which a window is
missed. This is the class already recorded as "a tight `elapsed <` assert tests
CI load, not the code" — so it is not a regression, and it is also not fixed:
those assertions should wait on the signal rather than on a window. Named here
rather than left as folklore about a flaky suite.

**One process lesson, because it cost a gate run.** During the first run I
removed containers for env `conn2`, having verified it was dead — the `:4200`
server did not list it and no `.odin/conn2` existed on disk. Both checks were
true and neither could see it: `tests/spec/test_connection_edges.py` applies that
env through its own TestClient with its own store root. So the cleanup was
correctly scoped to a prefix and still wrong, which is the subtle form of the
machine-wide-sweep rule already in CLAUDE.md. A test-owned env is invisible to
every check a human would think to run; the only safe rule is to touch nothing
while a suite is running.

## v0.8.18 — the egress gate is measured on a VM, and a cancelled subprocess stops leaking

**A drawn egress rule is now proven to block on a real EC2 Lima VM.** v0.8.17
compiled `IpPermissionsEgress` into nebula's `outbound` and measured a real
block — on CONTAINERS, and said so: *"No real Lima VM ever wore a restricted
egress … that is an inference, not a measurement."* A VM is a mesh member by a
different route (config landed with `limactl shell … sudo tee`, daemon under
systemd as root, its own tun) so "same bytes" was never "same effect". The
measurement, the falsification and the two things that fell out of it are in
`docs/limits.md`; the short version is that the block is real, and reverting the
compiler turns the one blocked cell — and no other — green.

**SQS long polling did NOT work in v0.8.16 — the backing holds a poll 1.5x
longer than the wait, and the fix's own integration test proved it the first
time it was run.** v0.8.16 derived the forward's read timeout as `client read +
this request's wait` and called it fixed on the strength of a real-socket unit
proof. The socket slept exactly the wait it was handed; real goaws does not.
MEASURED against the container with a 300s client-side timeout: `WaitTimeSeconds=
20` is held for **29.83–30.94s** (1.47–1.57x, and the queue-attribute door
overshoots the same), so 25s of patience produced the very `ServiceUnavailable`
the change existed to remove. `loops := waitTimeSeconds * 10` with a 100ms timer
per loop had been read correctly off the pinned v0.5.4 source and then reasoned
about as if a 100ms Go timer in a container costs 100ms. The timeout now scales
by a measured overshoot factor (`5 + wait × 2.5`; 55s for the longest legal
poll, inside botocore's 60s default), and — the part that stops the recurrence —
the socket stand-in overshoots like the real backing, so reverting the factor
fails the UNIT suite instead of waiting for a container. Measured after:
empty-queue 20s poll → empty answer in 28.06s, non-empty → 0.00s.

**A "regression" in the SG mesh test was a test with no time in it, and the
egress compiler was innocent.** `test_a_drawn_sg_gates_real_postgres_traffic_
over_the_overlay` began failing its host-path assertion right after egress
compilation landed, which reads like the change breaking connectivity to a
database. It is not: the wait was `for _ in range(60)` with no sleep, and all 60
attempts MEASURED 0.182s against a Postgres that first answered at 0.735s —
`create_db` returns when `docker run` does, PGDATA still needs initdb, and
docker-proxy accepts then closes while nothing listens. It had been green only
because `join_mesh` burned ~1.6s of unrelated wall clock first. Three
independent falsifications: it reproduces with the mesh skipped entirely (0.136s
of attempts vs a 2.218s first answer), the changed functions
(`sg_rules_to_firewall`/`union_firewalls`) are not on the test's code path at
all, and it fails identically at the pre-egress v0.8.16 commit. Both waits are
deadlines in seconds now. Production never had the bug — `rdsctl`'s create
waiter is already `while time.monotonic() < deadline`.

**A cancelled subprocess no longer leaks a process OR its transport, and the fix
that removed the leak nearly shipped a 30-second hang.** `run_command_async` and
three siblings unwound out of `communicate()` on cancellation having never
collected the child, leaving a live `docker`/`tofu` and an asyncio transport that
raised `RuntimeError: Event loop is closed` out of the garbage collector.
`util.reap` kills and collects in a `finally`. The trap, found by challenging the
first version rather than by a test: **`Process.wait()` does not return when the
process dies** — CPython resolves its waiters only once every pipe hits EOF, so a
SIGKILLed child that left a GRANDCHILD holding the inherited stdout pipe blocks
forever. Measured at 29.7s on the cancellation path, with the killed process
provably gone. So the wait is bounded (`REAP_TIMEOUT`, 0.5s — there is no middle
case for a larger value to cover) and `_transport.close()` makes the timeout path
lossless. Mutation-tested both ways in `tests/test_reap_bounded.py`.

*Honest gap:* the symptom originally reported — `PytestUnraisableExceptionWarning`
out of `tests/simulate/test_lambda_failure_e2e.py` — **did not reproduce here**,
with the fix or with `reap` neutralised (injection verified active by that same
run failing the reap ratchets). The defect and the fix are real and were
reproduced in pure asyncio; that particular test only produces the warning when
something actually cancels an in-flight command, which an isolated fast run never
does. Recorded rather than claimed.

## v0.8.16 — the gate stops lying, and two leaks that made it lie

**SQS long polling works.** *(CORRECTED in v0.8.18 — it did not. The derived
timeout accommodated the NOMINAL wait, and real goaws holds a poll ~1.5x longer
than that, so the 503 survived for any wait it mattered for. See the v0.8.18
entry; the "real socket" this section credits is exactly what missed it.)*
`WaitTimeSeconds >= 5` used to exceed the forward
client's 5s read timeout, so an empty queue answered `ServiceUnavailable` — for
the recommended way to consume a queue, and indistinguishable from the backing
being down while `/world` said healthy. Measured cost was 10s, not 5, because
botocore retries a 503. The timeout is DERIVED (`client read + this request's
wait`), not raised: a blanket 30s would make every other forwarded call wait 30s
before failing. A wait above AWS's 20s maximum is refused with
`InvalidParameterValue` rather than clamped, because answering sooner than asked
is indistinguishable from an empty queue. The trap worth recording:
`httpx.ASGITransport` enforces NO timeouts, so the natural test would have
passed against the broken code — it needed a real socket.

**Orphaned volumes are reclaimable.** Named volumes survive `docker rm -f -v` by
design — that is what makes RDS repair non-destructive — and nothing reclaimed
them. Scoped by a new `odin.env` LABEL, never by name: `odin-rds-conn2-app-db-data`
is ambiguous between env `conn2`/db `app-db` and env `conn2-app`/db `db`, so a
name filter over-reaches by construction. Deliberately NO reconciler-tick sweep:
a reconciler is per-env while volumes are per-machine, so "no env claims this" is
unanswerable from inside one, and every agent worktree is a second odin whose
databases such a sweep would delete.

**Two flakes fixed by changing the signal, not the timeout.** `test_rds_tf_e2e`
paired a CADENCE-FREE fact (`/world` reading `crashed`) with a CADENCED one (the
record, written by the background sweeper) and gave the second zero retries. It
now asserts on the recovery apply's own `recovered_resources` — synchronous, in
the response body — and `ODIN_DRIFT_SWEEP_TICKS=1` is GONE, so it runs at the
production cadence. Falsified rather than assumed: with the sweep pinned so it can
never run, the test still passes. `test_debug_e2e` was not cadence at all — the
model called a node "the working control… not implicated" in prose and listed it
in `suspects` anyway. The CONTRACT was the defect (`_SYSTEM`'s only rule was
provenance, which permits naming every evidence node); it is implicated-only in
all three channels the model reads now. The test hard-asserts recall and PRINTS
precision, because which nodes a model names is not schema-guaranteed.

**A "unit" test that booted four real containers and kept them.** Not
integration-marked, built with `FakeRuntime`/`FakeRds`/`FakeAws`, and the fakes
are bypassed: `/apply-full` runs real tofu and the gateway's models default to
`ColimaRuntime`. The asymmetry makes a leak certain rather than likely —
**creation is real, teardown is fake** — so `/destroy` destroyed a fake while the
real Postgres stood. Fixed twice: the first fix reclaimed containers and leaked a
data directory per node instead.

**The gate, stated honestly.** 84/84, rc=0, but as THREE serial invocations
(27+28+29) whose partition was verified by `diff` before running — a single
84-test invocation cannot complete in this harness, which kills at ~61 minutes
against a ~65-minute suite. Measured twice. Three green runs are not one green
run and are not described as such.

## Next — known, measured, not yet fixed

- [ ] **A fake runtime does not isolate `/apply-full`, so a "unit" test boots real
  containers.** Measured 2026-07-29, after it leaked four containers into every
  unit-suite run and cost a release-gate diagnosis.
  `create_app(runtime=FakeRuntime(), rds=FakeRds(), aws=FakeAws(), backings=False)`
  reads as hermetic and is not: `/apply-full` runs a real `tofu apply`, and the
  gateway's own models default their substrate (`lambdactl` to
  `FunctionRuntime(ColimaRuntime(), ...)`, rdsctl and cachectl likewise), so the
  injected fakes are bypassed and real Postgres, Redis and RIE containers start.
  `tests/spec/test_connection_edges.py` took 68s in the "unit" suite for this
  reason.

  The asymmetry is the actual defect, and it guarantees a leak rather than merely
  risking one: **creation is real and teardown is fake.** The gateway created a
  real Postgres through `ColimaRuntime`; `/destroy` runs through the reconciler,
  which DOES honour `FakeRds`, so it destroyed a fake and left the container
  standing. Measured — `/destroy` cleared the lambda and cache containers and left
  `odin-rds-conn2-app-db` and `odin-rds-conn2-other-db` running.

  Patched at the call site (`_torn_down` destroys, then reaps that env's own
  containers by name and asserts the end state). The real fix is in the seam: a
  test that injects a fake runtime should get a gateway whose models use it, or
  `create_app` should refuse the combination rather than silently half-honour it.
  Until then, every `/apply-full` test needs Docker and nobody is told.


- [x] **Event delivery: triggers that actually fire.** odin could record "when X
  happens, run Y" and nothing read the record. `reconcile/dispatch.py` is the
  other half — one dispatcher, three sources (a scheduled EventBridge rule, an
  SQS event source mapping, an S3 bucket notification), one sink (a Lambda
  invoke through `lambdactl.invoke`, so the durable `last_invocation_error`
  verdict `/world` projects can never be bypassed).

  **Cadence 1 tick, not the drift sweep's 10**, because a late sweep is a late
  report and a late dispatcher is a broken trigger. Measured at the production
  wiring, 20 runs: 0.02–0.94s from due to invoked. A repo-wide ratchet fails the
  build if any file assigns `ODIN_DISPATCH_TICKS`, so no test can fabricate the
  promptness the way the two drift e2e tests did (rule 1b).

  **What it refuses, loudly, rather than storing:** an `EventPattern` rule (no
  bus exists, so nothing could match it), a `cron(...)` schedule (no evaluator —
  firing at the wrong time silently is worse than refusing), a non-Lambda
  EventBridge target, and a non-SQS event source. Each of those previously
  applied clean, planned clean, and would never have fired once. `PutEvents`
  stays refused for a narrower reason that survived the dispatcher: its entries
  route by pattern, and there is no matcher.

  **What the integration tests found that the unit tests could not**, both fixed:
  a verdict for a label the World projection does not carry was **silently
  dropped** by the reconciler (a rule may target a function that does not exist;
  the unit test asserted on the dispatcher's return value and passed the whole
  time), and resolving the SQS backing port eagerly every tick put a `docker`
  shell-out storm on the loop — enough, measured, to destabilise the goaws
  backing until the lookup was made lazy. 15/15 mutations caught.

- [x] **The full integration suite is part of shipping now.** v0.8.12 went out
  on 2822 green unit tests and SIX of 71 integration tests — the six that
  touched the code being changed. Running all 71 (53 min) found five failures
  the unit suite could not see, in the one file that exercises the gateway
  against real containers:

  1. **A regression in the release itself.** `tests/gateway/test_gateway_e2e.py`
     grants to a PHANTOM node through `/apply` (no tofu), which is the contract
     v0.8.12 deliberately replaced — an edge grants nothing until an apply. The
     tests encoded the old contract, so every call denied.
  2. **A claim that became false.** `_not_in_terraform` said "the policy is
     emitted" for every IAM edge, including edges drawn FROM a kind that can
     hold no role, where nothing is emitted. Harmless while the gateway
     enforced from edges; a silent lie once it enforced from the file. It now
     distinguishes the two and names the reason.
  3. **A pre-existing silent leak**, unrelated to the release: the teardown
     fixture called `rt.stop(name)` without awaiting. `stop` became a coroutine
     in the v0.7.7 de-threading pass, so that fixture had cleaned up nothing
     since — failure mode #1 from CLAUDE.md, inside the fixture that exists to
     prevent it. Two backing containers were standing after the run.

  The rule this earns: **a release runs the whole integration suite, not the
  part that looks related.** Both #1 and #3 were invisible to 2822 unit tests
  and to the six integration tests chosen by relevance.

- [x] **The gateway authorizes from the APPLIED IAM, not from the canvas.**
  DONE v0.8.12, owner ask 2026-07-28: *"I want the permission to take effect
  only after an apply. Decorative shit shouldn't be there."*

  Before this, `evaluate()` read a policy map compiled straight from the canvas
  edges. Two things were wrong with that. A permission took effect the moment it
  was drawn and committed, with no apply — so the Terraform odin generated
  described an IAM posture the gateway was not using, which is the same shape as
  every other bug in the honesty section: a claim nothing verified. And the
  chat agent's own grant op wrote `data["actions"]` where the translator reads
  `data["permissions"]`, so an agent-drawn grant rendered on the canvas and
  granted nothing at all — decorative in the literal sense.

  Now: `agent/hcl.py` emits the policy, tofu applies it through the gateway,
  `iamctl` stores it, and `policy.compile_policies_from_iam` reads it back —
  each workload's role found in its OWN service record (a lambda's `role`, a
  task definition's `task_role_arn`, an instance's `iam_instance_profile`
  resolved through the profile that owns it) rather than by a naming
  convention. Each granted workload also `depends_on` its policy, because tofu
  is free to order two resources that merely share a role either way, and a
  container that starts first would be denied a permission that WAS applied —
  a race indistinguishable from a wrong grant.

- [ ] **Cleanup, redundancy removal and code hygiene — AFTER the owner's manual
  pass.** Owner ask, 2026-07-28: schedule proper maintenance work, but only once
  every feature has been tried by hand. The gate is deliberate: a hygiene sweep
  that lands before the features are exercised removes things nobody has yet
  discovered they need, and it makes any bug the manual pass finds ambiguous —
  pre-existing, or introduced by the sweep? So this stays blocked until the
  owner says the manual pass is done.

  What it covers when unblocked:
  - **Dead and duplicated code.** The parked app-layer left seams behind, the
    de-threading run rewrote ~28 call sites, and several passes in `hcl.py` grew
    by accretion. Find what nothing calls and what two modules both do.
  - **The docs-vs-source audit as a routine, not an incident.** Honesty rule 3
    exists because caveats outlive their fixes; this release closed four stale
    claims about edge-compiled enforcement that were true when written. Worth a
    check that can fail a build rather than a habit.
  - **Test-suite hygiene.** 2808 tests, some overlapping; and a ratchet is only
    worth its runtime if breaking the thing it guards still fails it. Re-run the
    mutation checks the honesty rules ask for and delete the ones that no longer
    bite.
  - **`try`/`except` and branch density**, per the owner's standing rule — the
    reconciler and the gateway builders are where multiple paths accumulated.
  - **Dependency and disk review.** Trim what is no longer imported; confirm
    every licence is still permissive.

- [ ] **Close the Known limits, one at a time.** `docs/limits.md` is the list, and
  each entry there is a promise odin does not keep yet. Owner ask, 2026-07-28:
  treat it as a work queue, not a disclaimer. In rough order of what a user hits
  first:

  1. ~~**A drawn IAM edge is not in the generated Terraform.**~~ CLOSED in
     v0.8.11/v0.8.12. A drawn permission is emitted as a real
     `aws_iam_role_policy` on the workload's role (an ec2/ecs node that is
     granted something gets the same auto-role a lambda always had, reached
     through an `aws_iam_instance_profile` for ec2), it round-trips through
     import without loss, and the gateway now authorizes from THAT — see the
     enforcement-source entry below. **Portability closed in v0.8.14**: the
     `Resource` is a real ARN, and `gateway/policy.py::arn_label` reduces it back
     to the node label the classifier reports, so the same policy is enforced
     locally and valid on Amazon. Emitting ARNs without that reducer would have
     silently denied every permission in the product, which is why the two
     tables are pinned against each other by `tests/agent/test_hcl_iam_arns.py`.
  2. ~~**An RDS container keeps no volume**, so odin's own repair returns an
     empty database.~~ CLOSED in v0.8.14. Each instance now has a named volume
     (`aws/rds.py::volume_name` → `odin-rds-<env>-<node>-data`) mounted at
     `PGDATA`, created before its container and removed with `delete_db`, so the
     repair is non-destructive. Measured through the product's own path in
     `tests/simulate/test_rds_tf_e2e.py`: rows `[42, 43]` written over the
     published `DATABASE_URL`, `docker kill`, one Apply → the same `[42, 43]`
     read back; and `docker volume ls` is empty for the env after teardown.

     Two things worth carrying forward. First, the fix was only half a fix
     without the lifecycle: `docker rm -f -v` deliberately does NOT remove a
     named volume (probed, and that is exactly what makes the repair work), so
     every teardown path and every test fixture had to learn to remove it or a
     Postgres volume would leak on each run. Second, `_RECOVERY_COST` did become
     a footnote — but a *measured* one. It is keyed `(kind, data_kept)` and
     `_recovering_resources` reads the real `docker volume ls` in the one
     instant between the sweep that marks the death and the converge that
     repairs it, so an instance whose volume was also destroyed still gets the
     old warning. A static "your data is safe" would have been a guard reading
     no signal — honesty rule 1, in the direction that reassures rather than
     alarms. Mutation-tested both ways.

     **And the first of those two came back in v0.8.15, which is the part worth
     carrying forward hardest.** "Every teardown path had to learn to remove it"
     was true of `delete_db` and false of `odin env rm`: four orphaned volumes
     from two environments that no longer existed in any form — no containers, no
     `.odin/<env>/`, absent from `GET /envs` — were found on the development
     machine by hand, with `docker volume ls` the only thing that could see them.
     A fix whose whole point is that state OUTLIVES its container leaves a
     reclaim question behind by construction, and that question was answered for
     one caller instead of as a lifecycle. The closure (`aws/rds.py::
     reclaim_env_volumes`, `GET /volumes`, `odin volumes`) also had to add a
     `odin.env` LABEL, because the volume's own name cannot be used to scope a
     deletion: `odin-rds-conn2-app-db-data` is env `conn2` database `app-db` and
     env `conn2-app` database `db`, so a name filter over-reaches by
     construction. Deliberately NOT on the reconciler tick — a reconciler is
     per-env and docker volumes are per-machine, so "no env claims this" is a
     question no per-env loop can answer without deleting another odin's
     databases. See `docs/limits.md` for the two residuals.
  3. ~~**An ECS service's canvas wiring cannot be imported.**~~ The GENERATE
     half is CLOSED in v0.8.14: a ref travels as an `odin:ref:<VAR>` tag whose
     value is `<producer>.<attr>` — the non-secret representation this entry
     asked for. A reference names a producer and an attribute; only the string
     it RESOLVES to at launch carries the password, and that is built by
     `gateway/wiring.py` long after the file is written, so it cannot appear.
     Static env entries are still never emitted, for the same reason (a user may
     have typed a credential into one).

     The READ half is CLOSED too, in the same release: the importer puts those
     tags back into a node's `env`, for **ecs and lambda** both. The ordering did
     come back for free, and the reason is worth keeping — `depends_on` is
     `sorted(set(...))` over the ref TARGETS, so it is a function of *which* refs
     exist and not of their order, which means recovering the refs recovers it
     with no ordering logic in the importer at all. Verified by generating,
     importing and generating again and diffing the `depends_on` lines, and then
     through the real `odin import-tf` against a running server. The literal
     `${{...}}` text was never an option and that was MEASURED rather than
     assumed: OpenTofu rejects it as a parse error, which fails the whole
     project, and the escaped form uses characters outside AWS's tag-value set.
     A project odin did not generate has no such tags and its wiring still cannot
     be rebuilt; the import says so, and says it only in that case.
  4. ~~**sqs/sns tags are dropped on import.**~~ CLOSED in v0.8.14 — and it was
     **four** kinds, not two: `dynamodb` and `iam_role` had the identical defect
     and were not in the report, which is why the fix went in as a shape rather
     than as the two instances named. It was also two bugs wearing one coat,
     since `hcl.py` stamps an `odin:node` tag on every primary: those four kinds
     *also* printed `imported without unmodeled attribute(s): tags` on every
     import of odin's own output, about a tag odin itself had just written.
     `tags` is unioned in centrally now instead of listed per kind, the second
     list that could disagree with the first (`_TAGGED_KINDS`) is deleted, the
     whole `odin:` tag namespace is reserved rather than the one key, and a test
     parametrized over `_KIND` covers a kind added later.
  5. ~~**Envs are never removable.**~~ CLOSED. `odin env rm <name>` (POST
     `/envs/rm`) is the decommission verb `odin destroy` deliberately is not: the
     same teardown, then the env's `.odin/<name>/` directory, its issued gateway
     credentials, its cached synth records and routing table, its reconciler —
     stopped, and *verified* stopped off the loop task's own `done()`, because
     `_task = None` proves only that nothing points at it — and its entry in
     `odin envs`. The status is derived from an outcome map (`_REMOVE_STATUS`)
     from the start rather than after four rounds of it, so a branch that
     reports no outcome fails loudly; every failure leaves the directory intact,
     so a retry is clean. Measured against a real server: a real
     `odin-aws-rustfs-envrm-c1` container and a `healthy` world, then removed —
     container gone, directory gone, delisted, the loop off `/health`, and
     nothing re-created over the following 8s while a second env kept ticking.
     Residual in `docs/limits.md`: the container check matches on naming, so an
     env whose name is a `-`-suffix of another's refuses (measured, and it
     refuses rather than over-deletes).
  6. **Lambda has one version and no S3-deployed packages.** Multi-file/
     directory packaging landed in v0.8.14 (`sourceDir`), so this is now only
     `$LATEST`-with-no-aliases plus the missing `s3_bucket`/`s3_key` deploy
     path. The S3 half is the cheapest of what is left — odin has a real S3
     substitute to test it against — and it wants an `aws_s3_object` upload in
     the generated project plus `S3Bucket`/`S3Key` in the gateway's
     CreateFunction, which today refuses them by name.
  7. **Nebula is single-host** — this is M7, and it stays deferred until the
     owner asks, because it cannot be honestly finished on one machine.


- [x] **The import direction is complete (v0.8.4).** NORTHSTAR says the
  translation runs BOTH ways; it generated 18 kinds and read back 13, so feeding
  odin its own `main.tf` lost a third of it. All 18 now round-trip, across 24
  resource types, and odin's own project imports with nothing unsupported.

  What each of the five added kinds actually cost, because none was just a line
  in a table:
  - **sg** — the rules are what the Nebula firewall compiles from, so losing them
    loses the security posture. Both directions of difference are reported: an
    `ingress` block that cannot be one `protocol:port:source` line (a port RANGE)
    is named with a count because the group then allows LESS, and `egress` cannot
    survive at all (odin re-emits its own wide-open default and has no outbound
    field) so a restricted source comes back UNRESTRICTED.
    **The port-RANGE half was closed in v0.8.17**: the line grammar takes
    `tcp:8000-8100:0.0.0.0/0` in both fields, a single port is the degenerate
    range so existing canvases emit byte-identical HCL, and an imported range
    round-trips with BOTH bounds instead of being dropped. The narrowing it
    replaced was a correctness bug, not a missing feature — regenerating
    someone's Terraform handed back a firewall tighter than the one they wrote.
    What still cannot be imported is a port that is not a literal number
    (`var.port`), which is named and counted the same way.
    **The outbound half of that was closed in v0.8.14**: the sg node has an
    `egressRules` field in the same line format, real `egress` blocks are
    emitted from it, and the wide-open default now applies only when the field
    is empty — which keeps every pre-existing canvas byte-identical. **And in
    v0.8.17 it became a control rather than portable configuration**: nebula
    compiles the outbound half too, measured blocking real traffic on a real
    mesh. This paragraph said "Nebula still compiles INGRESS only
    (`outbound: any`), so an egress rule is portable configuration rather than
    a control" until that landed.
  - **ec2** — three references that each decide something different: containment
    (unappliable without it), security groups (less protected), and the companion
    key pair (unreachable). One warning each, not one vague line.
  - **ecs** — one node, three resources. The task definition folds on; placement
    survives. Its canvas WIRING could not: env refs were deliberately never
    written into the HCL, and since odin re-derives `depends_on` from those refs,
    neither the values nor the ordering came back.
    *(v0.8.14 closed this too, with an `odin:ref:<VAR>` tag carrying the
    reference rather than its resolved value. The reasoning above still holds for
    a file odin did not generate, which has no such tags.)*
  - **lambda** — config from the HCL, body from the zip beside it. It also
    exposed a defect older than the feature: a lambda's AUTO-GENERATED role
    imported as a node the user never drew, so a one-lambda canvas round-tripped
    into one `iam_role` and no function.
  - **ecr** — one argument, unsupported only because nobody had written the line.

  Two things worth keeping from how it went. **`unquote` was half an inverse** —
  `quote` is `json.dumps` but unquote only stripped the quotes, so an imported
  shell script came back with a literal `\n` and would have run as one line on a
  real VM; that was never ec2-specific (ssm values, secrets, iam policies all had
  it). And **the last two defects were only visible end to end**: the CLI sent
  `.tf` text without the zip, so the code recovery worked in unit tests and
  nowhere else, and a correct import warned about env references that never
  existed. Both were found by running the real `odin import-tf` against a real
  server after the unit tests were green.


- [x] **The edge-type selector — built 2026-07-29, because the ratchet FIRED.**
  Owner design call (2026-07-28): what an edge MEANS depends on the components
  it connects, and where a pair could legitimately mean more than one thing odin
  should ASK rather than pick. This entry sat unbuilt because, measured, nothing
  was ambiguous: 729 ordered pairs, ZERO. The `<select>` in `ConfigPanel.tsx`
  had therefore **never once rendered** in production — written, reviewed,
  shipped, never executed.

  What made it real was the **connection edge**. `rds → ecs` is the most-drawn
  line in any architecture diagram and odin did nothing with it: it produced a
  cyan IAM edge defaulting to `rds-db:connect`, an action `gateway/classify.py`
  can never emit. Giving that pair its honest second meaning made `rds` and
  `elasticache` against `ecs` and `lambda` ambiguous — and
  `ui/src/lib/edge-ambiguity.test.ts` failed on the next run, printing all eight
  ordered pairs by name. That is the trigger working exactly as designed, one
  day after it was written.

  **It is multi-select, not exclusive**, because in AWS the meanings co-occur —
  a workload wired to a database may also call its control plane, exactly as an
  event source mapping needs the role to hold `sqs:ReceiveMessage`. Ticking a
  meaning auto-ticks the permissions that meaning requires (an `iam` edge with
  nothing ticked reserves a role and emits a policy-less `aws_iam_role`), and
  the last remaining meaning cannot be unticked, since an edge with no meaning
  reloads as `unmodelled` and silently loses the choice.

  The choice is stored ON the edge as the ROADMAP asked (`data.edgeType`), now
  as a `+`-joined set in registry order: `"connection+iam"`. A single meaning has
  no separator and serialises to the bytes it always did, so nothing needed
  migrating; `spec/translate.py::_edges` splits the set into one `Edge` per
  meaning, which is what lets `gateway/policy.py::compile_policies` and
  `agent/hcl.py::_granted_ids` keep gating on `kind == "iam"` unchanged. Handing
  them the joined string would have compiled NO policy — permissions ticked in
  the panel and none granted.

  The ratchet now asserts an **enumerated allowlist** rather than zero, so it
  keeps both properties: a ninth ambiguous pair fails and names itself, and a
  listed pair that silently STOPS being ambiguous fails too (the picker would
  simply vanish for it). Mutation-tested in both directions on 2026-07-29 —
  shrinking the allowlist failed naming the four dropped pairs; registering
  `sns ↔ lambda` as a second meaning failed naming both its orderings.

  The panel's logic lives in `ui/src/lib/iam.ts` (`selectedEdgeTypes`,
  `edgeTypeChoices`, `toggleEdgeType`) rather than in the TSX, because this repo
  has no React test runner and logic left in the component would have no
  coverage — which is precisely how a `<select>` shipped unexecuted.

  Two defects the picker's own tests then found, both pre-existing:
  `edgeDataForConnection` asked the DESTINATION end for the resource type, so
  `rds → ecs` offered to grant the DATABASE permission to run ECS tasks, and the
  two orderings of one pair disagreed — breaking that function's own documented
  property. The principal is now the compute end wherever exactly one end is
  compute, and the arrow decides only when both are, which is the one case where
  it is the only thing that can.

- [x] **Every drawn edge now has a consumer, or says it has none (2026-07-28).**
  Four defects, all the same shape -- odin showing the user one thing and doing
  another.
  **(1) The `iam_role → workload` edge was INERT.** `iam_role` declares no
  `iamActions` (correctly -- a role is not an IAM data-plane target), so the pair
  was never registered, fell through to the catch-all, and was read by NOTHING:
  drawing `admin-role → my-lambda` while the lambda's `role` field said
  `other-role` gave a dead line, `other-role` in the generated file, and
  `other-role` enforced by the gateway. There is a `role` edge type now, folded
  into the `role` FIELD `agent/hcl.py::_lambda` already reads, so it took effect
  with no builder change at all. Lambda only -- ec2/ecs reach a role through an
  auto-role plus an instance profile / `task_role_arn` and read no such field, so
  those pairs stay `unmodelled` rather than authoring a field nothing consumes
  (docs/limits.md names what honouring them needs).
  **(2) compute→compute IAM edges granted nothing.** `edgeDataForConnection`
  picked "whichever end is not compute" as the resource, which has no answer when
  both are -- and `ecs → lambda` (invoke) and `ec2 → ecs` (RunTask) are both
  explicitly in the catalog. The user got a cyan edge with nothing ticked and an
  `aws_iam_role` with no policy on it. The rule is now "which end is an IAM
  target", which also gets `ecs → ec2` right where a plain target-end tie-break
  would not.
  **(3) `edge.kind` was unvalidated where it is authored.** `odin chat` took a
  free string, so an invented kind round-tripped through a revision looking real.
  Validated against `spec/translate.py::EDGE_KINDS` now. This is the SMALLER
  half and must not be read as more: `hcl.py`'s subscription and ALB passes match
  on NODE kinds and never read `edge.kind`, so a valid `iam` edge between sns and
  sqs still builds a real subscription.
  **(4) `network` meant "no model" 340 times out of 341.** Renamed to
  `unmodelled` ("Not modelled"), with `target` (`alb↔ecs`) and `subscription`
  (`sns↔sqs`) lifted out as their own named types. Both are **presentational** by
  design and no builder may gate on them: every saved canvas types those edges
  `network`, so requiring the new name without a migration in the same commit
  would drop the subscription from the generated HCL and `tofu` would DESTROY the
  live subscription on the next apply -- silently, because `_desired_subs` only
  ever ADDS. `network` stays a valid stored kind and a defined type.
  **Plus a fifth, found while measuring:** an sns/sqs edge drawn BACKWARDS was a
  silent no-op -- both consumers key on the drawn direction, so `sqs → sns` gave
  a green Apply, no subscription, and no `unsupported`/`wiring_errors` entry, with
  no test in either direction. `spec/translate.py` now orients the edge
  topic→queue, which fixes `hcl.py` and `reconciler.py::_desired_subs` at once
  without either file changing. Every guard mutation-tested.

- [x] **Containment is strict (owner decision, 2026-07-28).** A leaf counted as
  inside a container once its CENTRE crossed the boundary, so a box visibly
  hanging out was silently claimed. That decides an SG's `vpc_id` -- required and
  immutable on a real `aws_security_group` -- so a few pixels of overlap decided
  infrastructure. A partially-overlapping box is OUTSIDE now: fully-inside is a
  property the user can see and aim for, and erring toward "not contained" is the
  recoverable direction (odin reports an SG with no VPC rather than attaching it
  to the wrong one). Subnet-in-VPC already worked this way; leaves now match.
  Five new cases, mutation-tested.

  The more interesting half -- SG **membership**, which instances an SG gates --
  said "still open ... wants to be an edge" here for a while after it had BECOME
  one (v0.7.13). It is built: `translate.py::SG_MEMBERSHIP` folds an `sg` edge
  into the same `securityGroups` field the text box already feeds, ADDING to it
  rather than replacing, so `hcl.py` is untouched and a hand-authored canvas
  keeps working. Membership is a many-to-many fact between peers, which geometry
  cannot express and an edge can. Eleven cases in
  `tests/spec/test_sg_membership_edges.py`, including that direction does not
  matter and that an `sg` edge to a kind with no such field is ignored rather
  than inventing a setting nothing reads.



- [x] **An edge with no handles routes backwards, putting its label on the
  source node.** I first blamed live convergence and was wrong twice -- neither
  deferring the edge commit by a frame nor preserving node identity across the
  merge changed anything, and a FULL RELOAD reproduced it identically, which is
  what finally ruled convergence out. Both speculative fixes were reverted.

  The real cause: an edge that omits `sourceHandle`/`targetHandle` is routed
  from an arbitrary default handle, so the path curves backwards (a bezier
  control point LEFT of the source) and the label lands at the source node's
  centre -- measured x=601 for a node spanning 500..700, where the midpoint is
  850. The UI's drawn edges always set handles, so only hand- or CLI-authored
  canvases hit it. Naming `"sourceHandle": "right", "targetHandle": "left"` puts
  the label at exactly 850.

  Worth doing, not done: odin could CHOOSE sensible handles when a canvas omits
  them, since a hand-authored or agent-authored canvas is a first-class input
  (`odin canvas set`, the translation agent) and silently drawing it wrong is
  the shape of bug this repo keeps auditing for. `scripts/record-gifs.sh` now
  asserts the label's GEOMETRY rather than its text, because a DOM check for the
  text passed the whole time this was broken.

- [x] **odin now CHOOSES sensible handles when a canvas omits them.**
  `lib/edgeHandles.ts` infers the sides two nodes face each other on, from their
  centres, and explicit handles always win. Verified live on the exact canvas
  that used to break: the permission label moved from x=601 (inside a source
  node spanning 500..700) to **x=850**, the true midpoint. This matters because
  a hand-authored canvas is a first-class input -- `odin canvas set` today, the
  translation agent next -- and drawing one wrong in silence is the shape the
  honesty rules exist for.

- [x] **An unknown node `type` announces itself instead of drawing a blank box.**
  ReactFlow falls back to its `default` node for a type it has no component for,
  and that default is an unlabelled white rectangle -- indistinguishable from
  odin mis-rendering a resource it DOES know. Found while regenerating the README
  hero: a canvas saying `"type": "role"` (the kind is `iam_role`) drew a white
  box, and I took it for an odin bug before finding the typo was mine.

  `UnknownNode` is the registered fallback and names the offending kind --
  verified live: `? | api-role | unknown kind: role`. Drawn rather than refused,
  because an unknown kind is applied-and-skipped BY DESIGN; the canvas is valid,
  it just cannot be built, and what was missing was any way to SEE that.

- [x] **SG membership is an edge.** Which instances a security group gates is a
  RELATIONSHIP, not ownership -- containment correctly supplies an SG's own
  `vpc_id` (one VPC, immutable), but membership is many-to-many between peers and
  geometry cannot express it. It could previously only be TYPED into an ec2/rds
  `securityGroups` field, so the canvas could not show it at all.

  The design that keeps it safe: the edge ADDS to that field rather than
  replacing it, so `agent/hcl.py` is untouched -- it still reads one field
  (`_security_group_refs`) and cannot tell how a line got there. A hand-authored
  canvas keeps working unchanged, duplicates collapse (real AWS rejects a doubled
  entry), and direction is not significant, exactly as for an IAM edge.

  Scoped to `ec2`/`rds`, the kinds whose HCL actually reads the field, so the
  edge cannot author something nothing consumes. Verified end to end: an edge
  drawn with NO field typed anywhere produced
  `vpc_security_group_ids = [aws_security_group.api_sg.id]` in real Terraform,
  and renders solid red against IAM's cyan dashed. 11 translate tests + 5 UI
  tests.

  It did NOT turn out ambiguous -- a "network" line between a group and an
  instance would describe nothing -- so this pair needs no selector. (Green now
  means "matches the eight allowlisted pairs", not "zero": the connection edge
  made `rds`/`elasticache` against `ecs`/`lambda` genuinely ambiguous on
  2026-07-29 and the picker was built. `sg ↔ ec2` is still not among them.)

- [~] **agent-browser cannot draw a connection; its RESULT is now covered.**
  The gesture is still not automatable: handles are 6px and `pointerdown`
  arrives with a non-handle target even at the handle's measured centre
  (`pointerup` does land on it). That stays open, and there is no automated
  coverage of the drag itself.

  What was ALSO uncovered, and no longer is: what a drawn edge MEANS.
  `lib/iam.ts` had no test file at all, so nothing checked that an IAM edge
  takes its permissions from the NON-COMPUTE end -- that `ec2 -> s3` and
  `s3 -> ec2` grant the same S3 permissions, because the user drew the same
  intent either way. `edgeDataForConnection` is extracted from
  `Canvas.tsx::onConnect` and tested (6 cases, mutation-tested: pinning the
  resource end to the target fails it). The gesture remains the gap; the
  decision behind it does not.

## v0.7.9 — the canvas is per-environment

- [x] **The canvas is PER-ENV** (owner decision, 2026-07-27). `/canvas` was the
  only env-taking route where `?env=` was IGNORED: one global
  `.odin/canvas.json` shared by every environment, so two envs could never hold
  different architectures, only the same one applied twice. It lives at
  `.odin/<env>/canvas.json` now and defaults to `default` like every other
  route. Verified live: `staging` and `prod` held different canvases, and
  switching the env field in the UI loaded that env's own canvas
  (`canvas?env=staging` on the wire).

  Three things this dragged in, each worth knowing:
  * **Migration.** Moving the read without moving the file would make a user's
    architecture appear to VANISH -- the same silently-empty-canvas failure
    v0.7.7 was spent on. The old global file seeds `default` and every existing
    env (all of them were showing it), then is RENAMED to
    `canvas.json.pre-per-env` rather than deleted: recoverable if the guess was
    wrong, and the rename makes the migration idempotent with no marker file.
  * **`/tf/plan`'s drift check was quietly wrong**, and is now right. It
    compared the one global canvas against a PER-ENV stack, so `canvas_drift`
    meant nothing for any env but the default.
  * **Backup.** The canvas would have been swept into the env-dir walk, and an
    env dir is replaced wholesale on import -- which would have made
    `--with-canvas` unkeepable, since every restore would clobber whatever the
    user had drawn. It is archived as its own entry, and an import WITHOUT the
    flag now carries the env's current canvas across the replacement.

  The CLI's `odin canvas get/set` take `--env`. Their module docstring used to
  say "no `env` parameter on purpose ... no fake `--env` here" -- true of the
  file it was written for, a lie the moment the canvas moved. The correction is
  left in place as a note, because a caveat outliving its subject is the doc
  failure this repo keeps auditing for.

## v0.7.8 — three PRE-EXISTING field failures, carried forward deliberately

All three fail IDENTICALLY at v0.7.6 (verified by running them against the
released tag, not asserted), so v0.7.7 regresses nothing. They are recorded
here rather than hacked green, because making them pass in five minutes would
have meant retiring a claim rather than fixing a bug.

- [x] **`test_a_noop_apply_cannot_report_success_*` — SPLIT (owner decision,
  2026-07-27). ECS split; LAMBDA resolved differently, see below.** The wiring
  guard now
  refuses an apply carrying an unresolvable `${{ghost.ENDPOINT}}` ref with a
  409, which made these tests' route to a zero-task service unreachable by
  design. Resolved by splitting rather than retiring: one test asserts the
  refusal (and that nothing was built), the other keeps the honesty claim via a
  route the guard permits.

  For the ECS one that route is: apply a bad image (a real update, so tofu
  does work), kill the tasks OUT OF BAND as the field did, then re-apply the
  UNCHANGED canvas — a genuine empty plan with the service at 0 of 3. Two
  premises failed on the way and are worth not re-deriving: a bad image ALONE
  never reaches zero, because ECS keeps healthy old-revision tasks when the new
  ones cannot start (measured: 3 still up); and reading `DescribeServices`
  straight after the recovery apply races the service's re-registration, giving
  an `IndexError` that says nothing. Poll the task containers instead.

  Both ECS tests pass against real containers and real tofu (93s).

  **LAMBDA: resolved differently, and deliberately.** The refusal half is done
  and passing. The e2e no-op half was RETIRED rather than faked, because lambda
  has no route to it through real containers -- measured dead ends: a bad
  `runtime` falls back to the DEFAULT image (`RUNTIME_IMAGES.get(runtime,
  RUNTIME_IMAGES[DEFAULT_RUNTIME])`) and boots a working container;
  `memory_size` is neither canvas-settable nor emitted by `agent/hcl.py`; and
  readiness is a TCP check on the RIE port, which a broken handler satisfies.

  The CLAIM is not retired -- it is covered by `tests/api/test_apply_full.py::
  test_apply_full_fails_on_a_function_whose_container_is_gone_though_the_record_says_active`,
  which seeds an `Active` record, removes the container, injects a
  `FunctionRuntime` whose boot raises, and asserts
  `applied_resources_unhealthy`. Its "tofu had nothing to do" premise is
  STRONGER than the e2e's empty plan: tofu is not installed at all there. What
  is lost is only the real-container substrate, which the ECS e2e covers for
  the same shape.