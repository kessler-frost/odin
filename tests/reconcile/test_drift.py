"""W2.2 -- reconcile/drift.py: the reality sweep for TF-owned compute.

Hand-built `SynthStores` + fake listing seams, no real Docker/Lima involved.
See tests/reconcile/test_reconciler.py for the Reconciler-level wiring (the
`crashed` WorldDelta + the `type:"log"` error line a drift transition emits)
and tests/simulate/test_ecs_drift_e2e.py for the one real-container proof.
"""
from __future__ import annotations

from odin.aws.rds import container_name as db_container_name
from odin.compute.instances import vm_name
from odin.compute.tasks import container_name as task_container_name
from odin.gateway.models import rdsctl
from odin.gateway.stores import SynthStores
from odin.reconcile.assertions import PgReady
from odin.gateway.models.ecsctl import container_gone_reason
from odin.reconcile.drift import _NO_PROBE_ERROR, DriftSweeper, sweep_compute
from odin.reconcile.tf_status import project

ENV = "default"


class FakeVms:
    """`InstanceVm.list_names(check=True)`'s shape -- one call per sweep, and
    the test counts them."""

    def __init__(self, names: list[str] | None = None, error: Exception | None = None) -> None:
        self.names = names if names is not None else []
        self.error = error
        self.calls = 0

    async def list_names(self, check: bool = False) -> list[str]:
        self.calls += 1
        assert check is True, "the drift sweep must NOT swallow a limactl failure"
        if self.error is not None:
            raise self.error
        return list(self.names)


