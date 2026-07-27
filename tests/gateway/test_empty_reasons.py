"""A failure reason recorded AT THE WRITER may never be blank.

`ecsctl`, `lambdactl` and `ec2compute` each catch a deliberately broad
`except Exception` on a daemon thread (a silent hang is forbidden) and write
the exception into the record as the resource's whole explanation. They all
did it with `str(exc)`, which is `''` for any exception constructed with no
arguments -- and unlike the same bug in a reader, this one enters the record
at the SOURCE: every downstream consumer inherits the blank and the real
reason is gone by then.

THE PREMISE, MEASURED (not assumed) before these tests were written:
  * a real interpreter-raised `MemoryError` has `args == ()`, so `str(exc)`
    is `''` -- reachable on all three paths, which allocate and shell out on
    a machine odin's own docs call short on headroom;
  * `httpcore` raises `PoolTimeout()` with no arguments
    (`_synchronization.py`), and httpx's own `map_httpcore_exceptions`
    preserves the empty message -- reachable on `lambdactl._invoke`, whose
    substrate is a real `httpx.post`;
  * every exception odin's OWN code raises on these paths does carry text
    (probed against the real components: a real `docker run` of a missing
    image, a real `docker inspect` of an absent container, a real `limactl`
    failure) -- so the writers were not wrong about the common case, only
    about the case where they are the last line of defence.

WHAT A BLANK COST, per model, measured against the real renderers:
  * ec2compute -- a `state_reason` dict is TRUTHY even with an empty message,
    so DescribeInstances emitted `<stateReason><code>Server.InternalError
    </code><message></message></stateReason>`: an assertion that the instance
    failed, with nothing about why.
  * lambdactl -- `_configuration_json` renders `state_reason or None` and
    `_json` drops every None, so `StateReason` VANISHED from the wire and
    GetFunction answered `State: Failed` with no reason at all.
  * ecsctl -- `task_verdict`/`_rollout` already guard, so a blank degraded to
    the generic "task stopped" / "a task failed to start": coherent, but the
    one fact odin actually had was lost.

Each test below is written so that reverting its writer to `str(exc)` fails
it -- see the module's report for the mutation runs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpcore
import pytest
from httpx._transports.default import map_httpcore_exceptions

from odin.compute.functions import InvokeResult
from odin.gateway.classify import classify
from odin.gateway.models import cachectl, ec2compute, ecsctl, lambdactl, rdsctl
from odin.gateway.stores import SynthStores
from odin.gateway.errors import exc_text
from odin.reconcile.reconciler import _exc_text as canonical_exc_text
from odin.simulate.workspace import tf_dir

from .conftest import split_url
from .test_cachectl import CLUSTER as CACHE_CLUSTER
from .test_cachectl import FakeRedisCache
from .test_ec2compute import FakeInstanceVm
from .test_ec2compute import _answer as _ec2_answer
from .test_ec2compute import _run_instance, _subnet, _wait_for_state
from .test_ecsctl import FakeTaskRuntime, _create_cluster, _create_service, _register_taskdef
from .test_lambdactl import FakeFunctionRuntime, _create
from .test_lambdactl import _answer as _lambda_answer
from .test_lambdactl import _parse as _lambda_parse
from .test_lambdactl import _wait_for_state as _wait_for_fn_state
from .test_rdsctl import DB as _RDS_ID
from .test_rdsctl import FakePostgresRds
from .test_rdsctl import _create as _rds_create
from .test_rdsctl import fast_probe  # noqa: F401  -- shrinks the create timeout

ENV = "default"


def _settle_rds(stores: SynthStores, identifier: str = _RDS_ID, timeout: float = 5.0) -> dict:
    """Wait for `_finish_create`'s daemon thread to leave `creating`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = rdsctl._record(stores, ENV, identifier)
        if record is not None and record["status"] != rdsctl.CREATING:
            return record
        time.sleep(0.01)
    raise AssertionError("the rds create thread never settled")


