"""Fix-wave 2b finding #1 -- reconcile/tf_status.py: a pure, read-only
projection of TF-owned resources (vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr/
logs -- kinds only tofu ever creates/destroys, never entered into World before
this fix) from the gateway's synth stores into `label -> (kind, phase,
facts, verdict)`. Hand-built `SynthStores`, no reconciler/asyncio involved
-- see tests/reconcile/test_reconciler.py for the Reconciler-level
integration (emitting WorldDeltas + pruning)."""
from __future__ import annotations

from odin.gateway.models import secretsctl, ssmctl
from odin.gateway.stores import SynthStores
from odin.reconcile.tf_status import TF_OWNED_KINDS, project
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST

ENV = "default"


def _param(name: str, value: str = "v") -> dict:
    """An ssmctl `param:` record, as `PutParameter` writes it."""
    return {
        "name": name, "arn": ssmctl.parameter_arn(name), "type": "SecureString", "value": value,
        "version": 1, "description": None, "key_id": None, "allowed_pattern": None,
        "tier": "Standard", "data_type": "text", "policies": None, "last_modified_date": 1.0,
    }


def test_tf_owned_kinds_excludes_reconciler_owned_kinds():
    # s3/sqs/sns/dynamodb already get real World entries via the reconciler's
    # own PROVISIONED path -- this projection must never double-own them.
    assert TF_OWNED_KINDS == {
        "vpc", "subnet", "sg", "ec2", "ecs", "lambda", "iam_role", "ecr", "logs", "secret", "ssm",
        "elasticache",
    }


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

    assert result["net"] == ("vpc", "healthy", {}, None)
    assert result["web"] == ("subnet", "healthy", {}, None)


def test_vpc_with_no_odin_node_tag_is_not_projected(tmp_path):
    # No AWS-native name field to fall back to -- an untagged vpc (e.g. one
    # applied before this feature existed) can't be mapped to a label yet.
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "vpc:vpc-1", {"vpc_id": "vpc-1"})
    assert project(stores, ENV) == {}


def test_sg_falls_back_to_its_own_group_name_when_untagged(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "sg:sg-1", {"group_id": "sg-1", "group_name": "web-sg", "vpc_id": "vpc-1"})
    assert project(stores, ENV)["web-sg"] == ("sg", "healthy", {}, None)


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
    assert project(stores, ENV)["lambda-exec"] == ("iam_role", "healthy", {}, None)


