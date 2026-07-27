"""Fix-wave 2b finding #1 -- reconcile/tf_status.py: a pure, read-only
projection of TF-owned resources (vpc/subnet/sg/ec2/ecs/lambda/iam_role/ecr/
logs/rds -- kinds only tofu ever creates/destroys, never entered into World
before this fix) from the gateway's synth stores into `label -> (kind, phase,
facts, verdict)`. Hand-built `SynthStores`, no reconciler/asyncio involved
-- see tests/reconcile/test_reconciler.py for the Reconciler-level
integration (emitting WorldDeltas + pruning)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from odin.aws.cache import container_name as cache_container_name
from odin.aws.rds import container_name as db_container_name
from odin.compute.functions import container_name as function_container_name
from odin.compute.proxy import container_name as proxy_container_name
from odin.fabric.models import MeshNetwork, SubnetAllocation
from odin.gateway.models import cachectl, elbv2ctl, lambdactl, rdsctl, secretsctl, ssmctl
from odin.gateway.stores import SynthStores
from odin.reconcile import mesh_health
from odin.reconcile.tf_status import TF_OWNED_KINDS, project, stranded_in_tf_state
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST
from odin.simulate.workspace import tf_dir
from odin.spec.models import ResourceObserved, World

ENV = "default"


class FakeContainers:
    """`container_names()`/`status()`/`exit_code()`'s shapes -- the seams
    `project()`'s own `live_verdicts` reads (field test 5).

    A lambda/rds record that CLAIMS to be up now only projects as up if its
    container really is running, so these tests have to say which containers
    exist -- the same thing the ecs tests below already do with
    `FakeTaskRuntime`. What a gone/exited/paused container does is
    tests/reconcile/test_drift.py's business."""

    def __init__(self, *running: str) -> None:
        self.running = list(running)

    async def container_names(self) -> list[str]:
        return list(self.running)

    async def status(self, name: str) -> str:
        return "running" if name in self.running else "absent"

    async def exit_code(self, name: str) -> int:
        return -1


def _fns_up(*function_names: str) -> FakeContainers:
    return FakeContainers(*(function_container_name(ENV, name) for name in function_names))


def _dbs_up(*identifiers: str) -> FakeContainers:
    return FakeContainers(*(db_container_name(ENV, identifier) for identifier in identifiers))


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
        "elasticache", "rds", "alb",
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


def test_a_running_ec2_publishes_its_private_and_overlay_addresses(tmp_path):
    """Field test 2 LOW-13: an ec2 node published NO facts at all -- nothing
    referencable as `${{web1.…}}`, and finding a VM's mesh address meant
    hand-reading `.odin/<env>/nebula/overlay.json`, while rds published three.
    Two facts, both plain addresses: `PRIVATE_IP` (host-reachable, NOT
    SG-gated) and `MESH_IP` (the overlay address a drawn SG really gates, and
    the one that is sticky across recreation)."""
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-1", {**_ec2_instance("i-1", "running"), "private_ip": "192.168.64.9"})
    stores.tags.set(ENV, "ec2:i-1", {"odin:node": "web1"})
    nebula = tmp_path / ENV / "nebula"
    nebula.mkdir(parents=True)
    (nebula / "ca.crt").write_text("---ca---\n")
    (nebula / "overlay.json").write_text(MeshNetwork(
        network=ENV, subnets={"hosts": SubnetAllocation(
            network=ENV, subnet="hosts", cidr="10.42.1.0/24", next_ip=2, assignments={"i-1": "10.42.1.1"},
        )},
    ).model_dump_json())
    (nebula / "lighthouse.pid").write_text(str(os.getpid()))  # this env's lighthouse is up

    mesh_health.reset_cache()
    kind, phase, facts, _ = project(stores, ENV)["web1"]
    assert (kind, phase) == ("ec2", "healthy")
    assert facts == {"PRIVATE_IP": "192.168.64.9", "MESH_IP": "10.42.1.1"}

    # ...and the overlay address is held to the same standard as rds's: no
    # lighthouse, no advertisement (the VM itself is still reachable privately).
    (nebula / "lighthouse.pid").unlink()
    mesh_health.reset_cache()
    _, phase, facts, verdict = project(stores, ENV)["web1"]
    assert (phase, facts) == ("crashed", {"PRIVATE_IP": "192.168.64.9"})
    assert "10.42.1.1 is unreachable" in verdict
    mesh_health.reset_cache()


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

    result = project(stores, ENV, containers=_fns_up("fn2"))
    assert result["fn1"] == ("lambda", "starting", {}, None)
    assert result["fn2"] == ("lambda", "healthy", {}, None)
    # `Failed` with no reason recorded: crashed, and the verdict says what odin
    # DOES know rather than nothing at all -- see the no-reason tests below.
    assert result["fn3"][:3] == ("lambda", "crashed", {})
    assert "the container behind it is odin-lambda-default-fn3" in result["fn3"][3]


