"""The gateway's ElastiCache control-plane model (W2.8): cache clusters
(engine `redis`) + their tags, backed by a REAL `redis:7-alpine` container per
cluster (`aws/cache.py::RedisCache`).

Like ec2compute/lambdactl/ecsctl, this module is the WHOLE answer for every
`elasticache:*` action -- there is no shared backing container to forward to,
and ElastiCache's own control plane is pure metadata anyway. What makes it
real is the substrate: `CreateCacheCluster` boots an actual Redis server and
`CacheNodes[].Endpoint` reports the port Docker actually published, so a
consumer that dials the advertised endpoint reaches a real cache.

THE DATA PLANE NEVER TOUCHES THIS GATEWAY. Real ElastiCache is the same: the
Redis wire protocol is not SigV4-signed and carries no AWS identity, so
`GET`/`SET` traffic is not an AWS API call at all. Consequence, stated plainly
because it limits what a drawn IAM edge means: an `elasticache` IAM edge gates
the CONTROL plane only (Describe/Modify/Delete/tags). Nothing odin can do at
this layer would gate a raw `SET` -- that is a network-reachability question
(security groups), not an IAM one. See ROADMAP's recorded limit.

The state machine is real and asynchronous, the ec2compute/lambdactl shape:
`CreateCacheCluster` mints the record as `creating` and returns immediately,
spawning a daemon thread that boots the container and waits for a real Redis
PING -- terraform-provider-aws's own create waiter (pending `creating`, target
`available`) absorbs that latency, so no timing hack is needed. A boot failure
lands the cluster in `create-failed`, NEVER a silent hang: that status string
is odin's own, deliberately NOT one of real ElastiCache's
(available/creating/deleting/modifying/snapshotting/incompatible-network/...),
because ElastiCache has no status meaning "creation failed" and reusing
`incompatible-network` would be a lie about WHY. The provider's waiter treats
any status outside its pending list as an immediate hard error and prints it,
so a failed boot fails `apply` fast and honestly; the real reason rides on the
record for the World verdict (reconcile/tf_status.py).

`DeleteCacheCluster` sets `deleting`, returns, and a daemon thread removes the
real container and then the record itself -- the provider's delete waiter
polls until DescribeCacheClusters answers `CacheClusterNotFound`, which is
exactly what a removed record produces. No grace window needed (unlike SQS's
delete shim), since `deleting` is itself a pending state for that waiter.

v1 model decisions, each recorded rather than hidden:
- SINGLE NODE ONLY: `NumCacheNodes` must be 1. A cluster with more nodes is
  rejected with a real `InvalidParameterValue` naming the limit, never
  silently collapsed to one node.
- REDIS ONLY: `Engine` must be `redis`. memcached would need a different
  substrate image and a different endpoint shape (a ConfigurationEndpoint,
  which this model deliberately never emits).
- `CacheNodeType` is accepted VERBATIM and never validated (the `ImageId`
  precedent in ec2compute.py): it names EC2-class hardware odin has no
  mapping for, so every cluster gets `aws/cache.py`'s own fixed memory cap.
- `EngineVersion` reports the container's REAL `redis_version`
  (`aws.cache.engine_version`), not a hardcoded string.

Every wire shape here was verified against botocore's OWN elasticache service
model (query protocol, apiVersion 2015-02-02): the request serialization
(`Tags.Tag.N.Key`, `SecurityGroupIds.SecurityGroupId.N`, `TagKeys.member.N`)
comes from real boto3 captures, and every response round-trips through
botocore's `create_parser("query")` in tests/gateway/test_cachectl.py.

Persistence: one `JsonStore` at `.odin/{env}/gateway/cachectl.json`
(`stores.cachectl`), flat keys `"cluster:{id}"`. Tags share the shared
`stores.tags` store, keyed `"elasticache:{arn}"` -- the same convention
ec2net/iamctl/ecr use for their own resource families.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from starlette.responses import Response

from odin.aws import cache as cache_mod
from odin.aws.backings import ACCOUNT, REGION
from odin.aws.cache import RedisCache
from odin.gateway import errors
from odin.gateway.errors import exc_text
from odin.gateway.stores import NO_CHANGE, SynthStores
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST

log = logging.getLogger("odin.gateway.cachectl")

_ELASTICACHE_NS = "http://elasticache.amazonaws.com/doc/2015-02-02/"
_REQUEST_ID = "00000000-0000-0000-0000-000000000000"

ENGINE = "redis"
# The redis:7-alpine line, used only when the running server can't be asked
# (`aws.cache.engine_version` returned "") -- the honest fallback, never the
# normal path.
_FALLBACK_ENGINE_VERSION = "7.1"
_PARAMETER_GROUP = "default.redis7"
_MAINTENANCE_WINDOW = "sun:05:00-sun:06:00"  # already lowercase: the provider normalizes to lower
_CACHE_NODE_ID = "0001"
_DEFAULT_NODE_TYPE = "cache.t3.micro"
_DEFAULT_AZ = f"{REGION}a"

# Real ElastiCache statuses this model uses, plus odin's own `create-failed`
# (see the module docstring for why it isn't one of AWS's).
STATUS_CREATING = "creating"
STATUS_AVAILABLE = "available"
STATUS_DELETING = "deleting"
STATUS_CREATE_FAILED = "create-failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- request parsing: ElastiCache's query-protocol serialization -------------
# Genuine "query" protocol like SNS/IAM, but with per-shape member names
# (`Tags.Tag.N`, not `Tags.member.N`) -- captured off real boto3 requests, see
# the module docstring. Kept self-contained (not imported from synth.py) for
# the same reason iamctl.py does: synth.py imports THIS module.


def _params(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _indexed(params: dict[str, str], prefix: str) -> list[dict[str, str]]:
    """`prefix.N.Field` -> a list of dicts, ordered by N (`Field` empty for a
    scalar list member)."""
    grouped: dict[int, dict[str, str]] = {}
    for key, value in params.items():
        if not key.startswith(f"{prefix}."):
            continue
        index, _, rest = key[len(prefix) + 1:].partition(".")
        if index.isdigit():
            grouped.setdefault(int(index), {})[rest] = value
    return [grouped[i] for i in sorted(grouped)]


def _parse_tags(params: dict[str, str]) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in _indexed(params, "Tags.Tag") if "Key" in t}


def _parse_tag_keys(params: dict[str, str]) -> list[str]:
    return [t[""] for t in _indexed(params, "TagKeys.member") if "" in t]


def _parse_security_group_ids(params: dict[str, str]) -> list[str]:
    return [g[""] for g in _indexed(params, "SecurityGroupIds.SecurityGroupId") if "" in g]


# --- wire building: query-protocol XML --------------------------------------


def _response(op: str, inner: str = "") -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{op}Response xmlns="{_ELASTICACHE_NS}"><{op}Result>{inner}</{op}Result>'
        f"<ResponseMetadata><RequestId>{_REQUEST_ID}</RequestId></ResponseMetadata>"
        f"</{op}Response>"
    )
    return Response(xml, media_type="text/xml")


def _not_found(cluster_id: str) -> Response:
    return errors.synth_error(
        "elasticache", "CacheClusterNotFound", f"CacheCluster not found: {cluster_id}", 404,
    )


def _invalid_parameter(message: str) -> Response:
    return errors.synth_error("elasticache", "InvalidParameterValue", message, 400)


def arn_for(cluster_id: str) -> str:
    return f"arn:aws:elasticache:{REGION}:{ACCOUNT}:cluster:{cluster_id}"


def _tags_xml(tags: dict[str, str]) -> str:
    items = "".join(f"<Tag><Key>{escape(k)}</Key><Value>{escape(v)}</Value></Tag>" for k, v in tags.items())
    return f"<TagList>{items}</TagList>"


def _cache_nodes_xml(cluster: dict) -> str:
    """The cluster's single cache node, with the REAL published host port on
    its Endpoint -- what makes the advertised address dialable. Omitted
    entirely until the container is up (no port yet == nothing to advertise),
    which is also real ElastiCache's shape for a `creating` cluster."""
    port = cluster.get("port")
    if not port:
        return ""
    return (
        "<CacheNodes><CacheNode>"
        f"<CacheNodeId>{_CACHE_NODE_ID}</CacheNodeId>"
        f"<CacheNodeStatus>{STATUS_AVAILABLE}</CacheNodeStatus>"
        f"<CacheNodeCreateTime>{cluster['cache_cluster_create_time']}</CacheNodeCreateTime>"
        f"<Endpoint><Address>{escape(cluster['address'])}</Address><Port>{port}</Port></Endpoint>"
        f"<ParameterGroupStatus>in-sync</ParameterGroupStatus>"
        f"<CustomerAvailabilityZone>{escape(cluster['preferred_availability_zone'])}</CustomerAvailabilityZone>"
        "</CacheNode></CacheNodes>"
    )


