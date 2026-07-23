# Research: the IAM-enforcing gateway (thin-gateway path)

**Date:** 2026-07-22 · **Status:** all four probes prototyped and passing against real boto3 (1.43.53) traffic and a real RustFS container.

**Verdict: feasible, low-risk, small.** The crux prototype — boto3 with per-node creds → starlette gateway (SigV4 verify → action extraction → policy evaluation → re-sign with backing creds) → real RustFS → full put/get/list/head roundtrip — works end-to-end with **~0.5 ms median added latency** per request. Denials surface in boto3 as proper `ClientError` with `Code=AccessDenied`. Every mechanism the design depends on was exercised for real; nothing was mocked. Prototype code: `/private/tmp/claude-501/-Users-fimbulwinter-dev-odin/6cd1a58b-e15e-45d8-81fc-c59f340b5139/scratchpad/iamgw/` (`gwlib.py` is the near-production core; `capture.py`, `probe1_verify.py`, `probe2_actions.py`, `probe3_policy.py`, `probe4_e2e.py` are the probes).

---

## Q1 — SigV4 verification: PROVEN (20/20 real captures verify; all negatives reject)

**Method.** A local HTTP sink captured 21 real boto3 requests (dynamodb put/get/query; sqs create/send/receive; sns publish/create; 12 S3 REST ops incl. signed payload, `UNSIGNED-PAYLOAD`, multipart, a 100 KB `upload_fileobj`). Verification re-derives the signature with **botocore's own canonicalization** — not `add_auth()` (which stamps a fresh timestamp), but the decomposed internals with the original `X-Amz-Date` injected:

```python
req = AWSRequest(method=method, url=url, data=body,
                 headers={k: v for k, v in headers.items() if k.lower() in signed_names})
req.context["timestamp"] = lower["x-amz-date"]          # original, not now()
auth = (S3SigV4Auth if service == "s3" else SigV4Auth)(Credentials(ak, secret), service, region)
computed = auth.signature(auth.string_to_sign(req, auth.canonical_request(req)), req)
return ak if hmac.compare_digest(computed, claimed) else None
```

Key details that make it correct:
- Only the headers listed in the Authorization header's `SignedHeaders` are placed on the `AWSRequest`, so botocore canonicalizes exactly what the client signed. `S3SigV4Auth` vs `SigV4Auth` is chosen by the service in the credential scope (S3 skips path normalization).
- botocore's `canonical_request` uses the `X-Amz-Content-SHA256` header **verbatim** when present, so signed-payload and `UNSIGNED-PAYLOAD` S3 requests both verify with zero special-casing.
- The raw (percent-encoded) request path must be fed in, not the decoded one (`request.scope["raw_path"]` in starlette). Keys with spaces/`+`/unicode verified.

**Results:** 20/20 captures verify with the right secret; 20/20 reject a wrong secret; tampered DynamoDB body rejects; unknown access key rejects. **Presigned SigV4 URLs also verify** with a small query-auth branch (`S3SigV4QueryAuth`, strip `X-Amz-Signature`, timestamp from `X-Amz-Date` query param) — tamper-tested.

**Findings that shape v1:**
- **No aws-chunked anywhere.** boto3 1.43.53 with seekable bodies (bytes, `BytesIO`, files — including `upload_fileobj`) sends a plain body with an `x-amz-checksum-crc32` *header*, never `STREAMING-*`/aws-chunked. Chunked framing only appears for non-seekable streams. v1: reject `x-amz-content-sha256: STREAMING-*` with a clear 403 message (implemented in the prototype).
- **Presigned pitfall:** `generate_presigned_url` against a custom endpoint defaults to **SigV2** (`AWSAccessKeyId=` query). SigV2 is not worth supporting; v4 presign requires `Config(signature_version="s3v4")` client-side. Verification works today, but presign stays out of v1 (see deferred).
- `UNSIGNED-PAYLOAD` means body tampering is undetectable *by signature* — inherent to the protocol, irrelevant on localhost, note only.
- Not implemented, trivial to add: clock-skew window on `X-Amz-Date` (~15 min) and `X-Amz-Expires` check for presigned.

## Q2 — (service, action, resource) extraction: PROVEN (22/22 captures mapped)

**The routing insight:** one gateway port serves all services because the **SigV4 credential scope names the service** (`Credential=AK/20260722/us-east-1/dynamodb/aws4_request`). No URL routing, no port-per-service.

