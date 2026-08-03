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

RDS (task W2.7) is the QUERY protocol (form-encoded `Action=`, like sns/ec2/
iam -- not a JSON target header), and its resource is workload-facing like
logs': an `rds` canvas node's label IS its `DBInstanceIdentifier`
(agent/hcl.py's `_rds` builder emits `identifier = <label>`), so an
`rds:DescribeDBInstances` edge drawn to that node compiles to a statement the
gateway enforces with no rds-specific code in the policy layer.

NOTE WHAT THAT DOES NOT COVER, because this paragraph asserted the opposite
until v0.8.15 and was believed for it: it named `rds-db:connect` as the example
action. Every string this function can produce for rds is built as
`f"rds:{action}"` from the `Action` form param, so `rds-db:` -- a DIFFERENT
service prefix -- is unreachable, and a policy granting it could never match
anything. odin does not implement IAM database authentication at all; the
Postgres container takes its password out of `DATABASE_URL` and consults
nobody. The action has been removed from the canvas's vocabulary
(`ui/src/lib/catalog.ts`), and what a user drawing rds -> workload actually
wants is the `connection` edge, which authors that `DATABASE_URL`.

ELBV2 (task W2.5) IS THE QUERY PROTOCOL, like sns/ec2/iam -- the operation
rides in the `Action` form param, not an `X-Amz-Target` header (verified
against botocore's own `elbv2` model: `protocol: query`, `endpointPrefix:
elasticloadbalancing`, which is also the SigV4 credential-scope service name
this module dispatches on). Its resource is the bare LOAD-BALANCER or
TARGET-GROUP name (`_elbv2_resource`), extracted from `Name` on a create and
from whichever ARN the call carries otherwise, falling back to `"*"` -- the
same OPERATOR-only "never return None" reasoning ec2/iam/ecr/ecs use, and for
the same reason: a load balancer is not an IAM data-plane target on odin's
canvas (see `_elbv2_resource`'s own docstring), so tofu is the only principal
that ever gets here.

EVENTBRIDGE (`events`) SHARES ECR's JSON-target WIRE (`X-Amz-Target:
AWSEvents.*`, protocol `json`, jsonVersion 1.1 -- read off botocore's OWN
`events` service model, along with `endpointPrefix: events`, which is the
SigV4 credential-scope name this module dispatches on) and logs/secrets/ssm's
WORKLOAD-FACING resource convention: the resource is the bare RULE NAME, which
for an `events` canvas node IS its label, so an iam edge drawn to that node
gates the call through the ordinary `evaluate(statements, action, resource)`
path with no events-specific plumbing.

Its extraction is where the DynamoDB-Streams trap lives, and it is worth
naming precisely because a repeat is invisible: `_target_resource` reads
`ResourceArn`, and EventBridge's tag API spells it **`ResourceARN`** (capital
ARN -- verified against botocore's own `TagResource`/`UntagResource`/
`ListTagsForResource` input shapes, whose `required` lists say `ResourceARN`).
Read the DynamoDB spelling here and every tag call resolves to `"*"`, which
the OPERATOR's wildcard still allows -- so nothing breaks until someone draws
an iam edge, and then `events:TagResource` on rule `nightly` denies with
`resource '*'`, indistinguishable from a policy denial. Three key families
carry a rule identifier and each op uses exactly one: `Name` (Put/Delete/
Describe/Enable/DisableRule, and the event-BUS ops, where the resource is the
bus), `Rule` (Put/RemoveTargets, ListTargetsByRule) and `ResourceARN` (the
three tag ops). `_events_resource` reads all three, in that order -- they never
co-occur -- falling back to `NamePrefix` for a bare `ListRules` and then to
`"*"`, never None, so `tofu` is never denied via `unmappable-action`.

`PutEvents` is the ONE op whose identifier is not a top-level member at all:
real IAM authorizes it against the EVENT BUS, and the bus name rides INSIDE
the entries list (`Entries[].EventBusName`, defaulting to `default`). A flat
key scan finds nothing there and returns `"*"` -- and PutEvents is the one
`events` action a WORKLOAD makes, so that miss would be the silent deny in the
exact place it costs most. `_events_bus` reads it, with the same bounded gap
`_ssm_resource` records for a batch `GetParameters`: a multi-bus PutEvents is
authorized against the FIRST entry's bus.

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
    if service == "kms":
        return _classify_kms(lower_headers, body)
    if service == "elasticache":
        return _classify_elasticache(body)
    if service == "rds":
        return _classify_rds(body)
    if service == "elasticloadbalancing":
        return _classify_elbv2(body)
    if service == "events":
        return _classify_events(lower_headers, body)
    if service == "route53":
        return _classify_route53(method, path, body)
    if service == "elasticfilesystem":
        return _classify_efs(method, path, query, body)
    # apigateway (v0.8.19). ONE branch for both API Gateway v1 and v2 -- they
    # share this credential scope; see `_APIGW_ROUTES` below.
    if service == "apigateway":
        return _classify_apigateway(method, path)
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


def _rds_resource(params: dict[str, str]) -> str:
    """The bare DB-instance IDENTIFIER -- which for an `rds` canvas node IS
    its label (agent/hcl.py's `_rds` builder emits `identifier = <label>`), so
    an `rds:DescribeDBInstances` edge drawn to that node
    gates through the ordinary `evaluate(statements, action, resource)` path
    with no rds-specific plumbing (the same identity rule s3's bucket, sqs's
    queue name and a log group's name already carry). The tag calls carry a
    full ARN in `ResourceName` instead -- reduced to the same bare identifier,
    the way `_sns_resource`/`_ecr_resource` strip theirs."""
    identifier = params.get("DBInstanceIdentifier")
    if identifier:
        return identifier
    resource_name = params.get("ResourceName", "")
    _prefix, sep, name = resource_name.rpartition(":db:")
    if sep and name:
        return name
    return "*"


def _classify_rds(body: bytes) -> tuple[str, str] | None:
    """RDS is the query protocol (form-encoded `Action=`), like sns/ec2/iam --
    not a JSON target header. Never returns None for a request that carries an
    `Action`: the fallback resource is `"*"` (the operator-only reasoning
    ec2/iam/ecr/ecs already use), so a `tofu apply`'s bare
    `DescribeDBInstances` is never denied as unmappable."""
    try:
        params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return None
    action_name = params.get("Action")
    if not action_name:
        return None
    return f"rds:{action_name}", _rds_resource(params)


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


def _kms_resource(payload: dict) -> str:
    """The bare KEY ID -- which for a `kms` canvas node IS its label, because
    `gateway/models/kmsctl.py` keys a key by the `odin:node` tag rather than
    minting a uuid (its deviation 1, which exists so this function can exist:
    a uuid would need a store lookup classify has no access to).

    Every form a `KeyId` arrives in reduces the same way `kmsctl.bare_key_id`
    reduces it -- kept in lock-step, and
    `tests/gateway/test_kmsctl.py::test_classify_and_model_agree_on_every_key_id_form`
    pins the two against each other rather than trusting this comment.
    `CreateKey` carries no KeyId at all and its identity is the tag, so that one
    resolves through `_kms_tag_key`; `ListKeys` names nothing and falls back to
    `"*"` -- never None, so the OPERATOR (tofu) is never denied via
    `unmappable-action`.
    """
    value = payload.get("KeyId")
    if isinstance(value, str) and value:
        tail = value.rpartition(":key/")[2] or value
        return tail.removeprefix("alias/")
    return _kms_tag_key(payload)


def _kms_tag_key(payload: dict) -> str:
    """CreateKey's identity: the `odin:node` tag `agent/hcl.py` stamps. KMS
    spells a tag `{"TagKey": ..., "TagValue": ...}`, NOT the `Key`/`Value` every
    other service modeled here uses -- read the common spelling and every
    `CreateKey` classifies to `"*"`, which the operator's wildcard still allows,
    so nothing breaks until someone draws an iam edge. That is the
    DynamoDB-Streams trap `_events_resource` already carries a paragraph about;
    both spellings come from botocore's own model, not from memory."""
    tags = payload.get("Tags")
    for entry in tags if isinstance(tags, list) else []:
        if isinstance(entry, dict) and entry.get("TagKey") == "odin:node":
            return str(entry.get("TagValue") or "*")
    return "*"


def _classify_kms(lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    """KMS shares ECR's JSON-target wire shape -- but its `X-Amz-Target` prefix
    is `TrentService`, KMS's internal name, NOT `kms` (verified against
    botocore's own `kms` model: protocol `json`, jsonVersion 1.1, targetPrefix
    `TrentService`, endpointPrefix `kms`). Only the endpointPrefix reaches this
    module (it is the SigV4 credential-scope name), and the op is taken from the
    part AFTER the dot, so the prefix's spelling never has to be hardcoded."""
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"kms:{op}", _kms_resource(payload)


def _elbv2_name(value: str) -> str:
    """The bare NAME an elbv2 ARN carries -- kept in lock-step with
    `gateway/models/elbv2ctl.py::name_from_arn`. A `loadbalancer/app/{name}/
    {id}` or `listener/app/{lb}/{lbid}/{id}` ARN yields the LOAD BALANCER's
    name (a listener isn't a canvas node of its own -- one `alb` node expands
    to lb + target group + listener, so all three classify to the same label);
    `targetgroup/{name}/{id}` yields the target group's. A value that isn't an
    ARN comes back unchanged."""
    tail = value.rsplit(":", 1)[-1]
    parts = tail.split("/")
    if parts[0] in ("loadbalancer", "listener") and len(parts) >= 3:
        return parts[2]
    if parts[0] == "targetgroup" and len(parts) >= 2:
        return parts[1]
    return value


# The id-carrying params, MOST SPECIFIC FIRST: a create call carries `Name`;
# everything else carries one of these ARNs, in singular or `.member.1` list
# form (the provider's own reads use the LIST spellings --
# `DescribeLoadBalancers(LoadBalancerArns=[...])` etc.). Target-group params
# precede load-balancer ones so a target-group read filtered BY a load balancer
# (`DescribeTargetGroups(LoadBalancerArn=...)`) still reports the group it's
# actually about. `ResourceArns.member.1` is last: it's the ARN-only tag API,
# which never carries a typed id at all.
_ELBV2_ARN_PARAMS = (
    "TargetGroupArn", "TargetGroupArns.member.1",
    "ListenerArn", "ListenerArns.member.1",
    "LoadBalancerArn", "LoadBalancerArns.member.1",
    "ResourceArns.member.1",
)


def _elbv2_resource(params: dict[str, str]) -> str:
    """The bare load-balancer / target-group name, in the OPERATOR-only style
    `_classify_iam`/`_classify_ecr`/`_classify_ecs` already use: extract a real
    value when the request carries one, `"*"` otherwise -- never None, so the
    operator (tofu) is never denied via `unmappable-action`. An `alb` canvas
    node's label IS its load-balancer name (`agent/hcl.py`'s `_alb` emits
    `name = <label>`), so this value is also what an iam edge's compiled
    statement would name -- but note the deliberate design choice in
    `ui/src/lib/iam.ts`: `alb` is NOT an IAM target on the canvas (nothing a
    workload "calls" on a load balancer; you send it HTTP, which no IAM policy
    gates), so in practice only the operator ever reaches these actions."""
    name = params.get("Name")
    if name:
        return name
    for key in _ELBV2_ARN_PARAMS:
        value = params.get(key)
        if value:
            return _elbv2_name(value)
    names = params.get("Names.member.1")
    return names if names else "*"


def _classify_elbv2(body: bytes) -> tuple[str, str] | None:
    """elbv2 is the query protocol like sns/ec2/iam -- the operation rides in
    the `Action` form param, NOT an `X-Amz-Target` header (verified against
    botocore's own `elbv2` model: `protocol: query`). Its list serialization is
    AWS's standard `Prefix.member.N`, unlike EC2's `Prefix.N`."""
    try:
        params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        return None
    action_name = params.get("Action")
    if not action_name:
        return None
    return f"elasticloadbalancing:{action_name}", _elbv2_resource(params)


# EventBridge's default event bus -- the one every rule lands on when a caller
# names none, and what an entry-less `PutEvents` is authorized against. Kept in
# lock-step with `gateway/models/eventsctl.py::DEFAULT_BUS`.
EVENTS_DEFAULT_BUS = "default"

# The rule/bus identifier members, MOST SPECIFIC FIRST. No two of these ever
# co-occur on one request (verified against botocore's `events` input shapes),
# so the order is documentation rather than tie-breaking:
#   Rule        -- PutTargets / RemoveTargets / ListTargetsByRule
#   Name        -- Put/Delete/Describe/Enable/DisableRule, and Create/Delete/
#                  DescribeEventBus (where the resource IS the bus)
#   ResourceARN -- TagResource / UntagResource / ListTagsForResource. CAPITAL
#                  ARN: this is the spelling that makes the difference between
#                  a real name and a silent `"*"` (module docstring).
#   NamePrefix  -- the LIST ops' only identifier, the same last-resort
#                  `_logs_resource` gives `logGroupNamePrefix`.
_EVENTS_ID_MEMBERS = ("Rule", "Name", "ResourceARN", "NamePrefix")


def _events_bare_name(value: str) -> str:
    """The bare RULE or EVENT BUS name -- kept in lock-step with
    `gateway/models/eventsctl.py::bare_name`.

    `arn:aws:events:…:rule/nightly` -> `nightly`; a custom-bus rule ARN
    (`…:rule/{bus}/{rule}`) and a bus ARN (`…:event-bus/{bus}`) reduce the same
    way, because in all three the name is the last `/`-segment. A value that
    isn't an ARN comes back UNCHANGED, which is safe rather than lucky:
    EventBridge rule and bus names are `[\\.\\-_A-Za-z0-9]+` (no slash), so a
    bare name has no `/`-segment to lose -- and `EventBusName` really is
    documented as "the name OR ARN of the event bus", so both forms arrive."""
    return value.rsplit("/", 1)[-1] if value.startswith("arn:") else value


def _events_bus(payload: dict) -> str:
    """`PutEvents`' resource: the EVENT BUS its first entry names.

    Not a top-level member -- `Entries[].EventBusName` -- which is the whole
    reason this has its own function instead of another `_EVENTS_ID_MEMBERS`
    entry (module docstring). An entry that names no bus, or a request with no
    usable entries at all, is the DEFAULT bus, matching EventBridge itself."""
    entries = payload.get("Entries")
    first = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
    name = first.get("EventBusName")
    return _events_bare_name(name) if isinstance(name, str) and name else EVENTS_DEFAULT_BUS


def _events_resource(op: str, payload: dict) -> str:
    """The bare rule name (or bus name, for the bus/PutEvents ops), in the
    never-None style every service since `_logs_resource` uses: a real value
    when the request carries one, `"*"` otherwise."""
    if op == "PutEvents":
        return _events_bus(payload)
    value = _first_str(payload, *_EVENTS_ID_MEMBERS)
    return _events_bare_name(value) if value else "*"


def _classify_events(lower_headers: dict[str, str], body: bytes) -> tuple[str, str] | None:
    """EventBridge: ECR's JSON-target wire shape, `X-Amz-Target: AWSEvents.*`."""
    target = lower_headers.get("x-amz-target")
    if target is None or "." not in target:
        return None
    op = target.rsplit(".", 1)[1]
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"events:{op}", _events_resource(op, payload)


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
    # Event source mappings -- `aws_lambda_event_source_mapping`, the SQS ->
    # Lambda trigger. Method+path taken from botocore's OWN lambda model
    # (printed, not remembered). The `{uuid}` group is named `mapping` rather
    # than `name` on purpose: `name` would make the loop below hand the UUID to
    # `lambdactl` as a FUNCTION name, and every one of these would 404 against
    # a function that does not exist. Ordered before nothing and after
    # everything: `/2015-03-31/event-source-mappings` cannot collide with
    # `/2015-03-31/functions/...`, the segment after the version differs.
    ("POST", re.compile(r"^/2015-03-31/event-source-mappings$"), "CreateEventSourceMapping"),
    ("GET", re.compile(r"^/2015-03-31/event-source-mappings$"), "ListEventSourceMappings"),
    ("GET", re.compile(r"^/2015-03-31/event-source-mappings/(?P<mapping>[^/]+)$"), "GetEventSourceMapping"),
    ("PUT", re.compile(r"^/2015-03-31/event-source-mappings/(?P<mapping>[^/]+)$"), "UpdateEventSourceMapping"),
    ("DELETE", re.compile(r"^/2015-03-31/event-source-mappings/(?P<mapping>[^/]+)$"), "DeleteEventSourceMapping"),
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
        if "mapping" in groups:
            # An event source mapping's identity is its UUID, and it is NOT a
            # function name: the mapping outlives no function and a policy
            # written against a function name must not accidentally match one.
            # A workload principal therefore denies these by ordinary
            # default-deny (no iam edge grants a mapping UUID), which is right
            # -- creating triggers is an operator action.
            return f"lambda:{op}", unquote(groups["mapping"])
        # CreateFunction / CreateEventSourceMapping: the path carries no name
        # -- read it from the body, same technique _classify_ecr uses for
        # repositoryName. `FunctionName` is documented as the name, the partial
        # ARN or the full ARN, and terraform sends the full ARN for a mapping
        # (`aws_lambda_function.x.arn`), so it reduces to the bare name here --
        # otherwise the same function is one resource under CreateFunction and
        # a different one under CreateEventSourceMapping, and no policy could
        # be written to cover both.
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        name = payload.get("FunctionName")
        if not isinstance(name, str) or not name:
            return f"lambda:{op}", "*"
        return f"lambda:{op}", name.rsplit(":", 1)[-1] if name.startswith("arn:") else name
    return None


# EFS, the SECOND rest-json service odin models (Lambda is the first), so it
# routes by method+path exactly like `_LAMBDA_ROUTES` above and not by an
# X-Amz-Target header or a query-protocol `Action` param. Verified against
# botocore's own efs model: `protocol: rest-json`, `endpointPrefix:
# elasticfilesystem`, `targetPrefix: None`.
#
# SEVEN routes and no more, because seven is what a real `tofu apply` + `plan` +
# `destroy` over `aws_efs_file_system` + `aws_efs_access_point` was MEASURED
# calling (OpenTofu 1.12.3 / hashicorp/aws 6.57.1, against a recording
# endpoint). `DescribeBackupPolicy`, `DescribeFileSystemPolicy`,
# `ListTagsForResource`, `DescribeMountTargets` and `TagResource` are never
# called at all -- a route for one of those would be a permission nothing can
# exercise, which is the decorative thing this repo keeps deleting.
#
# The action prefix is `elasticfilesystem:` -- the SIGNING NAME, which is also
# AWS's real IAM namespace, and the same rule every other branch in this file
# keeps (`elasticloadbalancing:`, `secretsmanager:`, `elasticache:`, `kms:` are
# all their own signing names). An earlier draft emitted a short `efs:`, and
# MEASURING it is what settled the question rather than taste:
#
#   arn_label("arn:aws:elasticfilesystem:...:file-system/fs-...", "efs:Describe...")
#
# `policy.py::arn_label` compares the ARN's OWN service field against
# `action.partition(":")[0]` and returns None on a mismatch BEFORE it ever looks
# up a resource pattern -- so an `efs:` prefix could never match an ARN-form
# `Resource`, no matter what `_ARN_RESOURCE_LABEL` entry anyone added later. A
# permission the classifier emits that nothing can grant is this repo's
# ecr/rds/kms bug, planted fresh. `elasticfilesystem:` is merely not-yet-wired
# there (no efs IAM edges exist -- see the tile's `iamActions` note), which is a
# gap that can be closed; a dead end is not.
_EFS_ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/2015-02-01/file-systems$"), "CreateFileSystem"),
    ("GET", re.compile(r"^/2015-02-01/file-systems$"), "DescribeFileSystems"),
    (
        "GET",
        re.compile(r"^/2015-02-01/file-systems/(?P<fs>[^/]+)/lifecycle-configuration$"),
        "DescribeLifecycleConfiguration",
    ),
    ("DELETE", re.compile(r"^/2015-02-01/file-systems/(?P<fs>[^/]+)$"), "DeleteFileSystem"),
    ("POST", re.compile(r"^/2015-02-01/access-points$"), "CreateAccessPoint"),
    ("GET", re.compile(r"^/2015-02-01/access-points$"), "DescribeAccessPoints"),
    ("DELETE", re.compile(r"^/2015-02-01/access-points/(?P<ap>[^/]+)$"), "DeleteAccessPoint"),
)

# Which request field names the resource, per operation, when the PATH does not.
# A create carries it in the body; a describe carries it in the querystring
# (rest-json puts a `querystring` location on those members), which is why
# `_classify_efs` needs `query` at all.
_EFS_BODY_RESOURCE = {"CreateFileSystem": "CreationToken", "CreateAccessPoint": "FileSystemId"}
_EFS_QUERY_RESOURCE = {
    "DescribeFileSystems": ("FileSystemId", "CreationToken"),
    "DescribeAccessPoints": ("AccessPointId", "FileSystemId"),
}


def _classify_efs(method: str, path: str, query: dict[str, str], body: bytes) -> tuple[str, str] | None:
    for route_method, pattern, op in _EFS_ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if match is None:
            continue
        groups = match.groupdict()
        path_id = groups.get("fs") or groups.get("ap")
        return f"elasticfilesystem:{op}", unquote(path_id) if path_id else _efs_resource(op, query, body)
    return None


def _efs_resource(op: str, query: dict[str, str], body: bytes) -> str:
    """The resource a create/describe names, or `*` for an unfiltered list.

    `*` for a list-all is the same choice `_classify_lambda` makes for a
    nameless CreateFunction, and it means the same thing here: the request is
    genuinely not about one resource, so a policy that grants one must not match
    it. An unscoped `DescribeFileSystems()` is a different permission from
    reading the file system an edge points at -- the exact distinction field
    test 2 cost an engineer an hour over on rds."""
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    body_key = _EFS_BODY_RESOURCE.get(op)
    if body_key:
        value = payload.get(body_key) if isinstance(payload, dict) else None
        return _efs_bare(value) if isinstance(value, str) and value else "*"
    for key in _EFS_QUERY_RESOURCE.get(op, ()):
        if query.get(key):
            return _efs_bare(unquote(query[key]))
    return "*"


def _efs_bare(value: str) -> str:
    """`arn:aws:elasticfilesystem:...:file-system/fs-x` -> `fs-x`; anything
    without a `/` (a bare id, a creation token) passes through unchanged. Every
    EFS id member accepts both forms -- its own botocore patterns spell out the
    ARN alternative -- so a policy written against one must match the other."""
    return value.rpartition("/")[2] or value


# --- apigateway (v0.8.19) --------------------------------------------------
#
# THE CREDENTIAL SCOPE IS `apigateway`, FOR BOTH v1 AND v2. Measured, not
# assumed: real terraform-provider-aws 5.100.0 creating an
# `aws_apigatewayv2_api` signs with
# `Credential=probe/20260803/us-east-1/apigateway/aws4_request`. botocore agrees
# from the other side -- the `apigatewayv2` service model's `endpointPrefix` is
# `apigateway` and its `signingName` is `apigateway`. Since `gateway/app.py`
# reads the service from the credential scope and NOTHING else, a reader who
# "corrects" this to `apigatewayv2` because that is the SDK's name breaks every
# call and gets `unmappable-action` for their trouble. This comment is here
# rather than only in the model because THIS is the file that dispatches on it.
#
# Shape is lambda's: REST, so (method, path) against an anchored table. Same
# OPERATOR-only reasoning as ec2/iam/ecr/lambda -- the only principal driving
# apigateway calls is tofu, so extraction only needs to never return None for a
# route it recognizes. The resource is the API ID (a bare `apiXXXXXXXX`) rather
# than a canvas label: an id is what every path carries, and the API's label is
# recoverable from its `odin:node` tag when anything needs it. No workload IAM
# edge targets an apigateway node (`ui/src/lib/iam.ts` grants it no actions), so
# nothing depends on the resource being a label -- exactly the `alb` precedent.
_APIGW_ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/v2/apis$"), "CreateApi"),
    ("GET", re.compile(r"^/v2/apis$"), "GetApis"),
    ("GET", re.compile(r"^/v2/apis/(?P<api>[^/]+)$"), "GetApi"),
    ("PATCH", re.compile(r"^/v2/apis/(?P<api>[^/]+)$"), "UpdateApi"),
    ("DELETE", re.compile(r"^/v2/apis/(?P<api>[^/]+)$"), "DeleteApi"),
    ("POST", re.compile(r"^/v2/apis/(?P<api>[^/]+)/integrations$"), "CreateIntegration"),
    ("GET", re.compile(r"^/v2/apis/(?P<api>[^/]+)/integrations/[^/]+$"), "GetIntegration"),
    ("PATCH", re.compile(r"^/v2/apis/(?P<api>[^/]+)/integrations/[^/]+$"), "UpdateIntegration"),
    ("DELETE", re.compile(r"^/v2/apis/(?P<api>[^/]+)/integrations/[^/]+$"), "DeleteIntegration"),
    ("POST", re.compile(r"^/v2/apis/(?P<api>[^/]+)/routes$"), "CreateRoute"),
    ("GET", re.compile(r"^/v2/apis/(?P<api>[^/]+)/routes/[^/]+$"), "GetRoute"),
    ("PATCH", re.compile(r"^/v2/apis/(?P<api>[^/]+)/routes/[^/]+$"), "UpdateRoute"),
    ("DELETE", re.compile(r"^/v2/apis/(?P<api>[^/]+)/routes/[^/]+$"), "DeleteRoute"),
    ("POST", re.compile(r"^/v2/apis/(?P<api>[^/]+)/stages$"), "CreateStage"),
    # The stage's own segment is `.+` and not `[^/]+`: a stage NAME is the id,
    # and a name containing a slash should reach the model's own error rather
    # than become `unmappable-action`, which would blame authorization for a
    # naming problem. The name odin has to serve is the literal `$default`, and
    # it arrives PERCENT-ENCODED as `%24default` -- measured on the raw path,
    # after a first measurement taken off Starlette's already-decoded
    # `request.url.path` said the opposite and produced a real bug
    # (`apigwctl.path_ids` records it).
    ("GET", re.compile(r"^/v2/apis/(?P<api>[^/]+)/stages/.+$"), "GetStage"),
    ("PATCH", re.compile(r"^/v2/apis/(?P<api>[^/]+)/stages/.+$"), "UpdateStage"),
    ("DELETE", re.compile(r"^/v2/apis/(?P<api>[^/]+)/stages/.+$"), "DeleteStage"),
)