def test_ecr_healthy_and_falls_back_to_repository_name(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecr.set(ENV, "repo:app-image", {"repository_name": "app-image", "repository_arn": "arn:aws:ecr:us-east-1:000000000000:repository/app-image"})
    assert project(stores, ENV)["app-image"] == ("ecr", "healthy", {}, None)


# --- logs (W2.1): healthy on existence, tag-then-group-name label -- except an
# `auto` group, which the SUBSTRATE created and the canvas never drew. --------


def _log_group(name: str, auto: bool = False, retention: int | None = None) -> dict:
    return {
        "log_group_name": name, "creation_time": 1_700_000_000_000,
        "retention_in_days": retention, "auto": auto,
    }


def test_log_group_projects_healthy_under_its_odin_node_tag(tmp_path):
    stores = SynthStores(tmp_path)
    stores.logsctl.set(ENV, "group:/odin/app", _log_group("/odin/app", retention=14))
    stores.tags.set(
        ENV, "logs:arn:aws:logs:us-east-1:000000000000:log-group:/odin/app",
        {"odin:node": "the-canvas-label"},
    )
    result = project(stores, ENV)
    assert result["the-canvas-label"] == ("logs", "healthy", {}, None)
    assert "/odin/app" not in result


def test_log_group_falls_back_to_its_own_group_name_when_untagged(tmp_path):
    # The group name already IS the canvas label by construction (hcl.py's
    # `_logs` builder), so the fallback is exact, not a guess.
    stores = SynthStores(tmp_path)
    stores.logsctl.set(ENV, "group:/odin/app", _log_group("/odin/app"))
    assert project(stores, ENV)["/odin/app"] == ("logs", "healthy", {}, None)


def test_auto_created_log_group_is_not_projected(tmp_path):
    # A substrate-created `/aws/lambda/{fn}` group is bookkeeping, not a canvas
    # node -- projecting it would strand a phantom World resource nothing can
    # ever prune (no Stack resource carries that label).
    stores = SynthStores(tmp_path)
    stores.logsctl.set(ENV, "group:/aws/lambda/fn1", _log_group("/aws/lambda/fn1", auto=True))
    assert project(stores, ENV) == {}


def test_adopted_log_group_projects_once_tofu_owns_it(tmp_path):
    # CreateLogGroup ADOPTS an auto group (logsctl.py's deviation 2), clearing
    # the flag -- from that point the canvas owns it and World must show it.
    stores = SynthStores(tmp_path)
    stores.logsctl.set(ENV, "group:/aws/lambda/fn1", _log_group("/aws/lambda/fn1", auto=False))
    assert project(stores, ENV)["/aws/lambda/fn1"] == ("logs", "healthy", {}, None)


def test_log_streams_events_and_cursors_are_not_projected_as_resources(tmp_path):
    # The logsctl store also holds the DATA plane (streams, event ring buffers,
    # tail cursors) -- only `group:` records are World resources.
    stores = SynthStores(tmp_path)
    stores.logsctl.set(ENV, "group:/odin/app", _log_group("/odin/app"))
    stores.logsctl.set(ENV, "stream:/odin/app:s1", {"log_group": "/odin/app", "log_stream_name": "s1"})
    stores.logsctl.set(ENV, "events:/odin/app", [{"stream": "s1", "timestamp": 1, "message": "hi"}])
    stores.logsctl.set(ENV, "cursor:/odin/app:s1", 1)
    assert set(project(stores, ENV)) == {"/odin/app"}


# --- secret + ssm (W2.4): healthy on existence, and NO FACTS EVER (a fact
# rides the WorldDelta onto the WebSocket and into world.json -- a secret's
# value must never travel either). ------------------------------------------


def test_a_secret_projects_healthy_with_no_facts(tmp_path):
    stores = SynthStores(tmp_path)
    stores.secretsctl.set(ENV, "secret:db-password", {
        "name": "db-password", "arn": secretsctl.secret_arn("db-password"),
        "description": None, "kms_key_id": None, "resource_policy": None,
        "created_date": 1.0, "last_changed_date": 1.0,
    })
    stores.tags.set(ENV, f"secretsmanager:{secretsctl.secret_arn('db-password')}", {"odin:node": "the-canvas-label"})
    result = project(stores, ENV)
    assert result["the-canvas-label"] == ("secret", "healthy", {}, None)
    assert "db-password" not in result


def test_a_secret_falls_back_to_its_own_name_when_untagged(tmp_path):
    stores = SynthStores(tmp_path)
    stores.secretsctl.set(ENV, "secret:db-password", {
        "name": "db-password", "arn": secretsctl.secret_arn("db-password"),
        "description": None, "kms_key_id": None, "resource_policy": None,
        "created_date": 1.0, "last_changed_date": 1.0,
    })
    assert project(stores, ENV)["db-password"] == ("secret", "healthy", {}, None)


def test_secret_versions_are_not_projected_as_resources_and_no_value_leaks(tmp_path):
    stores = SynthStores(tmp_path)
    stores.secretsctl.set(ENV, "secret:db-password", {
        "name": "db-password", "arn": secretsctl.secret_arn("db-password"),
        "description": None, "kms_key_id": None, "resource_policy": None,
        "created_date": 1.0, "last_changed_date": 1.0,
    })
    stores.secretsctl.set(ENV, "version:db-password:v1", {
        "secret_name": "db-password", "version_id": "v1", "secret_string": "hunter2-long",
        "secret_binary": None, "version_stages": ["AWSCURRENT"], "created_date": 1.0,
    })
    result = project(stores, ENV)
    assert set(result) == {"db-password"}
    assert "hunter2-long" not in repr(result)


def test_an_ssm_parameter_projects_healthy_with_no_facts(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ssmctl.set(ENV, "param:/odin/api-key", _param("/odin/api-key"))
    stores.tags.set(ENV, "ssm:/odin/api-key", {"odin:node": "the-canvas-label"})
    result = project(stores, ENV)
    assert result["the-canvas-label"] == ("ssm", "healthy", {}, None)
    assert "/odin/api-key" not in result


def test_an_ssm_parameter_falls_back_to_its_own_name_and_never_carries_its_value(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ssmctl.set(ENV, "param:/odin/api-key", _param("/odin/api-key", value="abc123456"))
    result = project(stores, ENV)
    assert result["/odin/api-key"] == ("ssm", "healthy", {}, None)
    assert "abc123456" not in repr(result)


def test_a_root_level_parameters_tag_key_is_the_canonical_name(tmp_path):
    # The tags store is keyed by the CANONICAL name (ssmctl.canonical_name), so
    # a `/db-url`-written parameter still resolves its odin:node tag.
    stores = SynthStores(tmp_path)
    stores.ssmctl.set(ENV, "param:db-url", _param("/db-url"))
    stores.tags.set(ENV, "ssm:db-url", {"odin:node": "the-canvas-label"})
    assert project(stores, ENV)["the-canvas-label"] == ("ssm", "healthy", {}, None)


# --- ec2: the flagship case -- a real Lima VM state machine mapped onto the
# World Phase enum. --------------------------------------------------------


def _ec2_instance(instance_id: str, state_name: str, state_reason: dict | None = None) -> dict:
    return {"instance_id": instance_id, "state_name": state_name, "state_reason": state_reason}


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
        verdict = None  # no state_reason recorded on any of these
        assert result[state_name] == ("ec2", phase, {}, verdict), state_name


def test_ec2_stopped_with_a_state_reason_carries_it_as_the_verdict(tmp_path):
    # w1 observability: a stop caused by a real boot/start failure records a
    # real StateReason -- it must not vanish into a bare "crashed" badge.
    stores = SynthStores(tmp_path)
    reason = {"code": "Server.InternalError", "message": "limactl start timed out"}
    stores.ec2compute.set(ENV, "instance:i-1", _ec2_instance("i-1", "stopped", reason))
    stores.tags.set(ENV, "ec2:i-1", {"odin:node": "server"})
    assert project(stores, ENV)["server"] == (
        "ec2", "crashed", {}, "Server.InternalError: limactl start timed out",
    )


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


def test_a_drifted_terminated_instance_projects_crashed_with_its_real_reason(tmp_path):
    """W2.2's honesty fix (reconcile/drift.py): the reality sweep marks an
    instance whose VM was deleted OUTSIDE odin `terminated`, because that is
    what makes the next Apply recreate it. Dropping such a record here (the
    rule for every other terminated instance) would trade one dishonesty for
    another -- odin would quietly forget a node still on the canvas instead of
    showing WHY it's down -- so a `drifted` record projects `crashed`."""
    stores = SynthStores(tmp_path)
    record = _ec2_instance("i-1", "terminated", {
        "code": "Client.UserInitiatedShutdown",
        "message": "VM odin-ec2-default-i-1 deleted outside odin — re-Apply to recreate",
    })
    stores.ec2compute.set(ENV, "instance:i-1", {**record, "drifted": True})
    stores.tags.set(ENV, "ec2:i-1", {"odin:node": "server"})

    assert project(stores, ENV)["server"] == (
        "ec2", "crashed", {},
        "Client.UserInitiatedShutdown: VM odin-ec2-default-i-1 deleted outside odin — re-Apply to recreate",
    )


def test_a_recreated_instance_wins_its_label_over_the_drifted_one_it_replaced(tmp_path):
    """The recovery apply mints a NEW instance while the drifted record can
    still be inside ec2compute's 60s lazy-sweep window, so both briefly carry
    the same `odin:node` label. The live one must win, whatever order the
    store happens to hold them in -- a recovered node reading `crashed` off
    the corpse it replaced would be the same false badge in reverse."""
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-new", _ec2_instance("i-new", "running"))
    stores.tags.set(ENV, "ec2:i-new", {"odin:node": "server"})
    stores.ec2compute.set(ENV, "instance:i-old", {
        **_ec2_instance("i-old", "terminated", {"code": "Client.UserInitiatedShutdown", "message": "gone"}),
        "drifted": True,
    })
    stores.tags.set(ENV, "ec2:i-old", {"odin:node": "server"})

    assert project(stores, ENV)["server"] == ("ec2", "healthy", {}, None)


def test_ec2_instance_with_no_odin_node_tag_is_not_projected(tmp_path):
    # No AWS-native "Name" field on a real EC2 instance either -- untagged
    # means unmappable, same as vpc/subnet.
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-1", _ec2_instance("i-1", "running"))
    assert project(stores, ENV) == {}


# --- lambda: two-state mapping, falls back to FunctionName (== the canvas
# label already, per agent/hcl.py's own builder). --------------------------


def _lambda_fn(name: str, state: str, state_reason: str | None = None) -> dict:
    return {
        "function_name": name, "function_arn": f"arn:aws:lambda:us-east-1:000000000000:function:{name}",
        "state": state, "state_reason": state_reason,
    }


def test_lambda_pending_active_failed_map_to_starting_healthy_crashed(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Pending"))
    stores.lambdactl.set(ENV, "fn:fn2", _lambda_fn("fn2", "Active"))
    stores.lambdactl.set(ENV, "fn:fn3", _lambda_fn("fn3", "Failed"))

    result = project(stores, ENV)
    assert result["fn1"] == ("lambda", "starting", {}, None)
    assert result["fn2"] == ("lambda", "healthy", {}, None)
    assert result["fn3"] == ("lambda", "crashed", {}, None)


def test_lambda_falls_back_to_function_name_without_a_tag(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active"))
    assert "fn1" in project(stores, ENV)


def test_lambda_failed_state_reason_becomes_the_verdict(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn(
        "fn1", "Failed", "fn1 RIE never became ready:\nImportError: No module named 'handler'",
    ))
    result = project(stores, ENV)
    assert result["fn1"] == (
        "lambda", "crashed", {}, "fn1 RIE never became ready:\nImportError: No module named 'handler'",
    )


# --- ecs: healthy iff runningCount == desiredCount; a STOPPED task (always
# a real failure -- a deliberate stop deletes its record outright) makes an
# under-capacity service read `crashed` with a real verdict, never a
# perpetual "starting"; INACTIVE (deleted, in its grace window) services are
# excluded entirely. -------------------------------------------------------


class FakeTaskRuntime:
    """`sweep_tasks`'s injectable seam -- reports whatever container status/
    exit code/log tail the test pre-seeds, no real Colima involved."""

    def __init__(
        self, statuses: dict[str, str] | None = None, exit_codes: dict[str, int] | None = None,
        logs: dict[str, str] | None = None,
    ):
        self._statuses = statuses or {}
        self._exit_codes = exit_codes or {}
        self._logs = logs or {}

    def status(self, env, task_id, container_name):
        return self._statuses.get(task_id, "running")

    def exit_code(self, env, task_id, container_name):
        return self._exit_codes.get(task_id, 0)

    def logs(self, env, task_id, container_name, tail=20):
        # W2.1: the sweep now also ships each task container's tail into
        # `/ecs/{service}` (ecsctl.py's `_ship_task_logs`), so this seam has to
        # answer for a log read too. "" (nothing seeded) is a real container's
        # own answer once it has been removed, so it needs no special case.
        return self._logs.get(task_id, "")


def _ecs_service(cluster: str, name: str, desired: int, status: str = "ACTIVE", node_label: str | None = None) -> dict:
    rec = {"cluster_name": cluster, "service_name": name, "desired_count": desired, "status": status}
    if node_label is not None:
        rec["node_label"] = node_label
    return rec


def _ecs_task(
    cluster: str, service: str, task_id: str, last_status: str,
    container_name: str = "app", stopped_reason: str | None = None, exit_code: int | None = None,
    stopped_at: float | None = None,
) -> dict:
    return {
        "cluster_name": cluster, "service_name": service, "task_id": task_id,
        "last_status": last_status, "container_name": container_name,
        "stopped_reason": stopped_reason, "exit_code": exit_code, "stopped_at": stopped_at,
    }


def test_ecs_service_healthy_when_running_equals_desired(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=2))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task("odin", "app", "t1", "RUNNING"))
    stores.ecsctl.set(ENV, "task:odin:t2", _ecs_task("odin", "app", "t2", "RUNNING"))
    assert project(stores, ENV, ecs_runtime=FakeTaskRuntime())["app"] == ("ecs", "healthy", {}, None)


def test_ecs_service_starting_when_running_below_desired_and_nothing_failed(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=2))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task("odin", "app", "t1", "RUNNING"))
    stores.ecsctl.set(ENV, "task:odin:t2", _ecs_task("odin", "app", "t2", "PROVISIONING"))
    assert project(stores, ENV, ecs_runtime=FakeTaskRuntime())["app"] == ("ecs", "starting", {}, None)


def test_ecs_service_with_a_stopped_task_projects_crashed_with_a_verdict(tmp_path):
    # The fix: a service short of capacity because a task already exited on
    # its own must NOT read "starting" forever -- w1's flagship bug.
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=1))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task(
        "odin", "app", "t1", "STOPPED",
        stopped_reason="Essential container in task exited", exit_code=1, stopped_at=100.0,
    ))
    result = project(stores, ENV, ecs_runtime=FakeTaskRuntime())
    assert result["app"] == ("ecs", "crashed", {}, "Essential container in task exited (exit 1)")