def _security_groups_xml(cluster: dict) -> str:
    ids = cluster.get("security_group_ids") or []
    items = "".join(
        f"<member><SecurityGroupId>{escape(gid)}</SecurityGroupId><Status>active</Status></member>" for gid in ids
    )
    return f"<SecurityGroups>{items}</SecurityGroups>" if items else ""


def _cluster_xml(cluster: dict, show_nodes: bool) -> str:
    parts = [
        f"<CacheClusterId>{escape(cluster['cache_cluster_id'])}</CacheClusterId>",
        f"<CacheNodeType>{escape(cluster['cache_node_type'])}</CacheNodeType>",
        f"<Engine>{cluster['engine']}</Engine>",
        f"<EngineVersion>{escape(cluster['engine_version'])}</EngineVersion>",
        f"<CacheClusterStatus>{cluster['status']}</CacheClusterStatus>",
        f"<NumCacheNodes>{cluster['num_cache_nodes']}</NumCacheNodes>",
        f"<PreferredAvailabilityZone>{escape(cluster['preferred_availability_zone'])}</PreferredAvailabilityZone>",
        f"<CacheClusterCreateTime>{cluster['cache_cluster_create_time']}</CacheClusterCreateTime>",
        f"<PreferredMaintenanceWindow>{escape(cluster['preferred_maintenance_window'])}</PreferredMaintenanceWindow>",
        "<CacheParameterGroup>"
        f"<CacheParameterGroupName>{escape(cluster['cache_parameter_group_name'])}</CacheParameterGroupName>"
        "<ParameterApplyStatus>in-sync</ParameterApplyStatus>"
        "</CacheParameterGroup>",
        f"<AutoMinorVersionUpgrade>{str(cluster['auto_minor_version_upgrade']).lower()}</AutoMinorVersionUpgrade>",
        f"<SnapshotRetentionLimit>{cluster['snapshot_retention_limit']}</SnapshotRetentionLimit>",
        f"<SnapshotWindow>{escape(cluster['snapshot_window'])}</SnapshotWindow>",
        "<TransitEncryptionEnabled>false</TransitEncryptionEnabled>",
        "<AtRestEncryptionEnabled>false</AtRestEncryptionEnabled>",
        "<AuthTokenEnabled>false</AuthTokenEnabled>",
        f"<ARN>{escape(cluster['arn'])}</ARN>",
    ]
    if cluster.get("cache_subnet_group_name"):
        parts.append(f"<CacheSubnetGroupName>{escape(cluster['cache_subnet_group_name'])}</CacheSubnetGroupName>")
    parts.append(_security_groups_xml(cluster))
    if show_nodes:
        parts.append(_cache_nodes_xml(cluster))
    return "".join(parts)


