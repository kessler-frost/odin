"""The wiring: applied records -> a real /etc/hosts push, per running instance.

EVERY EXPECTATION HERE IS A LITERAL. Nothing calls `vm_hosts` to work out what
the answer should be — the expected hosts map is spelled out as
`{"api.odin.internal": "10.42.0.5"}`, so a wiring that passed the container
plan where the VM plan belongs, or handed `push_hosts` the unresolvable half,
fails. Deriving the expectation from the resolver would make the subject grade
itself and the test could not fail (honesty rule 5).

The overlay substitution is the thing under test at this layer: `i-web`'s
record VALUE is `192.168.64.2` and what must reach the guest is `10.42.0.5`.
A test asserting only "something was pushed" would pass with the private_ip
written, which is the one address a VM cannot reach.
"""
from __future__ import annotations

import pytest

from odin.compute.instances import vm_name
from odin.fabric.models import MeshNetwork, SubnetAllocation
from odin.fabric.nebula import NebulaManager
from odin.gateway.route53_hosts import ensure_instance_hosts, overlay_addresses, records
from odin.gateway.stores import SynthStores

ENV = "r53w"
WEB = "i-web"
DB = "i-db"

# The literals every assertion below is measured against.
RECORD_VALUE = "192.168.64.2"     # what tofu resolved aws_instance.private_ip to
OVERLAY_IP = "10.42.0.5"          # what a VM must actually be told
FQDN = "api.odin.internal"


def _instance(instance_id: str, address: str, state: str = "running") -> dict:
    return {"instance_id": instance_id, "state_name": state,
            "private_ip": address, "public_ip": address, "vpc_id": "vpc-1"}


def _record(name: str, *values: str) -> dict:
    return {"name": name, "type": "A", "ttl": 60, "set_identifier": None,
            "values": list(values), "alias": None}


class FakeVm:
    """Records what the wiring actually handed the substrate."""

    def __init__(self, action: str = "pushed") -> None:
        self.pushes: list[tuple[str, str, dict[str, str]]] = []
        self._action = action

    async def push_hosts(self, name, root, env, host_id, hosts):
        self.pushes.append((name, host_id, dict(hosts)))
        return self._action


@pytest.fixture
def stores(tmp_path):
    store = SynthStores(tmp_path)
    store.route53ctl.set(ENV, "zone:odin.internal", {"name": "odin.internal"})
    store.route53ctl.set(ENV, "rrset:odin.internal", [_record(FQDN, RECORD_VALUE)])
    store.ec2compute.set(ENV, f"instance:{WEB}", _instance(WEB, RECORD_VALUE))
    return store


def _overlay(tmp_path, assignments: dict[str, str]) -> None:
    """Write the overlay through `NebulaManager.save_overlay` -- the REAL
    producer -- rather than hand-writing JSON.

    The first version of this helper invented a `Subnet` model that does not
    exist. Had it invented one that merely differed in shape, the tests would
    have passed against a file `fabric/nebula.py` never writes: a fixture
    fabricating its upstream, which is the defect that let `alb -> ec2` ship
    unrun. Going through the real writer means the on-disk bytes are whatever
    odin really produces."""
    manager = NebulaManager(tmp_path / ENV / "nebula")
    manager.save_overlay(MeshNetwork(
        network=ENV,
        subnets={"hosts": SubnetAllocation(
            network=ENV, subnet="hosts", cidr="10.42.1.0/24",
            assignments=dict(assignments),
        )},
    ))


# --- the substitution, which is the whole point -----------------------------


async def test_the_vm_is_handed_the_overlay_address_not_the_record_value(stores, tmp_path):
    _overlay(tmp_path, {WEB: OVERLAY_IP})
    vm = FakeVm()

    verdicts = await ensure_instance_hosts(stores, ENV, vm=vm)

    name = vm_name(ENV, WEB)
    assert vm.pushes == [(name, WEB, {FQDN: OVERLAY_IP})]
    # Spelled out rather than derived: the record's own value must NOT be what
    # reached the guest. A VM cannot reach another VM's vzNAT address.
    assert vm.pushes[0][2] != {FQDN: RECORD_VALUE}
    assert verdicts[name].healthy is True
    assert verdicts[name].reason == ""