class FakeContainers:
    """The two runtime seams the sweeps use -- `container_names()` (one bulk
    `ps -a`) and the per-name `status()`/`exit_code()` (`inspect`) -- both of
    which answer identically on docker and on nerdctl-in-Lima, which is why
    `reconcile/drift.py::_live_states` is built from these two and not from a
    single `ps --format '{{.State}}'` (that one does not exist on nerdctl).

    One source of names: `names` are the containers that are RUNNING, `exited`
    maps a name to its real exit code, `paused` are present-but-frozen, and
    anything in none of them is GONE. `calls` counts listings and `status_calls`
    counts inspects, so the boundedness test can pin both."""

    def __init__(
        self, names: list[str] | None = None, error: Exception | None = None,
        exited: dict[str, int] | None = None, paused: list[str] | None = None,
    ) -> None:
        self.names = names if names is not None else []
        self.error = error
        self.exited = exited or {}
        self.paused = paused or []
        self.calls = 0
        self.status_calls: list[str] = []

    async def container_names(self) -> list[str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [*self.names, *self.exited, *self.paused]

    async def status(self, name: str) -> str:
        self.status_calls.append(name)
        if name in self.names:
            return "running"
        if name in self.paused:
            return "paused"
        return "exited" if name in self.exited else "absent"

    async def exit_code(self, name: str) -> int:
        return self.exited.get(name, -1)


class FakeProbe:
    """`pg_ready_sync`'s shape. `ok` is what every probe answers; the test
    flips it to simulate a database that stopped responding."""

    def __init__(self, ok: bool = True, error: str = "connection refused") -> None:
        self.ok = ok
        self.error = error
        self.calls: list[tuple] = []

    async def __call__(self, host, port, user, password, db="postgres") -> PgReady:
        self.calls.append((host, port, user, password))
        return PgReady(ok=self.ok, error=None if self.ok else self.error)


def _sweeper(vms=None, containers=None, probe=None) -> DriftSweeper:
    return DriftSweeper(
        containers=containers or FakeContainers(), vms=vms or FakeVms(),
        probe=probe or FakeProbe(),
    )


def _ec2(stores: SynthStores, label: str, instance_id: str, state_name: str = "running") -> str:
    stores.ec2compute.set(ENV, f"instance:{instance_id}", {
        "instance_id": instance_id, "state_name": state_name, "state_reason": None,
    })
    stores.tags.set(ENV, f"ec2:{instance_id}", {"odin:node": label})
    return vm_name(ENV, instance_id)


def _fn(stores: SynthStores, name: str, state: str = "Active", last_update: str = "Successful") -> None:
    stores.lambdactl.set(ENV, f"fn:{name}", {
        "function_name": name,
        "function_arn": f"arn:aws:lambda:us-east-1:000000000000:function:{name}",
        "state": state, "state_reason": None, "last_update_status": last_update,
    })


def _ecs_task(stores: SynthStores, task_id: str, last_status: str = "RUNNING") -> str:
    stores.ecsctl.set(ENV, "service:odin:app", {
        "cluster_name": "odin", "service_name": "app", "desired_count": 1, "status": "ACTIVE",
    })
    stores.ecsctl.set(ENV, f"task:odin:{task_id}", {
        "cluster_name": "odin", "service_name": "app", "task_id": task_id,
        "container_name": "app", "last_status": last_status,
        "stopped_reason": None, "exit_code": None, "stopped_at": None,
    })
    return task_container_name(ENV, task_id, "app")


# --- ec2: the flagship case (a Lima VM deleted out of band) ----------------


async def test_deleted_vm_yields_a_crashed_verdict_naming_the_drift(tmp_path):
    stores = SynthStores(tmp_path)
    name = _ec2(stores, "web", "i-1")
    sweeper = _sweeper(vms=FakeVms(names=["veronica"]))  # the VM is simply gone

    verdicts = await sweeper.verdicts(stores, ENV)

    assert verdicts["web"] == f"VM {name} deleted outside odin — re-Apply to recreate"


async def test_deleted_vm_marks_the_record_terminated_so_re_apply_really_recreates_it(tmp_path):
    """The honesty fix W2.2 shipped without: telling the user "re-Apply to
    recreate" is only true if TOFU is told too. A record left claiming
    `running` answers DescribeInstances with a VM that doesn't exist -> empty
    plan -> the VM never comes back. `terminated` (+ a real StateReason) is
    what makes the provider drop it from state and plan a create."""
    stores = SynthStores(tmp_path)
    name = _ec2(stores, "web", "i-1")

    await _sweeper(vms=FakeVms(names=[])).verdicts(stores, ENV)

    record = stores.ec2compute.get(ENV, "instance:i-1")
    assert record["state_name"] == "terminated"
    assert record["state_reason"] == {
        "code": "Client.UserInitiatedShutdown",
        "message": f"VM {name} deleted outside odin — re-Apply to recreate",
    }
    assert record["drifted"] is True
    assert record["terminated_at"] is not None, "the normal lazy sweep must still reclaim it"


async def test_the_world_stops_claiming_healthy_for_a_deleted_vm(tmp_path):
    """The whole point, at the projection level: before the sweep the node
    reads `healthy` off a record nothing cross-checked; after it, `crashed`
    with the real reason -- and never `draft`, which would be odin quietly
    forgetting a node the user still has on the canvas."""
    stores = SynthStores(tmp_path)
    name = _ec2(stores, "web", "i-1")
    assert (await project(stores, ENV))["web"] == ("ec2", "healthy", {}, None)

    await _sweeper(vms=FakeVms(names=[])).verdicts(stores, ENV)

    kind, phase, _, verdict = (await project(stores, ENV))["web"]
    assert (kind, phase) == ("ec2", "crashed")
    assert f"VM {name} deleted outside odin" in verdict


async def test_live_vm_reports_no_drift(tmp_path):
    stores = SynthStores(tmp_path)
    name = _ec2(stores, "web", "i-1")
    assert await _sweeper(vms=FakeVms(names=[name])).verdicts(stores, ENV) == {}
    assert stores.ec2compute.get(ENV, "instance:i-1")["state_name"] == "running"


async def test_mid_boot_and_mid_delete_ec2_records_are_exempt(tmp_path):
    # v0.5.4's real boot threads: a VM limactl hasn't registered YET is
    # starting, not gone. `stopped`/`terminated` already carry their own
    # honest phase from the projection itself.
    for state in ("pending", "shutting-down", "stopping", "stopped", "terminated"):
        stores = SynthStores(tmp_path / state)
        _ec2(stores, "web", "i-1", state_name=state)
        vms = FakeVms(names=[])
        assert await _sweeper(vms=vms).verdicts(stores, ENV) == {}, state
        assert vms.calls == 0, f"{state}: no candidate, so no limactl call at all"
        # ...and the record itself is left completely alone: the store write is
        # only ever for a VM the sweep CONFIRMED gone, never for one that's
        # merely not registered yet.
        assert stores.ec2compute.get(ENV, "instance:i-1") == {
            "instance_id": "i-1", "state_name": state, "state_reason": None,
        }, state


async def test_untagged_ec2_record_is_not_reported(tmp_path):
    # It never enters World either (no odin:node tag == no label), so there'd
    # be nothing for a verdict to attach to.
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-1", {
        "instance_id": "i-1", "state_name": "running", "state_reason": None,
    })
    assert await _sweeper(vms=FakeVms(names=[])).verdicts(stores, ENV) == {}


# --- lambda: the RIE container, with the redeploy window exempt -----------


async def test_missing_lambda_container_yields_a_verdict(tmp_path):
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    verdicts = await _sweeper(containers=FakeContainers(names=[])).verdicts(stores, ENV)
    assert verdicts["hello"] == (
        "container odin-lambda-default-hello removed outside odin — re-Apply to recreate"
    )