def test_lambda_falls_back_to_function_name_without_a_tag(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active"))
    assert "fn1" in project(stores, ENV, containers=_fns_up("fn1"))


def test_lambda_failed_state_reason_becomes_the_verdict(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn(
        "fn1", "Failed", "fn1 RIE never became ready:\nImportError: No module named 'handler'",
    ))
    result = project(stores, ENV)
    assert result["fn1"] == (
        "lambda", "crashed", {}, "fn1 RIE never became ready:\nImportError: No module named 'handler'",
    )


# --- field test 2 finding #4: a DEPLOYED function whose invocations all fail --


def test_a_deployed_function_whose_last_invocation_failed_says_so_in_its_verdict(tmp_path):
    # M8 found this unprompted: the handler was named `handler` while the entry
    # point looked for `lambda_handler`, so every invocation raised
    # Runtime.HandlerNotFound -- and /world said `healthy` throughout.
    stores = SynthStores(tmp_path)
    record = _lambda_fn("fn1", "Active") | {"last_invocation_error": "Unhandled"}
    stores.lambdactl.set(ENV, "fn:fn1", record)

    kind, phase, facts, verdict = project(stores, ENV, containers=_fns_up("fn1"))["fn1"]
    assert (kind, phase, facts) == ("lambda", "healthy", {})  # the DEPLOY really did succeed
    assert verdict == "the last invocation failed (Unhandled) — the deploy succeeded, the handler did not"


def test_a_cold_function_that_has_never_been_invoked_raises_no_alarm(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active"))
    assert project(stores, ENV, containers=_fns_up("fn1"))["fn1"] == ("lambda", "healthy", {}, None)


def test_a_function_whose_last_invocation_succeeded_raises_no_alarm(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active") | {"last_invocation_error": None})
    assert project(stores, ENV, containers=_fns_up("fn1"))["fn1"] == ("lambda", "healthy", {}, None)


def test_a_failed_deploy_still_reports_the_deploy_reason_not_the_invocation_one(tmp_path):
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Failed", "container never became ready") | {
        "last_invocation_error": "Unhandled",
    })
    assert project(stores, ENV)["fn1"] == ("lambda", "crashed", {}, "container never became ready")


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

    async def status(self, env, task_id, container_name):
        return self._statuses.get(task_id, "running")

    async def exit_code(self, env, task_id, container_name):
        return self._exit_codes.get(task_id, 0)

    async def logs(self, env, task_id, container_name, tail=20):
        # W2.1: the sweep now also ships each task container's tail into
        # `/ecs/{service}` (ecsctl.py's `_ship_task_logs`), so this seam has to
        # answer for a log read too. "" (nothing seeded) is a real container's
        # own answer once it has been removed, so it needs no special case.
        return self._logs.get(task_id, "")


# Field test 3: the projection is now revision-aware (a service left serving
# its PREVIOUS revision by a failed deployment must not read `healthy`), so
# both fixtures carry a task-definition arn. They default to the SAME
# revision, which is the "nothing mid-rollout" shape every pre-existing test
# here means.
_TASKDEF_ARN = "arn:aws:ecs:us-east-1:000000000000:task-definition/app:1"


def _ecs_service(
    cluster: str, name: str, desired: int, status: str = "ACTIVE", node_label: str | None = None,
    task_definition_arn: str = _TASKDEF_ARN,
) -> dict:
    rec = {
        "cluster_name": cluster, "service_name": name, "desired_count": desired, "status": status,
        "task_definition_arn": task_definition_arn,
    }
    if node_label is not None:
        rec["node_label"] = node_label
    return rec


def _ecs_task(
    cluster: str, service: str, task_id: str, last_status: str,
    container_name: str = "app", stopped_reason: str | None = None, exit_code: int | None = None,
    stopped_at: float | None = None, task_definition_arn: str = _TASKDEF_ARN,
) -> dict:
    return {
        "cluster_name": cluster, "service_name": service, "task_id": task_id,
        "last_status": last_status, "container_name": container_name,
        "stopped_reason": stopped_reason, "exit_code": exit_code, "stopped_at": stopped_at,
        "task_definition_arn": task_definition_arn,
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


# --- ecs: the revision-aware half (field test 3) -------------------------
# Keeping the previous revision alive through a failed deployment
# (ecsctl.py's `_retire_stale`) is only honest if the projection REFUSES to
# call that healthy. These four pin the distinction odin can actually make:
# "N serving the previous revision, the new one failed" is not "N serving the
# current revision" and is not "zero tasks, dead".
_PREVIOUS_ARN = "arn:aws:ecs:us-east-1:000000000000:task-definition/app:1"
_CURRENT_ARN = "arn:aws:ecs:us-east-1:000000000000:task-definition/app:2"


def _rolled_over(stores, *, desired: int, previous_running: int, image: str = "nginx:typo-9z9z") -> None:
    """A service pointed at revision 2, whose replacements all failed, with
    `previous_running` revision-1 tasks still serving -- exactly the state
    field test 3's typo'd tag now leaves behind."""
    stores.ecsctl.set(ENV, "taskdef:app:2", {
        "family": "app", "revision": 2, "status": "ACTIVE",
        "container_definitions": [{"name": "app", "image": image}],
    })
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service(
        "odin", "app", desired=desired, task_definition_arn=_CURRENT_ARN,
    ))
    for i in range(previous_running):
        stores.ecsctl.set(ENV, f"task:odin:old{i}", _ecs_task(
            "odin", "app", f"old{i}", "RUNNING", task_definition_arn=_PREVIOUS_ARN,
        ))
    stores.ecsctl.set(ENV, "task:odin:new1", _ecs_task(
        "odin", "app", "new1", "STOPPED", task_definition_arn=_CURRENT_ARN,
        stopped_reason=f"no such image: {image}", stopped_at=100.0,
    ))


def test_ecs_serving_the_previous_revision_is_neither_healthy_nor_crashed(tmp_path):
    stores = SynthStores(tmp_path)
    _rolled_over(stores, desired=2, previous_running=2)

    kind, phase, _, verdict = project(stores, ENV, ecs_runtime=FakeTaskRuntime())["app"]

    assert kind == "ecs"
    # THE point: two tasks are serving, so this is not an outage -- and the
    # revision the operator asked for is not running, so it is not healthy.
    assert phase == "error", "a failed deployment behind a serving old revision must not read healthy"
    assert verdict == (
        "2 tasks serving the previous revision; "
        "deployment of nginx:typo-9z9z failed: no such image: nginx:typo-9z9z"
    ), verdict


def test_ecs_serving_previous_revision_verdict_counts_and_names_concretely(tmp_path):
    # One task left: singular, and the image the FAILED deployment asked for.
    stores = SynthStores(tmp_path)
    _rolled_over(stores, desired=3, previous_running=1, image="ghcr.io/acme/api:v9-typo")

    _, phase, _, verdict = project(stores, ENV, ecs_runtime=FakeTaskRuntime())["app"]

    assert phase == "error"
    assert verdict.startswith("1 task serving the previous revision; ")
    assert "ghcr.io/acme/api:v9-typo" in verdict


def test_ecs_zero_tasks_left_is_still_crashed_not_a_degraded_reading(tmp_path):
    # The counterweight: with nothing serving, `error` would UNDERSTATE a real
    # outage. Same failed deployment, no survivors -> crashed, as before.
    stores = SynthStores(tmp_path)
    _rolled_over(stores, desired=2, previous_running=0)

    _, phase, _, verdict = project(stores, ENV, ecs_runtime=FakeTaskRuntime())["app"]

    assert phase == "crashed"
    assert verdict == "no such image: nginx:typo-9z9z"


def test_ecs_stale_task_still_draining_behind_a_converged_rollout_reads_healthy(tmp_path):
    # The other counterweight: a GOOD rollout briefly runs both revisions at
    # once (200% surge). Once the current revision is at desired, the service
    # genuinely is healthy -- a leftover draining task must not demote it.
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service(
        "odin", "app", desired=2, task_definition_arn=_CURRENT_ARN,
    ))
    stores.ecsctl.set(ENV, "task:odin:new1", _ecs_task(
        "odin", "app", "new1", "RUNNING", task_definition_arn=_CURRENT_ARN))
    stores.ecsctl.set(ENV, "task:odin:new2", _ecs_task(
        "odin", "app", "new2", "RUNNING", task_definition_arn=_CURRENT_ARN))
    stores.ecsctl.set(ENV, "task:odin:old1", _ecs_task(
        "odin", "app", "old1", "RUNNING", task_definition_arn=_PREVIOUS_ARN))

    assert project(stores, ENV, ecs_runtime=FakeTaskRuntime())["app"] == ("ecs", "healthy", {}, None)


