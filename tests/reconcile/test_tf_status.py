"""Fix-wave 2b finding #1 -- reconcile/tf_status.py: a pure, read-only
projection of TF-owned resources (vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr
-- kinds only tofu ever creates/destroys, never entered into World before
this fix) from the gateway's synth stores into `label -> (kind, phase,
facts)`. Hand-built `SynthStores`, no reconciler/asyncio involved -- see
tests/reconcile/test_reconciler.py for the Reconciler-level integration
(emitting WorldDeltas + pruning)."""
from __future__ import annotations

from odin.gateway.stores import SynthStores
from odin.reconcile.tf_status import TF_OWNED_KINDS, project

ENV = "default"


def test_tf_owned_kinds_excludes_reconciler_owned_kinds():
    # s3/sqs/sns/dynamodb already get real World entries via the reconciler's
    # own PROVISIONED path -- this projection must never double-own them.
    assert TF_OWNED_KINDS == {"vpc", "subnet", "sg", "ec2", "ecs", "lambda", "iam_role", "ecr"}


# --- vpc/subnet/sg: no AWS-native name field, so the odin:node tag is the
# ONLY way back to the canvas label (vpc/subnet); sg falls back to its own
# GroupName when the tag is absent. -----------------------------------------


def test_vpc_and_subnet_resolve_label_from_the_odin_node_tag(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set(ENV, "ec2:vpc-1", {"odin:node": "net"})
    stores.ec2net.set(ENV, "subnet:subnet-1", {"subnet_id": "subnet-1", "vpc_id": "vpc-1"})
    stores.tags.set(ENV, "ec2:subnet-1", {"odin:node": "web"})

    result = project(stores, ENV)

    assert result["net"] == ("vpc", "healthy", {})
    assert result["web"] == ("subnet", "healthy", {})


def test_vpc_with_no_odin_node_tag_is_not_projected(tmp_path):
    # No AWS-native name field to fall back to -- an untagged vpc (e.g. one
    # applied before this feature existed) can't be mapped to a label yet.
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "vpc:vpc-1", {"vpc_id": "vpc-1"})
    assert project(stores, ENV) == {}


def test_sg_falls_back_to_its_own_group_name_when_untagged(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "sg:sg-1", {"group_id": "sg-1", "group_name": "web-sg", "vpc_id": "vpc-1"})
    assert project(stores, ENV)["web-sg"] == ("sg", "healthy", {})


def test_sg_prefers_the_odin_node_tag_over_group_name(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "sg:sg-1", {"group_id": "sg-1", "group_name": "web-sg", "vpc_id": "vpc-1"})
    stores.tags.set(ENV, "ec2:sg-1", {"odin:node": "the-canvas-label"})
    assert "the-canvas-label" in project(stores, ENV)
    assert "web-sg" not in project(stores, ENV)


# --- iam_role / ecr: healthy on existence, fall back to their own AWS-native
# name field. ------------------------------------------------------------


def test_iam_role_healthy_and_falls_back_to_role_name(tmp_path):
    stores = SynthStores(tmp_path)
    stores.iamctl.set(ENV, "role:lambda-exec", {"role_name": "lambda-exec", "arn": "arn:aws:iam::000000000000:role/lambda-exec"})
    assert project(stores, ENV)["lambda-exec"] == ("iam_role", "healthy", {})


