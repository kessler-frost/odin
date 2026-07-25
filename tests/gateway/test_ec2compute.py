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

from odin.fabric.models import FirewallRule
from odin.gateway.classify import classify
from odin.gateway.keys import KeyStore
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

    def boot(self, name, vm_config, *, hostname, ssh_pubkey=None, user_data=None, nebula=None, timeout=300.0, env_vars=None):
        self.booted.append((name, hostname, ssh_pubkey, user_data, nebula, env_vars))
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

    def boot(self, name, vm_config, *, hostname, ssh_pubkey=None, user_data=None, nebula=None, timeout=300.0, env_vars=None):
        self.boot_started.set()
        self.release.wait(timeout=5.0)
        return super().boot(name, vm_config, hostname=hostname, ssh_pubkey=ssh_pubkey, user_data=user_data, nebula=nebula, timeout=timeout, env_vars=env_vars)


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


def _answer(stores, req, vm=None, keystore=None, gateway_port=None) -> Response:
    path, query = split_url(req.url)
    classified = classify("ec2", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an EC2 request must never be unmappable"
    action, resource = classified
    response = ec2compute.pure_answer(
        action, resource, ENV, req.body, stores, time.monotonic(), vm,
        keystore=keystore, gateway_port=gateway_port,
    )
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


def _create_sg(stores, sink, ec2, vpc_id: str, name: str = "web") -> str:
    req = sink.call(lambda: ec2.create_security_group(GroupName=name, Description=name, VpcId=vpc_id))
    return _parse("CreateSecurityGroup", _answer(stores, req))["GroupId"]


def _run_instance(stores, sink, ec2, vm, *, keystore=None, gateway_port=None, **kwargs) -> dict:
    req = sink.call(lambda: ec2.run_instances(MinCount=1, MaxCount=1, **kwargs))
    return _parse("RunInstances", _answer(stores, req, vm, keystore=keystore, gateway_port=gateway_port))


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
    assert vm.booted[0][0].startswith(f"odin-ec2-{ENV}-{instance_id}")


def test_run_instances_reflects_security_groups_on_the_primary_eni(sink, ec2, stores):
    """Field-test finding #2: an instance launched with SecurityGroupIds must
    report them on a PRIMARY network interface so the TF provider reads
    `vpc_security_group_ids` with zero drift on re-apply. Before this the model
    ignored them -> the provider saw a changed group set with no primary NI to
    modify and errored 'does not contain a primary network interface'."""
    vpc_id = _create_vpc(stores, sink, ec2)
    subnet_id = _create_subnet(stores, sink, ec2, vpc_id)
    sg_id = _create_sg(stores, sink, ec2, vpc_id, name="web-sg")

    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, SecurityGroupIds=[sg_id])
    instance_id = result["Instances"][0]["InstanceId"]
    assert stores.ec2compute.get(ENV, f"instance:{instance_id}")["security_group_ids"] == [sg_id]

    req = sink.call(lambda: ec2.describe_instances(InstanceIds=[instance_id]))
    instance = _parse("DescribeInstances", _answer(stores, req, vm))["Reservations"][0]["Instances"][0]
    assert [g["GroupId"] for g in instance["SecurityGroups"]] == [sg_id]
    (eni,) = instance["NetworkInterfaces"]
    assert eni["Attachment"]["DeviceIndex"] == 0
    assert [g["GroupId"] for g in eni["Groups"]] == [sg_id]
    assert eni["Groups"][0]["GroupName"] == "web-sg"