def test_ecs_replacements_not_yet_up_read_starting_not_error(tmp_path):
    # Mid-rollout with nothing failed yet is honest asynchrony, not a failure:
    # `error` is reserved for a deployment that actually died.
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", _ecs_service(
        "odin", "app", desired=2, task_definition_arn=_CURRENT_ARN,
    ))
    stores.ecsctl.set(ENV, "task:odin:new1", _ecs_task(
        "odin", "app", "new1", "PROVISIONING", task_definition_arn=_CURRENT_ARN))
    stores.ecsctl.set(ENV, "task:odin:old1", _ecs_task(
        "odin", "app", "old1", "RUNNING", task_definition_arn=_PREVIOUS_ARN))

    assert project(stores, ENV, ecs_runtime=FakeTaskRuntime())["app"] == ("ecs", "starting", {}, None)


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


# --- elasticache (W2.8): a kind here that publishes real facts -- the
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
    # The half this test USED to leave unasserted, which is how the bug below
    # survived it: the deleting cluster still had a port on its record.
    assert result["c2"][2] == {}


async def test_a_deleting_cache_no_longer_advertises_a_live_redis_url(tmp_path):
    """Field test 5's facts audit, hazard 3. `_cache_clusters` published
    `await cachectl.facts(record)` in EVERY phase, so a cluster mid-delete kept
    handing out a `REDIS_URL` a consumer would dial -- the exact stale-green
    lie `_db_instances`'s gate exists to prevent and `_ec2_instances` gates
    for too. The record still carries the port (a delete can fail, and the
    record outlives the container), so nothing else stops it."""
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:cache", _cache_cluster("cache", "deleting", port=51234))

    kind, phase, facts, verdict = project(stores, ENV)["cache"]

    assert (kind, phase, verdict) == ("elasticache", "starting", None)
    assert facts == {}
    assert "51234" not in str(facts) and "redis://" not in str(facts)


