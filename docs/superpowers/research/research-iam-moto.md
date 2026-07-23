# Research: moto as the IAM engine inside odin's gateway

**Date:** 2026-07-22 · **Scope (narrowed by team-lead):** the gateway architecture is decided
(odin builds its own single endpoint, per-node keys, edge-compiled policies, forwards to real
backings). This report evaluates **moto's IAM policy evaluator as an importable library** inside
that gateway, plus dependency weight, git-history lessons, and one-paragraph answers on
moto-server-as-front / MiniStack / LocalStack.

All probes ran against **moto 5.2.2** on Python 3.13 (`uv run --no-project --with moto`).
Probe scripts: `probe1_swap.py`, `probe2_iam.py`, `probe3_standalone.py` in this session's
scratchpad (contents reproduced in essentials below). No servers left running.

---

## Verdict up front

`moto.iam.access_control.IAMPolicy` **works as a standalone library** and is the right IAM core
for odin's gateway: import it, feed it policy JSON strings + an action string + a resource ARN,
wrap it in a 10-line deny-wins loop. It correctly handles action/resource wildcards, action
lists, NotAction, single-or-list statements, and explicit-deny-overrides-allow across policies.
Known divergences from real AWS (all manageable because **odin compiles the policies itself**):
case-sensitive action match, no `?` wildcard, `NotResource` over-permits, `Condition` blocks
effectively ignored. Bare moto is Apache-2.0, ~28 MB, 0.1 s warm import, and the evaluator is
581 lines of private API — pin the version, vendor it if it ever breaks.

---

## 1. PRIORITY — `IAMPolicy` as an importable library

### 1.1 The import surface

```python
from moto.iam.access_control import IAMPolicy, PermissionResult
```

Works with **bare `moto`** (no extras, no server, no backends, no account state). `IAMPolicy`
accepts a **plain JSON string** (also a moto `Policy` object or a `{"policy_document": ...}`
dict, but the string path is the clean one). `is_action_permitted(action, resource)` returns
`PermissionResult.PERMITTED | DENIED | NEUTRAL` per policy; the caller owns aggregation.

The exact aggregation odin's gateway needs (mirrors moto's own
`IAMRequestBase.check_action_permitted`, verified against server-mode behavior in probe 2):

```python
def evaluate(policy_docs: list[str], action: str, resource: str) -> bool:
    """explicit deny > explicit allow > implicit deny"""
    permitted = False
    for doc in policy_docs:
        result = IAMPolicy(doc).is_action_permitted(action, resource)
        if result == PermissionResult.DENIED:
            return False
        permitted = permitted or (result == PermissionResult.PERMITTED)
    return permitted
```

### 1.2 Evaluation depth — measured, not read (probe 3, 20-case matrix)

Correct (matches AWS semantics):

