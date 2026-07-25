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
  in the answer. Still roadmap, not shipped: HCL for kinds with no builder,
  least-privilege policy synthesis, import of unmodeled resource types.

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
  6. ElastiCache (wave 2, W2.8): `aws_elasticache_cluster` → a real
     `redis:7-alpine` container per cluster, the published port advertised as
     the cluster's node endpoint and published as `REDIS_URL`/`REDIS_URL_VM`
     World facts; single-node redis only (see the limits below)

  **v1 limits, recorded rather than hidden** (northstar directive 5's honesty
  rule):
  - Lambda: inline code only, `$LATEST` only — no S3-deployed packages,
    versions, or aliases.
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
    **Residual gap, stated plainly:** odin retires the stale tasks before
    launching replacements, so a failed image update still takes the service to
    zero until the next Apply (real ECS's `minimumHealthyPercent = 100` keeps
    the old tasks serving). Closing that needs the World projection to
    distinguish "serving the previous revision" from "healthy", or a service
    running old tasks would report `healthy` while its deployment failed — a
    worse lie than the outage. The apply now FAILS loudly either way, so CI
    stops instead of scoring the outage green. (Fixed: a
    `tags` block on `aws_ecs_service` now plans zero-drift — the gateway stores
    the full tag set and echoes it back, with
    `TagResource`/`UntagResource`/`ListTagsForResource` modeled.)
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
    a real `InvalidParameterValue`, never a silent collapse to one node. No
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
      such labels must rename the node.
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
      - **What a revoke does NOT reach: a connection already open through
        it.** New connections are refused the moment the peer re-handshakes
        (sub-second, see the convergence note below). But nebula's firewall
        keeps a conntrack entry per flow and re-validates it only when its
        OWN rules change — not when the peer's certificate does — so a
        long-lived connection that keeps sending can outlive the revoke, up
        to nebula's conntrack timeout for that protocol. Editing the
        admitting group's RULES (which does bump the rule version, forcing
        re-validation) or restarting the admitting member closes it at once.
        Real AWS security groups behave the same way for established flows.
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
    - **EC2 nodes publish addresses too** (they published nothing before):
      `${{web1.PRIVATE_IP}}` (host-reachable, ungated) and
      `${{web1.MESH_IP}}` (the SG-gated overlay address, sticky across
      recreation). `MESH_IP` is held to the same standard as `*_MESH` above —
      withheld when the env's lighthouse is down. For a VM that lighthouse
      check is ALL that is verified: its nebula is a systemd unit inside a
      Lima VM, and a `limactl shell` per VM per sweep is not a tick's price.
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
    - Not modeled yet: egress rules (nebula gets allow-all outbound
      regardless of what a canvas draws), NACLs, an SG's self-reference
      (`self = true`), and ICMP rules from the canvas (`ingressRules` takes a
      numeric port, so `icmp:-1:...` can't be drawn — the API path can).
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
  - Targets: an `ecs` node is the only canvas kind that can be a target in v1
    (an alb→`ec2` edge is reported as unsupported). The model's
    `RegisterTargets` itself is generic — an `i-…` id resolves through the
    EC2-compute store to the VM's real address — so `aws_lb_target_group_
    attachment` is a small follow-up, not a redesign. A task target is
    `(host.docker.internal, its real published host port)`: the honest local
    analogue of an `ip` target for a bridge-mode container, not a fiction
    about instance ids.
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
  `scripts/install.sh` (one command: brew tools + colima up + odin + doctor)
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

- [x] **M8 — Region-select debugging ("what's wrong here?")** — SHIPPED 2026-07-25 as W2.9 (`agent/debugger.py`, `POST /agent/debug`, `ui/src/components/RegionAsk.tsx`); the "fix this part" half of the original wording deliberately did NOT ship — the agent explains, it never edits. Original entry: drag a selection rectangle over a canvas region → context menu ("Debug this" / "What's wrong here?" / "Fix this part" / free-form ask) → a region-scoped agent auto-gathers the enclosed nodes + edges and, for each, its World state (phase/facts/verdict/restarts) + recent events/logs + relevant Stack fields, then investigates or fixes from there. Reuses the existing Cmd+drag selection; new parts are the menu + a context-assembler that turns a selection into the agent prompt. **Point at a region instead of describing it — far less back-and-forth.**
- [ ] **M7 (multi-Mac) — the fleet:** a **self-hosted Nebula mesh** fabric (you own the lighthouse — runs in your private network, programmable, a control-plane/UI can be built on top; chosen over Tailscale, whose SaaS coordination would limit that) + multi-Mac membership (memberlist/raft) + apple-container runtime. The Nebula fabric foundation (cert/lighthouse/config primitives + the `NebulaFabric` resolve seam) is reinstated under `fabric/nebula.py`; cross-Mac placement is the deferred part. Additive, no core change.
- [ ] **Brain Toolbelt MCP:** make the Brain a candidate-only producer behind a typed `place` + `propose_changeset` + `review_iam` MCP membrane (stricter than today's best-effort completion).
- [ ] **MiniStack real-container backings** for the remaining stateful AWS services (ElastiCache→Redis, etc.) so apps use them for real, not just the API.
- [ ] **Packaging:** bundle the external tools (colima, lima, uv, …) into one distributable.

### Testing (superseded)
- [x] pytest suite: 80 unit + 9 integration (real Colima/MiniStack/Lima/Claude, marker-gated)
- [x] Browser e2e via playwright (skeleton + full-breadth scenarios)
- [ ] Broader end-to-end scenario coverage as milestones land