async def test_an_env_with_no_mesh_pushes_nothing_and_reports_it(stores, tmp_path):
    """No overlay file at all -- the mesh-less env. The name must NOT resolve,
    and the instance must be visible in World rather than silently fine."""
    vm = FakeVm()

    verdicts = await ensure_instance_hosts(stores, ENV, vm=vm)

    name = vm_name(ENV, WEB)
    assert vm.pushes == [(name, WEB, {})]
    assert verdicts[name].healthy is False
    assert "no mesh" in verdicts[name].reason
    assert FQDN in verdicts[name].reason


async def test_a_terminated_target_is_not_reported_as_a_missing_mesh(stores, tmp_path):
    """The bug this module had before review: every unresolvable name was
    reported as `no_mesh`, so a record pointing at an instance that no longer
    exists was explained as "this environment has no mesh" -- sending the
    reader to fix something that is not broken. The env here IS meshed."""
    _overlay(tmp_path, {WEB: OVERLAY_IP})
    stores.route53ctl.set(ENV, "rrset:odin.internal", [_record(FQDN, "10.9.9.9")])
    vm = FakeVm()

    verdicts = await ensure_instance_hosts(stores, ENV, vm=vm)

    reason = verdicts[vm_name(ENV, WEB)].reason
    assert "no mesh" not in reason, reason
    assert "10.9.9.9" in reason
    assert "no EC2 instance in this environment reports" in reason


# --- which instances get written to ----------------------------------------


async def test_only_running_instances_are_written_to(stores, tmp_path):
    """A pending or terminated instance has no guest to write to, and a
    verdict about its /etc/hosts would describe a file that does not exist.
    It gets its records at boot, through cloud-init."""
    _overlay(tmp_path, {WEB: OVERLAY_IP, DB: "10.42.0.6"})
    stores.ec2compute.set(ENV, f"instance:{DB}", _instance(DB, "192.168.64.3", state="pending"))
    vm = FakeVm()

    verdicts = await ensure_instance_hosts(stores, ENV, vm=vm)

    assert [p[1] for p in vm.pushes] == [WEB]
    assert set(verdicts) == {vm_name(ENV, WEB)}


async def test_a_record_naming_a_stopped_instance_still_resolves(stores, tmp_path):
    """Resolution and liveness are different questions. A record pointing at a
    STOPPED instance must still resolve on the VMs that are up -- real Route 53
    returns it, DNS does not model liveness, and the alternative is every name
    silently ceasing to resolve the moment its target is stopped, which reports
    an outage as a DNS failure.

    So the resolver sees ALL instances while only running ones are written to.
    Collapsing those two sets is the regression this pins."""
    _overlay(tmp_path, {WEB: OVERLAY_IP, DB: "10.42.0.6"})
    stores.ec2compute.set(ENV, f"instance:{DB}", _instance(DB, "192.168.64.3", state="stopped"))
    stores.route53ctl.set(ENV, "rrset:odin.internal", [_record(FQDN, "192.168.64.3")])
    vm = FakeVm()

    await ensure_instance_hosts(stores, ENV, vm=vm)

    # Written only to the running VM, but resolving to the STOPPED one's overlay.
    assert [p[1] for p in vm.pushes] == [WEB]
    assert vm.pushes[0][2] == {FQDN: "10.42.0.6"}


async def test_every_running_instance_gets_the_same_records(stores, tmp_path):
    """Records are env-wide, not per-instance: a name resolves the same way
    everywhere or the env is inconsistent with itself."""
    _overlay(tmp_path, {WEB: OVERLAY_IP, DB: "10.42.0.6"})
    stores.ec2compute.set(ENV, f"instance:{DB}", _instance(DB, "192.168.64.3"))
    vm = FakeVm()

    await ensure_instance_hosts(stores, ENV, vm=vm)

    assert sorted(p[1] for p in vm.pushes) == [DB, WEB]
    assert [p[2] for p in vm.pushes] == [{FQDN: OVERLAY_IP}, {FQDN: OVERLAY_IP}]