async def test_missing_lambda_container_marks_the_function_failed(tmp_path):
    """The record is CORRECTED, not deleted: a function whose RIE container is
    gone genuinely cannot run (Invoke refuses off this state), the World reads
    `crashed` + the reason, and an Apply's `lambdactl.converge_functions` is
    what re-creates the container. Deleting the record would be a bigger lie --
    real AWS never deletes a function because its sandbox died."""
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")

    await _sweeper(containers=FakeContainers(names=[])).verdicts(stores, ENV)

    record = stores.lambdactl.get(ENV, "fn:hello")
    assert record["state"] == "Failed"
    assert record["state_reason"] == (
        "container odin-lambda-default-hello removed outside odin — re-Apply to recreate"
    )
    assert record["state_reason_code"] == "InternalError"
    assert record["last_update_status"] == "Successful", "the last DEPLOY did succeed"
    kind, phase, _, verdict = (await project(stores, ENV))["hello"]
    assert (kind, phase) == ("lambda", "crashed")
    assert "removed outside odin" in verdict


async def test_live_lambda_container_is_left_alone(tmp_path):
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    name = "odin-lambda-default-hello"
    assert await _sweeper(containers=FakeContainers(names=[name])).verdicts(stores, ENV) == {}
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Active"


async def test_lambda_mid_redeploy_is_exempt(tmp_path):
    # FunctionRuntime.ensure rm -f's the old container before running the new
    # one: absent-but-Active is legitimate while LastUpdateStatus is InProgress.
    stores = SynthStores(tmp_path)
    _fn(stores, "hello", last_update="InProgress")
    containers = FakeContainers(names=[])
    assert await _sweeper(containers=containers).verdicts(stores, ENV) == {}
    assert containers.calls == 0
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Active", "a mid-deploy record is untouched"


async def test_pending_lambda_is_exempt(tmp_path):
    stores = SynthStores(tmp_path)
    _fn(stores, "hello", state="Pending", last_update="InProgress")
    assert await _sweeper(containers=FakeContainers(names=[])).verdicts(stores, ENV) == {}
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Pending"


# --- ecs: reality is written back into the task record (see drift.py's
# module docstring: a task is not a TF resource, so this is the honest record
# AND what lets an Apply relaunch it). ------------------------------------


async def test_removed_task_container_marks_the_task_stopped_with_a_drift_reason(tmp_path):
    stores = SynthStores(tmp_path)
    container = _ecs_task(stores, "t1")

    verdicts = await _sweeper(containers=FakeContainers(names=[])).verdicts(stores, ENV)

    assert verdicts == {}  # ecs reports through its own record, not the overlay
    task = stores.ecsctl.get(ENV, "task:odin:t1")
    assert task["last_status"] == "STOPPED"
    # Pinned to the SHARED wording, not a hand-copied string: ecsctl's passive
    # sweep races this path for the identical event, and when the two wrote
    # different sentences whichever won decided what the user saw -- which made
    # this very assertion flake under load. One source of truth for both.
    assert task["stopped_reason"] == container_gone_reason(container)
    assert task["exit_code"] is None  # a container that's GONE never reported one


async def test_live_task_container_is_left_running(tmp_path):
    stores = SynthStores(tmp_path)
    container = _ecs_task(stores, "t1")
    await _sweeper(containers=FakeContainers(names=[container])).verdicts(stores, ENV)
    assert stores.ecsctl.get(ENV, "task:odin:t1")["last_status"] == "RUNNING"


async def test_exited_task_container_still_present_is_not_drift(tmp_path):
    # `docker ps -a` still lists an EXITED container -- that's ecsctl's own
    # sweep_tasks' job (with the real exit code), never this one's.
    stores = SynthStores(tmp_path)
    container = _ecs_task(stores, "t1")
    await _sweeper(containers=FakeContainers(names=[container])).verdicts(stores, ENV)
    assert stores.ecsctl.get(ENV, "task:odin:t1")["stopped_reason"] is None


async def test_provisioning_task_is_exempt(tmp_path):
    stores = SynthStores(tmp_path)
    _ecs_task(stores, "t1", last_status="PROVISIONING")
    containers = FakeContainers(names=[])
    await _sweeper(containers=containers).verdicts(stores, ENV)
    assert stores.ecsctl.get(ENV, "task:odin:t1")["last_status"] == "PROVISIONING"
    assert containers.calls == 0


# --- boundedness: a fixed number of BULK listings per sweep, on a cadence ---


