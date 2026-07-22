# Research: OpenTofu AWS provider vs. odin's local substitutes (Simulate de-risk)

De-risks NORTHSTAR directive 3 (**Simulate** = agent-generated Terraform,
`tofu apply` routed through odin's gateway onto local substitutes). Everything
below was prototyped **for real** — OpenTofu v1.12.3, `hashicorp/aws` provider
**v5.100.0**, against real containers (RustFS S3, goaws SQS+SNS, dynalite
DynamoDB) on Colima, with a logging reverse-proxy in front of each substitute
capturing the exact API surface. No mocks.

## TL;DR — GO for s3/sqs/sns/dynamodb, gateway required

The TF AWS provider drives our substitutes fine **once a thin gateway sits in
front to (a) resolve identity and (b) synthesize the describe/tag/delete-confirm
calls the substitutes lack.** In this prototype the "gateway" was ~90 lines of
Python bolted into the logging proxy; it was enough to take **s3, sqs, sns, an
sns→sqs subscription, and dynamodb through full `apply` → `plan` (zero drift) →
`destroy`.** The substitutes provide *create + data-plane + most reads*; the
gateway must own *identity (STS), tags, and a few delete-confirmation error
codes.* One partial edge remains (SQS queue delete-confirmation, below) — minor,
gateway-solvable.

Harness (reusable — this **is** the gateway's request-surface spec tool):
`…/scratchpad/tofu-research/proxy.py` + `test-*/` dirs.

---

## 1. Verified provider config block (deliverable a)

This exact block was used for every successful run:

```hcl
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }  # resolved 5.100.0
  }
}

provider "aws" {
  access_key                  = "test"          # any value; gateway ignores or verifies
  secret_key                  = "testsecret"
  region                      = "us-east-1"
  skip_credentials_validation = true            # skip real STS pre-flight
  skip_metadata_api_check     = true            # no EC2 IMDS on a Mac
  skip_region_validation      = true
  skip_requesting_account_id  = true            # see identity note below
  s3_use_path_style           = true            # REQUIRED: no per-bucket DNS locally

  endpoints {
    s3       = "http://127.0.0.1:9000"
    sqs      = "http://127.0.0.1:4100"
    sns      = "http://127.0.0.1:4100"
    dynamodb = "http://127.0.0.1:4567"
  }
}
```

**Gotchas found the hard way:**
- `skip_requests_validation` **is not a valid provider argument** (init error). The
  real knobs are the four `skip_*` above.
- **The provider calls STS `GetCallerIdentity` + IAM `ListRoles` at startup** to
  learn the account id. Without `skip_requesting_account_id = true` (and with no
  STS/IAM endpoint) it hits *real AWS* and dies with `InvalidClientTokenId`. See
  identity recommendation in §5.

### Endpoints via env var — no `endpoints{}` block needed (Q3, verified)

Provider 5.x honors the aws-sdk-go-v2 endpoint env vars. Both worked with **no
`endpoints{}` block at all**:

- **Global, one URL for everything:** `AWS_ENDPOINT_URL=http://127.0.0.1:9000` →
  S3 applied. A single var points *all* services at one host — which is exactly
  odin's gateway model (gateway routes internally by service).
- **Per-service:** `AWS_ENDPOINT_URL_S3`, `AWS_ENDPOINT_URL_DYNAMODB`,
  `AWS_ENDPOINT_URL_SQS`, `AWS_ENDPOINT_URL_SNS` → S3 + DynamoDB applied.

**DX win:** the translation agent can emit *clean, portable* TF (`provider "aws"
{ region = … }`) with **no local endpoints baked in**. Simulate just exports
`AWS_ENDPOINT_URL=<gateway>` before `tofu apply`. Only `s3_use_path_style` and
the `skip_*`/creds still need to live in HCL (no env equivalents) — inject them
via a generated `override.tf`, **or** make them unnecessary by having the gateway
implement STS + accept the creds (then `skip_credentials_validation` /
`skip_requesting_account_id` can be dropped).

---

## 2. What applies cleanly vs. what needs the gateway (Q1)

| Resource | Substitute | apply | plan (drift) | destroy | Needs from gateway |
|---|---|---|---|---|---|
| `aws_s3_bucket` | RustFS | ✅ | ✅ **zero drift** | ✅ | nothing (works raw) |
| `aws_dynamodb_table` | dynalite | ✅ | ⚠️ **tags drift** | ✅ | tag store; PITR read |
| `aws_sqs_queue` | goaws | ✅ | ✅ (with GW) | ⚠️ delete-confirm | tags; delete-confirm; attr echo |
| `aws_sns_topic` | goaws | ✅ (with GW) | ✅ (with GW) | ✅ | Get/SetTopicAttributes; tags |
| `aws_sns_topic_subscription` | goaws | ✅ | ✅ (with GW) | ✅ (with GW) | Get/DeleteSubscription fidelity |

**S3 / RustFS is the standout — it needs no help at all.** RustFS persists tags
and answers all ~15 bucket sub-resource reads with correct 200/404 semantics.
DynamoDB/dynalite is nearly clean (only tags + PITR). SQS/SNS/goaws is the
opposite: goaws is a *data-plane* server (create, publish, subscribe, receive),
so nearly all the *control-plane* reads TF needs are synthesized by the gateway.

---

## 3. Captured API surface per resource lifecycle (deliverable b)

Exactly what the provider sent, with substitute responses. `[GW]` = the call the
substitute cannot serve and the gateway synthesized.

### S3 `aws_s3_bucket` — RustFS answers everything
```
HEAD  /bucket              -> 404 (probe) then 200 (HeadBucket)
PUT   /bucket              -> 200  CreateBucket
PUT   /bucket?tagging      -> 200  PutBucketTagging
DELETE/bucket              -> 204  DeleteBucket
# read/refresh probes (every plan):
GET /bucket?tagging  ?policy  ?acl  ?cors  ?website  ?versioning  ?accelerate
    ?requestPayment  ?logging  ?lifecycle  ?replication  ?encryption  ?object-lock
    -> mix of 200 (present) / 404 (absent) — all tolerated by the provider
```
Only imperfection: `GET ?encryption` returns **400** (RustFS) where AWS returns
404 `ServerSideEncryptionConfigurationNotFoundError`. Non-fatal; gateway can
normalize to 404 for fidelity.

### DynamoDB `aws_dynamodb_table` — dynalite, JSON `DynamoDB_20120810.*`
```
CreateTable                 -> 200
DescribeTable               -> 200   (polled to ACTIVE)
DescribeContinuousBackups   -> 400   ⚠ dynalite lacks PITR API
DescribeTimeToLive          -> 200
ListTagsOfResource          -> 200 but returns EMPTY  ⚠ (root of tags drift)
DeleteTable                 -> 200
DescribeTable (post-delete) -> 400   (dynalite) — tolerated, destroy completes
```

### SQS `aws_sqs_queue` — goaws, JSON `AmazonSQS.*` (`x-amz-json-1.0`)
```
CreateQueue           -> 200  (tags in body IGNORED by goaws)
GetQueueAttributes    -> 200  x9 over ~31s  ⚠ slow create convergence
ListQueueTags     [GW]-> 200  (goaws: "Bad Request")
TagQueue/UntagQueue [GW] (goaws lacks)
DeleteQueue           -> 200
GetQueueAttributes[GW]-> 400 QueueDoesNotExist  ⚠ delete-confirm still unhappy (§4)
```

### SNS `aws_sns_topic` (+ subscription) — goaws, **query/form** protocol (XML)
```
CreateTopic               -> 200
GetTopicAttributes    [GW]-> 200   (goaws: "Bad Request" — the READ is unsupported!)
SetTopicAttributes    [GW]-> 200   (goaws: "Bad Request")
ListTagsForResource   [GW]-> 200   (goaws lacks)
Subscribe                 -> 200
GetSubscriptionAttributes [GW]-> 200 (goaws returns incomplete attrs → drift; GW completes)
ListSubscriptionsByTopic  -> 200
Unsubscribe               -> 200
GetSubscriptionAttributes [GW]-> 404 NotFound (delete-confirm; goaws code was non-standard)
DeleteTopic               -> 200
```
**Protocol note that matters for the gateway:** on the *same goaws port*, **SQS
speaks AWS JSON 1.0** (`X-Amz-Target: AmazonSQS.*`) while **SNS speaks the classic
query protocol** (`Action=…` form body, XML responses). DynamoDB is JSON, S3 is
REST. The gateway's request classifier must branch on all three wire formats.

---

## 4. Which calls the substitutes handle vs. the gateway must synthesize (deliverable c)

**Substitutes already handle:** all creates; all data-plane; S3 = *entire*
lifecycle incl. tags; DynamoDB describe/TTL; SQS GetQueueAttributes; SNS
Subscribe/Unsubscribe/ListSubscriptionsByTopic/GetSubscriptionAttributes(partial).

**Gateway must synthesize (the real Simulate work — small, mostly stateless):**

1. **Identity — STS `GetCallerIdentity` (+ minimal IAM).** Highest leverage. Return
   the env's 12-digit account id. Removes `skip_requesting_account_id`, gives
   consistent per-env ARNs, and is the hook IAM enforcement (directive 4) needs.
2. **A per-env tag store.** The cross-cutting gap: SQS has *no* tag API, dynalite
   *accepts but drops* tags, goaws SNS lacks `ListTagsForResource`. RustFS is the
   only one that persists tags. Gateway owns: `ListQueueTags`/`TagQueue`/
   `UntagQueue` (SQS-JSON), `ListTagsForResource`/`TagResource`/`UntagResource`
   (SNS-XML), DynamoDB `ListTagsOfResource`/`TagResource`. ~a dict keyed by ARN.
3. **SNS topic attributes.** goaws has **no** `GetTopicAttributes`/
   `SetTopicAttributes` — the primary READ is missing. Gateway owns the topic
   attribute map (seed on `CreateTopic`, echo on Get, store on Set).
4. **Delete-confirmation error normalization.** Provider delete-waiters poll a
   read and expect the *SDK-modeled NotFound code*. goaws returns non-standard
   codes → waiter errors. Gateway rewrites: SNS subscription → `NotFound`
   (worked); SQS queue → `QueueDoesNotExist` (see caveat §below).
5. **Minor read fidelity:** S3 `?encryption` 400→404; DynamoDB
   `DescribeContinuousBackups` → `{ContinuousBackupsStatus:"DISABLED"}`; SNS
   subscription attribute completion (RawMessageDelivery/PendingConfirmation).

**One unresolved edge:** SQS **queue** delete-confirmation. Even after the gateway
returns the correct `QueueDoesNotExist` code, the provider's SQS delete-waiter
still errors. Real AWS keeps a deleted queue *readable* for ~60s and only then
returns NonExistentQueue; the provider models that transition. So the gateway
must emulate AWS's post-delete semantics (serve attributes briefly, then
NotFound), not just swap the error code. Low effort, but more than a one-liner.
Everything else round-trips clean. (The SNS subscription delete — same class of
problem — was fully solved by the gateway in this prototype.)

---

## 5. Import direction (Q4 / directive 2) — WORKS

Created a bucket + queue **out-of-band via boto3** against the substitutes, then:

- **`import {}` block + `tofu plan -generate-config-out=generated.tf`:** generated
  valid HCL for the bucket **including the tags RustFS had persisted**
  (`origin = outofband`). The plan then errors with *"Conflicting configuration
  arguments"* — this is the **well-known OpenTofu generate-config quirk** (it emits
  mutually-exclusive `bucket`/`bucket_prefix` and `tags`/`tags_all`), **not** a
  substitute problem. The config is still written; strip the conflicting lines
  (the translation agent does this anyway) and it applies.
