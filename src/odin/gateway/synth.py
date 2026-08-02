"""The gateway's synthesized control-plane -- the AWS calls the substitutes
can't answer, per research §§3-5 (docs/superpowers/research/research-tofu-provider.md,
build-order items 2-5): STS identity, tag CRUD, SNS topic attributes, SQS/SNS
delete-confirmation fidelity, and the CreateQueue host rewrite.

Every response shape below was verified by round-tripping candidate bytes
through botocore's OWN protocol parser against the real service models
(sqs/dynamodb=JSON, sns/sts=query-XML) -- not guessed at. The tag-CRUD and
SNS-attribute logic ports the research prototype's `proxy.py` gap-filler
(validated by a real `tofu apply` -> zero-drift `plan` -> `destroy`), with
two deliberate improvements beyond it: SNS tags are a REAL store here (the
prototype's `ListTagsForResource`/`TagResource`/`UntagResource` were no-op
stubs -- always-empty reads, discarded writes), and DynamoDB gains the same
tag store (the prototype had none for dynamodb at all -- research's own
table still lists dynamodb tags as "⚠ drift"). Both gaps are closed here
because the brief's ask is a REAL per-env store for all three services, not
just sqs/sns.

Three call shapes, dispatched by `app.py`:
  - `get_caller_identity` -- STS, handled OUTSIDE classify()/evaluate()
    entirely (verify() is the only gate: GetCallerIdentity isn't scoped to
    any canvas resource, so there's no applied statement that could
    ever grant/deny it -- matching real AWS, where it needs no IAM policy).
  - `pure_answer` -- a `_PURE_HANDLERS` action never reaches a backing
    (goaws/dynalite lack or mishandle them entirely): tag CRUD, SNS
    Get/SetTopicAttributes, SQS GetQueueAttributes (always synth-owned -- see
    its docstring for why).

    WHAT "PURE" MEANS HERE, because the word is misleading and the misreading
    is dangerous: it means NO FORWARD TO A BACKING -- the gateway answers this
    call itself. It emphatically does NOT mean side-effect-free. `pure_answer`
    dispatches `ec2:RunInstances` (boots a REAL Lima VM), `ecs:*` (real
    containers), `rds:*` (a real Postgres container), `elasticache:*` (a real
    Redis container) and `elasticloadbalancing:*` (a real nginx container), and
    every model writes to a real per-env store. "Pure" is a statement about the
    REQUEST PATH, never about the effects. The name is kept only because it is
    load-bearing in ~200 places including 14 model modules; read it as
    "synth-owned".
    Also covers the one CONDITIONAL action, SNS GetSubscriptionAttributes:
    returns None (meaning "not synth-owned for THIS call, forward normally")
    unless the subscription was already Unsubscribed -- a LIVE subscription
    forwards to goaws for real and gets patched by `postprocess` instead
    (below): a real `tofu apply` through a real gateway (S2) found goaws's
    live answer includes `FilterPolicy = "null"` (the literal string) for
    every unset optional attribute, which the TF provider reads as a REAL
    value and drifts on every subsequent plan -- `_sns_fix_subscription_attributes`
    strips it, closing the "goaws returns incomplete attrs -> drift; GW
    completes" half of research §3 that S1 left as a live-forward pass-through.
  - `postprocess` -- a `_POSTPROCESS_HANDLERS` action forwards for real (the
    create/delete/read must actually happen in goaws/dynalite) but the gateway also
    observes or reshapes the response: CreateQueue's host rewrite +
    attribute/tag seeding, CreateTopic's attribute/tag seeding, DeleteQueue/
    Unsubscribe's delete markers, CreateTable's tag seeding (dynalite
    accepts-but-drops tags on create -- the exact drift research documents),
    and (S2) a live GetSubscriptionAttributes' FilterPolicy fixup above.

Called ONLY after evaluate() allows the request (app.py's routing order:
verify -> scope -> classify -> evaluate -> synth -> forward) -- a synth
answer is never a way around policy; STS is the sole, deliberate exception,
justified above.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.keys import KeyStore, Principal
from odin.gateway.models import (
    cachectl,
    ec2compute,
    ecr,
    ecsctl,
    elbv2ctl,
    eventsctl,
    iamctl,
    lambdactl,
    logsctl,
    rdsctl,
    s3notify,
    secretsctl,
    ssmctl,
)
from odin.gateway.stores import SynthStores

_SNS_NS = "http://sns.amazonaws.com/doc/2010-03-31/"
_STS_NS = "https://sts.amazonaws.com/doc/2011-06-15/"

# The gateway must serve deleted-queue attributes for a short window after
# DeleteQueue rather than 400 immediately -- research's "one unresolved
# edge": swapping the error code alone still fails the provider's delete
# waiter, because it expects the transitional "still readable, then gone"
# shape real AWS has. Not real AWS's ~60s (no test should sleep that long);
# just long enough that a caller polling immediately after delete still
# briefly sees the queue before it goes to not-found.
QUEUE_DELETE_GRACE_SECONDS = 2.0

# The REAL wire error code, verified against terraform-provider-aws's own
# source (internal/service/sqs/consts.go: `errCodeQueueDoesNotExist =
# "AWS.SimpleQueueService.NonExistentQueue"`) after a real `tofu destroy`
# through the real gateway kept erroring despite the delete-grace window
# above (S2): botocore models this exception under the friendlier shape
# name `QueueDoesNotExist`, but that is NOT what SQS actually sends over
# the wire -- SQS predates the newer error-code-matches-shape-name
# convention, and aws-sdk-go-v2 (what the TF provider uses) checks the
# legacy string literally. Sending botocore's shape name instead of the
# real wire code makes the provider's `tfawserr.ErrCodeEquals` check MISS,
# so it never recognizes "gone" as the delete-waiter's target state and
# treats the response as a genuine unretryable error instead.
_SQS_QUEUE_DOES_NOT_EXIST = "AWS.SimpleQueueService.NonExistentQueue"

_TOPIC_ATTRIBUTE_DEFAULTS = {
    "SubscriptionsConfirmed": "0",
    "SubscriptionsPending": "0",
    "SubscriptionsDeleted": "0",
    "DisplayName": "",
    "EffectiveDeliveryPolicy": json.dumps({"http": {"defaultHealthyRetryPolicy": {"numRetries": 3}}}),
    "Policy": json.dumps({"Version": "2008-10-17", "Id": "__default_policy_ID", "Statement": []}),
}


def get_caller_identity(env: str, principal: Principal) -> Response:
    """Who the caller is, per `backings.ACCOUNT` -- the SINGLE account id in
    the system, deliberately the same one every ARN odin builds carries.

    There used to be a second one here: `account_for_env(env)`, a per-env
    sha256-derived id, used for this field alone while QueueArn/TopicArn/
    secret/log-group ARNs all used `ACCOUNT`. The v0.7.0 field test (U6) found
    what that costs a real workload: ask STS who you are, build an ARN from
    the answer -- the ordinary pattern -- and you build an ARN odin will never
    match. Unified toward `ACCOUNT` rather than the other way because nothing
    in odin needs per-env account ids (envs are already isolated by their own
    stores and backing containers), because ~15 modules and ~28 test files
    bake `ACCOUNT` into ARNs, and because the TF provider never notices either
    way -- `simulate/workspace.py` sets `skip_requesting_account_id = true`,
    so STS's answer is read by workload callers only."""
    arn = f"arn:aws:iam::{ACCOUNT}:user/{principal.node_id}"
    xml = (
        f'<GetCallerIdentityResponse xmlns="{_STS_NS}"><GetCallerIdentityResult>'
        f"<UserId>{principal.node_id}</UserId><Account>{ACCOUNT}</Account><Arn>{arn}</Arn>"
        f"</GetCallerIdentityResult>{_response_metadata_xml()}</GetCallerIdentityResponse>"
    )
    return Response(xml, media_type="text/xml")


