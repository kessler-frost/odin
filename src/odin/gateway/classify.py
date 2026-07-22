"""Map a decoded AWS request to (action, resource) in LABEL form.

`resource` is always the bare node label the policy compiler emits (G1,
`.superpowers/sdd/task-g2-brief.md` CONTRACT ADDENDUM), never an ARN: s3 ->
bucket name, sqs -> queue name, sns -> topic name, dynamodb -> table name.
Ported from the research prototype
(.superpowers/sdd/research-iam-gateway.md §Q2): dynamodb/sqs route on
`X-Amz-Target`, sns on the `Action` form param, s3 on a
(method, has_key, subresource) table covering the full multipart lifecycle.
Returns None for anything unmappable (unknown S3 subresource, CopyObject,
a control-plane call with no resource identifier yet) -- the caller denies
closed-world rather than guessing.

CREATE/NAME-CARRYING PATHS (S-plan task S1): SQS CreateQueue/GetQueueUrl and
SNS CreateTopic don't carry the QueueUrl/TopicArn the addendum's original
extraction rule keyed off of (they're discovering/creating the identifier,
not using it) -- resolved via QueueName/Name instead, so the operator
principal (S2, tofu) and the gateway's own synth post-processing
(synth.py's CreateQueue/CreateTopic hooks) can reach evaluate()/forward at
all. Workload-principal denial semantics are unaffected: no edge grants
create verbs to workers in v1, so these still deny -- just via ordinary
default-deny (`reason="denied"`) rather than `unmappable-action`. Tag-CRUD
reads/writes (SNS/DynamoDB Tag*/List*Tags carry `ResourceArn` instead of
Topic/TableName) resolve the same way. SNS subscription-scoped calls
(GetSubscriptionAttributes/Unsubscribe) carry `SubscriptionArn`, not
`TopicArn` -- the topic name lives in the ARN's second-to-last segment
(`...:<topic>:<subscription-id>`), extracted the same bare-label way.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qsl, unquote

# S3 subresources with no v1 action mapping: explicit deny, never silent
# pass-through (research §Q2: "unknown subresources ... -> explicit deny").
_S3_UNSUPPORTED_SUBRESOURCES = {
    "acl", "tagging", "versioning", "policy", "cors", "lifecycle",
    "notification", "replication", "encryption", "website", "logging",
    "accelerate", "requestPayment", "publicAccessBlock", "policyStatus",
    "object-lock", "restore", "torrent", "legal-hold", "retention",
    "ownershipControls", "intelligent-tiering", "metrics", "inventory",
    "analytics", "versions",
}

_S3_MULTIPART_ACTIONS = {
    "PUT": "s3:PutObject",
    "POST": "s3:PutObject",
    "DELETE": "s3:AbortMultipartUpload",
    "GET": "s3:ListMultipartUploadParts",
}
_S3_BUCKET_ACTIONS = {
    "PUT": "s3:CreateBucket",
    "DELETE": "s3:DeleteBucket",
    "HEAD": "s3:ListBucket",
    "GET": "s3:ListBucket",
}
_S3_OBJECT_ACTIONS = {
    "GET": "s3:GetObject",
    "HEAD": "s3:GetObject",
    "PUT": "s3:PutObject",
    "DELETE": "s3:DeleteObject",
}


def classify(
    service: str,
    method: str,
    path: str,
    query: dict[str, str],
    headers: dict[str, str],
    body: bytes,
) -> tuple[str, str] | None:
    lower_headers = {name.lower(): value for name, value in headers.items()}
    if service == "s3":
        return _classify_s3(method, path, query, lower_headers)
    if service in ("dynamodb", "sqs"):
        return _classify_target(service, lower_headers, body)
    if service == "sns":
        return _classify_sns(body)
    return None


def _classify_target(service: str, lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    resource = _target_resource(service, payload)
    if resource is None:
        return None
    return f"{service}:{op}", resource


def _first_str(payload: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _target_resource(service: str, payload: dict[str, object]) -> str | None:
    if service == "dynamodb":
        resource_arn = _first_str(payload, "ResourceArn")
        if resource_arn:
            return resource_arn.rsplit("/", 1)[-1] or None
        return _first_str(payload, "TableName")
    queue_url = _first_str(payload, "QueueUrl")
    if queue_url:
        return queue_url.rstrip("/").rsplit("/", 1)[-1] or None
    return _first_str(payload, "QueueName")


def _classify_sns(body: bytes) -> tuple[str, str] | None:
    try:
        params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return None
    action_name = params.get("Action")
    if not action_name:
        return None
    resource = _sns_resource(action_name, params)
    return (f"sns:{action_name}", resource) if resource else None


def _sns_resource(action_name: str, params: dict[str, str]) -> str | None:
    if action_name == "CreateTopic":
        return params.get("Name") or None
    arn = params.get("TopicArn") or params.get("ResourceArn")
    if arn:
        return arn.rsplit(":", 1)[-1] or None
    subscription_arn = params.get("SubscriptionArn")
    if not subscription_arn:
        return None
    segments = subscription_arn.split(":")
    return segments[-2] if len(segments) >= 2 else None


def _classify_s3(
    method: str, path: str, query: dict[str, str], lower_headers: dict[str, str]
) -> tuple[str, str] | None:
    if "x-amz-copy-source" in lower_headers:
        return None  # CopyObject dual-auth: deferred to v1.5
    if _S3_UNSUPPORTED_SUBRESOURCES & query.keys():
        return None
    trimmed = path.strip("/")
    segments = trimmed.split("/", 1) if trimmed else []
    bucket = unquote(segments[0]) if segments else None
    has_key = len(segments) > 1 and segments[1] != ""
    action = _s3_action(method, bucket, has_key, query)
    if action is None:
        return None
    return action, bucket if bucket else "*"


def _s3_action(method: str, bucket: str | None, has_key: bool, query: dict[str, str]) -> str | None:
    if bucket is None:
        return "s3:ListAllMyBuckets" if method == "GET" else None
    if "uploadId" in query:
        return _S3_MULTIPART_ACTIONS.get(method)
    if "uploads" in query:
        if method == "POST" and has_key:
            return "s3:PutObject"
        if method == "GET" and not has_key:
            return "s3:ListBucketMultipartUploads"
        return None
    if "location" in query:
        return "s3:GetBucketLocation" if method == "GET" else None
    if "delete" in query:
        return "s3:DeleteObject" if method == "POST" else None
    if not has_key:
        return _S3_BUCKET_ACTIONS.get(method)
    return _S3_OBJECT_ACTIONS.get(method)