- **Classic `tofu import` + `tofu show -json`** (cleaner for "import onto canvas"):
  imported into state and produced structured attributes — `bucket`, `arn`,
  `tags` — ready to render a node. This is the robust ingest path; prefer it over
  generate-config-out for canvas hydration, use generate-config-out when you need
  HCL back.

Import reads hit the same refresh surface already enumerated in §3, so the same
gateway coverage supports both `apply` and reverse-import.

---

## 6. Version / quirks (Q3)

- Provider **`hashicorp/aws` v5.100.0** (latest 5.x; `~> 5.0` resolved it),
  OpenTofu **1.12.3**. A 6.x provider line exists — not tested; 5.x recommended
  for Simulate v1 (stable, matches the captured surface).
- SQS uses **AWS JSON protocol** in 5.x (aws-sdk-go-v2), SNS still **query/XML** —
  don't assume one protocol per host.
- SQS create burns **~31s** in the provider's attribute-propagation waiter (9
  `GetQueueAttributes` polls). Partly inherent, partly because goaws's echoed
  attributes don't exactly match. Gateway should echo the set attributes (+
  `QueueArn`, `SqsManagedSseEnabled=false`, empty `Policy`) so it converges on the
  first poll — otherwise every Simulate of a queue costs ~30s.

---