| capability | result |
|---|---|
| exact action + exact resource ARN | ✓ |
| resource prefix wildcard `arn:aws:s3:::b/*` (incl. nested keys) | ✓ |
| resource mismatch → implicit deny | ✓ |
| verb wildcard `s3:Get*`, service wildcard `s3:*`, global `*` | ✓ |
| no cross-service bleed (`s3:*` doesn't grant `sqs:*`) | ✓ |
| explicit Deny beats Allow — same policy AND across policies | ✓ |
| Deny scoped to another resource doesn't block | ✓ |
| `Action` as list; `Statement` as single dict or list | ✓ |
| `NotAction` (both directions) | ✓ |

Divergences from real AWS (flagged by the probe):

1. **Action matching is case-sensitive** (AWS is case-insensitive for action names).
   `s3:getobject` in a policy does NOT match `s3:GetObject`. Harmless for odin: the compiler
   emits canonical casing.
2. **`?` wildcard unsupported** — `_match` only translates `*`→`.*` then runs `re.match` on the
   raw pattern. `?` becomes regex "optional previous char" (wrong), and other regex
   metacharacters in ARNs (`+`, `(`, `.`) are interpreted as regex — `.` is benign-ish,
   `+`/`(` can mis-match or raise `re.error`. Compiler rule: never emit `?`; if node labels can
   contain regex metachars, restrict or escape them at compile time.
3. **`NotResource` silently over-permits**: it is not handled at all, and a statement with no
   `Resource` key returns PERMITTED once the action matches (that branch exists for trust
   policies). Probe: Allow+`NotResource: secret/*` **granted access to `secret/k`**. Compiler
   rule: never emit `NotResource`.
4. **Conditions are effectively ignored for identity policies.** Only `StringEquals` is
   implemented (`moto/iam/policy_conditions.py`, `CONDITION_OPERATIONS` has exactly one entry);
   every other operator returns True (fail-open). Context values only arrive via an
   `incoming_condition_values` argument that moto itself populates only for AssumeRole
   trust-policy checks — the normal path passes none, so even `StringEquals` sees an empty
   actual-values list and passes. Measured: Allow+`IpAddress` condition → allowed;
   Allow+`StringEquals` (no context) → allowed; Deny+unsupported condition → **denies**
   (over-denies, since the condition never gates it). Compiler rule: reject `Condition` in v1,
   or build odin's own condition layer on top.
5. Not supported at all (by design of the module — its own header says so): resource-based
   policies, permission boundaries, SCPs, session policies, policy variables
   (`${aws:username}` would be treated as literal regex text).

Malformed documents raise raw `JSONDecodeError` / `TypeError` / `KeyError` — validate at
compile time, not request time. Bonus: moto ships a standalone validator that works without any
backend and raises AWS-style errors (verified):

```python
from moto.iam.policy_validation import IAMPolicyDocumentValidator
IAMPolicyDocumentValidator(doc).validate()
# bad Effect  -> MalformedPolicyDocument: Syntax errors in policy.
# bad Resource -> MalformedPolicyDocument: Resource not-an-arn must be in ARN format or "*".
# no Action   -> MalformedPolicyDocument: Policy statement must contain actions.
```

### 1.3 What odin must build around it (the exact scaffolding)

`IAMPolicy` is only the (policies, action, resource) → bool kernel. The gateway supplies:

1. **Principal resolution** — access key id → node identity → its compiled policy docs. Odin's
   own store; trivial.
2. **SigV4 verification** — moto's `IAMRequest.check_signature()` does real signature
   recomputation (probe 2: wrong secret → `SignatureDoesNotMatch`, unknown key →
   `InvalidAccessKeyId`), but `IAMRequestBase.__init__` is hard-coupled to
   `iam_backends`/`sts_backends` lookups (`create_access_key`), so it is NOT directly reusable.
   The portable part is small and odin already ships botocore: parse `Authorization` header
   (`Credential=`, `SignedHeaders=`, `Signature=`), rebuild an `AWSRequest` with only the
   signed headers, then `SigV4Auth(creds, service, region)` (or `S3SigV4Auth` for S3) →
   `canonical_request` → `string_to_sign` → `signature`, compare. ~30 lines, cribbed from
   `moto/iam/access_control.py` lines 329–360.
3. **Action derivation from the wire request** — the genuinely non-trivial part of any gateway:
   - JSON-protocol services (DynamoDB): `X-Amz-Target` header suffix → action. Nearly free.
   - Query-protocol services (SQS/SNS as goaws speaks them): `Action=` form/query param. Free.
   - S3 (REST): method + path + query → action needs a mapping table. Moto's S3 handlers do
     this per-endpoint; odin only needs the verbs its edges can compile
     (Get/Put/Delete/List/Head ≈ 10–20 actions), so a small explicit table is fine.
4. **Resource ARN construction** — S3 from bucket/key in the path
   (`arn:aws:s3:::bucket/key` — moto's `_authenticate_and_authorize_s3_action` shows the
   format, including bucket-only ARNs for `ListBucket`); SQS from the queue URL; DynamoDB from
   `TableName` in the body. RDS is out of scope: Postgres wire protocol, not an AWS API — the
   IAM layer simply doesn't sit in front of it.
5. **The aggregation loop** (§1.1) and properly-shaped per-protocol error responses
   (`AccessDenied` XML for S3/query, `AccessDeniedException` JSON for DynamoDB) — moto only
   shapes IAM/EC2/S3 errors; its DynamoDB denials came back as bare HTTP 403 in probe 2.

**Stability note:** `moto.iam.access_control` is not a public API. Pin moto; the whole module is
581 lines + a 120-line conditions file, so vendoring under `src/odin/` (Apache-2.0 permits, with
attribution) is a cheap escape hatch if an upgrade ever moves it.

### 1.4 Integration sketch (`src/odin/`)

- `gateway/policy.py` — compile canvas edges → policy JSON strings (canonical casing, no
  `?`/`NotResource`/`Condition`), validate with `IAMPolicyDocumentValidator`; the `evaluate()`
  loop wrapping `IAMPolicy`.
- `gateway/sigv4.py` — the ~30-line botocore recompute-and-compare (per-node secrets from the
  key store).
- `gateway/actions.py` — per-service action+resource extraction (S3 table, `X-Amz-Target`,
  `Action=` param).
- `gateway/server.py` — the single endpoint: authenticate → derive (action, resource) →
  `evaluate(node.policies, action, resource)` → forward to the backing container or return the
  protocol-correct AccessDenied. Keys/policies live alongside the Stack in `spec/`; the
  reconciler injects per-node keys as env, same as it injects AWS env today.

---

## 2. Dependency weight + license

- **License: Apache-2.0** (wheel `License-Expression`, moto 5.2.2). Compatible with the
  permissive-only rule; vendoring allowed.
- **Install:** bare `moto` + boto3 = 20 packages total. Core requires: boto3, botocore,
  cryptography, requests, xmltodict, werkzeug, responses. Odin's lock already has
  boto3/botocore/cryptography/pyyaml, so **net-new: moto, requests, xmltodict, werkzeug,
  responses**.
- **Disk:** moto package 28 MB (of which `moto/iam` is 8.2 MB, dominated by the 3.8 MB vendored
  `aws_managed_policies.py` data file — its JSON is only parsed when
  `MOTO_IAM_LOAD_MANAGED_POLICIES` is enabled; irrelevant for standalone `IAMPolicy` use, but a
  free source of real AWS managed-policy documents if canvas presets ever want them). Whole
  venv incl. boto3/botocore: 76 MB (botocore alone is 26 MB and already paid for).
- **Import:** `import moto.iam.access_control` = 1.7 s cold (first run, pyc compile), **0.10 s
  warm**; pulls 75 moto modules including `moto.iam.models` and `moto.sts.models` (backend
  classes get imported but no state is created).

---

## 3. Git-history lessons (the pre-pivot "Moto as router, Odin as executor")

Archaeology result: only the **design doc** (`docs/plans/2026-02-27-odin-aws-simulator-design.md`,
commit `c4f62c6`, tag `peak`) and the **implementation plan** (`...-impl.md`, commit `00ca893`)
ever landed in git. The implemented backends (`OdinEC2Backend` etc., the "236 tests" era) were
never committed — no code to read. The docs still carry the IAM/policy lessons:

1. **Enforcement was never built.** Impl-plan Task 10 shipped only IAM CRUD persistence;
   Step 3 says verbatim: *"The policy evaluation engine (parsing IAM JSON policies, matching
   actions/resources, implementing the deny-override logic) is complex enough to be its own
   task. … Enforcement is tracked as a follow-up."* The part that killed the old plan's IAM
   story is exactly the part `IAMPolicy` now provides off the shelf.