def test_run_instances_without_security_groups_has_no_primary_eni(sink, ec2, stores):
    """The no-SG path is byte-for-byte the pre-finding-#2 shape: no groupSet, no
    networkInterfaceSet -- the provider reads `vpc_security_group_ids` as an
    empty computed value (no drift), which is why the existing no-SG e2e stays
    zero-drift."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    req = sink.call(lambda: ec2.describe_instances(InstanceIds=[instance_id]))
    instance = _parse("DescribeInstances", _answer(stores, req, vm))["Reservations"][0]["Instances"][0]
    assert instance.get("SecurityGroups", []) == []
    assert instance.get("NetworkInterfaces", []) == []


def test_modify_instance_attribute_updates_the_security_group_set(sink, ec2, stores):
    """The in-place security-group change path (finding #2): ModifyInstanceAttribute
    with a new GroupId set updates the stored membership so the next describe --
    and a re-apply -- reflect it."""
    vpc_id = _create_vpc(stores, sink, ec2)
    subnet_id = _create_subnet(stores, sink, ec2, vpc_id)
    sg1 = _create_sg(stores, sink, ec2, vpc_id, name="sg-a")
    sg2 = _create_sg(stores, sink, ec2, vpc_id, name="sg-b")
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, SecurityGroupIds=[sg1])["Instances"][0]["InstanceId"]

    req = sink.call(lambda: ec2.modify_instance_attribute(InstanceId=instance_id, Groups=[sg2]))
    assert _answer(stores, req, vm).status_code == 200
    assert stores.ec2compute.get(ENV, f"instance:{instance_id}")["security_group_ids"] == [sg2]


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


# --- workload identity injection (fix-wave 2b finding #2) ----------------------


def test_run_instances_with_odin_node_tag_injects_workload_env(sink, ec2, stores, tmp_path):
    """An instance tagged `odin:node=<label>` (agent/hcl.py stamps this on
    every canvas-node-backed resource) boots with the four AWS-SDK env vars
    from `workload_env` -- the keystore identity issued for that label --
    baked into its cloud-init, so the VM can call the gateway AS ITSELF."""
    keystore = KeyStore(tmp_path)
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(
        stores, sink, ec2, vm, keystore=keystore, gateway_port=4266,
        SubnetId=subnet_id,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "odin:node", "Value": "myserver"}]}],
    )["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    env_vars = vm.booted[0][5]
    assert set(env_vars) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL", "AWS_DEFAULT_REGION"}
    access_key, secret_key = keystore.issue(ENV, "myserver")  # stable -- reissuing returns the SAME pair
    assert env_vars["AWS_ACCESS_KEY_ID"] == access_key
    assert env_vars["AWS_SECRET_ACCESS_KEY"] == secret_key
    assert env_vars["AWS_ENDPOINT_URL"].endswith(":4266")


def test_run_instances_with_keystore_but_no_odin_node_tag_boots_without_env_vars(sink, ec2, stores, tmp_path):
    keystore = KeyStore(tmp_path)
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(
        stores, sink, ec2, vm, keystore=keystore, gateway_port=4266, SubnetId=subnet_id,
    )["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    assert vm.booted[0][5] is None


def test_run_instances_without_keystore_boots_without_env_vars(sink, ec2, stores):
    """Regression: today's callers that pass no keystore/gateway_port get
    exactly the old behavior -- no env vars injected."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(
        stores, sink, ec2, vm, SubnetId=subnet_id,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "odin:node", "Value": "myserver"}]}],
    )["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    assert vm.booted[0][5] is None


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
    assert vm.stopped == [f"odin-ec2-{ENV}-{instance_id}"]

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
    assert vm.deleted == [f"odin-ec2-{ENV}-{instance_id}"]

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


def test_mark_instance_terminated_is_what_tofu_reads_after_an_out_of_band_vm_delete(sink, ec2, stores):
    """W2.2's honesty fix -- the reality sweep's seam (`reconcile/drift.py`):
    once odin has CONFIRMED the Lima VM is gone, DescribeInstances must answer
    `terminated` with a real StateReason, because that answer is the only thing
    that makes the "re-Apply to recreate" verdict TRUE (terraform-provider-aws
    drops a terminated instance from state and plans a create; a record still
    claiming `running` gives tofu an empty plan forever and the VM never comes
    back). The record is also reclaimed by the normal lazy sweep afterwards --
    no new lifecycle, just the existing terminal one."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    ec2compute.mark_instance_terminated(
        stores, ENV, instance_id,
        f"VM odin-ec2-{ENV}-{instance_id} deleted outside odin — re-Apply to recreate",
    )

    req = sink.call(lambda: ec2.describe_instances(InstanceIds=[instance_id]))
    instance = _parse("DescribeInstances", _answer(stores, req, vm))["Reservations"][0]["Instances"][0]
    assert instance["State"]["Name"] == "terminated"
    assert instance["StateReason"]["Code"] == "Client.UserInitiatedShutdown"
    assert "deleted outside odin" in instance["StateReason"]["Message"]
    assert vm.deleted == [], "the sweep records reality -- it never deletes a VM itself"

    # ...and the record is reclaimed by the SAME grace window a real terminate
    # uses, never parked in the store forever.
    path, query = split_url(req.url)
    action, resource = classify("ec2", req.method, path, query, req.headers, req.body)
    late = ec2compute.pure_answer(action, resource, ENV, req.body, stores, time.monotonic() + 120.0, vm)
    assert _parse("DescribeInstances", late, error=True)["Error"]["Code"] == "InvalidInstanceID.NotFound"


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
    name = f"odin-ec2-{ENV}-{instance_id}"
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


# --- R3/W2.6: the assigned-SG firewall + the lighthouse start/stop lifecycle ----


def test_run_instances_threads_the_vpc_default_sg_firewall_into_nebula_join(sink, ec2, stores):
    """An instance launched with NO SecurityGroupIds inherits its VPC's default
    SG, exactly like real AWS. An ingress rule authorized on that default SG
    must show up on the `NebulaJoin` `InstanceVm.boot` receives. (W2.6 made the
    ASSIGNED groups the primary source -- this is the fallback path for an
    instance that has none, which real AWS resolves the same way.)"""
    subnet_id = _subnet(stores, sink, ec2)
    vpc_id = stores.ec2net.get(ENV, f"subnet:{subnet_id}")["vpc_id"]
    default_sg_id = stores.ec2net.get(ENV, f"vpc:{vpc_id}")["default_sg_id"]
    req = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=default_sg_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ]))
    assert _answer(stores, req).status_code == 200

    vm = FakeInstanceVm()
    _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    ((_name, _hostname, _ssh_pubkey, _user_data, nebula, _env_vars),) = vm.booted
    assert nebula is not None and nebula.firewall is not None
    assert FirewallRule(port="8080", proto="tcp", cidr="0.0.0.0/0") in nebula.firewall.inbound


def _authorize(stores, sink, ec2, group_id: str, port: int) -> None:
    req = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ]))
    assert _answer(stores, req).status_code == 200


def test_run_instances_compiles_the_union_of_its_assigned_sgs_not_the_vpc_default(sink, ec2, stores):
    """W2.6 piece 1: the VM's nebula firewall comes from the instance's OWN
    security groups (all of them -- AWS rules are permissive-only, so the
    effective set is their union), NOT from the VPC's default SG. Before this,
    a canvas that assigned `web-sg` to an instance got the default SG's rules
    baked into the VM regardless -- the drawn group was decorative on the
    wire."""
    vpc_id = _create_vpc(stores, sink, ec2)
    subnet_id = _create_subnet(stores, sink, ec2, vpc_id)
    default_sg_id = stores.ec2net.get(ENV, f"vpc:{vpc_id}")["default_sg_id"]
    web_sg = _create_sg(stores, sink, ec2, vpc_id, name="web-sg")
    ops_sg = _create_sg(stores, sink, ec2, vpc_id, name="ops-sg")
    _authorize(stores, sink, ec2, default_sg_id, 7070)  # must NOT reach the VM
    _authorize(stores, sink, ec2, web_sg, 8080)
    _authorize(stores, sink, ec2, ops_sg, 9090)

    vm = FakeInstanceVm()
    _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, SecurityGroupIds=[web_sg, ops_sg])
    ((_name, _hostname, _ssh_pubkey, _user_data, nebula, _env_vars),) = vm.booted
    assert nebula is not None and nebula.firewall is not None
    ports = {(r.port, r.proto) for r in nebula.firewall.inbound}
    assert ("8080", "tcp") in ports, "the first assigned SG's rule must gate the VM"
    assert ("9090", "tcp") in ports, "the second assigned SG's rule must gate the VM too (union)"
    assert ("7070", "tcp") not in ports, "the VPC default SG must NOT leak in once groups are assigned"


def test_run_instances_stamps_assigned_sg_ids_as_nebula_cert_groups(sink, ec2, stores):
    """The other half of SG-to-SG rules: nebula matches a peer's `group:` rule
    against that peer's CERTIFICATE groups, so an instance's sg ids have to
    ride into cert signing (`InstanceVm._nebula_files`)."""
    vpc_id = _create_vpc(stores, sink, ec2)
    subnet_id = _create_subnet(stores, sink, ec2, vpc_id)
    web_sg = _create_sg(stores, sink, ec2, vpc_id, name="web-sg")

    vm = FakeInstanceVm()
    _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, SecurityGroupIds=[web_sg])
    ((_name, _hostname, _ssh_pubkey, _user_data, nebula, _env_vars),) = vm.booted
    assert nebula.groups == (web_sg,)


def test_run_instances_with_an_unknown_sg_falls_back_to_the_vpc_default(sink, ec2, stores):
    """A SecurityGroupId with no record contributes no rules -- the instance
    falls back to its VPC default rather than silently booting with an empty
    (deny-everything) or invented firewall."""
    vpc_id = _create_vpc(stores, sink, ec2)
    subnet_id = _create_subnet(stores, sink, ec2, vpc_id)
    default_sg_id = stores.ec2net.get(ENV, f"vpc:{vpc_id}")["default_sg_id"]
    _authorize(stores, sink, ec2, default_sg_id, 7070)

    vm = FakeInstanceVm()
    _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id, SecurityGroupIds=["sg-00000000000000000"])
    ((_name, _hostname, _ssh_pubkey, _user_data, nebula, _env_vars),) = vm.booted
    assert FirewallRule(port="7070", proto="tcp", cidr="0.0.0.0/0") in nebula.firewall.inbound


def test_run_instances_without_a_subnet_gets_no_nebula_join(sink, ec2, stores):
    vm = FakeInstanceVm()
    _run_instance(stores, sink, ec2, vm)  # no SubnetId at all
    ((_name, _hostname, _ssh_pubkey, _user_data, nebula, _env_vars),) = vm.booted
    assert nebula is None


def test_terminate_last_vpc_instance_stops_the_lighthouse(sink, ec2, stores, monkeypatch):
    stopped = []

    class FakeLighthouse:
        def ensure_stopped(self, root, env):
            stopped.append((root, env))

    monkeypatch.setattr(ec2compute, "LighthouseManager", FakeLighthouse)
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    term_req = sink.call(lambda: ec2.terminate_instances(InstanceIds=[instance_id]))
    _answer(stores, term_req, vm)
    _wait_for_state(stores, sink, ec2, instance_id, "terminated", vm)
    assert stopped == [(stores.root, ENV)]


def test_terminate_does_not_stop_the_lighthouse_while_another_instance_remains(sink, ec2, stores, monkeypatch):
    stopped = []

    class FakeLighthouse:
        def ensure_stopped(self, root, env):
            stopped.append((root, env))

    monkeypatch.setattr(ec2compute, "LighthouseManager", FakeLighthouse)
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    id1 = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    id2 = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, id1, "running", vm)
    _wait_for_state(stores, sink, ec2, id2, "running", vm)

    term_req = sink.call(lambda: ec2.terminate_instances(InstanceIds=[id1]))
    _answer(stores, term_req, vm)
    _wait_for_state(stores, sink, ec2, id1, "terminated", vm)
    assert stopped == []  # id2 (same env, same VPC) is still running


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


def test_modify_instance_attribute_returns_success(sink, ec2, stores):
    # Finding #2: ModifyInstanceAttribute is now a real (tolerant) success, not
    # the old InvalidAction 400 -- the provider calls it during destroy
    # (DisableApiTermination=false) and on an in-place security-group change;
    # neither must 400 (the SG case is covered below).
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)["Instances"][0]["InstanceId"]
    req = sink.call(lambda: ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiTermination={"Value": False}))
    assert _answer(stores, req, vm).status_code == 200


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
        self.listed = 0

    def list_names(self) -> list[str]:
        self.listed += 1
        return list(self._names)

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self._names.remove(name)


def test_reap_orphaned_vms_deletes_only_unmatched_ec2_named_vms(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-known", {"instance_id": "i-known"})
    vm = FakeReaperVm(names=[
        "odin-ec2-default-i-known",      # matches the store -- must survive
        "odin-ec2-default-i-orphaned",   # no matching record -- reaped
        "odin-ec2-staging-i-elsewhere",  # a different env, no record at all -- reaped
        "veronica",                           # a user's own Lima VM -- never even a candidate
        "some-other-tool-vm",                 # another subsystem's VM -- never touched
    ])

    reaped = ec2compute.reap_orphaned_vms(tmp_path, ["default", "staging"], vm=vm)

    assert sorted(reaped) == ["odin-ec2-default-i-orphaned", "odin-ec2-staging-i-elsewhere"]
    assert sorted(vm.deleted) == sorted(reaped)
    assert "odin-ec2-default-i-known" not in vm.deleted
    assert "veronica" not in vm.deleted
    assert "some-other-tool-vm" not in vm.deleted


def test_reap_orphaned_vms_is_a_no_op_when_everything_matches(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2compute.set("default", "instance:i-known", {"instance_id": "i-known"})
    vm = FakeReaperVm(names=["odin-ec2-default-i-known"])

    reaped = ec2compute.reap_orphaned_vms(tmp_path, ["default"], vm=vm)

    assert reaped == []
    assert vm.deleted == []


def test_reap_orphaned_vms_with_no_vms_at_all_is_a_no_op(tmp_path):
    vm = FakeReaperVm(names=[])
    assert ec2compute.reap_orphaned_vms(tmp_path, ["default"], vm=vm) == []


# --- ODIN_REAP_EC2_VMS: the opt-out a second instance needs (v0.7.1) -------


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " 0 "])
def test_reap_orphaned_vms_is_off_when_odin_reap_ec2_vms_says_so(tmp_path, monkeypatch, value):
    """A second odin on the same Mac must be able to leave the FIRST one's VMs
    alone: they are orphans by ITS store, which knows nothing about them."""
    monkeypatch.setenv("ODIN_REAP_EC2_VMS", value)
    vm = FakeReaperVm(names=["odin-ec2-default-i-someone-elses"])

    assert ec2compute.reap_orphaned_vms(tmp_path, ["default"], vm=vm) == []
    assert vm.deleted == []
    # Not merely "deletes nothing" -- it must not even ENUMERATE, so the
    # opt-out holds on a machine where listing VMs is itself unwanted.
    assert vm.listed == 0


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "", "anything-else"])
def test_reap_orphaned_vms_stays_on_for_every_other_value(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ODIN_REAP_EC2_VMS", value)
    vm = FakeReaperVm(names=["odin-ec2-default-i-orphaned"])
    assert ec2compute.reap_orphaned_vms(tmp_path, ["default"], vm=vm) == ["odin-ec2-default-i-orphaned"]


def test_reap_orphaned_vms_is_on_when_the_variable_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_REAP_EC2_VMS", raising=False)
    vm = FakeReaperVm(names=["odin-ec2-default-i-orphaned"])
    assert ec2compute.reap_orphaned_vms(tmp_path, ["default"], vm=vm) == ["odin-ec2-default-i-orphaned"]