async def test_one_listing_per_substrate_regardless_of_resource_count(tmp_path):
    stores = SynthStores(tmp_path)
    for n in range(5):
        _ec2(stores, f"web{n}", f"i-{n}")
        _fn(stores, f"fn{n}")
    for n in range(5):
        stores.ecsctl.set(ENV, f"task:odin:t{n}", {
            "cluster_name": "odin", "service_name": "app", "task_id": f"t{n}",
            "container_name": "app", "last_status": "RUNNING",
            "stopped_reason": None, "exit_code": None, "stopped_at": None,
        })
    vms, containers = FakeVms(names=[]), FakeContainers(names=[])

    verdicts = await _sweeper(vms=vms, containers=containers).verdicts(stores, ENV)

    assert len(verdicts) == 10  # 5 ec2 + 5 lambda, all drifted
    # 15 resources, THREE bulk calls: one `limactl list` (ec2) and two
    # `docker ps` (the live lambda/rds half, then the ecs task half). Never one
    # call per resource -- that is the property this pins, and it holds at any
    # count. Zero `inspect` calls here because every container is GONE, and the
    # listing alone settles that; an inspect is only spent on one that exists.
    assert (vms.calls, containers.calls) == (1, 2)
    assert containers.status_calls == []


async def test_non_sweep_ticks_make_no_runtime_calls_and_keep_the_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_DRIFT_SWEEP_TICKS", "3")
    stores = SynthStores(tmp_path)
    _ec2(stores, "web", "i-1")
    vms = FakeVms(names=[])
    sweeper = _sweeper(vms=vms)

    first = await sweeper.verdicts(stores, ENV)  # tick 1: sweeps
    assert vms.calls == 1
    assert await sweeper.verdicts(stores, ENV) == first  # tick 2: cached
    assert await sweeper.verdicts(stores, ENV) == first  # tick 3: cached
    assert vms.calls == 1, "a non-sweep tick must not shell out at all"

    # Tick 4 sweeps again -- and finds nothing to report, because tick 1
    # already corrected the RECORD (`state_name == "terminated"`, which every
    # sweep exempts). The report doesn't disappear, it changes owner: the
    # store's own StateReason is what /world reads from here on
    # (test_the_world_stops_claiming_healthy_for_a_deleted_vm), and that
    # handoff is exactly what a re-Apply needs to see -- and with no candidate
    # left, tick 4 doesn't even shell out.
    assert await sweeper.verdicts(stores, ENV) == {}
    assert vms.calls == 1
    assert (await project(stores, ENV))["web"][1] == "crashed"


async def test_a_recovered_resource_is_no_longer_reported(tmp_path, monkeypatch):
    """The recovery path for real: a re-Apply doesn't resurrect the drifted
    instance, it creates a NEW one (the provider dropped the terminated record
    from state), so the sweep goes quiet and the projection reads the live
    record instead of its predecessor."""
    monkeypatch.setenv("ODIN_DRIFT_SWEEP_TICKS", "2")
    stores = SynthStores(tmp_path)
    vms = FakeVms(names=[])
    sweeper = _sweeper(vms=vms)
    _ec2(stores, "web", "i-1")
    assert "web" in await sweeper.verdicts(stores, ENV)

    vms.names = [_ec2(stores, "web", "i-2")]  # what a re-Apply actually does
    await sweeper.verdicts(stores, ENV)  # cached tick: still reported
    assert await sweeper.verdicts(stores, ENV) == {}  # next sweep: clean
    assert (await project(stores, ENV))["web"] == ("ec2", "healthy", {}, None)


async def test_per_env_cadence_and_cache_are_independent(tmp_path):
    stores = SynthStores(tmp_path)
    _ec2(stores, "web", "i-1")
    stores.ec2compute.set("other", "instance:i-2", {
        "instance_id": "i-2", "state_name": "running", "state_reason": None,
    })
    stores.tags.set("other", "ec2:i-2", {"odin:node": "web2"})
    sweeper = _sweeper(vms=FakeVms(names=[vm_name("other", "i-2")]))

    assert "web" in await sweeper.verdicts(stores, ENV)
    assert await sweeper.verdicts(stores, "other") == {}


# --- a failed listing is NOT "everything is gone" ------------------------


async def test_a_failed_vm_listing_reports_no_drift(tmp_path):
    stores = SynthStores(tmp_path)
    _ec2(stores, "web", "i-1")
    vms = FakeVms(error=RuntimeError("limactl list --json failed: not installed"))
    assert await _sweeper(vms=vms).verdicts(stores, ENV) == {}
    # ...and NOTHING is written into the store either: marking a live instance
    # `terminated` over a limactl hiccup would make tofu recreate a VM that
    # was never gone.
    assert stores.ec2compute.get(ENV, "instance:i-1")["state_name"] == "running"


