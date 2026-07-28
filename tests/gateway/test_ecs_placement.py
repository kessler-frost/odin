"""An ECS service drawn INSIDE an ec2 node runs in that instance's VM.

The owner's flagship intelligence-layer gesture: *"when I expand the ec2 box and
put an ecs box inside it, that means I want ecs on ec2 ... and the configuration
and stuff updates accordingly if needed but things like name and stuff remains
as is."*

What makes it real rather than a label:

  * an EC2 node IS a Lima VM (`odin-ec2-<env>-<label>`), and `LimaRuntime` can
    now be bound to a NAMED VM, so the task container genuinely runs inside the
    instance it was drawn in;
  * it travels as a real `placement_constraints { type = "memberOf" }` — how AWS
    itself pins tasks — so it appears in `tofu plan` and round-trips through the
    provider rather than being an odin-only field.

What it is NOT: a Fargate/EC2 launch-type switch. odin emits
`launch_type = "EC2"` unconditionally and has no Fargate substrate at all, so
flipping that label would claim a distinction odin cannot back. WHERE THE TASK
RUNS is the part that is real.
"""
from __future__ import annotations

from odin.compute.tasks import TaskRuntime
from odin.gateway.models.ecsctl import placement_host, runtime_for_service
from odin.runtime.colima import ColimaRuntime
from odin.runtime.lima import LimaRuntime

TF_PAYLOAD = {
    "placementConstraints": [
        {"type": "memberOf", "expression": "attribute:odin.instance == api-server"},
    ],
}


def test_placement_is_read_from_the_aws_payload_shape():
    """The Terraform provider sends `placementConstraints`; odin must read AWS's
    own shape rather than a private field, or the constraint in `tofu plan` and
    the behaviour would be two different things."""
    assert placement_host(TF_PAYLOAD) == "api-server"


def test_a_stored_record_keeps_the_placement():
    """Convergence happens long after CreateService, so the record carries it."""
    assert placement_host({"placement_host": "api-server"}) == "api-server"


def test_an_unplaced_service_reports_no_host():
    assert placement_host({}) == ""
    assert placement_host({"placementConstraints": []}) == ""


def test_a_constraint_odin_did_not_write_is_not_guessed_at():
    """`distinctInstance` and a foreign expression are both real AWS things odin
    has no substrate for. Returning "" runs the task on the shared host, which
    is the pre-existing behaviour -- guessing would place it somewhere arbitrary."""
    assert placement_host({"placementConstraints": [{"type": "distinctInstance"}]}) == ""
    assert placement_host({
        "placementConstraints": [{"type": "memberOf", "expression": "attribute:ecs.instance-type == t3.micro"}],
    }) == ""


def test_an_unplaced_service_keeps_the_shared_host_runtime():
    """The no-change case, and the one that must not regress: a canvas that
    draws no workload inside an instance behaves exactly as before."""
    default = TaskRuntime()
    assert runtime_for_service({"env": "prod"}, default) is default


def test_a_placed_service_gets_that_instance_s_own_vm():
    placed = runtime_for_service({"env": "prod", "placement_host": "api-server"}, TaskRuntime())
    driver = placed._rt
    assert isinstance(driver, LimaRuntime)
    assert driver.VM == "odin-ec2-prod-api-server"


def test_two_services_on_different_instances_do_not_share_a_vm():
    """The bug a class-constant VM would have caused: both would drive whichever
    instance was constructed last."""
    default = TaskRuntime()
    first = runtime_for_service({"env": "prod", "placement_host": "api-server"}, default)
    second = runtime_for_service({"env": "prod", "placement_host": "worker"}, default)
    assert (first._rt.VM, second._rt.VM) == ("odin-ec2-prod-api-server", "odin-ec2-prod-worker")


def test_placement_is_per_env():
    """Two envs may both draw `web` inside `api-server`; they are different VMs."""
    staging = runtime_for_service({"env": "staging", "placement_host": "api-server"}, TaskRuntime())
    prod = runtime_for_service({"env": "prod", "placement_host": "api-server"}, TaskRuntime())
    assert staging._rt.VM != prod._rt.VM


def test_the_default_runtime_is_still_the_host_container_runtime():
    """Guards the meaning of 'unplaced': the shared host is Colima, not a VM."""
    assert isinstance(TaskRuntime()._rt, ColimaRuntime)
