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

from odin.aws.backings import PROVISIONED, BackingAws
from odin.fabric.models import MeshNetwork, SubnetAllocation
from odin.gateway.models import elbv2ctl
from odin.gateway.stores import SynthStores
from odin.gateway.wiring import UnresolvedRef, db_facts, ec2_facts, node_env, producer_facts
from odin.reconcile import mesh_health, tf_status
from odin.runtime.colima import CONTAINER_HOST
from odin.spec.models import REFERENCEABLE_KINDS
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


_ALB_NODE = "web-lb"


def _seed_alb(stores: SynthStores) -> None:
    """An `active` elbv2ctl record with a published proxy port -- the same shape
    `tests/reconcile/test_tf_status.py::_lb` builds, so the two projections of an
    ALB's one fact are seeded identically."""
    stores.elbv2ctl.set(ENV, f"lb:{_ALB_NODE}", {
        "name": _ALB_NODE, "lb_id": "abc123", "arn": elbv2ctl.lb_arn(_ALB_NODE, "abc123"),
        "scheme": "internal", "type": "application", "ip_address_type": "ipv4",
        "vpc_id": "vpc-1", "subnets": ["subnet-1"], "security_groups": [],
        "availability_zones": [], "created_time": "2026-07-25T00:00:00+00:00",
        "state": "active", "state_reason": None, "attributes": {}, "endpoints": {"80": 41234},
    })


def _save_stack(tmp_path, nodes: list[dict]) -> None:
    SpecStore(tmp_path).apply(canvas_to_stack({"nodes": nodes, "edges": []}, env=ENV))


def _ecs_node(env_map: dict) -> dict:
    return {"id": "n1", "type": "ecs", "data": {"label": "web", "image": "nginx:alpine", "env": env_map}}


# The PRODUCER node as it exists on a real canvas. Field test 6 (F3): the
# unresolved-ref reason is now derived from the target's KIND, so a test that
# seeds only the consumer is testing the "odin cannot tell what this node is"
# branch rather than the readiness branch it means to.
_DB_NODE = {"id": "n2", "type": "rds", "data": {"label": "appdb"}}
_VM_CANVAS_NODE = {"id": "n3", "type": "ec2", "data": {"label": _VM_NODE}}
_QUEUE_NODE = {"id": "n4", "type": "sqs", "data": {"label": "jobs"}}


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
async def test_the_injector_publishes_exactly_what_world_publishes_for_an_ec2(tmp_path, state):
    """The whole-pipeline guard, and the actual contract of this feature: for
    any instance state, `${{web1.X}}` resolves to a value if and only if
    `/world` shows `X` on that node. Two independently-built projections of
    one truth, held to each other rather than to a hand-written expectation --
    which is what went wrong when the mesh half and the injector half were
    built in parallel."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, state=state)
    projected = (await tf_status.project(stores, ENV)).get(_VM_NODE)
    mesh_health.reset_cache()
    assert (await producer_facts(stores, ENV)).get(_VM_NODE, {}) == (projected[2] if projected else {})


# --- REFERENCEABLE_KINDS, held to both halves that read it -----------------
#
# Field test 6, F3. The list used to be prose inside one error string, and it
# went wrong in the direction prose always goes: it said an sqs node "publishes
# no facts" when what was true was "an sqs node is not a WIRING producer". These
# two tests are what stop the tuple and the code drifting apart again -- one on
# the gateway side (which builds the values) and one on the hcl side (which
# refuses a ref before tofu runs).


async def test_every_referenceable_kind_really_publishes_and_no_other_kind_does(tmp_path):
    """One live producer per `REFERENCEABLE_KINDS`, all four in the same env: the
    tuple is exactly the set `producer_facts` can build for. A kind added to the
    tuple without a builder, or a builder added without the tuple, fails here."""
    stores = _stores(tmp_path)
    _seed_db(stores)
    _seed_cache(stores)
    _seed_alb(stores)
    _seed_ec2(stores, tmp_path)
    facts = await producer_facts(stores, ENV)
    assert set(facts) == {"appdb", "cache", _ALB_NODE, _VM_NODE}
    assert all(facts.values()), "a producer in the table must publish at least one fact"
    # ...and every kind NOT in the tuple contributes nothing, which is the claim
    # the error message now makes on the strength of this.
    assert set(REFERENCEABLE_KINDS) == {"rds", "elasticache", "alb", "ec2"}


class _PortOnlyRuntime:
    """Just enough runtime for the REAL `BackingAws.facts` to run, so the facts
    asserted below are the ones production publishes."""

    async def host_port(self, name, container_port):
        return 51001


async def test_the_four_provisioned_kinds_publish_world_facts_and_are_still_not_producers(tmp_path):
    """The exact pair of readings the field test took against a real server, as a
    test: `aws/backings.py::facts` authors a real fact for each of s3/sqs/sns/
    dynamodb -- which is why they show up in `odin world` -- and none of them is
    a wiring producer. The error message may only say the second thing."""
    assert set(PROVISIONED) == {"s3", "sqs", "sns", "dynamodb"}
    assert not set(PROVISIONED) & set(REFERENCEABLE_KINDS)
    aws = BackingAws(_PortOnlyRuntime(), env=ENV, root=tmp_path)
    # A plain loop, not `{k: await aws.facts(...) for k in ...}`: an `await`
    # inside a comprehension is legal here but one character away from an ASYNC
    # GENERATOR that a plain `for` silently cannot iterate.
    published = {}
    for kind in PROVISIONED:
        published[kind] = await aws.facts(kind, "thing")
    # Each one really does publish a named fact -- so "a kind that publishes no
    # facts" is a false statement about every one of them.
    assert {kind: sorted(facts) for kind, facts in published.items()} == {
        "s3": ["BUCKET", "endpoint"],
        "sqs": ["QUEUE_URL", "endpoint"],
        "sns": ["TOPIC_ARN", "endpoint"],
        "dynamodb": ["TABLE", "endpoint"],
    }


# --- producer_facts --------------------------------------------------------


async def test_producer_facts_publishes_rds_and_cache_endpoints_by_label(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _seed_cache(stores)
    facts = await producer_facts(stores, ENV)
    assert facts["appdb"]["DATABASE_URL"] == f"postgresql://app:s3cret@{CONTAINER_HOST}:33366/shop"
    assert facts["cache"]["REDIS_URL"] == f"redis://{CONTAINER_HOST}:33364"


async def test_producer_facts_prefers_the_odin_node_tag_over_the_native_name(tmp_path):
    """The canvas label is the identity every ref is written against, and
    `hcl.py::_tags_block` stamps it as `odin:node` -- the same bridge
    `workload_env` and the World projection use."""
    stores = _stores(tmp_path)
    _seed_db(stores)
    stores.tags.set(ENV, "rds:arn:aws:rds:us-east-1:000000000000:db:appdb", {"odin:node": "primary-db"})
    facts = await producer_facts(stores, ENV)
    assert "primary-db" in facts and "appdb" not in facts


async def test_producer_facts_withholds_a_database_that_is_not_available_yet(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores, status="creating")
    assert await producer_facts(stores, ENV) == {}


# --- ec2 as a producer ------------------------------------------------------
#
# The gap this closes: the mesh work shipped `${{web1.PRIVATE_IP}}` /
# `${{web1.MESH_IP}}` into World, and the wiring work shipped the injector,
# in parallel -- so an ECS task or Lambda could not consume a fact that
# demonstrably existed. Only the NAMES had been coordinated.


async def test_a_running_ec2_publishes_its_private_and_overlay_addresses(tmp_path):
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path)
    assert (await producer_facts(stores, ENV))[_VM_NODE] == {"PRIVATE_IP": _PRIVATE_IP, "MESH_IP": _OVERLAY_IP}


async def test_an_ec2_that_is_not_running_publishes_nothing(tmp_path):
    """The same gate every other producer gets: a VM that is still booting has
    no address to hand out, so a consumer must fail rather than receive one."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, state="pending")
    assert await producer_facts(stores, ENV) == {}