def test_every_fact_publishing_kind_gates_on_being_actually_up(tmp_path):
    """The consistency this fix restores, asserted as one rule rather than
    three separate tests: no kind that projects an address may project it
    while the thing is not available. rds and ec2 already held; elasticache
    was the outlier."""
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:cache", _cache_cluster("cache", "deleting", port=51234))
    stores.rdsctl.set(ENV, "db:appdb", _db_record("appdb", "deleting"))

    result = project(stores, ENV)
    assert result["cache"][2] == {}
    assert result["appdb"][2] == {}


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


# --- W2.5: alb -- another kind that projects FACTS ----------------------------


def _lb(name: str, state: str = "active", endpoints: dict | None = None, reason: str | None = None) -> dict:
    """An elbv2ctl `lb:` record, as CreateLoadBalancer writes it and
    `converge_proxy` then updates it."""
    return {
        "name": name, "lb_id": "abc123", "arn": elbv2ctl.lb_arn(name, "abc123"),
        "scheme": "internal", "type": "application", "ip_address_type": "ipv4",
        "vpc_id": "vpc-1", "subnets": ["subnet-1"], "security_groups": [],
        "availability_zones": [], "created_time": "2026-07-25T00:00:00+00:00",
        "state": state, "state_reason": reason, "attributes": {},
        "endpoints": endpoints if endpoints is not None else {"80": 41234},
    }


def test_an_active_load_balancer_projects_healthy_with_its_real_endpoint(tmp_path):
    """A load balancer's whole point is an address, and `DNSName` has nowhere to
    put the dynamic host port odin publishes the proxy on -- so the reachable URL
    is the one fact this projection carries."""
    stores = SynthStores(tmp_path)
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb"))
    stores.tags.set(ENV, f"elasticloadbalancing:{elbv2ctl.lb_arn('web-lb', 'abc123')}", {"odin:node": "the-canvas-label"})

    result = project(stores, ENV)
    assert result["the-canvas-label"] == ("alb", "healthy", {"ALB_ENDPOINT": "http://127.0.0.1:41234"}, None)
    assert "web-lb" not in result


def test_a_provisioning_load_balancer_projects_starting_not_healthy(tmp_path):
    # Honest asynchrony: the real nginx container is still coming up.
    stores = SynthStores(tmp_path)
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb", state="provisioning", endpoints={}))
    assert project(stores, ENV)["web-lb"] == ("alb", "starting", {}, None)


def test_a_failed_load_balancer_projects_crashed_with_the_real_docker_reason(tmp_path):
    stores = SynthStores(tmp_path)
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb(
        "web-lb", state="failed", endpoints={}, reason="docker run failed: no space left on device",
    ))
    assert project(stores, ENV)["web-lb"] == (
        "alb", "crashed", {}, "docker run failed: no space left on device",
    )


def test_a_load_balancer_falls_back_to_its_own_name_when_untagged(tmp_path):
    stores = SynthStores(tmp_path)
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb"))
    assert project(stores, ENV)["web-lb"][0] == "alb"


def test_target_groups_listeners_and_targets_are_not_projected_as_resources(tmp_path):
    """One `alb` canvas node expands to three tf resources; only the load
    balancer is the node. Projecting the companions would strand World entries
    no Stack resource can ever prune."""
    stores = SynthStores(tmp_path)
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb"))
    stores.elbv2ctl.set(ENV, "tg:web-lb-tg", {"name": "web-lb-tg", "arn": "arn:tg"})
    stores.elbv2ctl.set(ENV, "listener:deadbeef", {"listener_id": "deadbeef", "lb_name": "web-lb"})
    stores.elbv2ctl.set(ENV, "targets:web-lb-tg", [{"id": "host.docker.internal", "port": 32768}])
    assert set(project(stores, ENV)) == {"web-lb"}


# --- multi-kind smoke: nothing clobbers anything else's label namespace ---


