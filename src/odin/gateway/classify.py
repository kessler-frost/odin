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

EC2 (task V1a) IS A DIFFERENT RESOURCE CONVENTION: for ec2 the resource is
the id param carried by the request (`VpcId`/`GroupId`/`SubnetId`, or the
first `VpcId.1`/`SubnetId.1`/`GroupId.1`/`ResourceId.1` list entry), else
`"*"` -- NOT a canvas label. Created vpc/subnet/sg ids don't correspond to
canvas labels in V1 (no workload edge references them yet; EC2 instances
arrive in V3), and the only principal driving EC2 calls is the OPERATOR
(full allow), so extraction only needs to never return None -- a None here
would deny even the operator via `unmappable-action`.

IAM + ECR (task V2) share EC2's reasoning exactly: the only principal
driving these calls in v1 is the OPERATOR (TF-authored roles/policies/
repos), so `_classify_iam`/`_classify_ecr` extract a real id when the
request carries one (IAM: `RoleName`/`PolicyArn`/`InstanceProfileName`;
ECR: `repositoryName`/the first `repositoryNames` entry/`resourceArn`'s
last path segment) and fall back to `"*"` rather than ever returning None.
IAM's control-plane document store does NOT feed `evaluate()` (gateway/
models/iamctl.py's module docstring has the full boundary rule) -- this
`resource` value is used only for evaluate()'s wildcard match against the
OPERATOR's full-allow statement, never to authorize a workload.

