"""Canvas WIRING (field test 2, "the product hole"): a node's `env` map --
static entries plus `${{producer.ATTR}}` refs -- resolved from the gateway's own
live state, for injection into the REAL container at launch.

Before this, `spec/translate.py` parsed `env` and lifted refs into
`ResourceDesired.refs`, and NOTHING consumed either: `hcl.py` emitted no
`environment` block, `fabric.resolve` had no production caller, and the
container came up with the four AWS_* vars and nothing else.
"""
from __future__ import annotations

import os

import pytest

from odin.fabric.models import MeshNetwork, SubnetAllocation
from odin.gateway.stores import SynthStores
from odin.gateway.wiring import UnresolvedRef, db_facts, ec2_facts, node_env, producer_facts
from odin.reconcile import mesh_health, tf_status
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


_PRIVATE_IP = "192.168.64.9"
_OVERLAY_IP = "10.42.1.1"
_INSTANCE = "i-1"
_VM_NODE = "web1"


def _stores(tmp_path) -> SynthStores:
    return SynthStores(tmp_path)


def _seed_ec2(stores: SynthStores, tmp_path, *, state: str = "running", private_ip: str | None = _PRIVATE_IP,
              lighthouse: bool = True, label: str | None = _VM_NODE) -> None:
    """A real ec2 record plus this env's real nebula state, exactly as
    `tests/reconcile/test_tf_status.py` builds them -- so the two producers of
    these facts are seeded from the SAME shapes."""
    stores.ec2compute.set(ENV, f"instance:{_INSTANCE}", {
        "instance_id": _INSTANCE, "state_name": state, "state_reason": None, "private_ip": private_ip,
    })
    if label:
        stores.tags.set(ENV, f"ec2:{_INSTANCE}", {"odin:node": label})
    nebula = tmp_path / ENV / "nebula"
    nebula.mkdir(parents=True, exist_ok=True)
    (nebula / "ca.crt").write_text("---ca---\n")
    (nebula / "overlay.json").write_text(MeshNetwork(
        network=ENV, subnets={"hosts": SubnetAllocation(
            network=ENV, subnet="hosts", cidr="10.42.1.0/24", next_ip=2,
            assignments={_INSTANCE: _OVERLAY_IP},
        )},
    ).model_dump_json())
    if lighthouse:
        (nebula / "lighthouse.pid").write_text(str(os.getpid()))
    mesh_health.reset_cache()


@pytest.fixture(autouse=True)
def _clean_mesh_cache():
    """`mesh_health`'s cache is process-wide and keyed on (root, env, member);
    `tmp_path` makes the root unique per test, but a leftover entry from an
    earlier test in the same process must never decide this one."""
    mesh_health.reset_cache()
    yield
    mesh_health.reset_cache()


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


def test_ec2_facts_matches_the_world_projections_own_builder():
    """Same guard, for the ec2 pair. `wiring.ec2_facts` duplicates
    `tf_status._ec2_facts` for the same import-cycle reason as `db_facts`."""
    record = {"instance_id": _INSTANCE, "state_name": "running", "private_ip": _PRIVATE_IP}
    overlay = {_INSTANCE: _OVERLAY_IP}
    assert ec2_facts(record, overlay) == tf_status._ec2_facts(record, overlay)
    assert ec2_facts(record, {}) == tf_status._ec2_facts(record, {})
    no_ip = {**record, "private_ip": None}
    assert ec2_facts(no_ip, overlay) == tf_status._ec2_facts(no_ip, overlay)