# --- store access -----------------------------------------------------------


def _key(cluster_id: str) -> str:
    return f"cluster:{cluster_id}"


def _cluster(stores: SynthStores, env: str, cluster_id: str) -> dict | None:
    return stores.cachectl.get(env, _key(cluster_id))


def clusters(stores: SynthStores, env: str) -> list[dict]:
    """Every cluster record in `env` -- public so reconcile/tf_status.py and
    the /logs route read them the same way this module does."""
    return [v for k, v in stores.cachectl.items(env).items() if k.startswith("cluster:")]


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, f"elasticache:{arn}", {})


def _set_tags(stores: SynthStores, env: str, arn: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"elasticache:{arn}", tags)


def _update(stores: SynthStores, env: str, cluster_id: str, **fields: object) -> None:
    def mutate(cluster: dict | None) -> dict | object:
        if cluster is None:  # already deleted + swept -- nothing to update
            return NO_CHANGE
        updated = dict(cluster)
        updated.update(fields)
        return updated

    stores.cachectl.update(env, _key(cluster_id), mutate)


# --- background completion: the async state machine -------------------------


def _spawn(target: Callable[..., None], *args: object) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _finish_create(stores: SynthStores, env: str, cluster_id: str, cache: RedisCache) -> None:
    # Deliberately broad, for the ec2compute reason: this runs on a daemon
    # thread with no caller to propagate to, and an uncaught exception here
    # would strand the cluster `creating` forever -- the one failure mode the
    # brief forbids. Any failure becomes a real, provider-visible status.
    try:
        port = cache.ensure(env, cluster_id)
    except Exception as exc:
        log.warning("cache cluster %s failed to boot: %s", cluster_id, exc)
        _update(stores, env, cluster_id, status=STATUS_CREATE_FAILED, status_reason=exc_text(exc))
        return
    _update(
        stores, env, cluster_id, status=STATUS_AVAILABLE, status_reason=None,
        port=port, address=CONTAINER_HOST, engine_version=cache_mod.engine_version(port) or _FALLBACK_ENGINE_VERSION,
    )