2. **Enforcement was designed as middleware in front of dispatch, not inside a backend**:
   extract identity from access key → action + resource ARN → evaluate user/group/role policies
   with deny > allow > implicit deny → 403 AccessDenied. That is the same shape as the decided
   gateway — the old design validates the new one.
3. **Root bypass convention:** default/root credentials skip enforcement; only created
   principals are enforced. Maps cleanly to odin today: the reconciler/provisioner uses root
   creds against backings; workload nodes get issued keys that are always evaluated.
4. **Pain points that the new architecture already avoids:** (a) dual state authority — moto
   metadata + real bytes needed dual-writes and a startup reconciliation between moto memory,
   SQLite, and real infra; (b) the ID-authority problem — moto assigns IDs, so restart-replay
   changes them; the plan's chosen fix was "manipulate moto internal dicts directly" (their own
   word: brittle). With real executors owning all state and moto reduced to a stateless
   policy-evaluation function, both problems vanish — **keep moto stateless; never let it own
   resource state again.**

---

## 4. One-paragraph answers

**Moto server as the front (fallback only).** Both halves still work on moto 5.2.2: probe 1
swapped `s3_backends.backend = ProxyS3Backend` and served GET bytes from an external store
through the real wire protocol, and probe 2 ran full server-mode IAM enforcement — 13/14
expected results across S3 + DynamoDB (scoped allows, explicit deny wins, verb wildcards,
resource-ARN scoping, real SigV4 rejection of bad secrets). But the depth is uneven in ways that
matter: only S3/SQS/IAM/STS compute real resource ARNs — every other service authorizes against
`resource="*"` (`moto/core/responses.py` `call_action`), so probe 2's table-ARN-scoped DynamoDB
policy was **denied even though AWS would allow it**, and non-S3 denials come back as bare 403s
with IAM-shaped bodies. Add the global request-count auth gate (`INITIAL_NO_AUTH_ACTION_COUNT`),
moto re-owning state odin's executors already own, and `moto[server]`'s 62-package footprint,
and this is strictly worse than importing the evaluator — keep it as the fallback if the gateway
ever needs full 47-service protocol parsing overnight.