def _cache_create(stores: SynthStores, sink, elasticache, substrate) -> None:
    """A real `available` cluster through the real CreateCacheCluster handler,
    with `substrate` injected the way `pure_answer` already allows -- so this
    module needs no fixture of its own (and cannot shadow test_cachectl's)."""
    req = sink.call(lambda: elasticache.create_cache_cluster(
        CacheClusterId=CACHE_CLUSTER, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=1,
    ))
    path, query = split_url(req.url)
    action, resource = classify("elasticache", req.method, path, query, req.headers, req.body)
    cachectl.pure_answer(action, resource, ENV, req.body, stores, 0.0, cache=substrate)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = stores.cachectl.get(ENV, f"cluster:{CACHE_CLUSTER}")
        if record is not None and record["status"] != cachectl.STATUS_CREATING:
            return
        time.sleep(0.01)
    raise AssertionError("the cache create thread never settled")


# --- ONE treatment, not a fourth spelling --------------------------------


# Exceptions with no message at all, next to ones that have one. The first
# three are the reachable cases the module docstring measures; `Exception()`
# is the floor.
_NO_MESSAGE = [MemoryError(), TimeoutError(), RuntimeError(), Exception()]
_WITH_MESSAGE = [RuntimeError("boom"), ValueError("bad input"), KeyError("image")]


@pytest.mark.parametrize("exc", _NO_MESSAGE + _WITH_MESSAGE, ids=lambda e: type(e).__name__ + repr(str(e)))
def test_every_model_words_an_exception_exactly_as_the_reconciler_does(exc):
    """There is now exactly ONE implementation, and this is what holds it that
    way. Five modules needed this wording; three kept private copies because
    `reconcile.reconciler` -> `reconcile.drift` -> `gateway.models.*` is a real
    import chain and importing back would close a cycle. `gateway/errors.py`
    imports nothing from odin, so it is a leaf every one of them can reach --
    which is why the copies are gone rather than merely kept in step.

    Asserting identity, not equality: a re-spelled copy that happens to agree
    today would pass an equality check and drift tomorrow."""
    assert ecsctl.exc_text is exc_text
    assert lambdactl.exc_text is exc_text
    assert ec2compute.exc_text is exc_text
    # …and the two that were still spelling it `str(exc)`.
    assert rdsctl.exc_text is exc_text
    assert cachectl.exc_text is exc_text
    assert canonical_exc_text is exc_text
    assert exc_text(exc) == canonical_exc_text(exc)


@pytest.mark.parametrize("exc", _NO_MESSAGE, ids=lambda e: type(e).__name__)
def test_an_exception_with_no_message_still_names_its_class(exc):
    """The property every writer below depends on. `str(exc)` really is empty
    for each of these -- asserted here so the tests that follow are not
    resting on an assumption about their inputs."""
    assert str(exc) == "", "premise: these are the no-message cases"
    text = ecsctl.exc_text(exc)
    assert type(exc).__name__ in text
    assert text.strip(), "a reason may never be blank"
    assert not text.rstrip().endswith(":"), "and never a dangling colon"


# --- ecsctl: the task's stopped_reason -----------------------------------


class SilentlyFailingTaskRuntime(FakeTaskRuntime):
    """`run` raises with NO message -- the shape a real `MemoryError` (or any
    no-arg construction) takes when it reaches `_launch_task`'s broad catch."""

    def run(self, env, task_id, container_def, extra_env=None, cpu=None, memory=None):
        self.ran.append((env, task_id, container_def, extra_env, cpu, memory))
        raise RuntimeError()