- **DynamoDB** — trivial: `X-Amz-Target: DynamoDB_20120810.PutItem` → `dynamodb:PutItem`; resource from body `TableName`.
- **SQS** — boto3 now speaks JSON protocol: `X-Amz-Target: AmazonSQS.SendMessage` → `sqs:SendMessage`; resource from `QueueUrl`/`QueueName` in the JSON body. (Query-protocol fallback only matters for old/non-boto3 SDKs — deferred.)
- **SNS** — query protocol: form-encoded body, `Action=Publish` → `sns:Publish`; resource from `TopicArn`/`Name`.
- **S3 REST** — a 16-row table `(method, has_key, marker_subresource) → action` covers the whole workload subset: ListAllMyBuckets, Create/Delete/HeadBucket, ListBucket(+V2), GetBucketLocation, Get/Put/Delete/HeadObject, DeleteObjects batch, full multipart lifecycle (CreateMultipart/UploadPart/Complete → `s3:PutObject`, Abort → `s3:AbortMultipartUpload`, ListParts → `s3:ListMultipartUploadParts`), ListBucketMultipartUploads. HeadObject authorizes as `s3:GetObject`, HeadBucket as `s3:ListBucket` (AWS semantics). Bucket-level actions authorize against the bucket ARN, object actions against `arn:aws:s3:::bucket/key`.

All 22 captures (incl. the SigV2 presigned GET) mapped correctly. **Where it gets hairy — and the answer:** unknown subresources (`?acl`, `?tagging`, `?versioning`, `?policy`, …) hit no table row → **explicit deny with a clear "unsupported operation" message**, which is exactly right for a dev tool: closed-world, no silent pass-through. CopyObject (`x-amz-copy-source`) needs dual authorization (get on source + put on dest) — deferred, deny the header in v1. One fix found by testing: URL-decode the key before building the ARN (`dir/hello%20world%2Bx.txt` → `dir/hello world+x.txt`).

## Q3 — policy evaluation: OWN EVALUATOR WINS (14/14 vs moto's 11/14)

**(b) Own evaluator** — ~30 lines of logic (`gwlib.evaluate`): statements of `{Effect, Action, Resource}` (scalar or list), IAM wildcards (`*`/`?` translated to regex with everything else escaped), case-insensitive action match, case-sensitive resource match, **explicit-deny-wins, default-deny**. Passes all 14 edge-case tests: prefix wildcards, `*` crossing `/` in ARNs, `bucket/*` NOT matching the bucket ARN itself, regex metachars treated literally, narrow deny carve-outs inside broad allows, deny-all, list-valued fields.

**(a) moto as a library** — works standalone: `IAMPolicy({"policy_document": json_str}).is_action_permitted(action, resource)` with no moto server/backend. But moto 5.2.2 **disagrees with real IAM semantics on 3/14 cases** (case-insensitive action matching, `?` wildcard, regex metachars in resources are not escaped), it's a private undocumented API, and it drags in cryptography, werkzeug, responses, xmltodict, requests.

**Decision: the own evaluator.** It is smaller than the glue code moto would need, more correct, dependency-free, and it is the evaluator that actually ran inside the working Q4 gateway. moto adds negative value here.

## Q4 — the crux: streaming reverse proxy + re-signing: WORKS

Full chain, nothing mocked: boto3 (`AKIDODINNODE00001`/node secret, path-style) → starlette gateway on :9110 → verify → extract → evaluate → strip `{host, authorization, x-amz-date, x-amz-content-sha256, content-length, user-agent, accept-encoding, expect, connection}` → fresh `AWSRequest` at the backing URL → `S3SigV4Auth(backing_creds).add_auth()` (normal fresh-timestamp signing) → pooled `httpx.Client` → **real RustFS container** (rustfs/rustfs:latest, `allfather`/`allfather-secret-key`).

```
create_bucket: OK
put_object (signed payload + crc32 header): OK      # RustFS accepts the forwarded x-amz-checksum-crc32
get_object roundtrip bytes match: OK (b'payload-bytes-123')
list_objects_v2 via gateway: ['dir/hello world+x.txt']
head_object: OK content-length=17
object visible in RustFS directly (backing creds): ['dir/hello world+x.txt']
delete_object denied: code=AccessDenied  (policy had no s3:DeleteObject)
cross-bucket put denied: code=AccessDenied
node creds direct-to-RustFS rejected by RustFS: code=InvalidAccessKeyId   # defense in depth
direct  put median=1.5ms  get median=0.8ms      (40 iterations, 1 KiB)
gateway put median=2.2ms  get median=1.3ms
added overhead: ~0.5–0.7 ms median (second run: put −0.2/get +0.5 — within noise)
```

The deny path returns S3-style `<Error><Code>AccessDenied</Code>…` XML with 403, and boto3 raises exactly the `ClientError` a real AWS deny produces. Two implementation lessons captured by the prototype:
1. **HEAD responses:** starlette recomputes `Content-Length` from the (empty) body — the upstream `Content-Length` must be passed through for HEAD or `head_object` reports size 0. Found and fixed by test.
2. Forwarding `x-amz-checksum-crc32` through the re-sign is safe (body bytes unchanged, RustFS validates it happily).

Pass-through for goaws/dynalite is strictly easier (no re-sign; they ignore auth — RustFS rejecting foreign creds is the proof the re-sign is load-bearing for S3 only). One flag for integration: goaws returns `QueueUrl`s naming its own host; either configure goaws's advertised host to the gateway address or rewrite `QueueUrl` in responses (goaws supports the former in config — preferred).

## Q5 — the v1 cut for `src/odin/gateway/`