def test_ecr_healthy_and_falls_back_to_repository_name(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecr.set(ENV, "repo:app-image", {"repository_name": "app-image", "repository_arn": "arn:aws:ecr:us-east-1:000000000000:repository/app-image"})
    assert project(stores, ENV)["app-image"] == ("ecr", "healthy", {})


# --- ec2: the flagship case -- a real Lima VM state machine mapped onto the
# World Phase enum. --------------------------------------------------------


def _ec2_instance(instance_id: str, state_name: str) -> dict:
    return {"instance_id": instance_id, "state_name": state_name}


def test_ec2_instance_phases_across_the_real_state_machine(tmp_path):
    stores = SynthStores(tmp_path)
    # `terminated` is NOT here -- it's excluded entirely (see the dedicated
    # test below); every other live/transitional state maps onto a Phase.
    expected = {
        "pending": "starting", "running": "healthy", "stopping": "starting",
        "stopped": "crashed", "shutting-down": "starting",
    }
    for state_name, phase in expected.items():
        stores.ec2compute.set(ENV, f"instance:i-{state_name}", _ec2_instance(f"i-{state_name}", state_name))
        stores.tags.set(ENV, f"ec2:i-{state_name}", {"odin:node": state_name})

    result = project(stores, ENV)
    for state_name, phase in expected.items():
        assert result[state_name] == ("ec2", phase, {}), state_name


def test_terminated_ec2_instance_is_excluded_entirely(tmp_path):
    # Release sweep finding #2: a `terminated` instance is GONE -- the Lima VM
    # was really deleted (tofu destroy / empty-canvas Apply / boot failure). It
    # must NOT be projected: this projection reads the store directly and never
    # triggers ec2compute's Describe-driven lazy sweep, so a projected
    # `terminated` would keep the label in the snapshot forever and the
    # reconciler would never prune it -- the phantom `crashed` EC2 the sweep
    # found lingering in /world after teardown. Excluding it (the ECS INACTIVE
    # precedent) makes the reconciler prune it immediately.
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-1", _ec2_instance("i-1", "terminated"))
    stores.tags.set(ENV, "ec2:i-1", {"odin:node": "server"})
    assert project(stores, ENV) == {}


def test_ec2_instance_with_no_odin_node_tag_is_not_projected(tmp_path):
    # No AWS-native "Name" field on a real EC2 instance either -- untagged
    # means unmappable, same as vpc/subnet.
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-1", _ec2_instance("i-1", "running"))
    assert project(stores, ENV) == {}


# --- lambda: two-state mapping, falls back to FunctionName (== the canvas
# label already, per agent/hcl.py's own builder). --------------------------


def _lambda_fn(name: str, state: str) -> dict:
    return {"function_name": name, "function_arn": f"arn:aws:lambda:us-east-1:000000000000:function:{name}", "state": state}


def test_lambda_pending_active_failed_map_to_starting_healthy_crashed(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Pending"))
    stores.lambdactl.set(ENV, "fn:fn2", _lambda_fn("fn2", "Active"))
    stores.lambdactl.set(ENV, "fn:fn3", _lambda_fn("fn3", "Failed"))

    result = project(stores, ENV)
    assert result["fn1"] == ("lambda", "starting", {})
    assert result["fn2"] == ("lambda", "healthy", {})
    assert result["fn3"] == ("lambda", "crashed", {})


def test_lambda_falls_back_to_function_name_without_a_tag(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active"))
    assert "fn1" in project(stores, ENV)


# --- ecs: healthy iff runningCount == desiredCount; INACTIVE (deleted, in
# its grace window) services are excluded entirely. -------------------------


def _ecs_service(cluster: str, name: str, desired: int, status: str = "ACTIVE", node_label: str | None = None) -> dict:
    rec = {"cluster_name": cluster, "service_name": name, "desired_count": desired, "status": status}
    if node_label is not None:
        rec["node_label"] = node_label
    return rec


def _ecs_task(cluster: str, service: str, task_id: str, last_status: str) -> dict:
    return {"cluster_name": cluster, "service_name": service, "task_id": task_id, "last_status": last_status}


def test_ecs_service_healthy_when_running_equals_desired(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=2))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task("odin", "app", "t1", "RUNNING"))
    stores.ecsctl.set(ENV, "task:odin:t2", _ecs_task("odin", "app", "t2", "RUNNING"))
    assert project(stores, ENV)["app"] == ("ecs", "healthy", {})


def test_ecs_service_starting_when_running_below_desired(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=2))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task("odin", "app", "t1", "RUNNING"))
    stores.ecsctl.set(ENV, "task:odin:t2", _ecs_task("odin", "app", "t2", "PROVISIONING"))
    assert project(stores, ENV)["app"] == ("ecs", "starting", {})


def test_ecs_inactive_service_is_excluded_entirely(tmp_path):
    # A deleted service is kept around INACTIVE for a grace window
    # (ecsctl.py's own delete-waiter shim) -- it must not still read as
    # healthy; the reconciler prunes it immediately rather than waiting for
    # ecsctl's own sweep.
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=2, status="INACTIVE"))
    assert project(stores, ENV) == {}


def test_ecs_service_prefers_node_label_over_service_name(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=0, node_label="the-canvas-label"))
    result = project(stores, ENV)
    assert "the-canvas-label" in result
    assert "app" not in result


def test_ecs_service_falls_back_to_service_name_without_node_label(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=0))
    assert "app" in project(stores, ENV)


# --- multi-kind smoke: nothing clobbers anything else's label namespace ---


def test_multiple_kinds_project_independently(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set(ENV, "ec2:vpc-1", {"odin:node": "net"})
    stores.iamctl.set(ENV, "role:r1", {"role_name": "r1", "arn": "arn:aws:iam::000000000000:role/r1"})
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active"))

    result = project(stores, ENV)
    assert set(result) == {"net", "r1", "fn1"}