def test_ecs_crash_loop_verdict_uses_the_most_recently_stopped_task(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=1))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task(
        "odin", "app", "t1", "STOPPED", stopped_reason="Essential container in task exited",
        exit_code=1, stopped_at=100.0,
    ))
    stores.ecsctl.set(ENV, "task:odin:t2", _ecs_task(
        "odin", "app", "t2", "STOPPED", stopped_reason="Essential container in task exited",
        exit_code=137, stopped_at=200.0,
    ))
    result = project(stores, ENV, ecs_runtime=FakeTaskRuntime())
    assert result["app"] == ("ecs", "crashed", {}, "Essential container in task exited (exit 137)")


def test_ecs_launch_failure_with_no_exit_code_still_crashes_with_its_reason(tmp_path):
    # `_launch_task`'s own exception path stops a task before it ever runs a
    # container -- exit_code stays None, but the reason string IS the verdict.
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=1))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task(
        "odin", "app", "t1", "STOPPED", stopped_reason="no such image: bogus:latest",
        exit_code=None, stopped_at=100.0,
    ))
    result = project(stores, ENV, ecs_runtime=FakeTaskRuntime())
    assert result["app"] == ("ecs", "crashed", {}, "no such image: bogus:latest")


def test_ecs_sweep_promotes_a_spontaneously_exited_container_before_projecting(tmp_path):
    # The store still says RUNNING (no Describe* call has swept it yet) but
    # the REAL container already exited -- project() must sync that itself
    # (via sweep_tasks) so the crash is visible on THIS tick, not the next
    # incidental AWS API call.
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=1))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task("odin", "app", "t1", "RUNNING"))
    runtime = FakeTaskRuntime(statuses={"t1": "exited"}, exit_codes={"t1": 137})

    result = project(stores, ENV, ecs_runtime=runtime)

    assert result["app"][1] == "crashed"
    assert "137" in result["app"][3]
    # The store itself is now honest too, not just this one projection.
    assert stores.ecsctl.get(ENV, "task:odin:t1")["last_status"] == "STOPPED"


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