def _stopped_task(stores: SynthStores, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stopped = [t for t in ecsctl._all_tasks(stores, ENV) if t["last_status"] == "STOPPED"]
        if stopped:
            return stopped[0]
        time.sleep(0.02)
    raise AssertionError("no task ever reached STOPPED")


def test_a_task_that_dies_with_no_message_records_the_class_not_a_blank(sink, ecs, stores):
    runtime = SilentlyFailingTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)

    task = _stopped_task(stores)
    assert task["stopped_reason"], "a STOPPED task with a blank reason is the bug"
    assert "RuntimeError" in task["stopped_reason"]

    # ...and the two readers that render it for a human now quote the real
    # thing instead of falling through to their generic fallbacks.
    assert ecsctl.task_verdict(task) != "task stopped"
    assert "RuntimeError" in ecsctl.task_verdict(task)
    service = ecsctl._service(stores, ENV, "odin", "app")
    state, reason = ecsctl._rollout(service, ecsctl._tasks_for_service(stores, ENV, "odin", "app"), "d")
    assert state == "FAILED"
    assert "RuntimeError" in reason, "the deployment must quote the task's real reason"


async def test_the_apply_shortfall_names_the_class_when_that_is_all_odin_has(sink, ecs, stores):
    """`wait_for_steady_services` is what /apply-full turns into the apply's
    own failure line -- a blank there is a failed apply that says nothing."""
    runtime = SilentlyFailingTaskRuntime()
    _create_cluster(stores, sink, ecs, runtime)
    _register_taskdef(stores, sink, ecs, runtime)
    _create_service(stores, sink, ecs, runtime, desiredCount=1)
    _stopped_task(stores)

    (short,) = await ecsctl.wait_for_steady_services(stores, ENV, runtime)
    assert short.reason, "the apply's reason may not be blank"
    assert "RuntimeError" in short.reason


def test_mark_task_stopped_refuses_a_reason_that_says_nothing(stores):
    """The public seam `reconcile/drift.py` calls. A reason is the ONLY thing
    it adds to the record, so an empty one records a failure that explains
    itself with nothing."""
    stores.ecsctl.set(ENV, "task:c:t1", {
        "cluster_name": "c", "task_id": "t1", "service_name": "s", "container_name": "app",
        "last_status": "RUNNING", "host_ports": {},
    })
    ecsctl.mark_task_stopped(stores, ENV, "c", "t1", "   ")
    task = stores.ecsctl.get(ENV, "task:c:t1")
    assert task["last_status"] == "STOPPED"
    assert task["stopped_reason"].strip(), task
    assert ecsctl.task_verdict(task).strip()


# --- lambdactl: State/LastUpdateStatus reasons + the Invoke error ---------


class SilentlyFailingFunctionRuntime(FakeFunctionRuntime):
    """`ensure` raises with NO message."""

    def ensure(self, env, name, runtime, handler, env_vars, code_dir, memory_mib=None):
        self.ensured.append((env, name, runtime, handler, dict(env_vars), code_dir, memory_mib))
        raise RuntimeError()


def test_a_deploy_that_fails_with_no_message_keeps_statereason_on_the_wire(sink, lambda_, stores):
    """The strongest of the three: `_configuration_json` renders
    `state_reason or None` and `_json` drops every None, so a blank reason did
    not merely READ badly -- `StateReason` disappeared from the response
    entirely and GetFunction answered `State: Failed` with no reason at all."""
    substrate = SilentlyFailingFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    failed = _wait_for_fn_state(stores, sink, lambda_, "fn1", "Failed", substrate)

    assert "StateReason" in failed, "State: Failed with no StateReason is the bug"
    assert "RuntimeError" in failed["StateReason"]
    assert failed["StateReasonCode"] == "InternalError"
    # LastUpdateStatusReason is the same string and must survive the same way.
    assert "RuntimeError" in failed["LastUpdateStatusReason"]
    assert failed["LastUpdateStatus"] == "Failed"


class PoolTimeoutFunctionRuntime(FakeFunctionRuntime):
    """`invoke` fails the way the REAL substrate can: httpcore raises
    `PoolTimeout()` with no arguments and httpx's own mapping re-raises it as
    `httpx.PoolTimeout` with the empty message intact. Raised through httpx's
    real `map_httpcore_exceptions` rather than hand-built, so this is the
    upstream signal itself, not a stand-in for it."""

    def invoke(self, env, name, payload, timeout: float = 30.0) -> InvokeResult:
        self.invoked.append((env, name, payload))
        with map_httpcore_exceptions():
            raise httpcore.PoolTimeout()