def _classify_apigateway(method: str, path: str) -> tuple[str, str] | None:
    for route_method, pattern, op in _APIGW_ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if match is None:
            continue
        return f"apigateway:{op}", unquote(match.groupdict().get("api") or "*")
    return None


# ROUTE 53 IS LAMBDA'S SHAPE, NOT ELBV2'S: rest-xml is still REST, so it routes
# on (method, path) exactly like `_LAMBDA_ROUTES` above, and every pattern is
# `$`-anchored so no two can match the same pair. Method + requestUri MEASURED
# per operation off botocore's own `route53` model and confirmed against
# captured request bytes -- not remembered.
#
# THE COLLISION this anchoring is load-bearing for: `/2013-04-01/hostedzone` is
# CreateHostedZone on POST and ListHostedZones on GET, and
# `/2013-04-01/tags/{type}/{id}` is Change on POST and List on GET. METHOD is
# what separates each pair, so the paths must not be allowed to overlap further.
#
# THE TRAILING SLASH ON `/rrset` IS OPTIONAL, AND THAT IS A MEASURED CORRECTION
# RATHER THAN A CONVENIENCE. botocore's `requestUri` for ChangeResourceRecordSets
# really is `.../rrset/` WITH a slash while ListResourceRecordSets is `.../rrset`
# without one, and boto3 puts exactly that on the wire -- so the first cut of
# this table required the slash on the write route. The REAL terraform provider
# does not send it. Measured, driving a real `tofu apply` (provider 5.100.0,
# which uses aws-sdk-go-v2, not botocore) against this gateway:
#
#     POST /2013-04-01/hostedzone/odin.internal/rrset   -> UNMAPPABLE
#     Error: creating Route53 Record: ... StatusCode: 403,
#       api error AccessDenied: User is not authorized to perform: unmappable-action
#
# The zone had already been created; the record killed the apply. Two SDKs spell
# the same operation differently, and modelling only the one that is easy to
# capture from python is how a service passes its own tests and then fails the
# only client that matters. Accepting both spellings costs nothing here: the two
# rrset routes differ by METHOD, so no widening of the path can make them
# collide, and each is still `$`-anchored against every other route.
#
# `{zone}` is a hosted zone id, and deviation 1 in `models/route53ctl.py` is
# what makes it the thing an IAM policy is written against: the zone id IS the
# domain name, so `route53ctl.zone_id` reduces every spelling of it here with no
# store access at all. `{change}` deliberately is NOT a zone -- see below.
_ROUTE53_ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/2013-04-01/hostedzone$"), "CreateHostedZone"),
    ("GET", re.compile(r"^/2013-04-01/hostedzone$"), "ListHostedZones"),
    ("GET", re.compile(r"^/2013-04-01/hostedzonesbyname$"), "ListHostedZonesByName"),
    ("GET", re.compile(r"^/2013-04-01/hostedzone/(?P<zone>[^/]+)$"), "GetHostedZone"),
    ("DELETE", re.compile(r"^/2013-04-01/hostedzone/(?P<zone>[^/]+)$"), "DeleteHostedZone"),
    ("POST", re.compile(r"^/2013-04-01/hostedzone/(?P<zone>[^/]+)/rrset/?$"), "ChangeResourceRecordSets"),
    ("GET", re.compile(r"^/2013-04-01/hostedzone/(?P<zone>[^/]+)/rrset/?$"), "ListResourceRecordSets"),
    ("GET", re.compile(r"^/2013-04-01/change/(?P<change>[^/]+)$"), "GetChange"),
    ("POST", re.compile(r"^/2013-04-01/tags/hostedzone/(?P<zone>[^/]+)$"), "ChangeTagsForResource"),
    ("GET", re.compile(r"^/2013-04-01/tags/hostedzone/(?P<zone>[^/]+)$"), "ListTagsForResource"),
)