async def test_a_failed_container_listing_reports_no_drift_and_touches_no_record(tmp_path):
    stores = SynthStores(tmp_path)
    _ecs_task(stores, "t1")
    _fn(stores, "hello")
    containers = FakeContainers(error=RuntimeError("docker ps failed: daemon not running"))

    assert await _sweeper(containers=containers).verdicts(stores, ENV) == {}
    assert stores.ecsctl.get(ENV, "task:odin:t1")["last_status"] == "RUNNING"
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Active", (
        "a docker hiccup must never be written into a record as a real failure"
    )


async def test_a_docker_failure_does_not_hide_real_vm_drift(tmp_path):
    # The two listings are independent: one substrate being unreachable must
    # not suppress the other's honest report.
    stores = SynthStores(tmp_path)
    name = _ec2(stores, "web", "i-1")
    _fn(stores, "hello")
    sweeper = _sweeper(
        vms=FakeVms(names=[]), containers=FakeContainers(error=RuntimeError("docker down")),
    )

    verdicts = await sweeper.verdicts(stores, ENV)

    assert verdicts == {"web": f"VM {name} deleted outside odin — re-Apply to recreate"}


# --- network/synth-only kinds have no runtime footprint to sweep ---------


async def test_vpc_subnet_sg_iam_role_and_ecr_records_are_never_swept(tmp_path):
    stores = SynthStores(tmp_path)
    stores.ec2net.set(ENV, "vpc:vpc-1", {"vpc_id": "vpc-1"})
    stores.ec2net.set(ENV, "subnet:subnet-1", {"subnet_id": "subnet-1", "vpc_id": "vpc-1"})
    stores.ec2net.set(ENV, "sg:sg-1", {"group_id": "sg-1", "group_name": "web-sg"})
    stores.iamctl.set(ENV, "role:r1", {"role_name": "r1", "arn": "arn:aws:iam::000000000000:role/r1"})
    stores.ecr.set(ENV, "repo:img", {
        "repository_name": "img",
        "repository_arn": "arn:aws:ecr:us-east-1:000000000000:repository/img",
    })
    vms, containers = FakeVms(names=[]), FakeContainers(names=[])

    assert await _sweeper(vms=vms, containers=containers).verdicts(stores, ENV) == {}
    assert (vms.calls, containers.calls, containers.status_calls) == (0, 0, [])


# --- rds (W2.7): checked by its container's real STATE (the live half) and
# then by a real health probe -- see drift.py's "RDS IS CHECKED TWICE". ------


def _db(stores: SynthStores, label: str, identifier: str, status: str = "available", port: int = 54321) -> str:
    stores.rdsctl.set(ENV, f"db:{identifier}", {
        "db_instance_identifier": identifier, "status": status, "status_reason": None,
        "master_username": "app", "master_password": "apppass123", "db_name": "postgres",
        "endpoint_address": "host.docker.internal", "endpoint_port": port,
    })
    stores.tags.set(ENV, f"rds:{rdsctl.db_arn(identifier)}", {"odin:node": label})
    return db_container_name(ENV, identifier)


async def test_a_healthy_database_is_no_drift_and_costs_one_bulk_listing_plus_one_probe(tmp_path):
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")
    containers, probe = FakeContainers(names=[name]), FakeProbe(ok=True)

    assert await _sweeper(containers=containers, probe=probe).verdicts(stores, ENV) == {}
    # ONE real connection, ONE bulk listing, and ONE `inspect` for the one
    # container this env has -- never a second opinion per resource.
    assert probe.calls == [("127.0.0.1", 54321, "app", "apppass123")]
    assert (containers.calls, containers.status_calls) == (1, [name])


async def test_a_killed_container_is_real_drift_and_the_record_is_corrected(tmp_path):
    """`docker kill` -- the canonical way a database dies -- leaves an EXITED
    container that a NAME listing still lists, which is why the live half reads
    the container's real STATE. The verdict carries the container's real exit
    code, and the record goes `failed` so an Apply's converge recreates it."""
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")
    containers = FakeContainers(names=[], exited={name: 137})  # 137 = SIGKILL

    verdicts = await _sweeper(containers=containers, probe=FakeProbe(ok=False)).verdicts(stores, ENV)

    assert verdicts["app-db"] == f"container {name} is not running (exit 137) \u2014 re-Apply to recreate"
    record = stores.rdsctl.get(ENV, "db:app-db")
    assert record["status"] == "failed"
    assert record["status_reason"] == verdicts["app-db"]