def test_multiple_kinds_project_independently(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.tags.set(ENV, "ec2:vpc-1", {"odin:node": "net"})
    stores.iamctl.set(ENV, "role:r1", {"role_name": "r1", "arn": "arn:aws:iam::000000000000:role/r1"})
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Active"))
    stores.cachectl.set(ENV, "cluster:cache", _cache_cluster("cache", "available", port=51234))
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb"))

    result = project(stores, ENV)
    assert set(result) == {"net", "r1", "fn1", "cache", "web-lb"}


# --- rds (W2.7): a projected kind that carries real FACTS -------------------


def _db_record(identifier: str, status: str = "available", port: int = 54321, **extra) -> dict:
    return {
        "db_instance_identifier": identifier, "status": status, "status_reason": None,
        "master_username": "app", "master_password": "apppass123", "db_name": "postgres",
        "endpoint_address": "host.docker.internal", "endpoint_port": port, **extra,
    }


def _db(stores: SynthStores, label: str, identifier: str, **kwargs) -> None:
    stores.rdsctl.set(ENV, f"db:{identifier}", _db_record(identifier, **kwargs))
    stores.tags.set(ENV, f"rds:{rdsctl.db_arn(identifier)}", {"odin:node": label})


def test_an_available_database_projects_healthy_with_both_database_url_forms(tmp_path):
    """THE contract the move onto Terraform had to preserve byte-for-byte:
    existing canvases reference `${{db.DATABASE_URL}}` (containers) and
    `${{db.DATABASE_URL_VM}}` (an EC2 Lima VM, v0.5.4 finding #5) by name, and
    `fabric/` resolves both out of exactly these World facts."""
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db")

    kind, phase, facts, verdict = project(stores, ENV, containers=_dbs_up("app-db"))["app-db"]

    assert (kind, phase, verdict) == ("rds", "healthy", None)
    assert facts == {
        "DATABASE_URL": "postgresql://app:apppass123@host.docker.internal:54321/postgres",
        "endpoint": "host.docker.internal:54321",
        "DATABASE_URL_VM": "postgresql://app:apppass123@host.lima.internal:54321/postgres",
        "endpoint_vm": "host.lima.internal:54321",
    }


def test_a_database_on_the_mesh_also_publishes_its_gated_overlay_address(tmp_path):
    """W2.6: the overlay address is an ADDITIONAL fact. The two host-reachable
    forms above are untouched (the gateway, the create waiter's probe and every
    host-side client ride them), and `DATABASE_URL_MESH` is the SG-gated path a
    mesh member uses -- on the container's OWN 5432, not the published host
    port, because the mesh sidecar shares its network namespace."""
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db", overlay_ip="10.42.1.4")

    facts = project(stores, ENV, containers=_dbs_up("app-db"))["app-db"][2]

    assert facts["endpoint_mesh"] == "10.42.1.4:5432"
    assert facts["DATABASE_URL_MESH"] == "postgresql://app:apppass123@10.42.1.4:5432/postgres"
    assert facts["endpoint"] == "host.docker.internal:54321"  # the host path, unchanged
    assert facts["DATABASE_URL_VM"] == "postgresql://app:apppass123@host.lima.internal:54321/postgres"


def test_a_dead_mesh_path_withholds_the_overlay_fact_and_ends_healthy(tmp_path):
    """Field test 2 HIGH-2/B8, at the projection: the database is fine on its
    published host port, but the address odin ADVERTISES for mesh consumers
    cannot be reached (here: this env has a Nebula network and no lighthouse
    process, exactly B8's case). It must stop reading `healthy`, stop handing
    out that address, and say why -- while the two host forms carry on
    untouched. `reconcile/mesh_health.py`'s own tests cover every failure mode
    and the sweep cadence; this one pins that `project` is wired to it."""
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db", overlay_ip="10.42.1.4")
    nebula = tmp_path / ENV / "nebula"
    nebula.mkdir(parents=True)
    (nebula / "ca.crt").write_text("---ca---\n")  # this env HAS a mesh

    mesh_health.reset_cache()
    _, phase, facts, verdict = project(stores, ENV, containers=_dbs_up("app-db"))["app-db"]

    assert phase == "crashed", "healthy on an unverified overlay address is the bug"
    assert "DATABASE_URL_MESH" not in facts and "endpoint_mesh" not in facts
    assert facts["endpoint"] == "host.docker.internal:54321"
    assert "10.42.1.4:5432 is unreachable" in verdict
    mesh_health.reset_cache()


def test_a_database_with_no_mesh_publishes_no_overlay_facts(tmp_path):
    """An env with no Nebula network (no VPC drawn) publishes exactly the facts
    it always did -- no empty or placeholder mesh keys."""
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db", overlay_ip=None)

    facts = project(stores, ENV, containers=_dbs_up("app-db"))["app-db"][2]

    assert "DATABASE_URL_MESH" not in facts and "endpoint_mesh" not in facts


def test_the_database_url_path_is_the_instances_real_db_name(tmp_path):
    """`db_name` is a real `POSTGRES_DB` the substrate creates (aws/rds.py), so
    the URL points at a database that exists rather than at a label."""
    stores = SynthStores(tmp_path)
    stores.rdsctl.set(ENV, "db:app-db", _db_record("app-db", db_name="orders"))
    stores.tags.set(ENV, f"rds:{rdsctl.db_arn('app-db')}", {"odin:node": "app-db"})

    facts = project(stores, ENV, containers=_dbs_up("app-db"))["app-db"][2]

    assert facts["DATABASE_URL"].endswith("/orders")
    assert facts["DATABASE_URL_VM"].endswith("/orders")


def test_a_creating_database_is_starting_with_no_facts_yet(tmp_path):
    """A half-booted database must not advertise an endpoint that isn't
    serving -- the provider's create waiter is still polling at this point."""
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db", status="creating", port=0)

    assert project(stores, ENV)["app-db"] == ("rds", "starting", {}, None)


def test_a_deleting_database_stays_visible_until_its_record_is_gone(tmp_path):
    # ec2's `shutting-down` reasoning: a delete can fail and the container
    # outlive it, so the node must not vanish off the canvas early.
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db", status="deleting")

    assert project(stores, ENV)["app-db"] == ("rds", "starting", {}, None)


def test_a_failed_database_projects_crashed_with_the_real_reason_and_no_stale_url(tmp_path):
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db", status="failed", status_reason="Postgres never became ready: timeout")

    assert project(stores, ENV)["app-db"] == (
        "rds", "crashed", {}, "Postgres never became ready: timeout",
    )


def test_an_untagged_database_still_projects_under_its_identifier(tmp_path):
    """Unlike vpc/subnet/ec2, rds HAS an AWS-native name -- the
    DBInstanceIdentifier, which agent/hcl.py sets to the canvas label -- so an
    untagged instance (imported out of band) is still projectable."""
    stores = SynthStores(tmp_path)
    stores.rdsctl.set(ENV, "db:app-db", _db_record("app-db"))

    assert project(stores, ENV, containers=_dbs_up("app-db"))["app-db"][0] == "rds"


# --- a `crashed` resource whose record kept NO reason -------------------------
#
# The four crashed-phase sites here used to read `(record.get(...) or None)`,
# turning an empty reason into no verdict: a node went red on the canvas with
# nothing said, while its kind, identifier and container name were all known.
# Verified against a real running server before the fix -- four `crashed`
# resources, `"verdict": null` in `/world`, `odin world` printing phase and
# nothing else.


def _no_reason_verdict(result: dict, label: str) -> str:
    kind, phase, facts, verdict = result[label]
    assert phase == "crashed"
    return verdict


def test_a_crashed_resource_with_no_recorded_reason_still_names_what_is_known(tmp_path):
    """All four kinds, each with a reason that is empty (`str(exc)` of an
    exception built with no message -- the shape three of the four writers
    really store) or absent (the `.get()` default). None of them may project a
    bare `None`."""
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Failed", state_reason=""))
    stores.cachectl.set(ENV, "cluster:c1", _cache_cluster("c1", "create-failed", status_reason=""))
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb", state="failed", endpoints={}, reason=""))
    _db(stores, "app-db", "app-db", status="failed")  # `status_reason` is None on the record

    result = project(stores, ENV)

    # Each verdict names the KIND, the IDENTIFIER and the CONTAINER -- the
    # three things odin genuinely knows at this point.
    for label, kind, identifier, container in [
        ("fn1", "lambda", "fn1", function_container_name(ENV, "fn1")),
        ("c1", "elasticache", "c1", cache_container_name(ENV, "c1")),
        ("web-lb", "alb", "web-lb", proxy_container_name(ENV, "web-lb")),
        ("app-db", "rds", "app-db", db_container_name(ENV, "app-db")),
    ]:
        verdict = _no_reason_verdict(result, label)
        assert verdict, f"{label} projected crashed with NO verdict at all"
        assert kind in verdict and repr(identifier) in verdict and container in verdict
        assert "recorded with NO message" in verdict
        # It reports the gap; it does not invent a cause, and it does not
        # promise a recovery /apply-full does not perform for alb/elasticache.
        assert "re-Apply to recreate" not in verdict


def test_a_real_recorded_reason_is_never_replaced_by_the_fallback(tmp_path):
    """The fallback is the LAST resort. A record that carries a real reason
    projects that reason verbatim, byte for byte."""
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", _lambda_fn("fn1", "Failed", "fn1 RIE never became ready"))
    stores.cachectl.set(ENV, "cluster:c1", _cache_cluster("c1", "create-failed", status_reason="redis never became ready"))
    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb", state="failed", endpoints={}, reason="docker run failed"))
    _db(stores, "app-db", "app-db", status="failed", status_reason="Postgres never became ready: timeout")

    result = project(stores, ENV)
    assert result["fn1"][3] == "fn1 RIE never became ready"
    assert result["c1"][3] == "redis never became ready"
    assert result["web-lb"][3] == "docker run failed"
    assert result["app-db"][3] == "Postgres never became ready: timeout"


def test_the_real_writers_no_longer_leave_an_empty_reason(tmp_path):
    """The other half of the same defence, and the one that moved.

    When this was written, the three production failure paths stored
    `str(exc)` verbatim, so a substrate raising `StopIteration` -- whose
    `str()` really is `""`, as for a cancelled Future or a bare
    `KeyError()`/`TimeoutError()` -- landed a record with an empty reason.
    They are all guarded at the writer now (`gateway/errors.py::exc_text`),
    which is where a reason should be rescued: the projection below can only
    say what is KNOWN, while the writer still holds the exception.

    So this drives the real writers and asserts the blank never gets in.
    `_crash_verdict` stays as the backstop for a record that already has one --
    written by an older odin, or by a future writer that forgets."""

    class Boom:
        def ensure(self, *args, **kwargs):
            raise StopIteration

    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:c1", _cache_cluster("c1", "creating"))
    cachectl._finish_create(stores, ENV, "c1", Boom())

    stores.lambdactl.set(ENV, "fn:fn1", {**_lambda_fn("fn1", "Pending"), "environment": {}})
    lambdactl._finish_deploy(
        stores, ENV, "fn1", "python3.12", "app.handler", {}, tmp_path / "code", Boom(), None, None, 128,
    )

    stores.elbv2ctl.set(ENV, "lb:web-lb", _lb("web-lb", state="provisioning", endpoints={}))
    elbv2ctl._converge_safely(stores, ENV, "web-lb", Boom())

    recorded = (
        stores.cachectl.get(ENV, "cluster:c1")["status_reason"],
        stores.lambdactl.get(ENV, "fn:fn1")["state_reason"],
        stores.elbv2ctl.get(ENV, "lb:web-lb")["state_reason"],
    )
    for reason in recorded:
        assert reason, "a writer let a blank reason into the record"
        assert "StopIteration" in reason, f"the class is all there is to say, and it must be said: {reason!r}"


def test_a_record_that_already_holds_an_empty_reason_still_names_what_is_known(tmp_path):
    """The projection-side backstop. A record written by an older odin (or by a
    future writer that forgets) can carry `reason=""`, and `live_verdicts` does
    NOT rescue it -- that only sweeps records claiming to be UP, so an already
    failed one keeps the blank forever. Before `_crash_verdict`, `odin world`
    printed rows reading `crashed` and nothing else."""
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:c1", {**_cache_cluster("c1", "create-failed"), "status_reason": ""})
    stores.lambdactl.set(ENV, "fn:fn1", {**_lambda_fn("fn1", "Failed"), "state_reason": ""})
    stores.elbv2ctl.set(ENV, "lb:web-lb", {**_lb("web-lb", state="failed", endpoints={}), "state_reason": ""})

    result = project(stores, ENV)

    for label in ("c1", "fn1", "web-lb"):
        assert _no_reason_verdict(result, label), f"{label} crashed with no explanation"


# --- field test 3, P2-5: the resources a failed apply leaves ONLY in tofu's
# state. `/world` showed 12 s3/sqs/sns/dynamodb nodes with NO BADGE AT ALL --
# not pending, not crashed, absent -- while `tofu state` listed them and every
# call to them answered ServiceUnavailable. ----------------------------------


def _tf_state(root: Path, env: str, *resources: dict) -> None:
    """tofu's own state file, in tofu's own shape, where the runner puts it."""
    directory = tf_dir(root, env)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "terraform.tfstate").write_text(json.dumps({"version": 4, "resources": list(resources)}))