# The `<Name>` a CreateHostedZone carries in its rest-xml body -- the ONE route
# above whose path has no zone in it, so the resource has to come from the body
# (the same technique `_classify_lambda` uses for CreateFunction's FunctionName
# and `_classify_ecr` for repositoryName). Matched rather than XML-parsed
# because `classify` runs before any handler and must never raise on a body a
# caller malformed; a body with no Name at all classifies as `"*"` and the
# handler answers the real complaint.
_ROUTE53_NAME = re.compile(rb"<Name>([^<]*)</Name>")


def _route53_zone(value: str) -> str:
    """The bare zone id out of every spelling one arrives in.

    A DUPLICATE of `models/route53ctl.py::zone_id`, deliberately, and for the
    same reason `_kms_resource` duplicates `kmsctl.bare_key_id`: this module
    imports NOTHING from odin, and keeping it a leaf is worth more than sharing
    three lines -- `route53ctl` pulls in `gateway/stores.py`, `gateway/kms.py`
    and `spec/store.py` behind it, none of which classification needs.

    The cost of duplication is drift, and drift here is silent and expensive:
    if create stores one spelling while classify reports another, an IAM edge
    to that zone DENIES with no explanation. So unlike the kms pair, this one is
    not left to prose -- `tests/gateway/test_route53ctl.py::
    test_classify_and_model_agree_on_every_zone_id_spelling` asserts the two
    functions agree over a table of forms, and fails if either moves."""
    tail = value.rpartition("/hostedzone/")[2] or value
    return tail.strip("/").rstrip(".")


def _classify_route53(method: str, path: str, body: bytes) -> tuple[str, str] | None:
    for route_method, pattern, op in _ROUTE53_ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if match is None:
            continue
        groups = match.groupdict()
        if "zone" in groups:
            return f"route53:{op}", _route53_zone(unquote(groups["zone"]))
        if "change" in groups:
            # A change id is NOT a zone, and must not be reported as one: a
            # policy written against `odin.internal` must not accidentally match
            # a GetChange for some other zone's change. Same reasoning as
            # `_classify_lambda`'s event-source-mapping UUID -- polling a change
            # is an OPERATOR action (tofu's own waiter), and the operator's
            # full-allow statement covers it, while a workload principal denies
            # it by ordinary default-deny rather than by `unmappable-action`.
            return f"route53:{op}", unquote(groups["change"])
        found = _ROUTE53_NAME.search(body or b"")
        name = found.group(1).decode(errors="replace") if found else ""
        return f"route53:{op}", _route53_zone(name) if name else "*"
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