async def test_a_removed_database_container_reads_like_ecs_not_like_an_invented_exit_code(tmp_path):
    """Field test 2 LOW-17: removing a container out of band gave `container … is
    not running (exit -1)` for rds where ecs said `container … removed outside
    odin` for the SAME event. -1 is `exit_code`'s "I could not read one"
    sentinel, and a container that no longer exists never reported one."""
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")
    containers = FakeContainers(names=[], exited={})  # gone entirely: no exit code to read

    verdicts = await _sweeper(containers=containers, probe=FakeProbe(ok=False)).verdicts(stores, ENV)

    assert verdicts["app-db"] == f"container {name} removed outside odin — re-Apply to recreate"
    assert "exit -1" not in verdicts["app-db"]
    assert stores.rdsctl.get(ENV, "db:app-db")["status_reason"] == verdicts["app-db"]


async def test_the_world_stops_claiming_healthy_and_stops_publishing_a_dead_database_url(tmp_path):
    """The projection-level point, through the CADENCE half: a dead database
    must not keep advertising a DATABASE_URL nothing can connect to -- that
    stale fact is what the Fabric would hand a consumer. Once the sweep has
    corrected the record, the projection reads `crashed` off the record itself,
    with or without a live check."""
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")
    _kind, phase, facts, _verdict = (await project(stores, ENV, containers=FakeContainers(names=[name])))["app-db"]
    assert (phase, facts["DATABASE_URL"]) == (
        "healthy", "postgresql://app:apppass123@host.docker.internal:54321/postgres",
    )

    await _sweeper(
        containers=FakeContainers(names=[], exited={name: 137}), probe=FakeProbe(ok=False),
    ).verdicts(stores, ENV)

    assert stores.rdsctl.get(ENV, "db:app-db")["status"] == "failed"
    kind, phase, facts, verdict = (await project(stores, ENV, containers=FakeContainers(names=[name])))["app-db"]
    assert (kind, phase, facts) == ("rds", "crashed", {})
    assert "is not running (exit 137)" in verdict


async def test_a_wedged_but_running_container_is_reported_without_corrupting_the_record(tmp_path):
    """A probe failure against a container that IS up may be transient, so it's
    reported and left to self-heal on the next sweep -- never written into the
    record, which would need a human Apply to undo. (The pre-W2.7 reconciler
    drew exactly this line: it only cleared the container on a real exit.)"""
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")
    containers = FakeContainers(names=[name])

    verdicts = await _sweeper(containers=containers, probe=FakeProbe(ok=False, error="too many clients")).verdicts(stores, ENV)

    assert verdicts["app-db"] == f"Postgres on {name} is not accepting connections: too many clients"
    record = stores.rdsctl.get(ENV, "db:app-db")
    assert (record["status"], record["status_reason"]) == ("available", None)


async def test_the_verdict_quotes_the_probe_that_failed_and_spends_only_one(tmp_path):
    """The reported reason must come from the SAMPLE THAT FAILED, not from a
    fresh probe taken to fetch the text.

    The wedged path used to probe twice -- once to test, once to interpolate
    `.error` -- so a second probe that SUCCEEDED made odin assert a failure its
    own newest evidence had just disproved, and rendered the reason as the
    literal string "None". `calls` pins the single-probe property, so
    reintroducing the second probe fails here rather than silently."""
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")

    class FlipsToHealthy:
        """Fails once, then answers fine -- exactly the transient blip the
        second probe used to land in."""

        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def __call__(self, host, port, user, password, db="postgres") -> PgReady:
            self.calls.append((host, port, user, password))
            if len(self.calls) == 1:
                return PgReady(ok=False, error="server closed the connection unexpectedly")
            return PgReady(ok=True)

    probe = FlipsToHealthy()
    verdicts = await _sweeper(containers=FakeContainers(names=[name]), probe=probe).verdicts(stores, ENV)

    assert verdicts["app-db"] == (
        f"Postgres on {name} is not accepting connections: server closed the connection unexpectedly"
    )
    assert len(probe.calls) == 1


async def test_a_probe_that_fails_without_an_error_still_names_a_reason(tmp_path):
    """`PgReady.error` is None whenever `_pg_connect` returns False instead of
    raising, and interpolating that None printed the word "None" at the user.
    A verdict has to name something true even when the driver says nothing."""
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")

    class Silent:
        async def __call__(self, host, port, user, password, db="postgres") -> PgReady:
            return PgReady(ok=False)

    verdict = (await _sweeper(containers=FakeContainers(names=[name]), probe=Silent()).verdicts(stores, ENV))["app-db"]

    assert "None" not in verdict
    assert verdict == f"Postgres on {name} is not accepting connections: {_NO_PROBE_ERROR}"