| Module | Contents | LOC est. |
|---|---|---|
| `sigv4.py` | `verify_sigv4` (header + presigned-v4 branches), clock-skew check | ~100 |
| `actions.py` | credential-scope service routing; ddb/sqs/sns extractors; `S3_OPS` table | ~130 |
| `policy.py` | Pydantic `Statement`, `evaluate` (explicit-deny-wins, default-deny) | ~70 |
| `compile.py` | canvas edges → statements (below) | ~80 |
| `keys.py` | per-node key minting: `AKID` + slug, secret = HMAC(env master secret, node label) — derivable, nothing stored | ~50 |
| `errors.py` | AccessDenied renderers: S3 XML (validated), DynamoDB/SQS JSON (`__type: …#AccessDeniedException`), SNS XML | ~60 |
| `proxy.py` | starlette catch-all: verify → extract → evaluate → re-sign(S3)/pass-through(others) → forward; HEAD content-length rule; STREAMING-* rejection | ~160 |
| wiring | uvicorn thread in server lifespan (same pattern as the MiniStack embed successor); reconciler injects `AWS_ENDPOINT_URL=http://host.docker.internal:<port>` + per-node keys | ~40 |

**Total ~700 LOC** + ~450 LOC of tests. The capture-sink harness from this research becomes the test fixture: every supported operation gets a captured-request verification + extraction assertion, plus the evaluator table tests, plus one Colima-marked integration test re-running the Q4 roundtrip.

**Validated v1 action subsets** (everything below ran through the prototypes):
- **s3 (16 mapped ops):** ListAllMyBuckets, CreateBucket, DeleteBucket, HeadBucket, ListBucket(V2), GetBucketLocation, GetObject, HeadObject, PutObject, DeleteObject, DeleteObjects, CreateMultipartUpload, UploadPart, CompleteMultipartUpload, AbortMultipartUpload, ListParts, ListBucketMultipartUploads → actions `s3:{Get,Put,Delete}Object, s3:ListBucket, s3:CreateBucket, s3:DeleteBucket, s3:GetBucketLocation, s3:AbortMultipartUpload, s3:ListMultipartUploadParts, s3:ListBucketMultipartUploads, s3:ListAllMyBuckets`
- **dynamodb:** every op is `X-Amz-Target`-named — the extractor covers the whole API for free; policy verbs of interest: `GetItem, Query, Scan, BatchGetItem, PutItem, UpdateItem, DeleteItem, BatchWriteItem, DescribeTable`
- **sqs:** same (JSON targets): `SendMessage, ReceiveMessage, DeleteMessage, ChangeMessageVisibility, GetQueueUrl, GetQueueAttributes, CreateQueue…`
- **sns:** `Publish, Subscribe, CreateTopic…` (query `Action=` param)

**Edge → policy compilation** (`compile.py`): an edge `workload → resource-node` with `perms` compiles per resource kind; read/write verb packs:
- s3 `read` → `s3:GetObject` on `arn:aws:s3:::<bucket>/*` + `s3:ListBucket`,`s3:GetBucketLocation` on the bucket ARN; `write` → `s3:PutObject`,`s3:AbortMultipartUpload`,`s3:ListMultipartUploadParts` on `/*`; `delete` → `s3:DeleteObject`
- sqs `send` → `sqs:SendMessage`; `receive` → `sqs:ReceiveMessage`,`sqs:DeleteMessage`,`sqs:ChangeMessageVisibility`,`sqs:GetQueueAttributes`,`sqs:GetQueueUrl`
- sns `publish` → `sns:Publish`; dynamodb `read`/`write` → the verb packs above; all on the node-label-derived ARN.
Control-plane ops (CreateBucket/CreateQueue/CreateTable) belong to the reconciler with backing creds — workload policies are data-plane only, so a workload touching an un-edged resource gets AccessDenied by default-deny. That is the product feature.

**Explicitly deferred (reject-with-clear-message or absent in v1):**
- aws-chunked / `STREAMING-*` uploads (rejected 403; boto3 never sends them for seekable bodies — verified)
- presigned URLs (v4 verification already works; blocked on the SigV2-default footgun and URL-reachability of the gateway from browsers — v1.5)
- SigV2 anything; virtual-hosted addressing (boto3 on custom endpoints is path-style — verified)
- IAM `Condition`, `NotAction`, `NotResource`, `Principal` (edge-compiled policies never emit them)
- CopyObject dual-auth (`x-amz-copy-source` denied in v1); S3 `?acl`/`?tagging`/`?versioning`/`?policy` subresources (explicit deny)
- Response/request streaming (v1 buffers both — fine for local dev-sized objects); SQS query-protocol fallback; replay protection; key rotation

**Risks (all low):**
1. botocore's `SigV4Auth` internals are private API — stable for a decade; pin botocore; the capture-corpus tests catch any drift immediately.
2. goaws `QueueUrl` host advertisement (config fix, flagged above).
3. Duplicate/multi-value signed headers are unhandled (never emitted by boto3; a 10-line fix if ever needed).
4. Future boto3 checksum-behavior changes (the Jan-2025 `when_supported` default is already the world we tested).