def test_the_ecs_sweeps_own_auto_created_log_group_never_enters_world(tmp_path):
    # The `auto` skip against its real producer, not a hand-set flag: shipping a
    # task's tail really does create `/ecs/app` (logsctl's `ensure_group`), and
    # that group must stay OUT of World -- nobody drew it on the canvas.
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service("odin", "app", desired=1))
    stores.ecsctl.set(ENV, "task:odin:t1", _ecs_task("odin", "app", "t1", "RUNNING"))
    runtime = FakeTaskRuntime(logs={"t1": "hello from the task\n"})

    result = project(stores, ENV, ecs_runtime=runtime)

    assert stores.logsctl.get(ENV, "group:/ecs/app")["auto"] is True
    assert set(result) == {"app"}
    assert project(stores, ENV, ecs_runtime=runtime).keys() == {"app"}  # still absent next tick


# --- elasticache (W2.8): the ONE kind here that publishes real facts -- the
# cluster's redis endpoint, in both the container- and VM-reachable forms. ---


def _cache_cluster(cluster_id: str, status: str, port: int | None = None, status_reason: str | None = None) -> dict:
    return {
        "cache_cluster_id": cluster_id, "status": status, "status_reason": status_reason,
        "arn": f"arn:aws:elasticache:us-east-1:000000000000:cluster:{cluster_id}",
        "address": CONTAINER_HOST if port else None, "port": port,
    }