**MiniStack (1.4.4, current PyPI).** No, it does not take us there. Its "auth" is account
scoping only: `core/responses.py` `set_request_account_id` — if the access key id is a 12-digit
number it becomes the account id in a contextvar, otherwise the default account; that is
isolation, not authorization. There is no signature verification anywhere, no policy evaluation
engine (`services/iam.py` is 2,965 lines of CRUD storage plus vendored managed-policy JSON as
data; its few `AccessDenied` raises are API semantics like "can't tag an AWS-managed policy").
Any caller can act as any account by picking a key id.

**LocalStack.** Disqualified twice over. IAM *enforcement* (`ENFORCE_IAM=1`) is gated to the
paid **Base/Ultimate plans** — the docs page says "Included in Plans: Base, Ultimate" and "Per
default, IAM enforcement is disabled, and all APIs can be accessed without authentication";
community-edition users filed bugs (localstack#6173, #7183) because CE silently ignored the
flag. And the free Community Edition itself was discontinued on **March 23, 2026** in favor of
a single registration-required image with a limited free tier — incompatible with odin's
local-only, permissive-license, no-registration stance.
Sources: [IAM Policy Enforcement docs](https://docs.localstack.cloud/aws/capabilities/security-testing/iam-policy-enforcement/),
[LocalStack pricing changes](https://blog.localstack.cloud/2026-upcoming-pricing-changes/),
[The Road Ahead for LocalStack](https://blog.localstack.cloud/the-road-ahead-for-localstack/).

---

## Appendix: reproduction

```bash
# probe 1 — backend swap + external data store (moto server mode)
uv run --no-project --with 'moto[server,s3,iam]' --with boto3 python probe1_swap.py
# probe 2 — server-mode IAM allow/deny matrix (S3 + DynamoDB, 14 cases)
uv run --no-project --with 'moto[server,s3,iam]' --with boto3 python probe2_iam.py
# probe 3 — standalone IAMPolicy library drive (20 cases, bare moto)
uv run --no-project --with moto --with boto3 python probe3_standalone.py
```

Key moto source (5.2.2): `moto/iam/access_control.py` (581 lines — `IAMPolicy`,
`IAMPolicyStatement`, `IAMRequest`, SigV4 recompute), `moto/iam/policy_conditions.py`
(StringEquals only), `moto/iam/policy_validation.py` (`IAMPolicyDocumentValidator`),
`moto/core/authorization.py` (`ActionAuthenticatorMixin`, `enable_iam_authentication`),
`moto/core/responses.py` `call_action` (resource="*" for services without
`_determine_resource` — only core/sqs/iam/sts define it).
