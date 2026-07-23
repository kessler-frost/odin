# PRD — the odin gateway (C1.5): edges become real IAM

**Status:** requirements locked (user, 2026-07-22); internals pending the two
research reports (`.superpowers/sdd/research-iam-{moto,gateway}.md`).
**Owner intent:** "Odin is first and foremost an infra tool which has a layer
of application making built on top of it. I want to keep aws verbs and aws
compatibility COMPLETELY. Connecting two components via an edge (an IAM
permission) is absolutely needed — the UI/UX is the main selling point."

## 1. Problem

Post-MiniStack, workloads talk straight to the backing containers with shared
static creds. Nothing authenticates *which workload* is calling, and canvas
permission edges are decoration. That breaks two north-star invariants
(ROADMAP.md): edges = IAM = the core UX, and odin as an AWS-compatible infra
endpoint rather than a bag of open ports.

## 2. What ships (user-visible)

1. **One endpoint.** Every workload container gets `AWS_ENDPOINT_URL=
   http://host.docker.internal:<gateway port>` plus its OWN
   `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — issued per node by odin.
   Unmodified boto3/aws-cli code inside the container just works.
2. **Edges grant, absence denies.** A workload with an edge to `uploads`
   carrying `s3:GetObject, s3:PutObject` can do exactly that. No edge → the
   same call returns a genuine AWS `AccessDenied` (right status code, right
   error shape for that service's protocol). Deny is the default.
3. **Live in the canvas.** Edge edits take effect on the next Apply (policies
   recompile from the Stack — no container restarts needed, since identity
   lives in the creds, not the wiring).
4. **Observable.** Denied calls surface as World-adjacent events
   (`access_denied` with principal, action, resource) so the canvas/M8
   debugger can answer "why is my app broken" with "its edge lacks
   s3:PutObject".
5. **Host tools bypass.** `BackingAws.client()` (tests, REPL debugging) keeps
   talking directly to backings with the backing creds — the gateway
   authenticates *workloads*, it does not lock out the operator.

## 3. Requirements

- **R1 — AWS wire compatibility, completely.** boto3, aws-cli v2, and any
  SigV4 SDK work unmodified for the supported services. Errors are
  protocol-correct per service (XML for S3, JSON for DynamoDB/SQS, query-XML
  for SNS).
- **R2 — Principals are per-node.** Each workload node gets unique creds at
  (re)run; keys map to node id + env. Key rotation on every Apply is
  acceptable v1 (containers are re-specced anyway).
- **R3 — Policies compile from edges.** Edge (workload → resource, perms:
  [AWS verbs]) → Allow statements scoped to that resource's ARN/name.
  Explicit-deny-wins, default-deny, `*` wildcards in verbs supported
  (`s3:Get*`). No edge-level Deny authoring in v1.
- **R4 — Real execution behind.** The gateway holds no data. Allowed requests
  forward to the env's backing (re-signed for RustFS with backing creds;
  pass-through creds acceptable for goaws/dynalite which ignore auth).
- **R5 — Per-env everything.** Gateway routing, principals, and policies are
  env-scoped like every other odin object.
- **R6 — Fail closed, stay debuggable.** Unknown key, bad signature,
  unmappable action, dead backing → deny/503 with a distinct error + event;
  never silent pass-through.
- **R7 — The reconciler runs it.** The gateway is odin-supervised like
  everything else (in-process FastAPI router or sidecar — internals decide),
  visible in /health.
- **R8 — Latency budget.** ≤ ~10ms added per call on loopback (verify +
  evaluate + re-sign are all local CPU).

## 4. v1 scope cuts (explicit)

- Services: s3, sqs, sns, dynamodb. (rds keeps DATABASE_URL injection — SQL
  auth is Postgres's own job; an rds edge grants *connection string
  delivery*, not per-query IAM.)
- Action subsets per service: the ops the backings support and the canvas UX
  offers (research fixes the exact lists; ballpark ~20 S3 ops, full CRUD for
  sqs/sns/dynamodb data plane).
- Out (documented, error cleanly, revisit on demand): presigned URLs,
  S3 aws-chunked streaming uploads, policy Conditions, cross-service
  resource policies, IAM control-plane APIs (CreateRole etc. — odin's canvas
  IS the IAM control plane), STS.
- llm/dep/batch/service nodes: same injection mechanics; no AWS API of their
  own to guard (their access control = refs + future M7 network layer).

## 5. Internals (VALIDATED by prototype, 2026-07-22 — see
`.superpowers/sdd/research-iam-gateway.md`; every number below measured
against real boto3 1.43 traffic and a real RustFS container)

- **Own policy evaluator, not moto — FINAL** (both research reports in).
  The ~30-line evaluator passed 14/14 wildcard/deny edge cases. moto's
  `IAMPolicy` does import standalone (moto agent: 20-case matrix mostly
  correct on 5.2.2), but the two agents measured CONTRADICTORY matching
  semantics (case-insensitive vs case-sensitive actions), both found `?`
  unsupported, and moto silently over-permits `NotResource` and ignores
  `Condition` — an authorization kernel whose semantics two independent
  testers can't agree on fails the legibility criterion outright, before
  the 28MB dependency and private-API version pin. Adopted from the moto
  report anyway (transferable regardless of evaluator): the policy COMPILER
  emits canonical action casing and never emits `?` / `NotAction` /
  `NotResource` / `Condition`; the gateway stays stateless (old design's
  hard-won lesson); host-side tooling bypasses by construction (R5).
  Fallback if our evaluator ever hits a wall: moto's 581-line module is
  Apache-2.0 and vendorable.
- **Sibling verdicts (user's direct questions, both reports concur):**
  MiniStack 1.4.4 has account-scoping by key id only — zero signature
  verification, zero policy evaluation; it never could have taken us there.
  LocalStack: IAM enforcement (ENFORCE_IAM) is paid-tier-only and the
  community edition was discontinued 2026-03-23 — disqualified twice over.
- **SigV4 verify** via botocore internals, decomposed
  canonical_request/string_to_sign/signature recomputation using the
  request's ORIGINAL `X-Amz-Date` (never `add_auth`, which re-stamps). 20/20
  captured requests verify; wrong secret / tampered body / unknown key all
  reject; `UNSIGNED-PAYLOAD` verbatim. v1 explicitly rejects `STREAMING-*`
  payloads (boto3 doesn't send aws-chunked for seekable bodies — it sends
  crc32 headers instead — so real-world impact ≈ 0). Presign gotcha: boto3
  presign against custom endpoints defaults to SigV2 → presigned URLs
  cleanly out of v1.
- **One port serves all services**: the service name rides in the SigV4
  credential scope. Classifier: dynamodb/sqs via `X-Amz-Target` (trivial),
  sns via `Action` param, S3 via a 16-row (method, has_key, subresource)
  table incl. the full multipart flow; unknown S3 subresources (`?acl`, …)
  → explicit deny. 22/22 real requests mapped.
- **Proxy + re-sign proven**: create_bucket/put/get/list/head through the
  gateway against RustFS, byte-identical payloads, ~0.5–0.7ms median added
  (1KiB objects: 1.5ms direct vs 2.2ms gated) — 15-20× under the R8 budget.
  Deny path returns S3-shaped AccessDenied XML that boto3 raises as a
  proper `ClientError`. RustFS rejects node creds directly, proving the
  re-sign is the only way through.
- **goaws wiring flag**: goaws builds returned `QueueUrl`s from its
  configured Host — point its config's Host at the gateway (not at goaws
  itself), so clients that re-dial returned URLs stay inside the gateway.
- **Size**: ~700 LOC + ~450 test LOC; the research capture-sink harness
  becomes the test fixture. Deferred (error cleanly): aws-chunked,
  presigned, SigV2, policy Conditions, CopyObject dual-auth, streaming
  bodies.

Module layout (validated by the prototype):

    src/odin/gateway/
      keys.py        # per-(env, node) key issue/lookup — feeds injection
      classify.py    # request → (service, action, resource)
      policy.py      # edges → policy docs; evaluate(principal, action, arn)
      sigv4.py       # verify incoming; re-sign for RustFS
      app.py         # the ASGI router: verify → classify → evaluate → forward
    + reconciler injection swap: per-node creds + single AWS_ENDPOINT_URL
    + events: access_denied into the WS/event stream
    + UI: edge perms picker generalized (C1 Task 5 already does this)

## 6. Acceptance (all real, no mocks)

- A batch node WITH an s3 edge (`s3:PutObject,s3:ListBucket` on `uploads`)
  running `aws s3 cp` succeeds; the SAME container image with the edge
  removed gets `AccessDenied` (aws-cli exits non-zero, stderr shows it).
- Wrong/foreign creds (env A's keys against env B's gateway route) → deny.
- sns publish allowed by edge → message flows through to the subscribed
  queue exactly as today (delivery is backing-internal, unaffected).
- Deny event visible via /events with principal+action+resource.
- The full existing integration suite stays green with the gateway in the
  path (the injection swap is the only change workloads see).
- README quick-start unchanged for users: draw, apply, it works.
