"""V3a -- gateway/models/ec2compute.py: the EC2-compute model (instances +
key pairs), built to research-coverage.md §2b's captured call surface.

Same test method as V1a's tests/gateway/test_ec2net.py: every request is a
REAL boto3-signed capture (the `ec2` fixture), and every response round-trips
through botocore's own EC2-protocol parser. Calls go straight to
`ec2compute.pure_answer` (not `synth.pure_answer`) so a FAKE `InstanceVm` can
be injected -- these are the "model logic tested without VMs" unit tests the
V3 brief's Gate calls for; V3b's `tests/test_compute/test_instances.py`
covers the real `InstanceVm` against a fake subprocess runner instead, and
V3d's integration test is the only one that boots anything real.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway.classify import classify
from odin.gateway.models import ec2compute
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"


class FakeInstanceVm:
    """The InstanceVm shape (`boot`/`stop`/`start`/`delete`) with no
    subprocess/VM involved -- deterministic and near-instant, so the
    background-thread state transitions ec2compute.py spawns can be
    observed with a short poll instead of a real boot's ~30-60s."""

    def __init__(self, ip: str = "192.168.64.10", fail_boot: bool = False, fail_start: bool = False) -> None:
        self.ip = ip
        self.fail_boot = fail_boot
        self.fail_start = fail_start
        self.booted: list[tuple] = []
        self.stopped: list[str] = []
        self.started: list[str] = []
        self.deleted: list[str] = []

    def boot(self, name, vm_config, *, hostname, ssh_pubkey=None, user_data=None, nebula=None, timeout=300.0):
        self.booted.append((name, hostname, ssh_pubkey, user_data, nebula))
        if self.fail_boot:
            raise RuntimeError("boot failed")
        return self.ip

    def stop(self, name):
        self.stopped.append(name)

    def start(self, name, timeout=300.0):
        self.started.append(name)
        if self.fail_start:
            raise RuntimeError("start failed")
        return self.ip

    def delete(self, name):
        self.deleted.append(name)


class GatedDeleteInstanceVm(FakeInstanceVm):
    """`delete()` blocks on a per-attempt gate before doing anything --
    lets a test deterministically observe the "delete failed, waiting for a
    retry" window instead of racing a near-instant fake delete against its
    own retry (release finding #4's honesty fix). The FIRST attempt raises
    once released; every attempt after that succeeds once released."""

    def __init__(self, ip: str = "192.168.64.10") -> None:
        super().__init__(ip=ip)
        self.delete_attempts = 0
        self.first_delete_blocked = threading.Event()
        self.release_first_delete = threading.Event()
        self.release_retry = threading.Event()

    def delete(self, name):
        self.delete_attempts += 1
        if self.delete_attempts == 1:
            self.first_delete_blocked.set()
            self.release_first_delete.wait(timeout=5.0)
            self.deleted.append(name)
            raise RuntimeError("delete failed")
        self.release_retry.wait(timeout=5.0)
        self.deleted.append(name)


class SlowBootInstanceVm(FakeInstanceVm):
    """A `boot()` that blocks until `release` is set -- lets a test win a
    Terminate race against a still-in-flight RunInstances boot completion
    (release finding #3's "resurrection race")."""

    def __init__(self, ip: str = "192.168.64.10") -> None:
        super().__init__(ip=ip)
        self.release = threading.Event()
        self.boot_started = threading.Event()

    def boot(self, name, vm_config, *, hostname, ssh_pubkey=None, user_data=None, nebula=None, timeout=300.0):
        self.boot_started.set()
        self.release.wait(timeout=5.0)
        return super().boot(name, vm_config, hostname=hostname, ssh_pubkey=ssh_pubkey, user_data=user_data, nebula=nebula, timeout=timeout)


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("ec2")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