def _finish_delete(stores: SynthStores, env: str, cluster_id: str, arn: str, cache: RedisCache) -> None:
    try:
        cache.delete(env, cluster_id)
    except Exception as exc:
        # Container-removal honesty: the record stays `deleting` with the real
        # reason, so the provider's delete waiter keeps polling and eventually
        # times out loudly rather than odin claiming a clean delete over a
        # container that's still running.
        # `exc_text`, matching `_finish_create` twelve lines up: the SAME
        # module already had the treatment on its create path and not on its
        # delete path, so a no-message exception persisted `container removal
        # failed: ` -- a record stuck in `deleting` whose reason is a dangling
        # colon, which is exactly the polling caller this branch exists to be
        # honest with.
        log.error("cache container removal failed for %s: %s", cluster_id, exc_text(exc))
        _update(stores, env, cluster_id, status_reason=f"container removal failed: {exc_text(exc)}")
        return
    stores.cachectl.delete(env, _key(cluster_id))
    _set_tags(stores, env, arn, {})


# --- handlers ---------------------------------------------------------------


def _create_cache_cluster(params: dict[str, str], env: str, stores: SynthStores, now: float, cache: RedisCache) -> Response:
    cluster_id = params.get("CacheClusterId", "")
    if _cluster(stores, env, cluster_id) is not None:
        return errors.synth_error(
            "elasticache", "CacheClusterAlreadyExists", f"CacheCluster already exists: {cluster_id}", 400,
        )
    engine = (params.get("Engine") or ENGINE).lower()
    if engine != ENGINE:
        return _invalid_parameter(f"odin's ElastiCache model supports Engine={ENGINE} only (got {engine!r})")
    nodes = params.get("NumCacheNodes") or "1"
    if nodes != "1":
        return _invalid_parameter(
            f"odin's ElastiCache model is single-node in v1: NumCacheNodes must be 1 (got {nodes})"
        )
    created = _now_iso()
    cluster = {
        "cache_cluster_id": cluster_id,
        "engine": ENGINE,
        # The real version is read off the running server once it answers
        # (`_finish_create`); until then advertise the image's own line.
        "engine_version": params.get("EngineVersion") or _FALLBACK_ENGINE_VERSION,
        "cache_node_type": params.get("CacheNodeType") or _DEFAULT_NODE_TYPE,
        "num_cache_nodes": 1,
        "status": STATUS_CREATING,
        "status_reason": None,
        "preferred_availability_zone": params.get("PreferredAvailabilityZone") or _DEFAULT_AZ,
        "cache_cluster_create_time": created,
        "preferred_maintenance_window": (params.get("PreferredMaintenanceWindow") or _MAINTENANCE_WINDOW).lower(),
        "cache_parameter_group_name": params.get("CacheParameterGroupName") or _PARAMETER_GROUP,
        "cache_subnet_group_name": params.get("CacheSubnetGroupName") or None,
        "auto_minor_version_upgrade": (params.get("AutoMinorVersionUpgrade") or "true") == "true",
        "snapshot_retention_limit": int(params.get("SnapshotRetentionLimit") or 0),
        "snapshot_window": params.get("SnapshotWindow") or "00:00-01:00",
        "security_group_ids": _parse_security_group_ids(params),
        "arn": arn_for(cluster_id),
        "address": None,
        "port": None,
    }
    stores.cachectl.set(env, _key(cluster_id), cluster)
    _set_tags(stores, env, cluster["arn"], _parse_tags(params))

    # Render the `creating` response BEFORE spawning the boot thread: JsonStore
    # hands back the SAME dict object it was given, so `cluster` here and the
    # record `_finish_create` mutates are literally the same object -- render
    # after `_spawn` and a fast boot can race into the response body
    # (ec2compute.py hit exactly this).
    response = _response("CreateCacheCluster", f"<CacheCluster>{_cluster_xml(cluster, show_nodes=True)}</CacheCluster>")
    _spawn(_finish_create, stores, env, cluster_id, cache)
    return response