# --- shared wire-format helpers ---------------------------------------------


def _response_metadata_xml() -> str:
    return "<ResponseMetadata><RequestId>00000000-0000-0000-0000-000000000000</RequestId></ResponseMetadata>"


def _sns_result_xml(op: str, result_xml: str = "") -> str:
    return f'<{op}Response xmlns="{_SNS_NS}"><{op}Result>{result_xml}</{op}Result>{_response_metadata_xml()}</{op}Response>'


def _parse_map(params: dict[str, str], prefix: str) -> dict[str, str]:
    """`prefix.entry.N.key`/`prefix.entry.N.value` -> a flat dict (AWS's
    query-protocol Map<string,string> serialization, e.g. SNS CreateTopic's
    `Attributes` param)."""
    indexed: dict[int, dict[str, str]] = {}
    for param_key, value in params.items():
        if not param_key.startswith(f"{prefix}.entry."):
            continue
        _prefix, _entry, index, field = param_key.split(".", 3)
        indexed.setdefault(int(index), {})[field] = value
    return {item["key"]: item["value"] for item in indexed.values()}


def _parse_struct_list(params: dict[str, str], prefix: str) -> list[dict[str, str]]:
    """`prefix.member.N.Field` -> a list of dicts (AWS's query-protocol
    List<Structure> serialization, e.g. SNS's `Tags` param)."""
    indexed: dict[int, dict[str, str]] = {}
    for param_key, value in params.items():
        if not param_key.startswith(f"{prefix}.member."):
            continue
        _prefix, _member, index, field = param_key.split(".", 3)
        indexed.setdefault(int(index), {})[field] = value
    return [indexed[i] for i in sorted(indexed)]