async def test_an_invoke_that_fails_with_no_message_still_names_what_went_wrong(sink, lambda_, stores):
    """`_invoke`'s reason has the narrowest escape hatch of all: it is the
    whole `Message` of the AWS error the SDK raises at the caller, recorded
    nowhere else."""
    substrate = PoolTimeoutFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    _wait_for_fn_state(stores, sink, lambda_, "fn1", "Active", substrate)

    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    response = _lambda_answer(stores, req, substrate)
    parsed = _lambda_parse("Invoke", response, error=True)

    assert parsed["Error"]["Code"] == "ServiceException"
    message = parsed["Error"]["Message"]
    assert message.strip(), "an AWS error whose Message is blank tells the caller nothing"
    assert "PoolTimeout" in message


def test_mark_function_failed_refuses_a_reason_that_says_nothing(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    _wait_for_fn_state(stores, sink, lambda_, "fn1", "Active", substrate)

    lambdactl.mark_function_failed(stores, ENV, "fn1", "")
    req = sink.call(lambda: lambda_.get_function_configuration(FunctionName="fn1"))
    config = _lambda_parse("GetFunctionConfiguration", _lambda_answer(stores, req, substrate))

    assert config["State"] == "Failed"
    assert "StateReason" in config, "a Failed function must always carry a reason"
    assert config["StateReason"].strip()


# --- ec2compute: stateReason, the delete retry, and destroy's verdict ------


def _state_reason(sink, ec2, stores, vm, instance_id: str, want: str) -> dict:
    return _wait_for_state(stores, sink, ec2, instance_id, want, vm)["StateReason"]


def test_a_boot_that_fails_with_no_message_never_emits_an_empty_message_tag(sink, ec2, stores):
    """A `state_reason` DICT is truthy even when its message is empty, so
    `_instance_xml` emitted `<message></message>` -- an instance asserting
    Server.InternalError and saying nothing about why."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()

    def silent_boot(name, vm_config, **kwargs):
        raise RuntimeError()

    vm.boot = silent_boot
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]

    reason = _state_reason(sink, ec2, stores, vm, instance_id, "terminated")
    assert reason["Code"] == "Server.InternalError"
    assert reason["Message"].strip(), "<message></message> is the bug"
    assert "RuntimeError" in reason["Message"]


def test_a_start_that_fails_with_no_message_records_the_class(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    req = sink.call(lambda: ec2.stop_instances(InstanceIds=[instance_id]))
    _ec2_answer(stores, req, vm)
    _wait_for_state(stores, sink, ec2, instance_id, "stopped", vm)

    def silent_start(name, timeout=300.0):
        raise RuntimeError()

    vm.start = silent_start
    req = sink.call(lambda: ec2.start_instances(InstanceIds=[instance_id]))
    _ec2_answer(stores, req, vm)

    reason = _state_reason(sink, ec2, stores, vm, instance_id, "stopped")
    assert reason["Message"].strip()
    assert "RuntimeError" in reason["Message"]


def test_a_successful_start_clears_the_previous_failures_reason(sink, ec2, stores):
    """A caveat outliving its fix, in the record: `_finish_terminate` cleared
    `state_reason` on success and the boot/start paths did not, so an instance
    that failed once kept answering DescribeInstances with that stale failure
    forever after it recovered."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    req = sink.call(lambda: ec2.stop_instances(InstanceIds=[instance_id]))
    _ec2_answer(stores, req, vm)
    _wait_for_state(stores, sink, ec2, instance_id, "stopped", vm)

    working_start = vm.start

    def failing_start(name, timeout=300.0):
        raise RuntimeError("start failed")

    vm.start = failing_start
    req = sink.call(lambda: ec2.start_instances(InstanceIds=[instance_id]))
    _ec2_answer(stores, req, vm)
    assert _state_reason(sink, ec2, stores, vm, instance_id, "stopped")["Message"]

    vm.start = working_start  # the retry works
    req = sink.call(lambda: ec2.start_instances(InstanceIds=[instance_id]))
    _ec2_answer(stores, req, vm)
    running = _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    assert "StateReason" not in running, f"a recovered instance still blames its old failure: {running}"


def test_a_stop_that_fails_is_no_longer_silent(sink, ec2, stores):
    """`_finish_stop` recorded NOTHING for a failed stop while its sibling
    `_finish_start` recorded a reason for the identical failure -- so a stop
    that genuinely failed was indistinguishable from one that worked in every
    place a user can look."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    def silent_stop(name):
        raise RuntimeError()

    vm.stop = silent_stop
    req = sink.call(lambda: ec2.stop_instances(InstanceIds=[instance_id]))
    _ec2_answer(stores, req, vm)

    reason = _state_reason(sink, ec2, stores, vm, instance_id, "stopped")
    assert reason["Message"].strip(), "a failed stop that records nothing is the bug"
    assert "RuntimeError" in reason["Message"]


def test_a_failed_delete_retry_message_has_no_dangling_colon(sink, ec2, stores):
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    def silent_delete(name):
        raise RuntimeError()

    vm.delete = silent_delete
    ec2compute._finish_terminate(stores, ENV, instance_id, "odin-ec2-x", vm)

    record = ec2compute._instance(stores, ENV, instance_id)
    message = record["state_reason"]["message"]
    assert not message.rstrip().endswith(":"), f"a dangling colon where the cause belongs: {message!r}"
    assert "RuntimeError" in message


def test_destroys_verdict_never_reports_a_vm_with_empty_parentheses(sink, ec2, stores):
    """`ReclaimFailed` is the sentence that tells a user their destroy did not
    destroy. `f"{name} ({exc})"` rendered `odin-ec2-... ()` for an exception
    with no message."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    def silent_delete(name):
        raise RuntimeError()

    vm.delete = silent_delete
    with pytest.raises(ec2compute.ReclaimFailed) as raised:
        ec2compute.reclaim_env_instances(stores, ENV, vm)

    text = str(raised.value)
    assert "()" not in text, f"an empty reason inside destroy's own verdict: {text}"
    assert "RuntimeError" in text
    assert instance_id in text


def test_mark_instance_terminated_refuses_a_reason_that_says_nothing(sink, ec2, stores):
    """A drifted record's whole purpose is to say WHY the node vanished --
    with a blank reason it projects `crashed` with nothing attached."""
    subnet_id = _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    result = _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id)
    instance_id = result["Instances"][0]["InstanceId"]
    _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    ec2compute.mark_instance_terminated(stores, ENV, instance_id, "")
    record = ec2compute._instance(stores, ENV, instance_id)
    assert record["drifted"] is True
    assert record["state_reason"]["message"].strip()