def _describe_cache_clusters(params: dict[str, str], env: str, stores: SynthStores, now: float, cache: RedisCache) -> Response:
    cluster_id = params.get("CacheClusterId")
    show_nodes = params.get("ShowCacheNodeInfo") == "true"
    records = clusters(stores, env)
    if cluster_id:
        selected = [c for c in records if c["cache_cluster_id"] == cluster_id]
        if not selected:
            return _not_found(cluster_id)
    else:
        selected = records
    items = "".join(f"<CacheCluster>{_cluster_xml(c, show_nodes)}</CacheCluster>" for c in selected)
    return _response("DescribeCacheClusters", f"<CacheClusters>{items}</CacheClusters>")


def _delete_cache_cluster(params: dict[str, str], env: str, stores: SynthStores, now: float, cache: RedisCache) -> Response:
    cluster_id = params.get("CacheClusterId", "")
    cluster = _cluster(stores, env, cluster_id)
    if cluster is None:
        return _not_found(cluster_id)
    already_deleting = cluster["status"] == STATUS_DELETING
    _update(stores, env, cluster_id, status=STATUS_DELETING)
    response = _response("DeleteCacheCluster", f"<CacheCluster>{_cluster_xml(cluster, show_nodes=True)}</CacheCluster>")
    if not already_deleting:  # idempotent: a retried delete never double-spawns
        _spawn(_finish_delete, stores, env, cluster_id, cluster["arn"], cache)
    return response


# ModifyCacheCluster arguments this model actually carries. Anything else the
# provider sends is accepted and ignored rather than erroring -- real
# ElastiCache tolerates a no-op modify, and a hard failure here would break an
# apply over a field odin doesn't model.
_MODIFIABLE = {
    "CacheNodeType": "cache_node_type",
    "EngineVersion": "engine_version",
    "PreferredMaintenanceWindow": "preferred_maintenance_window",
    "SnapshotWindow": "snapshot_window",
}