async def test_no_records_touches_nothing(stores, tmp_path):
    """An env with no route53 nodes must cost zero `limactl` calls, not an
    empty push per instance -- this runs on every Apply."""
    _overlay(tmp_path, {WEB: OVERLAY_IP})
    stores.route53ctl.set(ENV, "rrset:odin.internal", [])
    vm = FakeVm()

    assert await ensure_instance_hosts(stores, ENV, vm=vm) == {}
    assert vm.pushes == []


async def test_no_instances_touches_nothing(stores, tmp_path):
    _overlay(tmp_path, {})
    stores.ec2compute.set(ENV, f"instance:{WEB}", _instance(WEB, RECORD_VALUE, state="terminated"))
    vm = FakeVm()

    assert await ensure_instance_hosts(stores, ENV, vm=vm) == {}
    assert vm.pushes == []


# --- failure carries through -----------------------------------------------


async def test_a_failed_push_is_reported_not_swallowed(stores, tmp_path):
    _overlay(tmp_path, {WEB: OVERLAY_IP})
    vm = FakeVm(action="failed")

    verdicts = await ensure_instance_hosts(stores, ENV, vm=vm)

    verdict = verdicts[vm_name(ENV, WEB)]
    assert verdict.healthy is False
    assert "could not write" in verdict.reason


# --- the seam World reads ---------------------------------------------------
#
# `reconcile/tf_status.py::_route53_zones` rebuilds a HostsVerdict from
# `hosts_action` / `hosts_names` / `hosts_details` on the instance record, and
# its own docstring says those are written by "the resolver". They were NOT,
# until the projector was read rather than assumed -- a field nobody wrote, so
# every zone would have read `healthy` whatever a push did. These pin the
# field names as literals; renaming one here without renaming it there breaks
# the projection silently.


async def test_the_verdict_is_recorded_on_the_instance(stores, tmp_path):
    _overlay(tmp_path, {WEB: OVERLAY_IP})

    await ensure_instance_hosts(stores, ENV, vm=FakeVm())

    record = stores.ec2compute.get(ENV, f"instance:{WEB}")
    assert record["hosts_action"] == "pushed"
    assert record["hosts_names"] == []
    assert record["hosts_details"] == []


async def test_an_unresolvable_name_records_its_real_cause(stores, tmp_path):
    """The detail must survive the round trip through the store, or the
    projector reconstructs a verdict with an empty reason slot and falls back
    to a generic sentence -- losing the cause `unresolvable` exists to keep."""
    _overlay(tmp_path, {WEB: OVERLAY_IP})
    stores.route53ctl.set(ENV, "rrset:odin.internal", [_record(FQDN, "10.9.9.9")])

    await ensure_instance_hosts(stores, ENV, vm=FakeVm())

    record = stores.ec2compute.get(ENV, f"instance:{WEB}")
    assert record["hosts_action"] == "unresolvable"
    assert record["hosts_names"] == [FQDN]
    assert len(record["hosts_details"]) == 1
    assert "no EC2 instance in this environment reports" in record["hosts_details"][0]


async def test_recording_preserves_the_rest_of_the_instance(stores, tmp_path):
    """A read-modify-write over someone else's record: the fields this module
    does not own must come back untouched."""
    _overlay(tmp_path, {WEB: OVERLAY_IP})

    await ensure_instance_hosts(stores, ENV, vm=FakeVm())

    record = stores.ec2compute.get(ENV, f"instance:{WEB}")
    assert record["state_name"] == "running"
    assert record["private_ip"] == RECORD_VALUE
    assert record["vpc_id"] == "vpc-1"


async def test_records_are_read_across_every_zone(stores):
    """Records live per ZONE (`rrset:{zone_id}`), and /etc/hosts has no notion
    of a zone -- a second zone's names must resolve too."""
    stores.route53ctl.set(ENV, "rrset:other.internal", [_record("db.other.internal", "10.0.0.9")])
    names = sorted(r["name"] for r in records(stores, ENV))
    assert names == ["api.odin.internal", "db.other.internal"]


def test_overlay_is_empty_when_the_env_has_no_mesh(tmp_path):
    """`{}` is the signal `vm_hosts` keys the whole no-mesh verdict on, so it
    must be what a mesh-less env really produces -- not an exception."""
    assert overlay_addresses(tmp_path, "never-meshed") == {}