async def test_a_creating_deleting_or_failed_database_is_never_probed(tmp_path):
    """Mid-boot is not drift: `rdsctl._finish_create` is still running its own
    `pg_ready` loop, and `deleting`/`failed` are already terminal."""
    for status in ("creating", "deleting", "failed"):
        stores = SynthStores(tmp_path / status)
        _db(stores, "app-db", "app-db", status=status)
        probe = FakeProbe(ok=False)
        assert await _sweeper(probe=probe).verdicts(stores, ENV) == {}
        assert probe.calls == []


async def test_a_database_with_no_endpoint_yet_is_never_probed(tmp_path):
    stores = SynthStores(tmp_path)
    _db(stores, "app-db", "app-db", port=0)
    probe = FakeProbe(ok=False)
    assert await _sweeper(probe=probe).verdicts(stores, ENV) == {}
    assert probe.calls == []


# --- field test 5: the LIVE half -- no cadence, no cache, no sweeper -------


async def test_the_live_sweep_corrects_a_removed_container_with_no_sweeper_at_all(tmp_path):
    """`sweep_compute` is the whole check, callable by anything that has to be
    sure RIGHT NOW. Before this the same truth existed only inside
    `DriftSweeper`, behind a ~10-tick cadence and a cache -- so /apply-full and
    `/world`, which both read the RECORD, stayed green for the whole window."""
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    name = _db(stores, "app-db", "app-db")

    verdicts = await sweep_compute(stores, ENV, FakeContainers(names=[]))

    assert verdicts["hello"] == (
        "container odin-lambda-default-hello removed outside odin — re-Apply to recreate"
    )
    assert verdicts["app-db"] == f"container {name} removed outside odin — re-Apply to recreate"
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Failed"
    assert stores.rdsctl.get(ENV, "db:app-db")["status"] == "failed"


async def test_a_paused_container_is_not_serving_and_says_so(tmp_path):
    """`docker pause` is the case that defeats every record-trusting check AND
    every name listing: the container is present, `docker ps` lists it, and
    nothing inside answers a single connection. The state is what tells the
    truth, and the verdict names docker's own word for it."""
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    db = _db(stores, "app-db", "app-db")
    fn = "odin-lambda-default-hello"

    verdicts = await sweep_compute(stores, ENV, FakeContainers(names=[], paused=[fn, db]))

    assert verdicts == {
        "hello": f"container {fn} is paused — re-Apply to recreate",
        "app-db": f"container {db} is paused — re-Apply to recreate",
    }
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Failed"
    assert stores.rdsctl.get(ENV, "db:app-db")["status"] == "failed"


async def test_a_killed_lambda_container_carries_its_real_exit_code(tmp_path):
    # `docker kill` on the RIE container: present, exited, 137. ECS has always
    # reported the exit code; lambda saw only "gone or not gone" before.
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    name = "odin-lambda-default-hello"

    verdicts = await sweep_compute(stores, ENV, FakeContainers(names=[], exited={name: 137}))

    assert verdicts["hello"] == f"container {name} is not running (exit 137) — re-Apply to recreate"


async def test_the_live_sweep_leaves_a_healthy_pair_completely_alone(tmp_path):
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    name = _db(stores, "app-db", "app-db")
    containers = FakeContainers(names=["odin-lambda-default-hello", name])

    assert await sweep_compute(stores, ENV, containers) == {}
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Active"
    assert stores.rdsctl.get(ENV, "db:app-db")["status"] == "available"
    assert containers.calls == 1, "one bulk listing for the whole env, whatever it holds"
    assert sorted(containers.status_calls) == sorted(["odin-lambda-default-hello", name])


async def test_the_live_sweep_never_calls_docker_when_nothing_claims_to_be_up(tmp_path):
    """The happy-path cost rule: an env with no lambda/rds record -- or one
    whose records are all mid-flight -- pays nothing at all, so putting this on
    the projection (every tick) and on every apply is free where it is
    irrelevant."""
    stores = SynthStores(tmp_path)
    _fn(stores, "deploying", last_update="InProgress")
    _db(stores, "booting", "booting", status="creating")
    containers = FakeContainers(names=[])

    assert await sweep_compute(stores, ENV, containers) == {}
    assert (containers.calls, containers.status_calls) == (0, [])
    assert stores.lambdactl.get(ENV, "fn:deploying")["state"] == "Active"
    assert stores.rdsctl.get(ENV, "db:booting")["status"] == "creating"