# --- the documented tfstate guard that did not exist ----------------------


def _write_state(root: Path, env: str, text: str) -> None:
    state = tf_dir(root, env) / "terraform.tfstate"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(text)


def _claim_instance(stores: SynthStores, env: str, instance_id: str) -> None:
    stores.ec2compute.set(env, f"instance:{instance_id}", {
        "instance_id": instance_id, "state_name": "running", "vpc_id": None,
    })


def test_a_corrupt_tfstate_is_no_evidence_rather_than_an_exception(tmp_path):
    """`tf_forgotten_instances`' own docstring says only a state "which
    parses" counts -- but `json.loads` was unguarded, so a half-written state
    (a `kill -9` mid-apply: exactly what this function exists for) raised
    `JSONDecodeError` straight out of it."""
    stores = SynthStores(tmp_path)
    _claim_instance(stores, "e1", "i-aaa")
    _write_state(tmp_path, "e1", '{"resources": [')  # truncated mid-write

    assert ec2compute.tf_forgotten_instances(stores, "e1") == []


@pytest.mark.parametrize("text", ["[]", "null", '"a string"', "42"])
def test_a_tfstate_that_is_not_a_state_object_never_accuses_every_vm(tmp_path, text):
    """Valid JSON is not necessarily a tofu state. Read as one, `[]` says "no
    resources are known", which is indistinguishable from "every VM in this
    env is forgotten" -- and this function's caller DELETES what it names."""
    stores = SynthStores(tmp_path)
    _claim_instance(stores, "e1", "i-aaa")
    _write_state(tmp_path, "e1", text)

    assert ec2compute.tf_forgotten_instances(stores, "e1") == []