def _tf_resource(tf_type: str, name_attr: str, name: str, label: str | None = None) -> dict:
    tags = {"odin:node": label} if label else {}
    return {
        "mode": "managed", "type": tf_type, "name": name,
        "instances": [{"attributes": {name_attr: name, "tags": tags}}],
    }


def test_a_bucket_tofu_created_is_visible_when_its_backing_never_started(tmp_path):
    """THE finding. The apply failed, so the desired state was never committed
    and the reconciler never provisioned or observed these -- and the trailing
    gc stopped the backings. They must not simply disappear."""
    _tf_state(
        tmp_path, ENV,
        _tf_resource("aws_s3_bucket", "bucket", "uploads"),
        _tf_resource("aws_sqs_queue", "name", "jobs"),
        _tf_resource("aws_sns_topic", "name", "events"),
        _tf_resource("aws_dynamodb_table", "name", "sessions"),
    )
    stranded = stranded_in_tf_state(tmp_path, ENV, World(env=ENV), reachable_kinds=set())

    assert {r.id for r in stranded} == {"uploads", "jobs", "events", "sessions"}
    assert {r.kind for r in stranded} == {"s3", "sqs", "sns", "dynamodb"}
    assert {r.phase for r in stranded} == {"crashed"}          # an honest phase, not absence
    verdict = next(r.verdict for r in stranded if r.id == "uploads")
    assert "exists in the env's tofu state" in verdict          # WHY it is still listed
    assert "no s3 backing container is running" in verdict      # WHY it does not answer
    assert "ServiceUnavailable" in verdict                      # the error the user actually sees
    assert "Apply again" in verdict                             # ...and the fix