def test_elasticache_available_is_healthy_and_publishes_both_endpoint_forms(tmp_path):
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:cache", _cache_cluster("cache", "available", port=51234))

    kind, phase, facts, verdict = project(stores, ENV)["cache"]

    assert (kind, phase, verdict) == ("elasticache", "healthy", None)
    assert facts["REDIS_URL"] == f"redis://{CONTAINER_HOST}:51234"
    assert facts["REDIS_URL_VM"] == f"redis://{LIMA_HOST}:51234"  # finding #5: a Lima VM can't resolve the container host
    assert facts["endpoint"] == f"{CONTAINER_HOST}:51234"


def test_elasticache_creating_and_deleting_are_starting_with_no_facts_yet(tmp_path):
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:c1", _cache_cluster("c1", "creating"))
    stores.cachectl.set(ENV, "cluster:c2", _cache_cluster("c2", "deleting", port=51234))

    result = project(stores, ENV)
    assert result["c1"] == ("elasticache", "starting", {}, None)  # nothing to advertise until it's up
    assert result["c2"][1] == "starting"  # a delete can fail: stays visible until the record is gone


def test_elasticache_create_failed_is_crashed_with_the_real_reason_as_verdict(tmp_path):
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:c1", _cache_cluster("c1", "create-failed", status_reason="redis never became ready"))
    assert project(stores, ENV)["c1"] == ("elasticache", "crashed", {}, "redis never became ready")


def test_elasticache_prefers_the_odin_node_tag_over_the_cluster_id(tmp_path):
    stores = SynthStores(tmp_path)
    cluster = _cache_cluster("cache", "available", port=51234)
    stores.cachectl.set(ENV, "cluster:cache", cluster)
    stores.tags.set(ENV, f"elasticache:{cluster['arn']}", {"odin:node": "the-canvas-label"})

    result = project(stores, ENV)
    assert "the-canvas-label" in result
    assert "cache" not in result


# --- multi-kind smoke: nothing clobbers anything else's label namespace ---


def test_multiple_kinds_project_independently(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set(ENV, "ec2:vpc-1", {"odin:node": "net"})
    stores.iamctl.set(ENV, "role:r1", {"role_name": "r1", "arn": "arn:aws:iam::000000000000:role/r1"})
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active"))
    stores.cachectl.set(ENV, "cluster:cache", _cache_cluster("cache", "available", port=51234))

    result = project(stores, ENV)
    assert set(result) == {"net", "r1", "fn1", "cache"}
