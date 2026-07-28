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

import pytest

from odin.compute.tasks import TaskRuntime
from odin.gateway.models.ecsctl import placement_host, runtime_for_service
from odin.runtime.colima import ColimaRuntime
from odin.runtime.lima import LimaRuntime

pytestmark = pytest.mark.anyio

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


# --- ordering: a placed task must not be scheduled before its instance -------
#
# One of the four costs `docs/intelligence-layer.md` named up front. The
# container literally cannot launch into a VM that is not up, and nothing
# sequenced an ecs node behind its ec2 node. `depends_on` is the honest fix
# rather than a wait loop: tofu already owns ordering, the dependency is real,
# and it appears in `tofu plan` instead of being an invisible sleep.

def _canvas(host: str | None = None, ref: bool = False) -> dict:
    data = {"label": "web", "image": "nginx:alpine", "count": "1", "port": "80"}
    if host:
        data["host"] = host
    if ref:
        data["env"] = {"DB": "${{app-db.DATABASE_URL}}"}
    return {
        "nodes": [
            {"id": "v1", "type": "vpc", "position": {"x": 0, "y": 0}, "data": {"label": "prod-vpc"}},
            {"id": "s1", "type": "subnet", "position": {"x": 0, "y": 0},
             "data": {"label": "app-subnet", "vpc": "prod-vpc"}},
            {"id": "e1", "type": "ec2", "position": {"x": 0, "y": 0},
             "data": {"label": "api-server", "subnet": "app-subnet"}},
            {"id": "d1", "type": "rds", "position": {"x": 0, "y": 0}, "data": {"label": "app-db"}},
            {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0}, "data": data},
        ],
        "edges": [],
    }


def _depends_on(canvas: dict) -> str:
    from odin.agent.hcl import generate_tf
    from odin.spec.translate import canvas_to_stack

    tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    return next((line.strip() for line in tf.splitlines() if "depends_on" in line), "")


def test_a_placed_service_depends_on_its_instance():
    assert _depends_on(_canvas(host="api-server")) == "depends_on = [aws_instance.api_server]"


def test_an_unplaced_service_gains_no_dependency():
    assert _depends_on(_canvas()) == ""


def test_placement_and_ref_dependencies_combine():
    """A workload can both consume a database endpoint AND live on an instance;
    dropping either ordering would reintroduce a real race."""
    line = _depends_on(_canvas(host="api-server", ref=True))
    assert "aws_instance.api_server" in line
    assert "aws_db_instance.app_db" in line


def test_a_host_that_is_not_an_ec2_node_adds_nothing():
    """An unresolvable placement is reported by `_ecs` itself; inventing a
    dependency on nothing would only produce a worse error later."""
    canvas = _canvas(host="app-db")  # a real node, wrong kind
    assert "aws_instance" not in _depends_on(canvas)


# --- failure meaning: "the VM is not up" is not "the task failed" ------------
#
# The second of placement's four named costs. They need opposite responses from
# a person -- bring the instance back, versus fix the workload -- so collapsing
# them into one message sends the user to the wrong place.

class _BrokenRuntime:
    """A driver whose container boot always fails, the way `limactl shell`
    against a VM that does not exist does."""

    async def run_container(self, spec):
        raise RuntimeError("limactl shell odin-ec2-prod-api-server failed: instance not found")

    async def host_port(self, *args):
        return 0


async def _boot_error(placed_on: str) -> str:
    from odin.compute.tasks import TaskRuntime

    runtime = TaskRuntime(runtime=_BrokenRuntime(), placed_on=placed_on)
    try:
        await runtime.run("prod", "t1", {"name": "web", "image": "nginx:alpine"})
    except Exception as exc:  # noqa: BLE001 -- the message is what is under test
        return str(exc)
    raise AssertionError("the boot was supposed to fail")


async def test_a_placed_task_names_the_instance_it_could_not_start_on():
    message = await _boot_error("api-server")
    assert "api-server" in message
    assert "instance is up" in message, message
    # ...and the underlying cause is still there, not swallowed by the re-phrase.
    assert "instance not found" in message


async def test_an_unplaced_task_is_reported_exactly_as_before():
    """No placement, no re-phrasing: an ordinary workload failure must not grow
    a sentence about instances that has nothing to do with it."""
    message = await _boot_error("")
    assert "instance is up" not in message
    assert message == "limactl shell odin-ec2-prod-api-server failed: instance not found"


async def test_the_placed_runtime_carries_the_label_from_the_service_record():
    """The seam that makes the message possible at all -- without this the
    runtime knows a VM name but not the instance the user drew."""
    placed = runtime_for_service({"env": "prod", "placement_host": "api-server"}, TaskRuntime())
    assert placed._placed_on == "api-server"
    assert TaskRuntime()._placed_on == ""