def test_the_stranded_verdict_warns_that_a_failed_apply_reprints_it(tmp_path):
    """The advice used to be a bare "Apply to start it" for a state whose usual
    cause is a FAILED apply -- so re-Applying, failing the same way and reading
    the identical sentence was a loop with nothing in it to notice. Measured on
    a real server: a successful apply clears it on the same tick, and the
    moment the committed Stack stops naming the kind (what a failed apply
    leaves, since `/apply-full` skips `store.apply` on `tf_failed`) the next
    tick's gc stops the backing and this comes back within one poll.

    `odin destroy` must NOT be offered here: under this exact condition
    `/destroy`'s `ensure_backings(last_applied)` boots nothing (the last
    applied Stack is the one without these resources), so the destroy 503-
    retries into the documented wedge."""
    _tf_state(tmp_path, ENV, _tf_resource("aws_s3_bucket", "bucket", "uploads"))
    verdict = stranded_in_tf_state(tmp_path, ENV, World(env=ENV), reachable_kinds=set())[0].verdict

    assert "SUCCEEDS" in verdict and "FAILS" in verdict     # the two outcomes differ, and it says so
    assert "never commits the desired state" in verdict     # WHY a failed apply leaves this standing
    assert "back within about one tick" in verdict          # ...and that the retry changes nothing
    assert "fix the error your last apply printed" in verdict
    assert "odin destroy" not in verdict                    # would wedge; never recommended here