@pytest.mark.parametrize("state", ["running", "pending", "stopped", "shutting-down", "terminated"])
def test_the_injector_publishes_exactly_what_world_publishes_for_an_ec2(tmp_path, state):
    """The whole-pipeline guard, and the actual contract of this feature: for
    any instance state, `${{web1.X}}` resolves to a value if and only if
    `/world` shows `X` on that node. Two independently-built projections of
    one truth, held to each other rather than to a hand-written expectation --
    which is what went wrong when the mesh half and the injector half were
    built in parallel."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, state=state)
    projected = tf_status.project(stores, ENV).get(_VM_NODE)
    mesh_health.reset_cache()
    assert producer_facts(stores, ENV).get(_VM_NODE, {}) == (projected[2] if projected else {})


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


# --- ec2 as a producer ------------------------------------------------------
#
# The gap this closes: the mesh work shipped `${{web1.PRIVATE_IP}}` /
# `${{web1.MESH_IP}}` into World, and the wiring work shipped the injector,
# in parallel -- so an ECS task or Lambda could not consume a fact that
# demonstrably existed. Only the NAMES had been coordinated.


def test_a_running_ec2_publishes_its_private_and_overlay_addresses(tmp_path):
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path)
    assert producer_facts(stores, ENV)[_VM_NODE] == {"PRIVATE_IP": _PRIVATE_IP, "MESH_IP": _OVERLAY_IP}


def test_an_ec2_that_is_not_running_publishes_nothing(tmp_path):
    """The same gate every other producer gets: a VM that is still booting has
    no address to hand out, so a consumer must fail rather than receive one."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, state="pending")
    assert producer_facts(stores, ENV) == {}


def test_an_untagged_ec2_is_not_a_producer(tmp_path):
    """No `odin:node` tag means no canvas label, and a ref is written against
    the label -- there is nothing to publish it under (real EC2 has no native
    name field to fall back to either)."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, label=None)
    assert producer_facts(stores, ENV) == {}


def test_a_dead_lighthouse_withholds_mesh_ip_but_not_private_ip(tmp_path):
    """`reconcile/mesh_health.py`'s rule, applied verbatim to the injector: an
    overlay address no peer can find or relay to is not published at all. The
    host-reachable private address is untouched -- the VM really is still
    reachable that way."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, lighthouse=False)
    assert producer_facts(stores, ENV)[_VM_NODE] == {"PRIVATE_IP": _PRIVATE_IP}


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


def test_a_workload_consumes_a_vms_addresses_through_its_env_map(tmp_path):
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path)
    _save_stack(tmp_path, [_ecs_node({"VM_HOST": "${{web1.PRIVATE_IP}}", "VM_MESH": "${{web1.MESH_IP}}"})])
    assert node_env(stores, ENV, "web") == {"VM_HOST": _PRIVATE_IP, "VM_MESH": _OVERLAY_IP}


def test_a_withheld_mesh_ip_fails_the_ref_instead_of_injecting_an_empty_string(tmp_path):
    """The explicit requirement for this feature. `MESH_IP` is withheld when
    the env's lighthouse is down; the consumer must then fail exactly like any
    other unresolvable ref -- never an empty string, and never the stale
    address from before the lighthouse died. The message names what the VM
    DOES still publish, so the fix (use `PRIVATE_IP`, or bring the lighthouse
    back) is visible from the verdict alone."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, lighthouse=False)
    _save_stack(tmp_path, [_ecs_node({"VM_MESH": "${{web1.MESH_IP}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "VM_MESH" in message
    assert "'MESH_IP'" in message
    assert "PRIVATE_IP" in message  # what it DOES publish
    assert _OVERLAY_IP not in message  # and never the address it just refused to hand out


def test_a_ref_to_a_vm_that_is_not_up_yet_fails_honestly(tmp_path):
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, state="pending")
    _save_stack(tmp_path, [_ecs_node({"VM_HOST": "${{web1.PRIVATE_IP}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        node_env(stores, ENV, "web")
    assert "not healthy yet" in str(excinfo.value)


def test_a_lambda_node_resolves_the_same_way(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _save_stack(tmp_path, [
        {"id": "n1", "type": "lambda", "data": {"label": "notify", "env": {"DB": "${{appdb.endpoint}}"}}},
    ])
    assert node_env(stores, ENV, "notify") == {"DB": f"{CONTAINER_HOST}:33366"}