def _parse_scalar_list(params: dict[str, str], prefix: str) -> list[str]:
    """`prefix.member.N` -> a list of scalars (AWS's query-protocol
    List<String> serialization, e.g. SNS's `TagKeys` param)."""
    indexed: dict[int, str] = {}
    for param_key, value in params.items():
        if not param_key.startswith(f"{prefix}.member."):
            continue
        indexed[int(param_key.rsplit(".", 1)[-1])] = value
    return [indexed[i] for i in sorted(indexed)]


def _tags_from_structs(items: list[dict[str, str]]) -> dict[str, str]:
    return {item["Key"]: item["Value"] for item in items}


def _structs_from_tags(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def _sns_tag_members_xml(tags: dict[str, str]) -> str:
    return "".join(f"<member><Key>{key}</Key><Value>{value}</Value></member>" for key, value in tags.items())


def _sns_attribute_entries_xml(attributes: dict[str, str]) -> str:
    return "".join(f"<entry><key>{key}</key><value>{value}</value></entry>" for key, value in attributes.items())


def _tags_key(service: str, resource: str) -> str:
    return f"{service}:{resource}"


def _rewrite_host(url: str, host: str) -> str:
    if not url or not host:
        return url
    return urlunsplit(urlsplit(url)._replace(netloc=host))


# --- pure answers: SQS -------------------------------------------------------


def _sqs_list_tags(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    tags = stores.tags.get(env, _tags_key("sqs", resource), {})
    return Response(json.dumps({"Tags": tags}), media_type="application/x-amz-json-1.0")


def _sqs_tag_queue(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    payload = json.loads(body or b"{}")
    tags = {**stores.tags.get(env, _tags_key("sqs", resource), {}), **payload.get("Tags", {})}
    stores.tags.set(env, _tags_key("sqs", resource), tags)
    return Response("{}", media_type="application/x-amz-json-1.0")


def _sqs_untag_queue(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    payload = json.loads(body or b"{}")
    tags = dict(stores.tags.get(env, _tags_key("sqs", resource), {}))
    for key in payload.get("TagKeys", []):
        tags.pop(key, None)
    stores.tags.set(env, _tags_key("sqs", resource), tags)
    return Response("{}", media_type="application/x-amz-json-1.0")


def _sqs_get_queue_attributes(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """Always synth-owned, never forwarded (research build-order item 5/6,
    folded together here): goaws's own answer converges slowly (9 polls over
    ~31s) and, once DeleteQueue has run, goaws no longer HAS the queue to
    ask at all -- so the gateway needs its own attribute store regardless,
    and using it for the live case too is what makes the create-time waiter
    converge on the first poll (research §6)."""
    payload = json.loads(body or b"{}")
    state = stores.sqs_queues.get(env, resource, {"attributes": {}, "deleted_at": None})
    deleted_at = state.get("deleted_at")
    if deleted_at is not None and now - deleted_at > QUEUE_DELETE_GRACE_SECONDS:
        return errors.synth_error("sqs", _SQS_QUEUE_DOES_NOT_EXIST, "The specified queue does not exist.", 400)
    requested = payload.get("AttributeNames") or []
    attributes = state.get("attributes", {})
    if requested and "All" not in requested:
        attributes = {k: v for k, v in attributes.items() if k in requested}
    return Response(json.dumps({"Attributes": attributes}), media_type="application/x-amz-json-1.0")


# --- pure answers: SNS -------------------------------------------------------


def _sns_get_topic_attributes(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    params = dict(parse_qsl(body.decode("utf-8")))
    topic_arn = params.get("TopicArn", "")
    stored = stores.sns_topics.get(env, resource, {})
    attributes = {"TopicArn": topic_arn, "Owner": ACCOUNT, **_TOPIC_ATTRIBUTE_DEFAULTS, **stored}
    result = f"<Attributes>{_sns_attribute_entries_xml(attributes)}</Attributes>"
    return Response(_sns_result_xml("GetTopicAttributes", result), media_type="text/xml")


def _sns_set_topic_attributes(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    params = dict(parse_qsl(body.decode("utf-8")))
    stored = dict(stores.sns_topics.get(env, resource, {}))
    stored[params.get("AttributeName", "")] = params.get("AttributeValue", "")
    stores.sns_topics.set(env, resource, stored)
    return Response(_sns_result_xml("SetTopicAttributes"), media_type="text/xml")


def _sns_list_tags(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    tags = stores.tags.get(env, _tags_key("sns", resource), {})
    result = f"<Tags>{_sns_tag_members_xml(tags)}</Tags>"
    return Response(_sns_result_xml("ListTagsForResource", result), media_type="text/xml")


def _sns_tag_resource(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    params = dict(parse_qsl(body.decode("utf-8")))
    new_tags = _tags_from_structs(_parse_struct_list(params, "Tags"))
    tags = {**stores.tags.get(env, _tags_key("sns", resource), {}), **new_tags}
    stores.tags.set(env, _tags_key("sns", resource), tags)
    return Response(_sns_result_xml("TagResource"), media_type="text/xml")


def _sns_untag_resource(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    params = dict(parse_qsl(body.decode("utf-8")))
    keys_to_remove = _parse_scalar_list(params, "TagKeys")
    tags = dict(stores.tags.get(env, _tags_key("sns", resource), {}))
    for key in keys_to_remove:
        tags.pop(key, None)
    stores.tags.set(env, _tags_key("sns", resource), tags)
    return Response(_sns_result_xml("UntagResource"), media_type="text/xml")


def _sns_get_subscription_attributes(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response | None:
    """CONDITIONAL: only synth-owned once the subscription is gone (research:
    the SNS case needed no transitional grace window, unlike SQS -- "fully
    solved" by an immediate NotFound). Live subscriptions fall through
    (returns None) to a normal forward -- goaws answers those for real (then
    `_sns_fix_subscription_attributes`, a POSTPROCESS handler below, patches
    the one field goaws gets wrong)."""
    params = dict(parse_qsl(body.decode("utf-8")))
    subscription_arn = params.get("SubscriptionArn", "")
    if stores.sns_subscriptions.get(env, subscription_arn) is None:
        return None
    return errors.synth_error("sns", "NotFound", "Subscription does not exist", 404)


# --- pure answers: DynamoDB ---------------------------------------------------


def _ddb_list_tags(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    tags = stores.tags.get(env, _tags_key("dynamodb", resource), {})
    return Response(json.dumps({"Tags": _structs_from_tags(tags)}), media_type="application/x-amz-json-1.0")


def _ddb_tag_resource(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    payload = json.loads(body or b"{}")
    new_tags = _tags_from_structs(payload.get("Tags", []))
    tags = {**stores.tags.get(env, _tags_key("dynamodb", resource), {}), **new_tags}
    stores.tags.set(env, _tags_key("dynamodb", resource), tags)
    return Response("{}", media_type="application/x-amz-json-1.0")


def _ddb_untag_resource(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    payload = json.loads(body or b"{}")
    tags = dict(stores.tags.get(env, _tags_key("dynamodb", resource), {}))
    for key in payload.get("TagKeys", []):
        tags.pop(key, None)
    stores.tags.set(env, _tags_key("dynamodb", resource), tags)
    return Response("{}", media_type="application/x-amz-json-1.0")


_PureHandler = Callable[[str, str, bytes, SynthStores, float], Response | None]

_PURE_HANDLERS: dict[str, _PureHandler] = {
    "sqs:ListQueueTags": _sqs_list_tags,
    "sqs:TagQueue": _sqs_tag_queue,
    "sqs:UntagQueue": _sqs_untag_queue,
    "sqs:GetQueueAttributes": _sqs_get_queue_attributes,
    "sns:GetTopicAttributes": _sns_get_topic_attributes,
    "sns:SetTopicAttributes": _sns_set_topic_attributes,
    "sns:ListTagsForResource": _sns_list_tags,
    "sns:TagResource": _sns_tag_resource,
    "sns:UntagResource": _sns_untag_resource,
    "sns:GetSubscriptionAttributes": _sns_get_subscription_attributes,
    "dynamodb:ListTagsOfResource": _ddb_list_tags,
    "dynamodb:TagResource": _ddb_tag_resource,
    "dynamodb:UntagResource": _ddb_untag_resource,
    # The two S3 actions that must NEVER be forwarded: RustFS rejects every
    # PutBucketNotificationConfiguration ARN form with `InvalidArgument` and
    # persists the configuration anyway, so a forward makes `apply` fail,
    # `plan` read clean and nothing fire -- three answers that cannot all be
    # true (gateway/models/s3notify.py's docstring has the measured probe).
    "s3:PutBucketNotification": s3notify.put_notification,
    "s3:GetBucketNotification": s3notify.get_notification,
}


async def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    backing_port: int | None = None, query: dict[str, str] | None = None,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
    rds=None,
) -> Response | None:
    """A direct synth answer for a PURE/CONDITIONAL action, or None if
    `action` isn't synth-owned for this call -- the caller (app.py) forwards
    normally in that case.

    "PURE" = NEVER FORWARDED TO A BACKING. It does NOT mean side-effect-free,
    and this function is the loudest counter-example in odin: the branches
    below boot a real Lima VM (`ec2:RunInstances`), real containers
    (`ecs:*`, `rds:*`, `elasticache:*`, `elasticloadbalancing:*`) and write
    real per-env stores. See the module docstring.

    Every `ec2:*`/`iam:*`/`lambda:*`/`ecs:*` action is
    owned wholesale by its own model module(s) -- `ec2compute.py` (task V3:
    instances + key pairs, falling through to `ec2net.py`'s VPC/Subnet/SG for
    everything else), `iamctl.py`, `lambdactl.py` (task V4a), and
    `ecsctl.py` (task V5a) -- none has a backing container to forward to, so
    those paths never return None.
    `ecr:*` is likewise all-synth for its CONTROL plane, but (task V2b) needs
    the registry:2 backing's own live port to build `repositoryUri` --
    `backing_port` is app.py's existing `GatewayState.backing_port` lookup,
    threaded through here rather than forwarded (ECR's data plane, image
    bytes, bypasses the gateway entirely -- see gateway/models/ecr.py).
    `query` is app.py's already-parsed query-string dict, needed only by
    lambdactl.py's UntagResource (`TagKeys` rides the querystring on
    Lambda's REST wire, unlike every other service modeled here -- see its
    own docstring for the resulting v1 limitation). `ecs:*` (task V5a) is
    all-synth the same way, with its own REAL Colima-container substrate
    (`compute/tasks.py::TaskRuntime`, defaulted inside ecsctl.py itself --
    it needs no live fact threaded through here, unlike ecr's backing_port).
    `elasticache:*` (W2.8) is all-synth the same way, with its own REAL
    per-cluster `redis:7-alpine` container substrate (`aws/cache.py::
    RedisCache`, defaulted inside cachectl.py itself -- it needs no live fact
    threaded through here, unlike ecr's backing_port, and no workload identity
    either: Redis is not SigV4-signed, so a cache never calls the gateway).
    `keystore`/`gateway_port` (fix-wave 2b finding #2) are threaded to the
    THREE substrate-launching models only (ec2/ecs/lambda -- iam/ecr never
    launch a workload runtime of their own): each resolves the launching
    resource's own `odin:node` tag and calls `gateway.keys.workload_env` to
    inject the workload's keystore identity into the real container/VM it's
    booting. Both are None in every test that doesn't care (app.py's
    production caller always supplies both).
    `logs:*` (task W2.1) is all-synth too, and the ONE modeled family whose
    substrate is odin's own JSON sidecar rather than a container: the
    CloudWatch Logs control plane (`aws_cloudwatch_log_group`) plus its data
    plane (Put/Get/FilterLogEvents, DescribeLogStreams, CreateLogStream) --
    the SINK the Lambda/ECS substrates ship their real container output into,
    so `odin logs` reads one place regardless of kind (gateway/models/
    logsctl.py).
    `secretsmanager:*` and `ssm:*` (task W2.4) are all-synth on the same
    JSON-sidecar substrate: the Secrets Manager control+value plane
    (gateway/models/secretsctl.py) and the SSM Parameter Store
    (gateway/models/ssmctl.py). These two are the only models whose store
    holds user SECRETS in cleartext (0600, no KMS -- each module's own
    docstring records the limit), and a value only ever leaves through a
    GetSecretValue/GetParameter that evaluate() already allowed -- which,
    since both classify to the canvas node's label, means an IAM EDGE is what
    grants it.
    `rds:*` (task W2.7) is all-synth as well, with a REAL Postgres container
    per instance as its substrate (`aws/rds.py::PostgresRds`) -- `rds` is the
    injectable seam for it, threaded from app.py's `create_app(rds=...)` the
    way `keystore`/`gateway_port` are, and None in production (the model then
    builds a per-ENV substrate from the request's own env -- see
    rdsctl.py::_substrate).
    `elasticloadbalancing:*` (task W2.5) is all-synth too, and the ONE modeled
    family whose substrate is a REVERSE PROXY: an nginx container per load
    balancer (`compute/proxy.py`), whose upstreams are the target group's
    actually-registered targets. Like ecs's TaskRuntime it needs no live fact
    threaded through here -- `gateway/models/elbv2ctl.py` defaults its own
    `LoadBalancerProxy`.
    `events:*` (EventBridge) is all-synth on the JSON-sidecar substrate too,
    and it is the one family whose CONTROL plane is real while its DATA plane
    deliberately is not: rules and targets are stored and round-trip for tofu,
    and `PutEvents` returns an error naming the missing dispatcher rather than
    an accepted-and-never-delivered `FailedEntryCount: 0` (gateway/models/
    eventsctl.py).
    `s3:PutBucketNotification`/`s3:GetBucketNotification` are the ONE pair of
    S3 actions that are synth-owned (`gateway/models/s3notify.py`, in
    `_PURE_HANDLERS` below): S3 otherwise forwards wholesale to RustFS, but
    RustFS was measured rejecting every notification ARN form with
    `InvalidArgument` while persisting the configuration anyway -- so a forward
    makes `tofu apply` FAIL, the next `plan` read CLEAN, and nothing ever FIRE.
    Being pure is what makes those three answers one answer. The module refuses
    an SQS/SNS target outright rather than storing a trigger odin cannot
    deliver."""
    # EVERY model's `pure_answer` is a coroutine, including the JSON-sidecar
    # ones that await nothing, so every branch here is a plain `await` (v0.7.7).
    # That uniformity is the guard: while some models were coroutines and some
    # were not, a branch that forgot its `await` returned a COROUTINE OBJECT --
    # which is truthy, so `app.py`'s `if pure is not None` accepted it and the
    # gateway answered with a coroutine instead of a Response. Adding a model
    # can no longer reintroduce that, because there is only one shape to copy.
    if action.startswith("ec2:"):
        return await ec2compute.pure_answer(action, resource, env, body, stores, now, keystore=keystore, gateway_port=gateway_port)
    if action.startswith("iam:"):
        return await iamctl.pure_answer(action, resource, env, body, stores, now)
    if action.startswith("ecr:"):
        return await ecr.pure_answer(action, resource, env, body, stores, now, backing_port)
    if action.startswith("lambda:"):
        return await lambdactl.pure_answer(action, resource, env, body, stores, now, query=query, keystore=keystore, gateway_port=gateway_port)
    if action.startswith("ecs:"):
        return await ecsctl.pure_answer(action, resource, env, body, stores, now, keystore=keystore, gateway_port=gateway_port)
    if action.startswith("logs:"):
        return await logsctl.pure_answer(action, resource, env, body, stores, now)
    if action.startswith("secretsmanager:"):
        return await secretsctl.pure_answer(action, resource, env, body, stores, now)
    if action.startswith("ssm:"):
        return await ssmctl.pure_answer(action, resource, env, body, stores, now)
    if action.startswith("elasticache:"):
        return await cachectl.pure_answer(action, resource, env, body, stores, now)
    if action.startswith("rds:"):
        return await rdsctl.pure_answer(action, resource, env, body, stores, now, rds=rds)
    if action.startswith("elasticloadbalancing:"):
        return await elbv2ctl.pure_answer(action, resource, env, body, stores, now)
    if action.startswith("events:"):
        return await eventsctl.pure_answer(action, resource, env, body, stores, now)
    # `_PURE_HANDLERS` is the one table that stays SYNCHRONOUS: these are the
    # gap-fill handlers for services that DO have a backing container, and
    # every one of them is pure in-memory reshaping of the request body -- no
    # substrate, nothing to await. They are called, not awaited, and their
    # signature type says so.
    handler = _PURE_HANDLERS.get(action)
    return handler(resource, env, body, stores, now) if handler else None


# --- postprocess: forwarded for real, then observed/reshaped ----------------


def _sqs_create_queue(resource: str, env: str, request_body: bytes, response_body: bytes, stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str]) -> bytes:
    request = json.loads(request_body or b"{}")
    payload = json.loads(response_body)
    payload["QueueUrl"] = _rewrite_host(payload.get("QueueUrl", ""), gateway_host)
    queue_arn = f"arn:aws:sqs:{REGION}:{ACCOUNT}:{resource}"
    attributes = {**request.get("Attributes", {}), "QueueArn": queue_arn, "SqsManagedSseEnabled": "false", "Policy": ""}
    stores.sqs_queues.set(env, resource, {"attributes": attributes, "deleted_at": None})
    stores.tags.set(env, _tags_key("sqs", resource), dict(request.get("tags", {})))
    return json.dumps(payload).encode()


def _sqs_delete_queue(resource: str, env: str, request_body: bytes, response_body: bytes, stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str]) -> bytes:
    state = stores.sqs_queues.get(env, resource, {"attributes": {}, "deleted_at": None})
    state["deleted_at"] = now
    stores.sqs_queues.set(env, resource, state)
    return response_body


def _sns_create_topic(resource: str, env: str, request_body: bytes, response_body: bytes, stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str]) -> bytes:
    params = dict(parse_qsl(request_body.decode("utf-8")))
    stores.sns_topics.set(env, resource, _parse_map(params, "Attributes"))
    stores.tags.set(env, _tags_key("sns", resource), _tags_from_structs(_parse_struct_list(params, "Tags")))
    return response_body


def _sns_fix_subscription_attributes(resource: str, env: str, request_body: bytes, response_body: bytes, stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str]) -> bytes:
    """goaws's LIVE GetSubscriptionAttributes answer includes an entry like
    `FilterPolicy = "null"` (the literal 4-char string) for every optional
    attribute nobody set -- verified against goaws's own wire response.
    Real AWS OMITS an unset attribute entirely, so without this fix the TF
    provider's refreshed state reads `filter_policy = "null"` (a real value)
    instead of unset, and every `plan` shows drift (research §3: "goaws
    returns incomplete attrs -> drift; GW completes" -- S1 only implemented
    the post-Unsubscribe NotFound half of that; this closes the other half).
    Strips ANY entry whose value is the bare string "null" -- goaws's
    general placeholder shape for "unset", not just this one field."""
    return re.sub(rb"<entry><key>[^<]+</key><value>null</value></entry>", b"", response_body)


def _sns_unsubscribe(resource: str, env: str, request_body: bytes, response_body: bytes, stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str]) -> bytes:
    params = dict(parse_qsl(request_body.decode("utf-8")))
    subscription_arn = params.get("SubscriptionArn", "")
    if subscription_arn:
        stores.sns_subscriptions.set(env, subscription_arn, now)
    return response_body


def _ddb_create_table(resource: str, env: str, request_body: bytes, response_body: bytes, stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str]) -> bytes:
    request = json.loads(request_body or b"{}")
    stores.tags.set(env, _tags_key("dynamodb", resource), _tags_from_structs(request.get("Tags", [])))
    return response_body


_PostprocessHandler = Callable[[str, str, bytes, bytes, SynthStores, str, float, str, dict[str, str]], bytes]

_POSTPROCESS_HANDLERS: dict[str, _PostprocessHandler] = {
    "sqs:CreateQueue": _sqs_create_queue,
    "sqs:DeleteQueue": _sqs_delete_queue,
    "sns:CreateTopic": _sns_create_topic,
    "sns:Unsubscribe": _sns_unsubscribe,
    "sns:GetSubscriptionAttributes": _sns_fix_subscription_attributes,
    "dynamodb:CreateTable": _ddb_create_table,
    # S3 bucket notifications: every successful object write is matched against
    # the bucket's stored configurations and ENQUEUED for the tick-driven
    # dispatcher (gateway/models/s3notify.py). Deliberately not an invoke --
    # this function is synchronous and cannot await, and a real S3 notification
    # is asynchronous anyway.
    "s3:PutObject": s3notify.enqueue_put_object,
    "s3:DeleteObject": s3notify.enqueue_delete_object,
}


def is_postprocess_action(action: str) -> bool:
    return action in _POSTPROCESS_HANDLERS


def postprocess(
    action: str, resource: str, env: str, request_body: bytes, response_body: bytes,
    stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str],
) -> bytes:
    """The (possibly rewritten) response body for a POSTPROCESS action --
    called only after a successful (<300) forward. `gateway_host` is the
    caller's own arrival `Host` header, so a rewritten QueueUrl re-dials
    through whichever path (container docker-host-gateway alias, or a
    host-side tofu process on 127.0.0.1) reached the gateway in the first
    place, matching research's "rewrite to the gateway's own host:port".

    `path` and `query` are the request's RAW target and its already-parsed
    query dict, and they are here even though `resource` exists because
    `classify` REDUCES an S3 request to its BUCKET -- the object key survives
    only in the path, and the four distinct wire shapes that all classify to
    `s3:PutObject` (a plain write, CreateMultipartUpload, UploadPart,
    CompleteMultipartUpload) are told apart only by the query. Without them the
    S3-notification hook would enqueue an empty key and fire once per uploaded
    PART; see `s3notify._writes`, which is where that is worked out. Do not
    tidy them away because the other five handlers ignore them."""
    return _POSTPROCESS_HANDLERS[action](resource, env, request_body, response_body, stores, gateway_host, now, path, query)