def test_a_resource_whose_backing_is_up_is_left_entirely_alone(tmp_path):
    """The false-alarm guard, and the reason the check is the GATEWAY's own
    routing table: during a healthy apply the backing is up from
    `ensure_backings` while the reconciler simply has not observed the new
    bucket yet. Reporting `crashed` there would be a fresh lie."""
    _tf_state(tmp_path, ENV, _tf_resource("aws_s3_bucket", "bucket", "uploads"))
    assert stranded_in_tf_state(tmp_path, ENV, World(env=ENV), reachable_kinds={"s3"}) == ()


def test_world_always_wins(tmp_path):
    """A resource the reconciler really observed keeps its own phase: this
    overlay only ever speaks about labels World says nothing about."""
    _tf_state(tmp_path, ENV, _tf_resource("aws_s3_bucket", "bucket", "uploads"))
    world = World(env=ENV, resources=(ResourceObserved(id="uploads", kind="s3", phase="healthy"),))
    assert stranded_in_tf_state(tmp_path, ENV, world, reachable_kinds=set()) == ()


def test_a_resource_tofu_destroyed_stops_being_reported_immediately(tmp_path):
    """The v0.5.2 phantom-EC2 guard. Nothing here outlives tofu's state entry,
    so `tofu destroy` / an empty-canvas Apply makes it disappear the same
    instant -- a resource that genuinely no longer exists must still vanish."""
    _tf_state(tmp_path, ENV, _tf_resource("aws_s3_bucket", "bucket", "uploads"))
    assert stranded_in_tf_state(tmp_path, ENV, World(env=ENV), reachable_kinds=set())
    _tf_state(tmp_path, ENV)  # what `tofu destroy` leaves behind
    assert stranded_in_tf_state(tmp_path, ENV, World(env=ENV), reachable_kinds=set()) == ()


def test_the_odin_node_tag_wins_over_the_aws_name(tmp_path):
    _tf_state(tmp_path, ENV, _tf_resource("aws_s3_bucket", "bucket", "uploads-abc123", label="uploads"))
    assert [r.id for r in stranded_in_tf_state(tmp_path, ENV, World(env=ENV), set())] == ["uploads"]


def test_tf_owned_kinds_are_never_duplicated_by_this_overlay(tmp_path):
    """`project()` already owns every TF_OWNED kind and the reconciler prunes
    them properly. This overlay is ONLY for the shared-backing kinds."""
    _tf_state(
        tmp_path, ENV,
        _tf_resource("aws_instance", "id", "i-1", label="web"),
        _tf_resource("aws_db_instance", "identifier", "app-db"),
        _tf_resource("aws_ecs_service", "name", "svc"),
    )
    assert stranded_in_tf_state(tmp_path, ENV, World(env=ENV), reachable_kinds=set()) == ()


def test_a_missing_or_half_written_state_is_no_evidence(tmp_path):
    """tofu rewrites state IN PLACE, so a reader can land mid-write -- and
    `/world` is polled throughout an apply. Neither case may raise."""
    assert stranded_in_tf_state(tmp_path, ENV, World(env=ENV), set()) == ()   # never applied
    directory = tf_dir(tmp_path, ENV)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "terraform.tfstate").write_text("")
    assert stranded_in_tf_state(tmp_path, ENV, World(env=ENV), set()) == ()   # pre-created, empty
    (directory / "terraform.tfstate").write_text('{"version": 4, "resou')
    assert stranded_in_tf_state(tmp_path, ENV, World(env=ENV), set()) == ()   # caught mid-write