def _modify_cache_cluster(params: dict[str, str], env: str, stores: SynthStores, now: float, cache: RedisCache) -> Response:
    cluster_id = params.get("CacheClusterId", "")
    cluster = _cluster(stores, env, cluster_id)
    if cluster is None:
        return _not_found(cluster_id)
    nodes = params.get("NumCacheNodes")
    if nodes is not None and nodes != "1":
        return _invalid_parameter(
            f"odin's ElastiCache model is single-node in v1: NumCacheNodes must be 1 (got {nodes})"
        )
    fields = {field: params[param] for param, field in _MODIFIABLE.items() if params.get(param)}
    if "preferred_maintenance_window" in fields:
        fields["preferred_maintenance_window"] = fields["preferred_maintenance_window"].lower()
    if params.get("SnapshotRetentionLimit"):
        fields["snapshot_retention_limit"] = int(params["SnapshotRetentionLimit"])
    if params.get("AutoMinorVersionUpgrade"):
        fields["auto_minor_version_upgrade"] = params["AutoMinorVersionUpgrade"] == "true"
    security_group_ids = _parse_security_group_ids(params)
    if security_group_ids:
        fields["security_group_ids"] = security_group_ids
    # No `modifying` transition: every field above is pure metadata (the Redis
    # substrate needs no reconfiguration), so the change IS already applied by
    # the time this returns -- reporting `modifying` would invent a wait that
    # nothing is waiting on.
    _update(stores, env, cluster_id, **fields)
    updated = _cluster(stores, env, cluster_id) or cluster
    return _response("ModifyCacheCluster", f"<CacheCluster>{_cluster_xml(updated, show_nodes=True)}</CacheCluster>")


def _resource_cluster(stores: SynthStores, env: str, resource_name: str) -> dict | None:
    """The cluster a tag call's `ResourceName` ARN points at (`arn:aws:
    elasticache:region:account:cluster:<id>` -- the id is the last
    colon-segment, the same bare-label extraction classify.py does)."""
    return _cluster(stores, env, resource_name.rsplit(":", 1)[-1])


def _list_tags_for_resource(params: dict[str, str], env: str, stores: SynthStores, now: float, cache: RedisCache) -> Response:
    resource_name = params.get("ResourceName", "")
    cluster = _resource_cluster(stores, env, resource_name)
    if cluster is None:
        return _not_found(resource_name)
    return _response("ListTagsForResource", _tags_xml(_tags_for(stores, env, cluster["arn"])))


def _add_tags_to_resource(params: dict[str, str], env: str, stores: SynthStores, now: float, cache: RedisCache) -> Response:
    resource_name = params.get("ResourceName", "")
    cluster = _resource_cluster(stores, env, resource_name)
    if cluster is None:
        return _not_found(resource_name)
    arn = cluster["arn"]
    _set_tags(stores, env, arn, {**_tags_for(stores, env, arn), **_parse_tags(params)})
    return _response("AddTagsToResource", _tags_xml(_tags_for(stores, env, arn)))


def _remove_tags_from_resource(params: dict[str, str], env: str, stores: SynthStores, now: float, cache: RedisCache) -> Response:
    resource_name = params.get("ResourceName", "")
    cluster = _resource_cluster(stores, env, resource_name)
    if cluster is None:
        return _not_found(resource_name)
    arn = cluster["arn"]
    remove = set(_parse_tag_keys(params))
    _set_tags(stores, env, arn, {k: v for k, v in _tags_for(stores, env, arn).items() if k not in remove})
    return _response("RemoveTagsFromResource", _tags_xml(_tags_for(stores, env, arn)))


# --- dispatch ---------------------------------------------------------------


_Handler = Callable[[dict[str, str], str, SynthStores, float, RedisCache], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateCacheCluster": _create_cache_cluster,
    "DescribeCacheClusters": _describe_cache_clusters,
    "DeleteCacheCluster": _delete_cache_cluster,
    "ModifyCacheCluster": _modify_cache_cluster,
    "ListTagsForResource": _list_tags_for_resource,
    "AddTagsToResource": _add_tags_to_resource,
    "RemoveTagsFromResource": _remove_tags_from_resource,
}