LAMBDA (task V4a) IS A FOURTH WIRE SHAPE: unlike ec2/iam's query-protocol
`Action=` param and ecr's `X-Amz-Target` header, Lambda is REST (method +
path) -- research §2d / §3: `POST /2015-03-31/functions`, `GET .../
functions/{name}`, `.../invocations`, etc. `_classify_lambda` matches
`(method, path)` against the exact captured route table (each pattern
anchored full-string, so `GET /functions/{name}` and `GET /functions/
{name}/configuration` never collide) and extracts the FunctionName from
the path's `{name}` segment -- except `CreateFunction`, whose path carries
no name at all (`POST /2015-03-31/functions`), so that one route reads
`FunctionName` out of the JSON body instead (the same body-reads-what-the-
-path-doesn't-carry technique `_classify_ecr` already uses). The `/2017-
03-31/tags/{Resource}` routes carry a full ARN, not a bare name -- resolved
to the bare function name the same way SNS's `_sns_resource` strips an ARN
down (`arn:...:function:name` -> the last segment). Same OPERATOR-only
reasoning as ec2/iam/ecr: extraction only needs to never return None for a
route it recognizes; an unrecognized (method, path) pair returns None
(unmappable, closed-world deny) rather than guessing.

ELASTICACHE (W2.8) SHARES SNS/IAM's QUERY-PROTOCOL WIRE and IAM/ECR's
OPERATOR-only REASONING: `_classify_elasticache` reads the `Action` form param
and extracts `CacheClusterId` when the request carries one, else the cluster id
out of a tag call's `ResourceName` ARN (`arn:...:cluster:<id>` -> the last
colon-segment, the same bare-label strip `_sns_resource` does), else `"*"`.
The only principal driving elasticache calls in v1 is the OPERATOR (TF-authored
clusters), so extraction only needs to never return None. NOTE the scope of
what this classification can ever govern: ElastiCache's DATA plane is the raw
Redis protocol, which is not SigV4-signed and never reaches this gateway at
all, so an `elasticache:*` action is always a CONTROL-plane action -- see
gateway/models/cachectl.py's module docstring.

ECS (task V5a) SHARES ECR's JSON-target SHAPE, IAM/ECR's OPERATOR-only
REASONING: `_classify_ecs` extracts a real id when the request carries one
(clusterName/serviceName/family, or the last path segment of an ARN) and
falls back to `"*"` rather than ever returning None -- the only principal
driving ECS calls in v1 is the OPERATOR (TF-authored clusters/services), so
this only needs to never deny it via `unmappable-action`.

LOGS (task W2.1) SHARES ECR's JSON-target SHAPE but is the FIRST modeled
service whose resource is a real WORKLOAD-FACING label again (like s3/sqs/
sns/dynamodb, unlike the operator-only ec2/iam/ecr/lambda/ecs families): a
`logs` canvas node's label IS its log-group name (agent/hcl.py's `_logs`
builder emits `name = <label>`), so `_logs_resource` returning the bare group
name is exactly what `policy.compile_policies` puts in an iam edge's
statement -- draw `lambda -> log-group` with `logs:PutLogEvents` and the
gateway enforces it for real, with no logs-specific code in the policy layer.
A call that carries no group at all (a bare `DescribeLogGroups`) falls back to
its `logGroupNamePrefix`, else `"*"` -- never None, so the OPERATOR is never
denied via `unmappable-action` (the ec2/iam/ecr reasoning), while a workload
principal without a matching statement still denies through ordinary
default-deny.

SECRETS MANAGER + SSM (task W2.4) ARE THE PAYOFF CASE for the LOGS reasoning
above: both share ECR's JSON-target wire shape, and for both the resource IS
the canvas node's label -- a `secret` node's label is its secret name
(`agent/hcl.py`'s `_secret` emits `name = <label>`), an `ssm` node's label is
its parameter name (`_ssm` emits the same). So `_secretsmanager_resource` /
`_ssm_resource` returning the bare name is exactly what
`policy.compile_policies` puts in an iam edge's statement: draw
`lambda -> secret` with `secretsmanager:GetSecretValue` and the gateway
enforces it for real, with no secrets-specific code in the policy layer, while
a workload with no such edge gets an ordinary default-deny
`AccessDeniedException`. Both accept an ARN wherever a name can appear (a
`SecretId` is an ARN whenever terraform passes
`aws_secretsmanager_secret.x.id`) and reduce it to the same bare label; SSM
additionally canonicalizes the leading slash exactly as
`gateway/models/ssmctl.py::canonical_name` does (kept in lock-step: root-level
`/db` == `db`, hierarchical `/odin/db` keeps its slash). A call carrying no
identifier at all (`ListSecrets`, a bare `DescribeParameters`) falls back to
`"*"` -- never None, so the OPERATOR is never denied via `unmappable-action`.
One bounded gap, the same one `_ecr_resource`/`_ecs_resource` already carry for
every list-carrying call: a BATCH `GetParameters(Names=[a, b])` is authorized
against `a` alone, so an edge to `a` (and none to `b`) would let both through.
Recorded in ROADMAP.md; the single-name reads a workload actually makes
(`GetParameter`, `GetSecretValue`) are exact.

S3 BUCKET-CONFIG READS (S2, discovered running real tofu through the real
gateway): the TF AWS provider's `aws_s3_bucket` refresh probes bucket-config
subresources -- `?policy`, `?tagging`, `?acl`, `?cors`, `?versioning`, etc.
-- on every create-then-read and every plan (research §3). These used to sit
in the same "unmappable" bucket as truly-unsupported object-level ops
(`?restore`, `?legal-hold`, ...), which denied the OPERATOR principal (S2)
before `evaluate()` even ran -- its full-allow statement never got a chance.
Now mapped to their real IAM action names (`_S3_BUCKET_CONFIG_READ_ACTIONS`)
so evaluate() decides: the operator's wildcard grants them, no workload iam
edge grants a bucket-config verb in v1, so a workload principal still denies
-- via ordinary default-deny, not `unmappable-action`. Only the GET (read)
side is mapped; v1 has no write path for any of these, so a PUT/DELETE
still denies.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, unquote

# Bucket-config GET probes the TF AWS provider's `aws_s3_bucket` makes on
# every create-then-read and every plan's refresh (research §3: "read/
# refresh probes (every plan): GET /bucket?tagging ?policy ?acl ?cors
# ?website ?versioning ?accelerate ?requestPayment ?logging ?lifecycle
# ?replication ?encryption ?object-lock ... — all tolerated by the
# provider"). Mapped to their real IAM action names (S2: "G2 left them None
# -> extend classify for operator flows" -- the same technique S1 used for
# SQS/SNS create-paths) so evaluate() gets a chance to run at all: the
# OPERATOR principal's full-allow grants them, but no workload iam edge
# grants a bucket-CONFIG verb in v1, so a workload principal still denies --
# via evaluate()'s ordinary default-deny now, not classify()'s
# "unmappable-action" short-circuit.
_S3_BUCKET_CONFIG_READ_ACTIONS = {
    "acl": "s3:GetBucketAcl",
    "tagging": "s3:GetBucketTagging",
    "versioning": "s3:GetBucketVersioning",
    "policy": "s3:GetBucketPolicy",
    "cors": "s3:GetBucketCORS",
    "lifecycle": "s3:GetLifecycleConfiguration",
    "notification": "s3:GetBucketNotification",
    "replication": "s3:GetReplicationConfiguration",
    "encryption": "s3:GetEncryptionConfiguration",
    "website": "s3:GetBucketWebsite",
    "logging": "s3:GetBucketLogging",
    "accelerate": "s3:GetAccelerateConfiguration",
    "requestPayment": "s3:GetBucketRequestPayment",
    "publicAccessBlock": "s3:GetBucketPublicAccessBlock",
    "policyStatus": "s3:GetBucketPolicyStatus",
    "object-lock": "s3:GetBucketObjectLockConfiguration",
    "ownershipControls": "s3:GetBucketOwnershipControls",
    "intelligent-tiering": "s3:GetIntelligentTieringConfiguration",
    "metrics": "s3:GetMetricsConfiguration",
    "inventory": "s3:GetInventoryConfiguration",
    "analytics": "s3:GetAnalyticsConfiguration",
    "versions": "s3:ListBucketVersions",
}

# Bucket-config WRITES: the S3b translation agent may add tags (and other
# arguments) to any resource, so the provider's PutBucketTagging & co. arrive
# through the gateway during apply (first seen live in the S5 e2e — an
# unmapped PUT ?tagging turned into AccessDenied and killed the whole apply).
# DELETE maps to the same write action as PUT, mirroring real AWS IAM (e.g.
# DeleteBucketTagging is authorized by s3:PutBucketTagging). Read-only
# subresources (versions, policyStatus) are deliberately absent.
_S3_BUCKET_CONFIG_WRITE_ACTIONS = {
    "acl": "s3:PutBucketAcl",
    "tagging": "s3:PutBucketTagging",
    "versioning": "s3:PutBucketVersioning",
    "policy": "s3:PutBucketPolicy",
    "cors": "s3:PutBucketCORS",
    "lifecycle": "s3:PutLifecycleConfiguration",
    "notification": "s3:PutBucketNotification",
    "replication": "s3:PutReplicationConfiguration",
    "encryption": "s3:PutEncryptionConfiguration",
    "website": "s3:PutBucketWebsite",
    "logging": "s3:PutBucketLogging",
    "accelerate": "s3:PutAccelerateConfiguration",
    "requestPayment": "s3:PutBucketRequestPayment",
    "publicAccessBlock": "s3:PutBucketPublicAccessBlock",
    "object-lock": "s3:PutBucketObjectLockConfiguration",
    "ownershipControls": "s3:PutBucketOwnershipControls",
    "intelligent-tiering": "s3:PutIntelligentTieringConfiguration",
    "metrics": "s3:PutMetricsConfiguration",
    "inventory": "s3:PutInventoryConfiguration",
    "analytics": "s3:PutAnalyticsConfiguration",
}

# Object-level subresources v1 has no create path for at all (nothing ever
# PUTs a legal hold, restores an object, requests a torrent) -- genuinely
# unmapped regardless of method, never silent pass-through (research §Q2:
# "unknown subresources ... -> explicit deny").
_S3_UNSUPPORTED_SUBRESOURCES = {"restore", "torrent", "legal-hold", "retention"}

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
    if service == "ec2":
        return _classify_ec2(body)
    if service == "iam":
        return _classify_iam(body)
    if service == "ecr":
        return _classify_ecr(lower_headers, body)
    if service == "lambda":
        return _classify_lambda(method, path, body)
    if service == "ecs":
        return _classify_ecs(lower_headers, body)
    if service == "logs":
        return _classify_logs(lower_headers, body)
    if service == "secretsmanager":
        return _classify_secretsmanager(lower_headers, body)
    if service == "ssm":
        return _classify_ssm(lower_headers, body)
    if service == "elasticache":
        return _classify_elasticache(body)
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


# Ordered id-param candidates (some requests carry several id kinds; the
# scoped one wins -- e.g. CreateSubnet carries VpcId but is subnet-scoped
# work under that vpc, which is fine: the OPERATOR is the only ec2 caller).
_EC2_ID_PARAMS = ("VpcId", "VpcId.1", "SubnetId", "SubnetId.1", "GroupId", "GroupId.1", "ResourceId.1")


def _classify_ec2(body: bytes) -> tuple[str, str] | None:
    try:
        params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return None
    action_name = params.get("Action")
    if not action_name:
        return None
    resource = next((params[key] for key in _EC2_ID_PARAMS if params.get(key)), "*")
    return f"ec2:{action_name}", resource


_IAM_ID_PARAMS = ("RoleName", "PolicyArn", "InstanceProfileName")


def _classify_iam(body: bytes) -> tuple[str, str] | None:
    try:
        params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return None
    action_name = params.get("Action")
    if not action_name:
        return None
    resource = next((params[key] for key in _IAM_ID_PARAMS if params.get(key)), "*")
    return f"iam:{action_name}", resource


def _classify_elasticache(body: bytes) -> tuple[str, str] | None:
    try:
        params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return None
    action_name = params.get("Action")
    if not action_name:
        return None
    cluster_id = params.get("CacheClusterId")
    resource_name = params.get("ResourceName")
    resource = cluster_id or (resource_name.rsplit(":", 1)[-1] if resource_name else "*")
    return f"elasticache:{action_name}", resource or "*"


def _ecr_resource(payload: dict) -> str:
    name = payload.get("repositoryName")
    if isinstance(name, str) and name:
        return name
    names = payload.get("repositoryNames")
    if isinstance(names, list) and names and isinstance(names[0], str):
        return names[0]
    arn = payload.get("resourceArn")
    if isinstance(arn, str) and arn:
        return arn.rsplit("/", 1)[-1]
    return "*"


def _classify_ecr(lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"ecr:{op}", _ecr_resource(payload)


def _bare_id(value: str) -> str:
    return value.rsplit("/", 1)[-1]


# op -> which payload field(s) carry the id, in the OPERATOR-only style
# _classify_iam/_classify_ecr already use: extract a real value when present,
# "*" otherwise (never None -- see module docstring's ECS note).
def _ecs_resource(op: str, payload: dict) -> str:
    if op in ("CreateCluster", "DeleteCluster"):
        value = payload.get("clusterName") or payload.get("cluster")
        return _bare_id(value) if value else "*"
    if op == "DescribeClusters":
        names = payload.get("clusters")
        return _bare_id(names[0]) if isinstance(names, list) and names else "*"
    if op == "RegisterTaskDefinition":
        return payload.get("family") or "*"
    if op in ("DescribeTaskDefinition", "DeregisterTaskDefinition"):
        value = payload.get("taskDefinition")
        return _bare_id(value) if value else "*"
    if op == "CreateService":
        return payload.get("serviceName") or "*"
    if op in ("UpdateService", "DeleteService"):
        value = payload.get("service")
        return _bare_id(value) if value else "*"
    if op == "DescribeServices":
        names = payload.get("services")
        return _bare_id(names[0]) if isinstance(names, list) and names else "*"
    if op == "ListTasks":
        return payload.get("serviceName") or payload.get("family") or "*"
    if op == "DescribeTasks":
        tasks = payload.get("tasks")
        return _bare_id(tasks[0]) if isinstance(tasks, list) and tasks else "*"
    return "*"


def _logs_resource(payload: dict) -> str:
    """The bare LOG GROUP NAME -- which for a `logs` canvas node IS its label
    (agent/hcl.py's `_logs` builder sets `name = <label>`), so an iam edge
    drawn to that node gates every call here through the ordinary
    `evaluate(statements, action, resource)` path with no logs-specific
    plumbing (see the module docstring's LOGS note). `logGroupIdentifier` /
    `resourceArn` carry an ARN instead of a name -- reduced to the same bare
    group name, the way `_sns_resource`/`_ecr_resource` already strip theirs.
    """
    for key in ("logGroupName", "logGroupIdentifier", "resourceArn"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _bare_log_group(value)
    identifiers = payload.get("logGroupIdentifiers")
    if isinstance(identifiers, list) and identifiers and isinstance(identifiers[0], str):
        return _bare_log_group(identifiers[0])
    prefix = payload.get("logGroupNamePrefix")
    return prefix if isinstance(prefix, str) and prefix else "*"


def _bare_log_group(value: str) -> str:
    """`arn:aws:logs:...:log-group:/aws/lambda/f[:*]` -> `/aws/lambda/f`; a
    value that isn't an ARN comes back unchanged. Kept in lock-step with
    `gateway/models/logsctl.py::_group_from_arn` (same two ARN forms: real
    CloudWatch reports the `:*` wildcard suffix, the TF provider trims it)."""
    trimmed = value[:-2] if value.endswith(":*") else value
    _prefix, sep, name = trimmed.partition(":log-group:")
    return name.split(":log-stream:")[0] if sep else trimmed


def _classify_logs(lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"logs:{op}", _logs_resource(payload)


def _secretsmanager_resource(payload: dict) -> str:
    """The bare SECRET NAME -- which for a `secret` canvas node IS its label
    (see the module docstring's W2.4 note). `SecretId` is an ARN whenever
    terraform passes `aws_secretsmanager_secret.x.id`, reduced here the same
    way `gateway/models/secretsctl.py::_secret_name` reduces it (kept in
    lock-step)."""
    for key in ("SecretId", "Name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            _prefix, sep, name = value.partition(":secret:")
            return name if sep else value
    return "*"  # ListSecrets & co. name no secret at all


def _classify_secretsmanager(lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"secretsmanager:{op}", _secretsmanager_resource(payload)


def _ssm_canonical(value: str) -> str:
    """Kept in lock-step with `gateway/models/ssmctl.py::canonical_name` --
    AWS treats a root-level parameter's leading slash as optional, a
    hierarchical name's as part of the name; an ARN reduces to the same form."""
    _prefix, sep, path = value.partition(":parameter")
    bare = (path if sep else value).lstrip("/")
    return bare if "/" not in bare else f"/{bare}"


def _ssm_resource(payload: dict) -> str:
    """The canonical PARAMETER NAME -- which for an `ssm` canvas node IS its
    label. `ResourceId` is SSM's tag-API carrier (a bare name, not an ARN);
    `Names`/`Path` cover the batch reads."""
    for key in ("Name", "ResourceId", "Path"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _ssm_canonical(value)
    names = payload.get("Names")
    if isinstance(names, list) and names and isinstance(names[0], str):
        return _ssm_canonical(names[0])
    return "*"  # a bare DescribeParameters names no parameter at all


def _classify_ssm(lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"ssm:{op}", _ssm_resource(payload)


def _classify_ecs(lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"ecs:{op}", _ecs_resource(op, payload)


# The captured REST route table (research §2d + botocore's own lambda
# service-2.json `http.requestUri` per operation, verified live): (method,
# path pattern, op name). `{name}` is the FunctionName path segment;
# `{arn}` is the tag routes' full resource ARN. Order doesn't matter --
# every pattern is `$`-anchored to its own full path, so no two can match
# the same (method, path) pair.
_LAMBDA_ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/2015-03-31/functions$"), "CreateFunction"),
    ("GET", re.compile(r"^/2015-03-31/functions/(?P<name>[^/]+)$"), "GetFunction"),
    ("DELETE", re.compile(r"^/2015-03-31/functions/(?P<name>[^/]+)$"), "DeleteFunction"),
    ("GET", re.compile(r"^/2015-03-31/functions/(?P<name>[^/]+)/configuration$"), "GetFunctionConfiguration"),
    ("PUT", re.compile(r"^/2015-03-31/functions/(?P<name>[^/]+)/code$"), "UpdateFunctionCode"),
    ("PUT", re.compile(r"^/2015-03-31/functions/(?P<name>[^/]+)/configuration$"), "UpdateFunctionConfiguration"),
    ("GET", re.compile(r"^/2015-03-31/functions/(?P<name>[^/]+)/versions$"), "ListVersionsByFunction"),
    ("POST", re.compile(r"^/2015-03-31/functions/(?P<name>[^/]+)/invocations$"), "Invoke"),
    ("GET", re.compile(r"^/2020-06-30/functions/(?P<name>[^/]+)/code-signing-config$"), "GetFunctionCodeSigningConfig"),
    ("GET", re.compile(r"^/2017-03-31/tags/(?P<arn>.+)$"), "ListTags"),
    ("POST", re.compile(r"^/2017-03-31/tags/(?P<arn>.+)$"), "TagResource"),
    ("DELETE", re.compile(r"^/2017-03-31/tags/(?P<arn>.+)$"), "UntagResource"),
)


def _classify_lambda(method: str, path: str, body: bytes) -> tuple[str, str] | None:
    for route_method, pattern, op in _LAMBDA_ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if match is None:
            continue
        groups = match.groupdict()
        if "name" in groups:
            return f"lambda:{op}", unquote(groups["name"])
        if "arn" in groups:
            return f"lambda:{op}", unquote(groups["arn"]).rsplit(":", 1)[-1]
        # CreateFunction: the path carries no name -- read it from the body,
        # same technique _classify_ecr uses for repositoryName.
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        name = payload.get("FunctionName")
        return f"lambda:{op}", name if isinstance(name, str) and name else "*"
    return None


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
    config_hit = _S3_BUCKET_CONFIG_READ_ACTIONS.keys() & query.keys()
    if config_hit:
        if bucket is None:
            return None
        sub = next(iter(config_hit))
        if method == "GET":
            return _S3_BUCKET_CONFIG_READ_ACTIONS[sub], bucket
        if method in ("PUT", "DELETE") and sub in _S3_BUCKET_CONFIG_WRITE_ACTIONS:
            return _S3_BUCKET_CONFIG_WRITE_ACTIONS[sub], bucket
        return None  # a write to a read-only subresource stays unmappable
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