async def test_a_container_the_listing_found_but_inspect_will_not_describe_is_not_a_death(tmp_path):
    """The second half of "unknown is not gone", one level down. `status` answers
    `absent` both for "no such container" AND for "docker didn't answer" -- it
    cannot tell them apart (its own docstring). For a name the bulk listing JUST
    returned, the honest reading is ambiguity, so nothing is reported and
    nothing is written; the next tick asks again. Without this, one busy-daemon
    `inspect` under a `tofu apply` could mark a live database `failed`, and the
    next apply would delete and recreate it."""
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    name = _db(stores, "app-db", "app-db")

    class ListedButUndescribable(FakeContainers):
        async def status(self, container: str) -> str:
            self.status_calls.append(container)
            return "absent"

    containers = ListedButUndescribable(names=["odin-lambda-default-hello", name])
    assert await sweep_compute(stores, ENV, containers) == {}
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Active"
    assert stores.rdsctl.get(ENV, "db:app-db")["status"] == "available"


async def test_the_live_sweep_corrects_nothing_when_docker_does_not_answer(tmp_path):
    """Unknown is not "gone" -- `_listing`'s rule, and it has to hold here too:
    this half runs on EVERY tick and inside every apply, so a daemon hiccup
    must never write `Failed` into a live resource's record."""
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    _db(stores, "app-db", "app-db")
    containers = FakeContainers(error=RuntimeError("docker ps failed: daemon not running"))

    assert await sweep_compute(stores, ENV, containers) == {}
    assert stores.lambdactl.get(ENV, "fn:hello")["state"] == "Active"
    assert stores.rdsctl.get(ENV, "db:app-db")["status"] == "available"


async def test_world_is_not_green_for_a_dead_function_on_the_very_next_projection(tmp_path):
    """The `/world` half of the fix, with NO DriftSweeper in sight: `await project()`
    runs the live check itself, so the tick after the container disappears
    reports `crashed` + the real reason instead of `healthy`."""
    stores = SynthStores(tmp_path)
    _fn(stores, "hello")
    assert (await project(stores, ENV, containers=FakeContainers(names=["odin-lambda-default-hello"])))["hello"] == (
        "lambda", "healthy", {}, None,
    )

    kind, phase, _facts, verdict = (await project(stores, ENV, containers=FakeContainers(names=[])))["hello"]

    assert (kind, phase) == ("lambda", "crashed")
    assert "removed outside odin" in verdict


async def test_world_stops_publishing_a_dead_databases_url_without_touching_its_record(tmp_path):
    """Both halves of the projection's contract in one place.

    IT REPORTS: `crashed`, the real reason, and NO FACTS -- a database that is
    not running must stop advertising a DATABASE_URL nothing can connect to.

    IT DOES NOT WRITE: the record still says `available`. That is what keeps
    the recovery honest -- `converge_db_instances` recreates a `failed`
    database, destroying its data, so nothing may mark it failed until an APPLY
    has reported the death. A projection that corrected the record would let
    the next unrelated apply silently recreate the database and report success."""
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")

    kind, phase, facts, verdict = (await project(
        stores, ENV, containers=FakeContainers(names=[], exited={name: 137}),
    ))["app-db"]

    assert (kind, phase, facts) == ("rds", "crashed", {})
    assert "is not running (exit 137)" in verdict
    record = stores.rdsctl.get(ENV, "db:app-db")
    assert (record["status"], record["status_reason"]) == ("available", None), (
        "the projection must report, never correct -- see drift.py's WHY THE PROJECTION MAY NOT WRITE"
    )


async def test_a_single_bad_sample_is_a_blip_not_drift(tmp_path, monkeypatch):
    """CONFIRM BEFORE CORRECTING (found running the real thing): under a busy
    docker daemon -- a `tofu apply` pulling a 250MB image -- one probe can fail
    AND `docker inspect` can come back empty for a container that's perfectly
    alive. Writing `failed` on that single sample corrupts the record, and only
    a human Apply undoes it. Both questions are asked again before the record is
    ever touched."""
    monkeypatch.setattr("odin.reconcile.drift._CONFIRM_DELAY", 0.0)
    stores = SynthStores(tmp_path)
    name = _db(stores, "app-db", "app-db")

    class Blip(FakeContainers):
        """`status` answers the live listing honestly, then lies ONCE to the
        probe half -- the busy-daemon `inspect` that comes back empty for a
        container that is perfectly alive."""

        async def status(self, container: str) -> str:
            self.status_calls.append(container)
            return "absent" if len(self.status_calls) == 2 else "running"

    class ProbeBlip(FakeProbe):
        async def __call__(self, *args, **kwargs):
            self.calls.append(args)
            return PgReady(ok=len(self.calls) > 1, error=None if len(self.calls) > 1 else "timeout")

    verdicts = await _sweeper(containers=Blip(names=[name]), probe=ProbeBlip()).verdicts(stores, ENV)

    assert verdicts == {}, "a single bad sample must not be reported as drift"
    record = stores.rdsctl.get(ENV, "db:app-db")
    assert (record["status"], record["status_reason"]) == ("available", None)