def _answer(stores, req, vm=None) -> Response:
    path, query = split_url(req.url)
    classified = classify("ec2", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an EC2 request must never be unmappable"
    action, resource = classified
    response = ec2compute.pure_answer(action, resource, ENV, req.body, stores, time.monotonic(), vm)
    assert response is not None, "ec2compute delegates VPC/SG/unknown actions to ec2net, never falls through to None"
    return response


def _create_vpc(stores, sink, ec2) -> str:
    req = sink.call(lambda: ec2.create_vpc(CidrBlock="10.0.0.0/16"))
    return _parse("CreateVpc", _answer(stores, req))["Vpc"]["VpcId"]


def _create_subnet(stores, sink, ec2, vpc_id: str) -> str:
    req = sink.call(lambda: ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24"))
    return _parse("CreateSubnet", _answer(stores, req))["Subnet"]["SubnetId"]


def _subnet(stores, sink, ec2) -> str:
    return _create_subnet(stores, sink, ec2, _create_vpc(stores, sink, ec2))


def _run_instance(stores, sink, ec2, vm, **kwargs) -> dict:
    req = sink.call(lambda: ec2.run_instances(MinCount=1, MaxCount=1, **kwargs))
    return _parse("RunInstances", _answer(stores, req, vm))


def _wait_for_state(stores, sink, ec2, instance_id: str, want: str, vm, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        req = sink.call(lambda: ec2.describe_instances(InstanceIds=[instance_id]))
        parsed = _parse("DescribeInstances", _answer(stores, req, vm))
        instance = parsed["Reservations"][0]["Instances"][0]
        last = instance["State"]["Name"]
        if last == want:
            return instance
        time.sleep(0.02)
    raise AssertionError(f"instance {instance_id} never reached {want!r} (last seen {last!r})")


# --- RunInstances / DescribeInstances ----------------------------------------


def test_run_instances_starts_pending_and_boots_to_running(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm(ip="192.168.64.42")
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, ImageId="ami-test", InstanceType="t3.small")
    instance = result["Instances"][0]
    assert instance["State"]["Name"] == "pending"
    assert instance["InstanceType"] == "t3.small"
    instance_id = instance["InstanceId"]
    assert instance_id.startswith("i-")

    running = _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    assert running["PrivateIpAddress"] == "192.168.64.42"
    assert running["PublicIpAddress"] == "192.168.64.42"
    assert running["SubnetId"] == subnet_id
    assert len(vm.booted) == 1
    assert vm.booted[0][0].startswith(f"allfather-ec2-{ENV}-{instance_id}")


def test_run_instances_default_instance_type_and_ami(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance = result["Instances"][0]
    assert instance["InstanceType"] == "t3.micro"
    assert instance["ImageId"]


def test_run_instances_unknown_subnet_is_not_found(sink, ec2, stores):
    req = sink.call(lambda: ec2.run_instances(MinCount=1, MaxCount=1, SubnetId="subnet-00000000000000000"))
    response = _answer(stores, req, FakeInstanceVm())
    assert response.status_code == 400
    parsed = _parse("RunInstances", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidSubnetID.NotFound"


def test_run_instances_unknown_key_pair_is_not_found(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    req = sink.call(lambda: ec2.run_instances(MinCount=1, MaxCount=1, SubnetId=subnet_id, KeyName="ghost"))
    response = _answer(stores, req, FakeInstanceVm())
    assert response.status_code == 400
    parsed = _parse("RunInstances", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidKeyPair.NotFound"


def test_run_instances_boot_failure_lands_terminated_with_state_reason(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm(fail_boot=True)
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]

    terminated = _wait_for_state(stores, sink, ec2, instance_id, "terminated", vm)
    assert terminated["StateReason"]["Message"] == "boot failed"


def test_describe_instances_filters_by_state_and_vpc(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    a = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]
    _wait_for_state(stores, sink, ec2, a["InstanceId"], "running", vm)

    req = sink.call(lambda: ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}]))
    parsed = _parse("DescribeInstances", _answer(stores, req, vm))
    ids = {i["InstanceId"] for r in parsed["Reservations"] for i in r["Instances"]}
    assert a["InstanceId"] in ids


# --- Stop / Start / Terminate -------------------------------------------------


def test_stop_then_start_round_trips_through_the_vm(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm(ip="192.168.64.7")
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    stop_req = sink.call(lambda: ec2.stop_instances(InstanceIds=[instance_id]))
    stop_parsed = _parse("StopInstances", _answer(stores, stop_req, vm))
    assert stop_parsed["StoppingInstances"][0]["CurrentState"]["Name"] == "stopping"
    stopped = _wait_for_state(stores, sink, ec2, instance_id, "stopped", vm)
    assert stopped.get("PrivateIpAddress") is None
    assert vm.stopped == [f"allfather-ec2-{ENV}-{instance_id}"]

    start_req = sink.call(lambda: ec2.start_instances(InstanceIds=[instance_id]))
    _answer(stores, start_req, vm)
    running = _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    assert running["PrivateIpAddress"] == "192.168.64.7"


def test_terminate_transitions_then_sweeps_after_grace_window(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    term_req = sink.call(lambda: ec2.terminate_instances(InstanceIds=[instance_id]))
    term_parsed = _parse("TerminateInstances", _answer(stores, term_req, vm))
    assert term_parsed["TerminatingInstances"][0]["CurrentState"]["Name"] == "shutting-down"
    _wait_for_state(stores, sink, ec2, instance_id, "terminated", vm)
    assert vm.deleted == [f"allfather-ec2-{ENV}-{instance_id}"]

    # Still visible right after termination (the ~60s grace window)...
    req = sink.call(lambda: ec2.describe_instances(InstanceIds=[instance_id]))
    _parse("DescribeInstances", _answer(stores, req, vm))

    # ...but a describe with `now` far past the window sweeps it, and a
    # by-id describe then reports NotFound like real AWS's post-terminate
    # delete-confirm.
    path, query = split_url(req.url)
    action, resource = classify("ec2", req.method, path, query, req.headers, req.body)
    late = ec2compute.pure_answer(action, resource, ENV, req.body, stores, time.monotonic() + 120.0, vm)
    parsed = _parse("DescribeInstances", late, error=True)
    assert parsed["Error"]["Code"] == "InvalidInstanceID.NotFound"


def test_terminate_delete_failure_keeps_shutting_down_with_reason_and_retries(sink, ec2, stores):
    """Release finding #4 -- VM delete honesty: a failed `vm.delete` must
    NOT be reported as a clean `terminated`. The record stays
    `shutting-down` with the error recorded, and the NEXT Describe-driven
    pass retries the delete until it actually succeeds."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = GatedDeleteInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    term_req = sink.call(lambda: ec2.terminate_instances(InstanceIds=[instance_id]))
    _answer(stores, term_req, vm)
    assert vm.first_delete_blocked.wait(timeout=2.0)  # the first (failing) delete attempt is mid-flight
    vm.release_first_delete.set()

    # Poll until the failure has genuinely landed. Every poll ALSO drives
    # `_retry_failed_deletes`, but its retry attempt blocks on
    # `release_retry` (not yet set) before mutating anything -- so once the
    # failed state is observed here it's stable, not a narrow race window.
    deadline = time.monotonic() + 2.0
    reasoned = None
    while time.monotonic() < deadline:
        req = sink.call(lambda: ec2.describe_instances(InstanceIds=[instance_id]))
        parsed = _parse("DescribeInstances", _answer(stores, req, vm))
        instance = parsed["Reservations"][0]["Instances"][0]
        if instance["State"]["Name"] == "shutting-down" and instance.get("StateReason"):
            reasoned = instance
            break
        time.sleep(0.02)
    assert reasoned is not None, "the delete failure was never recorded"
    assert "delete failed" in reasoned["StateReason"]["Message"].lower()

    # Release the retry that the polling above already spawned -- it
    # succeeds, and the instance genuinely reaches terminated.
    vm.release_retry.set()
    _wait_for_state(stores, sink, ec2, instance_id, "terminated", vm)
    name = f"allfather-ec2-{ENV}-{instance_id}"
    assert vm.deleted.count(name) == 2  # the original failed attempt + the successful retry


def test_late_boot_completion_cannot_resurrect_a_terminated_instance(sink, ec2, stores):
    """Release finding #3 -- the resurrection race: RunInstances's background
    `_finish_boot` thread is still in flight when Terminate wins first. The
    instance must stay `terminated` forever; a stale boot completion landing
    afterward must never bounce it back to `running`."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = SlowBootInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    assert vm.boot_started.wait(timeout=2.0)  # the boot thread is genuinely mid-flight

    term_req = sink.call(lambda: ec2.terminate_instances(InstanceIds=[instance_id]))
    _answer(stores, term_req, vm)
    _wait_for_state(stores, sink, ec2, instance_id, "terminated", vm)

    vm.release.set()  # let the stale boot finish AFTER the terminate already won
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        req = sink.call(lambda: ec2.describe_instances(InstanceIds=[instance_id]))
        parsed = _parse("DescribeInstances", _answer(stores, req, vm))
        state = parsed["Reservations"][0]["Instances"][0]["State"]["Name"]
        assert state == "terminated", f"resurrected to {state!r}"
        time.sleep(0.02)


def test_terminate_unknown_instance_is_not_found(sink, ec2, stores):
    req = sink.call(lambda: ec2.terminate_instances(InstanceIds=["i-00000000000000000"]))
    response = _answer(stores, req, FakeInstanceVm())
    assert response.status_code == 400


# --- hydration describes -------------------------------------------------------


def test_describe_volumes_returns_the_auto_root_volume(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, InstanceType="t3.medium")["Instances"][0]["InstanceId"]

    req = sink.call(lambda: ec2.describe_volumes(Filters=[{"Name": "attachment.instance-id", "Values": [instance_id]}]))
    parsed = _parse("DescribeVolumes", _answer(stores, req, vm))
    (volume,) = parsed["Volumes"]
    assert volume["VolumeId"].startswith("vol-")
    assert volume["Size"] == 20  # t3.medium's VmConfig.disk == "20GiB"
    assert volume["Attachments"][0]["InstanceId"] == instance_id
    assert volume["Attachments"][0]["Device"] == "/dev/sda1"


def test_describe_instance_types(sink, ec2, stores):
    req = sink.call(lambda: ec2.describe_instance_types(InstanceTypes=["t3.micro"]))
    parsed = _parse("DescribeInstanceTypes", _answer(stores, req, FakeInstanceVm()))
    (info,) = parsed["InstanceTypes"]
    assert info["InstanceType"] == "t3.micro"
    assert info["VCpuInfo"]["DefaultVCpus"] == 1
    assert info["MemoryInfo"]["SizeInMiB"] == 1024


def test_describe_instance_attribute_defaults(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]

    req = sink.call(lambda: ec2.describe_instance_attribute(InstanceId=instance_id, Attribute="disableApiTermination"))
    parsed = _parse("DescribeInstanceAttribute", _answer(stores, req, vm))
    assert parsed["DisableApiTermination"] == {"Value": False}


def test_modify_instance_attribute_is_a_tolerated_stub(sink, ec2, stores):
    # research §2b: the provider calls this during destroy and TOLERATES a
    # 400 -- no dedicated handler needed, it falls through to the generic
    # InvalidAction envelope (ec2compute has no ModifyInstanceAttribute
    # entry, ec2net doesn't either -- both land on the shared fallback).
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    req = sink.call(lambda: ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiTermination={"Value": False}))
    response = _answer(stores, req, vm)
    assert response.status_code == 400


# --- Key pairs -----------------------------------------------------------------


def test_import_key_pair_stores_pubkey_for_ssh_injection(sink, ec2, stores):
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@host"
    req = sink.call(lambda: ec2.import_key_pair(KeyName="deploy", PublicKeyMaterial=pubkey.encode()))
    parsed = _parse("ImportKeyPair", _answer(stores, req))
    assert parsed["KeyName"] == "deploy"
    assert parsed["KeyPairId"].startswith("key-")

    keypair = stores.ec2compute.get(ENV, "keypair:deploy")
    assert keypair["public_key"] == pubkey


def test_run_instances_injects_the_imported_pubkey(sink, ec2, stores):
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@host"
    req = sink.call(lambda: ec2.import_key_pair(KeyName="deploy", PublicKeyMaterial=pubkey.encode()))
    _parse("ImportKeyPair", _answer(stores, req))

    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, KeyName="deploy")
    assert vm.booted[0][2] == pubkey  # ssh_pubkey positional slot


def test_describe_key_pairs_and_delete(sink, ec2, stores):
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@host"
    req = sink.call(lambda: ec2.import_key_pair(KeyName="deploy", PublicKeyMaterial=pubkey.encode()))
    _answer(stores, req)

    describe_req = sink.call(lambda: ec2.describe_key_pairs(KeyNames=["deploy"]))
    parsed = _parse("DescribeKeyPairs", _answer(stores, describe_req))
    assert parsed["KeyPairs"][0]["KeyName"] == "deploy"

    delete_req = sink.call(lambda: ec2.delete_key_pair(KeyName="deploy"))
    _parse("DeleteKeyPair", _answer(stores, delete_req))
    assert stores.ec2compute.get(ENV, "keypair:deploy") is None

    # deleting again is a tolerated no-op, matching real AWS
    delete_again = sink.call(lambda: ec2.delete_key_pair(KeyName="deploy"))
    response = _answer(stores, delete_again)
    assert response.status_code == 200


def test_create_key_pair_generates_a_real_rsa_keypair(sink, ec2, stores):
    req = sink.call(lambda: ec2.create_key_pair(KeyName="generated"))
    parsed = _parse("CreateKeyPair", _answer(stores, req))
    assert parsed["KeyMaterial"].startswith("-----BEGIN")
    assert stores.ec2compute.get(ENV, "keypair:generated")["public_key"].startswith("ssh-rsa")


# --- delegation to ec2net -------------------------------------------------------


def test_ec2compute_delegates_vpc_calls_to_ec2net(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    assert vpc_id.startswith("vpc-")


def test_tags_flow_through_ec2net_and_report_the_right_resource_type(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, TagSpecifications=[
        {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "web"}]},
    ])["Instances"][0]["InstanceId"]

    req = sink.call(lambda: ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [instance_id]}]))
    parsed = _parse("DescribeTags", _answer(stores, req, vm))
    (tag,) = parsed["Tags"]
    assert tag == {"ResourceId": instance_id, "ResourceType": "instance", "Key": "Name", "Value": "web"}


