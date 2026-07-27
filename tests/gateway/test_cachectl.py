"""W2.8 -- gateway/models/cachectl.py: the ElastiCache control-plane model.

Same test method as V2a/V5a's: every request is a REAL boto3-signed capture
(tests/gateway/harness.py CaptureSink + the `elasticache` fixture), and every
response round-trips through botocore's OWN parser for the REAL ElastiCache
service model (`create_parser("query")`) -- proof the wire bytes are
real-AWS-shaped, not string-matched. Every call ALSO routes through classify()
-> synth.pure_answer(), exercising the elasticache branch of the dispatch
pipeline, not just the model functions in isolation.

The Redis container substrate is injected as a `FakeRedisCache` with
`RedisCache`'s shape (`ensure`/`delete`/`host_port`/`status`) -- no Docker; the
real thing is proven by tests/aws/test_cache.py (real RESP over a real socket)
and the W2.8 integration test (real container, real SET/GET).
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.models import cachectl
from odin.gateway.stores import SynthStores
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
CLUSTER = "sessions"
HOST_PORT = 51234


class FakeRedisCache:
    """The `RedisCache` shape, no Docker. `block` lets a test hold `ensure()`
    mid-flight so the cluster is observably `creating`; `fail` makes the boot
    raise, the path that must land `create-failed` rather than hang.

    Every method is `async def` because every one of `RedisCache`'s is after
    v0.7.7 -- `cachectl` awaits `ensure`/`delete`, and a fake that answered
    synchronously would hand the model a plain int where production hands it a
    coroutine. `block` is an `asyncio.Event` for the same reason: a
    `threading.Event.wait()` inside a task blocks the WHOLE loop, including the
    test that is supposed to release it."""

    def __init__(self, port: int = HOST_PORT, fail: bool = False, block: asyncio.Event | None = None) -> None:
        self.port = port
        self.fail = fail
        self.block = block
        self.ensured: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    async def ensure(self, env: str, cluster_id: str, memory_mib: float | None = None) -> int:
        if self.block is not None:
            # The 5s cap `threading.Event.wait(5)` carried, kept: a test that
            # forgets to release the block fails on its own assertion instead
            # of hanging the run.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.block.wait(), 5)
        self.ensured.append((env, cluster_id))
        if self.fail:
            raise RuntimeError("redis never became ready")
        return self.port

    async def delete(self, env: str, cluster_id: str) -> None:
        self.deleted.append((env, cluster_id))

    async def host_port(self, env: str, cluster_id: str) -> int:
        return self.port

    async def status(self, env: str, cluster_id: str) -> str:
        return "running"


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


@pytest.fixture
def cache(monkeypatch) -> FakeRedisCache:
    """The substrate every call in this module gets -- patched at the seam
    cachectl.pure_answer defaults through, so the tests drive the SAME
    `classify -> synth.pure_answer` path production does (no extra argument).

    Both patches stay plain `lambda`s: the constructor `RedisCache(...)` is a
    normal call, and `cache_mod.engine_version` is read off a socket
    SYNCHRONOUSLY (`_finish_create` calls it without `await`), so making either
    a coroutine would hand cachectl an object it never awaits."""
    fake = FakeRedisCache()
    monkeypatch.setattr(cachectl, "RedisCache", lambda *a, **k: fake)
    # The real version read talks to a real server; the fake substrate has none.
    monkeypatch.setattr(cachectl.cache_mod, "engine_version", lambda port: "7.4.2")
    return fake


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("elasticache")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


async def _answer(stores: SynthStores, req) -> Response:
    path, query = split_url(req.url)
    classified = classify("elasticache", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an elasticache request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "elasticache is all-synth: pure_answer must never fall through"
    return response


async def _create(elasticache, sink, stores, cluster_id: str = CLUSTER, **extra) -> Response:
    # `sink.call` and the lambda stay SYNC: `elasticache` is a boto3 client,
    # whose operations are synchronous however much of odin went async.
    req = sink.call(lambda: elasticache.create_cache_cluster(
        CacheClusterId=cluster_id, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=1, **extra,
    ))
    return await _answer(stores, req)


async def _describe(elasticache, sink, stores, **kwargs) -> Response:
    req = sink.call(lambda: elasticache.describe_cache_clusters(ShowCacheNodeInfo=True, **kwargs))
    return await _answer(stores, req)


async def _settle(cache: FakeRedisCache, stores: SynthStores, cluster_id: str = CLUSTER) -> None:
    """Wait for the background create/delete task to land its state.

    `await asyncio.sleep`, never a blocking wait: the work being waited for is
    now a task on THIS event loop (v0.7.7), so anything that blocks here stops
    the very boot the poll exists to observe."""
    for _ in range(200):
        record = stores.cachectl.get(ENV, f"cluster:{cluster_id}")
        if record is None or record["status"] != cachectl.STATUS_CREATING:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background task never settled")


# --- classify --------------------------------------------------------------


def test_classify_extracts_the_cluster_id(elasticache, sink):
    req = sink.call(lambda: elasticache.describe_cache_clusters(CacheClusterId=CLUSTER))
    path, query = split_url(req.url)
    assert classify("elasticache", req.method, path, query, req.headers, req.body) == (
        "elasticache:DescribeCacheClusters", CLUSTER,
    )


def test_classify_extracts_the_cluster_id_out_of_a_tag_arn(elasticache, sink):
    arn = cachectl.arn_for(CLUSTER)
    req = sink.call(lambda: elasticache.list_tags_for_resource(ResourceName=arn))
    path, query = split_url(req.url)
    assert classify("elasticache", req.method, path, query, req.headers, req.body) == (
        "elasticache:ListTagsForResource", CLUSTER,
    )


def test_classify_falls_back_to_wildcard_never_none(elasticache, sink):
    req = sink.call(lambda: elasticache.describe_cache_clusters())
    path, query = split_url(req.url)
    assert classify("elasticache", req.method, path, query, req.headers, req.body) == (
        "elasticache:DescribeCacheClusters", "*",
    )


# --- create ----------------------------------------------------------------


async def test_create_returns_creating_immediately_then_goes_available(elasticache, sink, stores, cache):
    block = asyncio.Event()
    cache.block = block
    response = await _create(elasticache, sink, stores)
    cluster = _parse("CreateCacheCluster", response)["CacheCluster"]
    assert cluster["CacheClusterId"] == CLUSTER
    assert cluster["CacheClusterStatus"] == cachectl.STATUS_CREATING
    assert cluster["Engine"] == "redis"
    assert cluster["NumCacheNodes"] == 1
    assert cluster["ARN"] == cachectl.arn_for(CLUSTER)
    assert cluster.get("CacheNodes", []) == []  # nothing to advertise until the container is up

    block.set()
    await _settle(cache, stores)
    assert cache.ensured == [(ENV, CLUSTER)]
    described = _parse("DescribeCacheClusters", await _describe(elasticache, sink, stores))
    (live,) = described["CacheClusters"]
    assert live["CacheClusterStatus"] == cachectl.STATUS_AVAILABLE
    assert live["EngineVersion"] == "7.4.2"  # the REAL redis version, not a constant


async def test_available_cluster_advertises_the_real_published_port(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    (live,) = _parse("DescribeCacheClusters", await _describe(elasticache, sink, stores))["CacheClusters"]
    (node,) = live["CacheNodes"]
    assert node["Endpoint"] == {"Address": CONTAINER_HOST, "Port": HOST_PORT}
    assert node["CacheNodeId"] == "0001"


async def test_describe_without_show_cache_node_info_omits_the_nodes(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    req = sink.call(lambda: elasticache.describe_cache_clusters(CacheClusterId=CLUSTER))
    (live,) = _parse("DescribeCacheClusters", await _answer(stores, req))["CacheClusters"]
    assert live.get("CacheNodes", []) == []


async def test_a_boot_failure_lands_create_failed_with_the_real_reason(elasticache, sink, stores, cache):
    cache.fail = True
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    (live,) = _parse("DescribeCacheClusters", await _describe(elasticache, sink, stores))["CacheClusters"]
    # NOT still "creating": the provider's waiter must fail fast, never hang.
    assert live["CacheClusterStatus"] == cachectl.STATUS_CREATE_FAILED
    record = stores.cachectl.get(ENV, f"cluster:{CLUSTER}")
    # Carries the exception CLASS as well as its message, the one wording every
    # writer now shares (`gateway/errors.py::exc_text`) — a `TimeoutError:
    # connection refused` and an `OSError: connection refused` are different
    # diagnoses, and the class is the only thing that says which.
    assert record["status_reason"] == "RuntimeError: redis never became ready"


async def test_create_twice_is_already_exists(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    response = await _create(elasticache, sink, stores)
    parsed = _parse("CreateCacheCluster", response, error=True)
    assert parsed["Error"]["Code"] == "CacheClusterAlreadyExists"


async def test_multi_node_is_an_honest_invalid_parameter(elasticache, sink, stores, cache):
    req = sink.call(lambda: elasticache.create_cache_cluster(
        CacheClusterId=CLUSTER, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=3,
    ))
    parsed = _parse("CreateCacheCluster", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "InvalidParameterValue"
    assert "single-node" in parsed["Error"]["Message"]
    assert cache.ensured == []  # nothing booted for a rejected request


async def test_memcached_is_an_honest_invalid_parameter(elasticache, sink, stores, cache):
    req = sink.call(lambda: elasticache.create_cache_cluster(
        CacheClusterId=CLUSTER, Engine="memcached", CacheNodeType="cache.t3.micro", NumCacheNodes=1,
    ))
    parsed = _parse("CreateCacheCluster", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "InvalidParameterValue"
    assert "Engine=redis" in parsed["Error"]["Message"]


async def test_create_carries_security_group_ids_for_zero_drift(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores, SecurityGroupIds=["sg-abc123"])
    await _settle(cache, stores)
    (live,) = _parse("DescribeCacheClusters", await _describe(elasticache, sink, stores))["CacheClusters"]
    assert [g["SecurityGroupId"] for g in live["SecurityGroups"]] == ["sg-abc123"]


# --- describe --------------------------------------------------------------


async def test_describe_unknown_cluster_is_not_found(elasticache, sink, stores, cache):
    req = sink.call(lambda: elasticache.describe_cache_clusters(CacheClusterId="ghost"))
    parsed = _parse("DescribeCacheClusters", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "CacheClusterNotFound"


# --- modify ----------------------------------------------------------------


async def test_modify_applies_the_modelled_fields_in_place(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    req = sink.call(lambda: elasticache.modify_cache_cluster(
        CacheClusterId=CLUSTER, ApplyImmediately=True, CacheNodeType="cache.t3.small",
        PreferredMaintenanceWindow="MON:03:00-MON:04:00",
    ))
    cluster = _parse("ModifyCacheCluster", await _answer(stores, req))["CacheCluster"]
    assert cluster["CacheNodeType"] == "cache.t3.small"
    assert cluster["PreferredMaintenanceWindow"] == "mon:03:00-mon:04:00"  # provider normalizes to lower
    assert cluster["CacheClusterStatus"] == cachectl.STATUS_AVAILABLE  # metadata-only: nothing to wait on


async def test_modify_rejects_a_multi_node_resize(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    req = sink.call(lambda: elasticache.modify_cache_cluster(
        CacheClusterId=CLUSTER, ApplyImmediately=True, NumCacheNodes=2,
    ))
    parsed = _parse("ModifyCacheCluster", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "InvalidParameterValue"


async def test_modify_unknown_cluster_is_not_found(elasticache, sink, stores, cache):
    req = sink.call(lambda: elasticache.modify_cache_cluster(CacheClusterId="ghost", ApplyImmediately=True))
    parsed = _parse("ModifyCacheCluster", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "CacheClusterNotFound"


# --- delete ----------------------------------------------------------------


async def test_delete_removes_the_container_then_the_record(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    req = sink.call(lambda: elasticache.delete_cache_cluster(CacheClusterId=CLUSTER))
    cluster = _parse("DeleteCacheCluster", await _answer(stores, req))["CacheCluster"]
    assert cluster["CacheClusterStatus"] in (cachectl.STATUS_AVAILABLE, cachectl.STATUS_DELETING)
    for _ in range(200):
        if stores.cachectl.get(ENV, f"cluster:{CLUSTER}") is None:
            break
        await asyncio.sleep(0.01)
    assert cache.deleted == [(ENV, CLUSTER)]
    assert stores.cachectl.items(ENV) == {}
    # The provider's delete waiter's target state: gone == NotFound.
    parsed = _parse(
        "DescribeCacheClusters",
        await _describe(elasticache, sink, stores, CacheClusterId=CLUSTER), error=True,
    )
    assert parsed["Error"]["Code"] == "CacheClusterNotFound"
    assert stores.tags.get(ENV, f"elasticache:{cachectl.arn_for(CLUSTER)}") == {}


async def test_delete_unknown_cluster_is_not_found(elasticache, sink, stores, cache):
    req = sink.call(lambda: elasticache.delete_cache_cluster(CacheClusterId="ghost"))
    parsed = _parse("DeleteCacheCluster", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "CacheClusterNotFound"


# --- tags (the zero-drift half) --------------------------------------------


async def test_tags_seeded_on_create_are_readable_back(elasticache, sink, stores, cache):
    await _create(
        elasticache, sink, stores,
        Tags=[{"Key": "odin:node", "Value": CLUSTER}, {"Key": "team", "Value": "web"}],
    )
    await _settle(cache, stores)
    req = sink.call(lambda: elasticache.list_tags_for_resource(ResourceName=cachectl.arn_for(CLUSTER)))
    tags = _parse("ListTagsForResource", await _answer(stores, req))["TagList"]
    assert {t["Key"]: t["Value"] for t in tags} == {"odin:node": CLUSTER, "team": "web"}


async def test_add_and_remove_tags_round_trip(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores, Tags=[{"Key": "keep", "Value": "1"}])
    await _settle(cache, stores)
    arn = cachectl.arn_for(CLUSTER)
    add = sink.call(lambda: elasticache.add_tags_to_resource(ResourceName=arn, Tags=[{"Key": "extra", "Value": "2"}]))
    tags = _parse("AddTagsToResource", await _answer(stores, add))["TagList"]
    assert {t["Key"]: t["Value"] for t in tags} == {"keep": "1", "extra": "2"}
    drop = sink.call(lambda: elasticache.remove_tags_from_resource(ResourceName=arn, TagKeys=["extra"]))
    tags = _parse("RemoveTagsFromResource", await _answer(stores, drop))["TagList"]
    assert {t["Key"]: t["Value"] for t in tags} == {"keep": "1"}


async def test_tag_calls_on_an_unknown_cluster_are_not_found(elasticache, sink, stores, cache):
    req = sink.call(lambda: elasticache.list_tags_for_resource(ResourceName=cachectl.arn_for("ghost")))
    parsed = _parse("ListTagsForResource", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "CacheClusterNotFound"


# --- facts / dispatch ------------------------------------------------------


async def test_facts_publish_both_the_container_and_vm_reachable_forms(elasticache, sink, stores, cache):
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    record = stores.cachectl.get(ENV, f"cluster:{CLUSTER}")
    assert cachectl.facts(record) == {
        "REDIS_URL": f"redis://{CONTAINER_HOST}:{HOST_PORT}",
        "endpoint": f"{CONTAINER_HOST}:{HOST_PORT}",
        "REDIS_URL_VM": f"redis://{LIMA_HOST}:{HOST_PORT}",
        "endpoint_vm": f"{LIMA_HOST}:{HOST_PORT}",
        "port": str(HOST_PORT),
    }


def test_facts_are_empty_before_the_container_is_up(stores):
    assert cachectl.facts({"port": None, "address": None}) == {}


async def test_an_unknown_action_is_an_invalid_action_envelope(stores, cache):
    response = await cachectl.pure_answer("elasticache:RebootCacheCluster", CLUSTER, ENV, b"", stores, 0.0)
    assert response is not None and response.status_code == 400
    assert b"InvalidAction" in response.body


# --- a request that named NO cluster ---------------------------------------
#
# The logsctl/secretsctl/rdsctl defect in this module, and here NOT ONE handler
# checked -- not even the create path. Measured against the real handlers with
# `CacheClusterId` omitted:
#
#   DeleteCacheCluster -> 404 CacheClusterNotFound "CacheCluster not found: "
#   CreateCacheCluster -> 200, having MINTED the store key `cluster:` with
#                         `<CacheClusterId></CacheClusterId>` on the wire
#
# The second is not a message defect at all, it is a success odin did not
# achieve -- a record keyed by nothing that no later call can name (ecsctl's
# `taskdef::1`, exactly). ModifyCacheCluster and the tag ops then answered 200
# ABOUT that nameless record, so one unchecked identifier produced five wrong
# answers. `DescribeCacheClusters` stays ungated: an id-less describe is a
# legitimate list, which is also what botocore's model says.


@pytest.mark.parametrize("op,body,expected", [
    ("CreateCacheCluster", b"Action=CreateCacheCluster&Engine=redis", "CacheClusterId"),
    ("DeleteCacheCluster", b"Action=DeleteCacheCluster", "CacheClusterId"),
    ("ModifyCacheCluster", b"Action=ModifyCacheCluster", "CacheClusterId"),
    ("CreateCacheCluster", b"Action=CreateCacheCluster&CacheClusterId=", "CacheClusterId"),
    ("CreateCacheCluster", b"Action=CreateCacheCluster&CacheClusterId=%20%20", "CacheClusterId"),
    ("ListTagsForResource", b"Action=ListTagsForResource", "ResourceName"),
    ("AddTagsToResource", b"Action=AddTagsToResource", "ResourceName"),
    ("RemoveTagsFromResource", b"Action=RemoveTagsFromResource", "ResourceName"),
])
async def test_a_request_that_named_no_cluster_says_so_instead_of_answering_about_nothing(
    op, body, expected, stores, cache,
):
    response = await cachectl.pure_answer(f"elasticache:{op}", "", ENV, body, stores, 0.0)
    text = response.body.decode()

    assert response.status_code == 400, text
    assert "<Code>InvalidParameterValue</Code>" in text
    assert f"<Message>{expected} is required</Message>" in text
    assert "not found: <" not in text, "it must not blame a name that was never sent"
    # ...and nothing was minted under a nameless key.
    assert stores.cachectl.items(ENV) == {}


async def test_an_id_less_describe_is_still_a_legitimate_list(stores, sink, elasticache, cache):
    """The gate must not turn the LIST call into an error -- `tofu refresh`
    drives exactly this."""
    await _create(elasticache, sink, stores)
    await _settle(cache, stores)
    listed = _parse("DescribeCacheClusters", await _describe(elasticache, sink, stores))
    assert [c["CacheClusterId"] for c in listed["CacheClusters"]] == [CLUSTER]
