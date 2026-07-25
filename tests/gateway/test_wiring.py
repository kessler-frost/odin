"""Canvas WIRING (field test 2, "the product hole"): a node's `env` map --
static entries plus `${{producer.ATTR}}` refs -- resolved from the gateway's own
live state, for injection into the REAL container at launch.

Before this, `spec/translate.py` parsed `env` and lifted refs into
`ResourceDesired.refs`, and NOTHING consumed either: `hcl.py` emitted no
`environment` block, `fabric.resolve` had no production caller, and the
container came up with the four AWS_* vars and nothing else.
"""
from __future__ import annotations

import pytest

from odin.gateway.stores import SynthStores
from odin.gateway.wiring import UnresolvedRef, db_facts, node_env, producer_facts
from odin.reconcile import tf_status
from odin.runtime.colima import CONTAINER_HOST
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack

ENV = "default"

_DB_RECORD = {
    "db_instance_identifier": "appdb",
    "master_username": "app",
    "master_password": "s3cret",
    "db_name": "shop",
    "status": "available",
    "endpoint_port": 33366,
}
_CACHE_RECORD = {
    "cache_cluster_id": "cache",
    "arn": "arn:aws:elasticache:us-east-1:000000000000:cluster:cache",
    "status": "available",
    "address": CONTAINER_HOST,
    "port": 33364,
}


def _stores(tmp_path) -> SynthStores:
    return SynthStores(tmp_path)


def _seed_db(stores: SynthStores, **overrides) -> None:
    record = {**_DB_RECORD, **overrides}
    stores.rdsctl.set(ENV, f"db:{record['db_instance_identifier']}", record)


def _seed_cache(stores: SynthStores, **overrides) -> None:
    record = {**_CACHE_RECORD, **overrides}
    stores.cachectl.set(ENV, f"cluster:{record['cache_cluster_id']}", record)


def _save_stack(tmp_path, nodes: list[dict]) -> None:
    SpecStore(tmp_path).apply(canvas_to_stack({"nodes": nodes, "edges": []}, env=ENV))


def _ecs_node(env_map: dict) -> dict:
    return {"id": "n1", "type": "ecs", "data": {"label": "web", "image": "nginx:alpine", "env": env_map}}


# --- the duplication guard --------------------------------------------------


def test_db_facts_matches_the_world_projections_own_builder():
    """`wiring.db_facts` is a deliberate duplicate of
    `tf_status._db_facts` (wiring cannot import tf_status -- tf_status imports
    ecsctl, which imports wiring). This test is what keeps them from drifting
    apart silently."""
    assert db_facts(_DB_RECORD) == tf_status._db_facts(_DB_RECORD)
    with_mesh = {**_DB_RECORD, "overlay_ip": "10.42.1.3"}
    assert db_facts(with_mesh) == tf_status._db_facts(with_mesh)
    assert db_facts({**_DB_RECORD, "endpoint_port": 0}) == tf_status._db_facts({**_DB_RECORD, "endpoint_port": 0})


# --- producer_facts --------------------------------------------------------


def test_producer_facts_publishes_rds_and_cache_endpoints_by_label(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _seed_cache(stores)
    facts = producer_facts(stores, ENV)
    assert facts["appdb"]["DATABASE_URL"] == f"postgresql://app:s3cret@{CONTAINER_HOST}:33366/shop"
    assert facts["cache"]["REDIS_URL"] == f"redis://{CONTAINER_HOST}:33364"


def test_producer_facts_prefers_the_odin_node_tag_over_the_native_name(tmp_path):
    """The canvas label is the identity every ref is written against, and
    `hcl.py::_tags_block` stamps it as `odin:node` -- the same bridge
    `workload_env` and the World projection use."""
    stores = _stores(tmp_path)
    _seed_db(stores)
    stores.tags.set(ENV, "rds:arn:aws:rds:us-east-1:000000000000:db:appdb", {"odin:node": "primary-db"})
    facts = producer_facts(stores, ENV)
    assert "primary-db" in facts and "appdb" not in facts


def test_producer_facts_withholds_a_database_that_is_not_available_yet(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores, status="creating")
    assert producer_facts(stores, ENV) == {}


# --- node_env --------------------------------------------------------------


def test_node_env_resolves_refs_and_keeps_static_entries(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _seed_cache(stores)
    _save_stack(tmp_path, [
        _ecs_node({
            "DATABASE_URL": "${{appdb.DATABASE_URL}}",
            "REDIS_URL": "${{cache.REDIS_URL}}",
            "APP_TIER": "web",
        }),
    ])
    assert node_env(stores, ENV, "web") == {
        "APP_TIER": "web",
        "DATABASE_URL": f"postgresql://app:s3cret@{CONTAINER_HOST}:33366/shop",
        "REDIS_URL": f"redis://{CONTAINER_HOST}:33364",
    }


def test_node_env_is_empty_for_a_node_that_is_not_on_the_canvas(tmp_path):
    stores = _stores(tmp_path)
    _save_stack(tmp_path, [_ecs_node({"A": "b"})])
    assert node_env(stores, ENV, "not-a-node") == {}


def test_an_unhealthy_producer_fails_loudly_instead_of_injecting_an_empty_string(tmp_path):
    """The explicit requirement: a ref to a not-yet-healthy producer must fail
    honestly. An empty DATABASE_URL would let the app fail far from the cause."""
    stores = _stores(tmp_path)
    _seed_db(stores, status="creating")
    _save_stack(tmp_path, [_ecs_node({"DATABASE_URL": "${{appdb.DATABASE_URL}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "appdb" in message
    assert "not healthy yet" in message


def test_a_ref_to_a_nonexistent_node_fails_loudly(tmp_path):
    stores = _stores(tmp_path)
    _save_stack(tmp_path, [_ecs_node({"DATABASE_URL": "${{typo.DATABASE_URL}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        node_env(stores, ENV, "web")
    assert "typo" in str(excinfo.value)


def test_a_ref_to_an_attribute_the_producer_does_not_publish_names_what_it_does(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _save_stack(tmp_path, [_ecs_node({"X": "${{appdb.NOPE}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "'NOPE'" in message
    assert "DATABASE_URL" in message  # what it DOES publish, so the fix is obvious


def test_a_lambda_node_resolves_the_same_way(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _save_stack(tmp_path, [
        {"id": "n1", "type": "lambda", "data": {"label": "notify", "env": {"DB": "${{appdb.endpoint}}"}}},
    ])
    assert node_env(stores, ENV, "notify") == {"DB": f"{CONTAINER_HOST}:33366"}