def test_one_corrupt_tfstate_no_longer_silences_every_later_env(tmp_path):
    """The blast radius, and why the missing guard mattered beyond one env:
    `reclaim_tf_forgotten_vms` promises "Never raises" and
    `server.py::_reap_orphaned_ec2_vms` catches whatever escapes, so ONE
    unparseable state file skipped the remaining envs' reclaim AND the
    lighthouse reaper queued behind it -- three startup safety nets quietly
    off, with only a log line."""
    stores = SynthStores(tmp_path)
    _claim_instance(stores, "broken", "i-aaa")
    _claim_instance(stores, "healthy", "i-bbb")
    _write_state(tmp_path, "broken", "{not json at all")
    _write_state(tmp_path, "healthy", json.dumps({"resources": []}))

    vm = FakeInstanceVm()
    reclaimed = ec2compute.reclaim_tf_forgotten_vms(stores, ["broken", "healthy"], vm)

    assert reclaimed == ["odin-ec2-healthy-i-bbb"], reclaimed
    assert "odin-ec2-broken-i-aaa" not in vm.deleted, "a corrupt state may not condemn a VM"
    assert ec2compute._instance(stores, "broken", "i-aaa") is not None


def test_a_state_tofu_really_wrote_still_names_what_it_forgot(tmp_path):
    """The guard above must not have turned the whole function into a no-op:
    a state that DOES parse and really does not name an instance still yields
    it (this is the field-test-3 recovery path)."""
    stores = SynthStores(tmp_path)
    _claim_instance(stores, "e1", "i-kept")
    _claim_instance(stores, "e1", "i-forgotten")
    _write_state(tmp_path, "e1", json.dumps({"resources": [
        {"type": "aws_instance", "instances": [{"attributes": {"id": "i-kept"}}]},
    ]}))

    assert ec2compute.tf_forgotten_instances(stores, "e1") == ["i-forgotten"]


# --- rdsctl + cachectl: the two writers that never got the treatment ------
#
# The same defect, found in the same shape, two releases later. `rdsctl` and
# `cachectl` each catch a deliberately broad `except Exception` on a daemon
# thread and write the exception into the record as the resource's whole
# explanation -- and they did it with `str(exc)`, so a no-message exception
# persisted `container did not start: ` / `container removal failed: `. The
# `cachectl` one is the sharpest illustration that a per-instance fix does not
# generalise: that module's `_finish_create` ALREADY used `exc_text` while its
# `_finish_delete`, twelve lines below, did not.


def _rds_record(stores, sink, rds_client, substrate, identifier=_RDS_ID):
    """A real `creating` record, minted by the real CreateDBInstance handler."""
    _rds_create(sink, rds_client, stores, substrate, identifier=identifier)
    return rdsctl._record(stores, ENV, identifier)


class _SilentCreate(FakePostgresRds):
    """`create_db` raises with NO message -- what a real interpreter-raised
    `MemoryError` looks like when it reaches `_finish_create`'s broad catch on a
    machine odin's own docs call short on headroom."""

    def create_db(self, db_id, user, password, db_name="postgres"):
        raise RuntimeError()


class _SilentDelete(FakePostgresRds):
    def delete_db(self, db_id):
        raise RuntimeError()


class _SilentPassword(FakePostgresRds):
    def set_password(self, db_id, user, current, new):
        raise RuntimeError()