# logsctl/secretsctl/rdsctl's defect, in this module -- and here NO handler
# checked, not even the create path. Measured against the real handlers with
# the member omitted:
#
#   DeleteCacheCluster -> 404 CacheClusterNotFound "CacheCluster not found: "
#   CreateCacheCluster -> 200, having MINTED `cluster:` -- a record keyed by
#                         nothing, with `<CacheClusterId></CacheClusterId>` on
#                         the wire, which no later call can name
#
# The second is worse than a message that says nothing: it is a success odin
# did not achieve, and it is exactly the `taskdef::1` record
# `ecsctl._missing_parameter` was written for. ModifyCacheCluster and the three
# tag ops then answered 200 about that nameless record, which is how one
# unchecked identifier becomes five wrong answers.
#
# The member lists are botocore's OWN `required` metadata for the
# `elasticache` model, and a real boto3 client refuses to send a request
# without them (`delete_cache_cluster()` -> `ParamValidationError: Missing
# required parameter in input: "CacheClusterId"`). `DescribeCacheClusters` is
# deliberately absent: an id-less describe is a legitimate LIST.
_REQUIRED: dict[str, str] = {
    "CreateCacheCluster": "CacheClusterId",
    "DeleteCacheCluster": "CacheClusterId",
    "ModifyCacheCluster": "CacheClusterId",
    "ListTagsForResource": "ResourceName",
    "AddTagsToResource": "ResourceName",
    "RemoveTagsFromResource": "ResourceName",
}


def _missing_identifier(op: str, params: dict[str, str]) -> str | None:
    """The required identifier this request did not carry, or None."""
    member = _REQUIRED.get(op, "")
    return member if member and not params.get(member, "").strip() else None


def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    cache: RedisCache | None = None,
) -> Response | None:
    """The gateway's whole `elasticache:*` answer -- same no-backing contract
    as ec2/iam/ecr/lambda/ecs, so this never returns None. `cache` is the
    injectable `RedisCache` (or a test's fake stand-in with the same
    `ensure`/`delete`/`host_port`/`status` shape); production callers
    (gateway/synth.py) never pass one, so a real container manager is used."""
    op = action.removeprefix("elasticache:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("elasticache", "InvalidAction", f"The action {op} is not valid.", 400)
    params = _params(body)
    missing = _missing_identifier(op, params)
    if missing is not None:
        return _invalid_parameter(f"{missing} is required")
    return handler(params, env, stores, now, cache or RedisCache())


# --- facts for consumers ----------------------------------------------------


def facts(cluster: dict) -> dict[str, str]:
    """The World facts an `available` cluster publishes, so a consumer can
    wire `${{cache.REDIS_URL}}` (reconcile/tf_status.py projects these).

    TWO forms, for the v0.5.4 finding-#5 reason RDS publishes DATABASE_URL and
    DATABASE_URL_VM: a CONTAINER consumer resolves `host.docker.internal`; an
    EC2 (Lima VM) consumer does NOT -- it resolves `host.lima.internal`. Both
    point at the SAME Redis on the same published port, so an ec2 node consumes
    `${{cache.REDIS_URL_VM}}` while containers keep `${{cache.REDIS_URL}}`
    (per-consumer-type ref routing stays deferred -- a distinct fact is the
    honest, smaller fix). Empty for a cluster that isn't up yet.

    EVERY VALUE IS A `str` -- `str(port)` below is not cosmetic. World facts
    round-trip through `world.json` on every emit, so a value JSON reshapes (a
    tuple read back as a list) compares unequal to itself forever and storms
    one delta per tick. `Reconciler._assert_string_facts` enforces that at the
    boundary and carries the full argument."""
    port = cluster.get("port")
    if not port:
        return {}
    address = cluster["address"]
    return {
        "REDIS_URL": f"redis://{address}:{port}", "endpoint": f"{address}:{port}",
        "REDIS_URL_VM": f"redis://{LIMA_HOST}:{port}", "endpoint_vm": f"{LIMA_HOST}:{port}",
        "port": str(port),
    }