async def test_an_untagged_ec2_is_not_a_producer(tmp_path):
    """No `odin:node` tag means no canvas label, and a ref is written against
    the label -- there is nothing to publish it under (real EC2 has no native
    name field to fall back to either)."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, label=None)
    assert await producer_facts(stores, ENV) == {}


async def test_a_dead_lighthouse_withholds_mesh_ip_but_not_private_ip(tmp_path):
    """`reconcile/mesh_health.py`'s rule, applied verbatim to the injector: an
    overlay address no peer can find or relay to is not published at all. The
    host-reachable private address is untouched -- the VM really is still
    reachable that way."""
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, lighthouse=False)
    assert (await producer_facts(stores, ENV))[_VM_NODE] == {"PRIVATE_IP": _PRIVATE_IP}


# --- node_env --------------------------------------------------------------


async def test_node_env_resolves_refs_and_keeps_static_entries(tmp_path):
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
    assert await node_env(stores, ENV, "web") == {
        "APP_TIER": "web",
        "DATABASE_URL": f"postgresql://app:s3cret@{CONTAINER_HOST}:33366/shop",
        "REDIS_URL": f"redis://{CONTAINER_HOST}:33364",
    }


async def test_node_env_is_empty_for_a_node_that_is_not_on_the_canvas(tmp_path):
    stores = _stores(tmp_path)
    _save_stack(tmp_path, [_ecs_node({"A": "b"})])
    assert await node_env(stores, ENV, "not-a-node") == {}


async def test_an_unhealthy_producer_fails_loudly_instead_of_injecting_an_empty_string(tmp_path):
    """The explicit requirement: a ref to a not-yet-healthy producer must fail
    honestly. An empty DATABASE_URL would let the app fail far from the cause."""
    stores = _stores(tmp_path)
    _seed_db(stores, status="creating")
    _save_stack(tmp_path, [_ecs_node({"DATABASE_URL": "${{appdb.DATABASE_URL}}"}), _DB_NODE])
    with pytest.raises(UnresolvedRef) as excinfo:
        await node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "appdb" in message
    # An rds IS a valid producer, so this is readiness -- and the message has to
    # say that rather than the wiring-model reason (field test 6, F3).
    assert "has not published its endpoint yet" in message
    assert "IS a valid reference producer" in message
    assert "publishes no facts" not in message


async def test_a_ref_to_a_nonexistent_node_fails_loudly(tmp_path):
    stores = _stores(tmp_path)
    _save_stack(tmp_path, [_ecs_node({"DATABASE_URL": "${{typo.DATABASE_URL}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        await node_env(stores, ENV, "web")
    assert "typo" in str(excinfo.value)


async def test_a_ref_to_an_attribute_the_producer_does_not_publish_names_what_it_does(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _save_stack(tmp_path, [_ecs_node({"X": "${{appdb.NOPE}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        await node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "'NOPE'" in message
    assert "DATABASE_URL" in message  # what it DOES publish, so the fix is obvious


async def test_a_workload_consumes_a_vms_addresses_through_its_env_map(tmp_path):
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path)
    _save_stack(tmp_path, [_ecs_node({"VM_HOST": "${{web1.PRIVATE_IP}}", "VM_MESH": "${{web1.MESH_IP}}"})])
    assert await node_env(stores, ENV, "web") == {"VM_HOST": _PRIVATE_IP, "VM_MESH": _OVERLAY_IP}


async def test_a_withheld_mesh_ip_fails_the_ref_instead_of_injecting_an_empty_string(tmp_path):
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
        await node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "VM_MESH" in message
    assert "'MESH_IP'" in message
    assert "PRIVATE_IP" in message  # what it DOES publish
    assert _OVERLAY_IP not in message  # and never the address it just refused to hand out


async def test_a_ref_to_a_vm_that_is_not_up_yet_fails_honestly(tmp_path):
    stores = _stores(tmp_path)
    _seed_ec2(stores, tmp_path, state="pending")
    _save_stack(tmp_path, [_ecs_node({"VM_HOST": "${{web1.PRIVATE_IP}}"}), _VM_CANVAS_NODE])
    with pytest.raises(UnresolvedRef) as excinfo:
        await node_env(stores, ENV, "web")
    assert "has not published its endpoint yet" in str(excinfo.value)


async def test_a_ref_to_a_kind_that_can_never_produce_says_so_and_does_not_deny_its_facts(tmp_path):
    """Field test 6, F3's sub-finding. The message said an sqs node "publishes no
    facts (only rds, elasticache, alb and ec2 do)" -- while `/world` was
    publishing that same node's `QUEUE_URL`. Measured against a real server at
    the same instant:

        /world?env=srvfixf3  ->  sqs srvfix-queue healthy
                                 {"QUEUE_URL": "http://host.docker.internal:4796/...", ...}
        wiring.producer_facts(stores, "srvfixf3")  ->  {}

    Both readings were right; the SENTENCE conflated observed facts with wiring
    values. It must now name the real reason, name which kinds ARE referenceable,
    and NOT deny the facts the user can see."""
    stores = _stores(tmp_path)
    _save_stack(tmp_path, [_ecs_node({"QUEUE_URL": "${{jobs.QUEUE_URL}}"}), _QUEUE_NODE])
    with pytest.raises(UnresolvedRef) as excinfo:
        await node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "'jobs' is a sqs node" in message
    assert "no sqs node publishes an endpoint a reference can resolve" in message
    assert "only rds, elasticache, alb and ec2 do" in message
    # The claim the field test falsified must not come back in any form.
    assert "publishes no facts" not in message
    assert "not healthy yet" not in message
    assert "has not published its endpoint yet" not in message
    # ...and it says what DOES work instead.
    assert "AWS_ENDPOINT_URL" in message


async def test_a_ref_whose_target_kind_is_unknown_says_that_rather_than_guessing(tmp_path):
    """A resource no canvas node backs (an imported HCL project) has no kind in
    either the staged wiring or the applied Stack. Reporting either of the other
    two reasons would be a claim odin cannot make."""
    stores = _stores(tmp_path)
    _save_stack(tmp_path, [_ecs_node({"X": "${{ghost.ENDPOINT}}"})])
    with pytest.raises(UnresolvedRef) as excinfo:
        await node_env(stores, ENV, "web")
    message = str(excinfo.value)
    assert "cannot tell what kind of node 'ghost' is" in message
    assert "Nothing resolves from it either way" in message


async def test_a_lambda_node_resolves_the_same_way(tmp_path):
    stores = _stores(tmp_path)
    _seed_db(stores)
    _save_stack(tmp_path, [
        {"id": "n1", "type": "lambda", "data": {"label": "notify", "env": {"DB": "${{appdb.endpoint}}"}}},
    ])
    assert await node_env(stores, ENV, "notify") == {"DB": f"{CONTAINER_HOST}:33366"}