## 7. Go / no-go + recommendations (deliverable d)

**GO** for the s3/sqs/sns/dynamodb Simulate v1 scope. The provider + substitutes +
a thin gateway complete real create/read/plan/destroy round-trips; the gateway
work is small, mostly stateless, and per-call enumerable (§4).

**Build order for the gateway (by leverage):**
1. **SigV4 verify → (service, action, resource) classify → forward.** Classifier
   must branch S3-REST / JSON-target / SNS-query (§3). The `proxy.py` in this
   research already parses all three and doubles as the spec.
2. **STS `GetCallerIdentity`** returning the env account id (unblocks clean ARNs +
   IAM, lets you drop two `skip_*` flags).
3. **Per-env tag store** (SQS/SNS/DynamoDB) — the single biggest correctness win;
   without it every plan drifts on tags.
4. **SNS topic attribute store** (Get/SetTopicAttributes) — goaws's biggest hole.
5. **Delete-confirmation shims** (SQS queue transitional-delete, SNS sub NotFound).
6. **Read-fidelity polish** (S3 encryption 404, DDB PITR, SQS attr echo for speed).

**Gotchas to bake into the plan:**
- Inject endpoints via `AWS_ENDPOINT_URL` env var, not HCL — keep agent-generated
  TF portable; add `s3_use_path_style` + creds via a generated `override.tf`.
- `s3_use_path_style = true` is mandatory (no local bucket DNS).
- goaws emits queue URLs with host `us-east-1.goaws.com:4100`; harmless for
  routing (custom endpoint overrides it) but pollutes TF state — gateway should
  rewrite the `CreateQueue` response host to the gateway's own.
- Evaluate **ElasticMQ (Apache-2.0)** as the SQS backend later: goaws's control
  plane is thin enough that the gateway carries it; a more complete SQS server
  would shrink gateway surface. SNS has no strong standalone OSS server — the
  gateway owning SNS attributes/tags is the right call regardless.

**Scope note:** IAM enforcement (directive 4) rides on top of #1–#2 here — the
SigV4 identity + STS caller-identity are the same primitives the IAM engine
evaluates. This research only exercised provisioning, not policy denial.