def test_an_rds_boot_that_fails_with_no_message_records_the_class(stores, sink, rds):
    """PERSISTED, and DescribeDBInstances hands it straight back: a `failed`
    status whose `StatusReason` is a dangling colon is a resource that says it
    broke and nothing about how."""
    substrate = _SilentCreate()
    _rds_create(sink, rds, stores, substrate)
    record = _settle_rds(stores)

    assert record["status"] == rdsctl.FAILED
    assert record["status_reason"] == "container did not start: RuntimeError (raised with no message, so the class is the whole of it)"
    assert not record["status_reason"].rstrip().endswith(":")


def test_an_rds_delete_that_fails_with_no_message_records_the_class(stores, sink, rds):
    """This branch exists to be honest with a polling caller -- the record STAYS
    `deleting` with the failure recorded -- so a blank reason defeats its whole
    purpose."""
    substrate = _SilentDelete()
    _rds_create(sink, rds, stores, FakePostgresRds())
    _settle_rds(stores)
    rdsctl._finish_delete(stores, ENV, _RDS_ID, substrate)
    record = rdsctl._record(stores, ENV, _RDS_ID)

    assert record is not None, "a failed delete must not remove the record"
    assert "RuntimeError" in record["status_reason"]
    assert not record["status_reason"].rstrip().endswith(":")


def test_an_rds_password_change_that_fails_with_no_message_still_names_something(stores, sink, rds):
    """This one goes on the WIRE as the apply's own failure line, so a blank
    left tofu reporting an `InvalidDBInstanceState` with no state named."""
    _rds_create(sink, rds, stores, FakePostgresRds())
    _settle_rds(stores)
    response = rdsctl.pure_answer(
        "rds:ModifyDBInstance", _RDS_ID, ENV,
        f"Action=ModifyDBInstance&DBInstanceIdentifier={_RDS_ID}&MasterUserPassword=brandnew123".encode(),
        stores, time.monotonic(), rds=_SilentPassword(),
    )
    body = response.body.decode()

    assert response.status_code == 400
    assert "RuntimeError" in body
    assert f"could not change the master password on {_RDS_ID}: <" not in body


class _SilentCacheDelete:
    """The `RedisCache` shape whose `delete` raises with no message."""

    def ensure(self, env, cluster_id, memory_mib=None):
        return 51234

    def delete(self, env, cluster_id):
        raise RuntimeError()

    async def host_port(self, env, cluster_id):
        return 51234

    async def status(self, env, cluster_id):
        return "running"


def test_a_cache_removal_that_fails_with_no_message_records_the_class(stores, sink, elasticache):
    """The sibling `_finish_create` in this SAME module already had `exc_text`
    and `_finish_delete` did not -- which is why the honesty rule says fix the
    SHAPE, then immediately hunt siblings."""
    _cache_create(stores, sink, elasticache, FakeRedisCache())
    cachectl._finish_delete(stores, ENV, CACHE_CLUSTER, "arn:aws:elasticache:x", _SilentCacheDelete())
    record = stores.cachectl.get(ENV, f"cluster:{CACHE_CLUSTER}")

    assert record is not None, "a failed removal must not remove the record"
    assert record["status_reason"] == "container removal failed: RuntimeError (raised with no message, so the class is the whole of it)"
    assert not record["status_reason"].rstrip().endswith(":")


def test_both_writers_still_quote_a_real_message_when_there_is_one(stores, sink, rds):
    """The common case must not regress: `exc_text` prefixes the class, it does
    not replace the message."""
    class _Loud(FakePostgresRds):
        def create_db(self, db_id, user, password, db_name="postgres"):
            raise RuntimeError("docker: no space left on device")

    _rds_create(sink, rds, stores, _Loud())
    record = _settle_rds(stores)
    assert record["status_reason"] == (
        "container did not start: RuntimeError: docker: no space left on device"
    )


# --- the fixtures the imported helpers expect ----------------------------


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)