# --- startup reaper (release finding #4) -------------------------------------


class FakeReaperVm:
    """The `list_names`/`delete` shape `reap_orphaned_vms` needs --
    deliberately NOT the full InstanceVm boot/stop/start surface, since the
    reaper never touches those."""

    def __init__(self, names: list[str]) -> None:
        self._names = list(names)
        self.deleted: list[str] = []

    def list_names(self) -> list[str]:
        return list(self._names)

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self._names.remove(name)


def test_reap_orphaned_vms_deletes_only_unmatched_ec2_named_vms(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-known", {"instance_id": "i-known"})
    vm = FakeReaperVm(names=[
        "allfather-ec2-default-i-known",      # matches the store -- must survive
        "allfather-ec2-default-i-orphaned",   # no matching record -- reaped
        "allfather-ec2-staging-i-elsewhere",  # a different env, no record at all -- reaped
        "veronica",                           # a user's own Lima VM -- never even a candidate
        "some-other-tool-vm",                 # another subsystem's VM -- never touched
    ])

    reaped = ec2compute.reap_orphaned_vms(tmp_path, ["default", "staging"], vm=vm)

    assert sorted(reaped) == ["allfather-ec2-default-i-orphaned", "allfather-ec2-staging-i-elsewhere"]
    assert sorted(vm.deleted) == sorted(reaped)
    assert "allfather-ec2-default-i-known" not in vm.deleted
    assert "veronica" not in vm.deleted
    assert "some-other-tool-vm" not in vm.deleted


def test_reap_orphaned_vms_is_a_no_op_when_everything_matches(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-known", {"instance_id": "i-known"})
    vm = FakeReaperVm(names=["allfather-ec2-default-i-known"])

    reaped = ec2compute.reap_orphaned_vms(tmp_path, ["default"], vm=vm)

    assert reaped == []
    assert vm.deleted == []


def test_reap_orphaned_vms_with_no_vms_at_all_is_a_no_op(tmp_path):
    vm = FakeReaperVm(names=[])
    assert ec2compute.reap_orphaned_vms(tmp_path, ["default"], vm=vm) == []
