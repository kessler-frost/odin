"""w1 observability -- GET /logs: resolve a canvas node label to its real
backing container(s)/VM and return their logs, or (W2.1) read one CloudWatch
log GROUP out of odin's own sink. `fetch_logs`/`fetch_group_logs` are tested
directly (fast, precise per-kind resolution); a couple of TestClient smoke
tests lock the route's wiring end to end.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.api.logs import NO_BACKING_KINDS, fetch_group_logs, fetch_logs
from odin.gateway.models import logsctl
from odin.gateway.stores import SynthStores
from odin.server import create_app
from odin.spec.models import ResourceDesired, Stack
from odin.spec.store import SpecStore

ENV = "default"


class FakeRuntime:
    def __init__(self):
        self._status: dict[str, str] = {}
        self._logs: dict[str, str] = {}

    def set(self, name: str, status: str, logs: str = "") -> None:
        self._status[name] = status
        self._logs[name] = logs

    async def status(self, name: str) -> str:
        return self._status.get(name, "absent")

    async def logs(self, name: str, tail: int = 20) -> str:
        return self._logs.get(name, "")


def _store(tmp_path, resources=()):
    store = SpecStore(tmp_path)
    store.apply(Stack(resources=resources))
    return store


# --- unknown node: the one real error ---------------------------------------


async def test_unknown_node_is_an_honest_error_not_found(tmp_path):
    store = _store(tmp_path)
    stores = SynthStores(tmp_path)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "nope")
    assert result.found is False
    assert "nope" in result.error


# --- rds -----------------------------------------------------------------


async def test_rds_logs_from_its_direct_postgres_container(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="db", kind="rds"),))
    stores = SynthStores(tmp_path)
    rt = FakeRuntime()
    rt.set("odin-rds-default-db", "running", "PostgreSQL init complete")

    result = await fetch_logs(store, stores, rt, ENV, "db")

    assert result.found and result.running
    assert result.sources == ["odin-rds-default-db"]
    assert result.lines == "PostgreSQL init complete"
    assert result.message is None


async def test_rds_not_running_is_honest_not_a_500(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="db", kind="rds"),))
    stores = SynthStores(tmp_path)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "db")  # container never booted
    assert result.found is True
    assert result.running is False
    assert "not running" in result.message


# --- s3/sqs/sns/dynamodb: the shared per-env backing container -------------


async def test_provisioned_kind_reads_the_shared_backing_container(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="uploads", kind="s3"),))
    stores = SynthStores(tmp_path)
    rt = FakeRuntime()
    rt.set("odin-aws-rustfs-default", "running", "listening on :9000")

    result = await fetch_logs(store, stores, rt, ENV, "uploads")

    assert result.sources == ["odin-aws-rustfs-default"]
    assert result.lines == "listening on :9000"


async def test_exited_container_still_returns_its_last_logs(tmp_path):
    # A crash's whole diagnostic value is in the logs -- absent (never
    # existed) is the only status that skips reading them.
    store = _store(tmp_path, (ResourceDesired(id="db", kind="rds"),))
    stores = SynthStores(tmp_path)
    rt = FakeRuntime()
    rt.set("odin-rds-default-db", "exited", "FATAL: out of memory")

    result = await fetch_logs(store, stores, rt, ENV, "db")

    assert result.running is False
    assert result.lines == "FATAL: out of memory"
    assert "not running" in result.message


# --- ec2: resolves via the odin:node tag, reads the VM's journal -----------


async def test_ec2_resolves_instance_by_tag_and_reads_vm_logs(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="server", kind="ec2"),))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-1", {"instance_id": "i-1", "state_name": "running"})
    stores.tags.set(ENV, "ec2:i-1", {"odin:node": "server"})

    class FakeVm:
        async def status(self, name):
            return "running"

        async def logs(self, name, tail=20):
            return f"journal for {name}"

    monkeypatch.setattr("odin.api.logs.ec2_compute.InstanceVm", FakeVm)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "server")

    assert result.found and result.running
    assert result.sources == ["odin-ec2-default-i-1"]
    assert result.lines == "journal for odin-ec2-default-i-1"


async def test_a_deleted_vms_message_does_not_contradict_itself(tmp_path, monkeypatch):
    """Field test 2 LOW-12: `odin-ec2-… is not running (state: running)` -- the
    real check found the VM gone, the parenthetical printed the stale model
    state. Each half must be attributed to whoever said it."""
    store = _store(tmp_path, (ResourceDesired(id="web2", kind="ec2"),))
    stores = SynthStores(tmp_path)
    stores.ec2compute.set(ENV, "instance:i-9", {"instance_id": "i-9", "state_name": "running"})
    stores.tags.set(ENV, "ec2:i-9", {"odin:node": "web2"})

    class DeletedVm:
        async def status(self, name):
            return "absent"  # reality: `limactl list` doesn't have it

        async def logs(self, name, tail=20):
            raise AssertionError("an absent VM's journal must not be read")

    monkeypatch.setattr("odin.api.logs.ec2_compute.InstanceVm", DeletedVm)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "web2")

    assert result.running is False and result.lines == ""
    assert result.message == (
        "odin-ec2-default-i-9 is not running (VM state: absent; odin's record says running)"
    )


async def test_ec2_no_instance_yet_is_honest_not_found(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="server", kind="ec2"),))
    stores = SynthStores(tmp_path)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "server")
    assert result.found is True
    assert "no EC2 instance" in result.message


# --- lambda: resolves via tag or FunctionName, always reads via Colima -----


async def test_lambda_resolves_by_function_name_and_reads_rie_container(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="fn1", kind="lambda"),))
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:fn1", {
        "function_name": "fn1", "function_arn": "arn:aws:lambda:us-east-1:000000000000:function:fn1",
        "state": "Active",
    })

    class FakeColima:
        async def status(self, name):
            return "running"

        async def logs(self, name, tail=20):
            return "RIE listening"

    monkeypatch.setattr("odin.api.logs.ColimaRuntime", FakeColima)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "fn1")

    assert result.sources == ["odin-lambda-default-fn1"]
    assert result.lines == "RIE listening"


# --- elasticache: the cluster's own redis container (W2.8) -------------------


def _cache_record(cluster_id: str, status: str = "available") -> dict:
    return {
        "cache_cluster_id": cluster_id, "status": status,
        "arn": f"arn:aws:elasticache:us-east-1:000000000000:cluster:{cluster_id}",
    }


async def test_elasticache_reads_its_redis_container_logs(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="cache", kind="elasticache"),))
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:cache", _cache_record("cache"))

    class FakeColima:
        async def status(self, name):
            return "running"

        async def logs(self, name, tail=20):
            return "Ready to accept connections tcp"

    monkeypatch.setattr("odin.api.logs.ColimaRuntime", FakeColima)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "cache")

    assert result.sources == ["odin-cache-default-cache"]
    assert result.lines == "Ready to accept connections tcp"
    assert result.running is True


async def test_elasticache_with_no_cluster_yet_is_honest_not_a_500(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="cache", kind="elasticache"),))
    result = await fetch_logs(store, SynthStores(tmp_path), FakeRuntime(), ENV, "cache")
    assert result.found is True and result.error is None
    assert "no cache cluster backs" in result.message


async def test_elasticache_absent_container_reports_the_cluster_status(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="cache", kind="elasticache"),))
    stores = SynthStores(tmp_path)
    stores.cachectl.set(ENV, "cluster:cache", _cache_record("cache", status="creating"))

    class FakeColima:
        async def status(self, name):
            return "absent"

        async def logs(self, name, tail=20):
            return ""

    monkeypatch.setattr("odin.api.logs.ColimaRuntime", FakeColima)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "cache")
    assert "cluster status: creating" in result.message


# --- ecs: every task container for the service ------------------------------


async def test_ecs_reads_logs_from_every_task_container(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", {
        "cluster_name": "odin", "service_name": "app", "status": "ACTIVE", "desired_count": 2,
    })
    stores.ecsctl.set(ENV, "task:odin:t1", {
        "cluster_name": "odin", "service_name": "app", "task_id": "t1aaaaaa", "container_name": "web",
        "last_status": "RUNNING", "started_at": 2.0,
    })
    stores.ecsctl.set(ENV, "task:odin:t2", {
        "cluster_name": "odin", "service_name": "app", "task_id": "t2bbbbbb", "container_name": "web",
        "last_status": "STOPPED", "started_at": 1.0,
    })

    class FakeColima:
        async def status(self, name):
            return "running" if "t1aaaaaa" in name else "exited"

        async def logs(self, name, tail=20):
            return f"log:{name}"

    monkeypatch.setattr("odin.api.logs.ColimaRuntime", FakeColima)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "app")

    assert result.running is True  # at least one task is up
    assert len(result.sources) == 2
    assert result.sources[0].startswith("odin-ecs-default-t1aaaaaa")  # newest task first
    assert "==>" in result.lines  # multi-source: headered blocks


def _ecs_stores(tmp_path, live: int, dead: int) -> SynthStores:
    """A service with `live` RUNNING tasks and `dead` STOPPED ones -- what a
    few break/recover cycles leave behind (field test 3 saw 27 records for 3
    live tasks)."""
    stores = SynthStores(tmp_path)
    stores.ecsctl.set(ENV, "service:odin:app", {
        "cluster_name": "odin", "service_name": "app", "status": "ACTIVE", "desired_count": live,
    })
    for i in range(live):
        stores.ecsctl.set(ENV, f"task:odin:live{i}", {
            "cluster_name": "odin", "service_name": "app", "task_id": f"live{i}", "container_name": "web",
            "last_status": "RUNNING", "started_at": 100.0 + i,
        })
    for i in range(dead):
        stores.ecsctl.set(ENV, f"task:odin:dead{i}", {
            "cluster_name": "odin", "service_name": "app", "task_id": f"dead{i}", "container_name": "web",
            "last_status": "STOPPED", "started_at": float(i), "stopped_at": float(i),
        })
    return stores


class _CountingColima:
    """Counts the real driver calls -- the whole cost of the bug is one docker
    call per source, and there were 27 sources for 3 live tasks."""

    def __init__(self, lines_per_container: int = 1):
        self.status_calls: list[str] = []
        self.log_calls: list[tuple[str, int]] = []
        self.lines_per_container = lines_per_container

    async def status(self, name):
        self.status_calls.append(name)
        return "running" if "live" in name else "exited"

    async def logs(self, name, tail=20):
        self.log_calls.append((name, tail))
        return "\n".join(f"{name} line {i}" for i in range(self.lines_per_container))


async def test_ecs_sources_are_bounded_by_the_live_tasks_not_every_task_that_ever_ran(tmp_path, monkeypatch):
    """Field test 3 (MED): 27 sources for 3 live tasks, one docker call each,
    growing without bound as deployments accumulate."""
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = _ecs_stores(tmp_path, live=3, dead=24)
    colima = _CountingColima()
    monkeypatch.setattr("odin.api.logs.ColimaRuntime", lambda: colima)

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "app")

    # 3 live + at most 3 recent dead -- bounded, and the dead ones are kept
    # because a crash-looping service has NO live task and its last lines are
    # the entire diagnostic.
    assert len(result.sources) == 6, result.sources
    assert len(colima.status_calls) == 6
    assert all("live" in name for name in result.sources[:3]), result.sources
    assert result.sources[3:] == [
        s for s in result.sources if "dead23" in s or "dead22" in s or "dead21" in s
    ], "the three most recently stopped, newest first"


async def test_ecs_sources_are_only_the_recent_dead_ones_when_nothing_is_live(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = _ecs_stores(tmp_path, live=0, dead=9)
    colima = _CountingColima()
    monkeypatch.setattr("odin.api.logs.ColimaRuntime", lambda: colima)

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "app")

    assert len(result.sources) == 3, result.sources
    assert result.running is False
    assert result.lines, "a crash-looping service's last lines must still be readable"


async def test_tail_n_means_n_lines_of_output_for_a_multi_task_service(tmp_path, monkeypatch):
    """Field test 3 (MED): `--tail 1` returned 8 lines for a 3-task service.
    RDS honours --tail exactly; a multi-source node now does too -- N lines of
    CONTENT, newest source first, with the `==>` headers not counted."""
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = _ecs_stores(tmp_path, live=3, dead=0)
    colima = _CountingColima(lines_per_container=5)
    monkeypatch.setattr("odin.api.logs.ColimaRuntime", lambda: colima)

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "app", tail=1)

    content = [line for line in result.lines.splitlines() if not line.startswith("==> ")]
    assert len(content) == 1, result.lines
    assert result.lines.count("==> ") == 1, "only a source that contributed gets a header"
    assert len(result.sources) == 3, "every live task is still reported as a source"


async def test_tail_budget_spreads_across_sources_when_one_cannot_fill_it(tmp_path, monkeypatch):
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = _ecs_stores(tmp_path, live=3, dead=0)
    colima = _CountingColima(lines_per_container=2)
    monkeypatch.setattr("odin.api.logs.ColimaRuntime", lambda: colima)

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "app", tail=5)

    content = [line for line in result.lines.splitlines() if not line.startswith("==> ")]
    assert len(content) == 5, result.lines
    assert result.lines.count("==> ") == 3


async def test_a_single_source_node_still_honours_tail_exactly_and_has_no_header(tmp_path):
    """RDS parity -- the yardstick field test 3 measured against."""
    store = _store(tmp_path, (ResourceDesired(id="db", kind="rds"),))
    stores = SynthStores(tmp_path)
    rt = FakeRuntime()
    rt.set("odin-rds-default-db", "running", "one\ntwo\nthree")

    result = await fetch_logs(store, stores, rt, ENV, "db", tail=2)

    assert result.lines == "two\nthree"
    assert "==>" not in result.lines


async def test_ecs_no_service_yet_is_honest_not_found(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="app", kind="ecs"),))
    stores = SynthStores(tmp_path)
    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "app")
    assert result.found is True
    assert "no ECS service" in result.message


# --- logs (W2.1): the node IS the sink -- its own group's stored events -----


async def test_logs_node_returns_its_own_groups_stored_events(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="/odin/app", kind="logs"),))
    stores = SynthStores(tmp_path)
    logsctl.ingest(stores, ENV, "/odin/app", "stream-a", ["first", "second"])

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "/odin/app")

    assert result.found and result.kind == "logs"
    assert result.running is True  # the group exists; a sink has no process to be up
    assert result.sources == ["stream-a"]
    assert result.message is None
    lines = result.lines.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" first") and lines[1].endswith(" second")  # newest last
    assert lines[0].startswith("20")  # ISO-8601 UTC timestamp prefix
    assert "[stream-a]" not in result.lines  # single stream: no stream prefix


async def test_logs_node_with_no_group_yet_is_honest_not_an_error(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="/odin/app", kind="logs"),))
    stores = SynthStores(tmp_path)

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "/odin/app")

    assert result.found is True and result.error is None
    assert result.running is False
    assert result.lines == ""
    assert "no log group" in result.message


async def test_logs_node_whose_group_exists_but_is_empty_says_so(tmp_path):
    store = _store(tmp_path, (ResourceDesired(id="/odin/app", kind="logs"),))
    stores = SynthStores(tmp_path)
    logsctl.ensure_group(stores, ENV, "/odin/app")

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "/odin/app")

    assert result.running is True
    assert "no events yet" in result.message


async def test_a_sink_node_reads_the_group_it_was_renamed_to(tmp_path):
    """v0.8.15: a Log Group drawn as a workload's sink is CREATED under the
    name that workload's substrate really ships to (`/aws/lambda/myfn`), not
    under the node's label -- `agent/hcl.py::_LOG_DESTINATIONS`.

    THE RENAME WOULD STRAND THE NODE without this: `odin logs --node
    /odin/logs` read the group NAMED `/odin/logs`, which after the rename does
    not exist, so the drawn node would answer "no log group" while collecting
    every line under another name. Resolution goes through the `odin:node` tag
    odin's own generated HCL stamps, exactly like ec2/lambda/ecs already do.
    """
    store = _store(tmp_path, (ResourceDesired(id="/odin/logs", kind="logs"),))
    stores = SynthStores(tmp_path)
    logsctl.ingest(stores, ENV, "/aws/lambda/myfn", "odin-lambda-default-myfn", ["hello"])
    stores.tags.set(ENV, f"logs:{logsctl.group_arn('/aws/lambda/myfn')}", {"odin:node": "/odin/logs"})

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "/odin/logs")

    assert result.found and result.running is True
    assert result.sources == ["odin-lambda-default-myfn"]
    assert result.lines.endswith(" hello")


async def test_an_untagged_group_still_reads_by_label(tmp_path):
    """The fallback that keeps every pre-v0.8.15 env working: a group the
    substrate auto-created, or one applied by an older build, carries no
    `odin:node` tag and is still found by its name."""
    store = _store(tmp_path, (ResourceDesired(id="/odin/app", kind="logs"),))
    stores = SynthStores(tmp_path)
    logsctl.ingest(stores, ENV, "/odin/app", "stream-a", ["first"])

    result = await fetch_logs(store, stores, FakeRuntime(), ENV, "/odin/app")

    assert result.sources == ["stream-a"]


# --- ?group=: any group, including the substrate-created ones ---------------


def test_group_read_reaches_a_substrate_created_group(tmp_path):
    stores = SynthStores(tmp_path)
    logsctl.ingest(stores, ENV, "/aws/lambda/fn1", "odin-lambda-default-fn1", ["hello from the handler"])

    result = fetch_group_logs(stores, ENV, "/aws/lambda/fn1")

    assert result.found and result.running
    assert result.sources == ["odin-lambda-default-fn1"]
    assert result.lines.endswith(" hello from the handler")
    assert result.node == ""  # no node involved at all


def test_group_read_labels_each_line_when_the_group_has_several_streams(tmp_path):
    # `/ecs/{service}` gets one stream per task -- unlabelled, two tasks'
    # output would interleave unattributably.
    stores = SynthStores(tmp_path)
    logsctl.ingest(stores, ENV, "/ecs/app", "odin-ecs-default-t1-app", ["task one up"])
    logsctl.ingest(stores, ENV, "/ecs/app", "odin-ecs-default-t2-app", ["task two up"])

    result = fetch_group_logs(stores, ENV, "/ecs/app")

    assert result.sources == ["odin-ecs-default-t1-app", "odin-ecs-default-t2-app"]
    assert "[odin-ecs-default-t1-app] task one up" in result.lines
    assert "[odin-ecs-default-t2-app] task two up" in result.lines


def test_group_read_honours_tail(tmp_path):
    stores = SynthStores(tmp_path)
    logsctl.ingest(stores, ENV, "/ecs/app", "s", [f"line {i}" for i in range(10)])
    result = fetch_group_logs(stores, ENV, "/ecs/app", tail=3)
    assert [line.split(" ", 1)[1] for line in result.lines.splitlines()] == ["line 7", "line 8", "line 9"]


def test_group_read_of_an_unknown_group_is_honest_not_an_error(tmp_path):
    stores = SynthStores(tmp_path)
    result = fetch_group_logs(stores, ENV, "/aws/lambda/ghost")
    assert result.found is True and result.error is None
    assert result.running is False
    assert "no log group" in result.message


# --- kinds with no runnable backing at all ----------------------------------


async def test_no_backing_kinds_answer_honestly(tmp_path):
    assert NO_BACKING_KINDS == {"vpc", "subnet", "sg", "iam_role", "ecr"}
    for kind in NO_BACKING_KINDS:
        store = _store(tmp_path, (ResourceDesired(id="thing", kind=kind),))
        stores = SynthStores(tmp_path)
        result = await fetch_logs(store, stores, FakeRuntime(), ENV, "thing")
        assert result.found is True
        assert "no logs available" in result.message


# --- route wiring (TestClient) ----------------------------------------------


def test_logs_route_returns_200_never_500_for_an_unknown_node(tmp_path):
    from tests.api.test_apply import FakeRds
    from tests.api.test_apply import FakeRuntime as ServerFakeRuntime

    app = create_app(runtime=ServerFakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        resp = client.get("/logs", params={"node": "ghost"})
        assert resp.status_code == 200
        assert resp.json()["error"]


# --- env state odin wrote and can no longer parse ---------------------------
#
# The residual: `gateway/stores.py::_data` reads its file with a bare
# `json.loads`, so a truncated `.odin/<env>/gateway/<name>.json` made EVERY
# store-backed kind raise -- probed: ecs, lambda, ec2, elasticache, logs and
# `?group=` all `JSONDecodeError`, against a docstring promising "never a 500".
# The fix may not go the other way either: an empty `lines` with `found=True`
# would read as "the container said nothing" when odin never found out which
# container to ask.


def _corrupt_gateway_state(tmp_path, name: str) -> object:
    gateway = tmp_path / ENV / "gateway"
    gateway.mkdir(parents=True, exist_ok=True)
    path = gateway / f"{name}.json"
    path.write_text('{"service:app": ')  # exactly what an interrupted write leaves
    return path


def _corruptible_app(tmp_path):
    from tests.api.test_apply import FakeRds
    from tests.api.test_apply import FakeRuntime as ServerFakeRuntime

    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV, resources=(ResourceDesired(id="app", kind="ecs"),)))
    return create_app(runtime=ServerFakeRuntime(), store=store, rds=FakeRds(), backings=False)


def test_a_corrupt_gateway_state_file_is_a_named_error_not_a_500(tmp_path):
    path = _corrupt_gateway_state(tmp_path, "ecsctl")
    with TestClient(_corruptible_app(tmp_path)) as client:
        resp = client.get("/logs", params={"env": ENV, "node": "app"})
    assert resp.status_code == 200, "this route's whole contract is that it does not 500"
    body = resp.json()
    assert str(path) in body["error"], "the raised JSONDecodeError carries no path -- the route must find it"
    assert "JSONDecodeError" in body["error"]


def test_a_corrupt_gateway_state_file_never_reads_as_an_empty_log(tmp_path):
    """The false-green half (honesty rule 2). `found`/`lines` must not say the
    backing was consulted and had nothing to say."""
    _corrupt_gateway_state(tmp_path, "ecsctl")
    with TestClient(_corruptible_app(tmp_path)) as client:
        body = client.get("/logs", params={"env": ENV, "node": "app"}).json()
    assert body["found"] is False
    assert body["lines"] == "" and body["error"], "empty lines only ever alongside the error that explains them"


def test_a_corrupt_gateway_state_file_is_reported_on_the_group_path_too(tmp_path):
    """`?group=` bypasses node resolution but reads the same store."""
    path = _corrupt_gateway_state(tmp_path, "logsctl")
    with TestClient(_corruptible_app(tmp_path)) as client:
        resp = client.get("/logs", params={"env": ENV, "group": "/ecs/app"})
    assert resp.status_code == 200
    assert str(path) in resp.json()["error"]


def test_a_healthy_env_is_completely_unaffected_by_that_guard(tmp_path):
    """The guard must not turn ordinary answers into errors -- an env with no
    gateway state at all is the common case."""
    with TestClient(_corruptible_app(tmp_path)) as client:
        body = client.get("/logs", params={"env": ENV, "node": "app"}).json()
    assert body["error"] is None
    assert body["found"] is True and "no ECS service backs" in body["message"]


def test_logs_route_with_neither_node_nor_group_is_an_honest_error(tmp_path):
    from tests.api.test_apply import FakeRds
    from tests.api.test_apply import FakeRuntime as ServerFakeRuntime

    app = create_app(runtime=ServerFakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        resp = client.get("/logs")
        assert resp.status_code == 200
        assert "group" in resp.json()["error"]  # both params are optional, one is required


def test_logs_route_group_param_reads_the_sink_with_no_node_at_all(tmp_path):
    from tests.api.test_apply import FakeRds
    from tests.api.test_apply import FakeRuntime as ServerFakeRuntime

    app = create_app(runtime=ServerFakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        # The app's own stores live under its configured root -- reach them the
        # same way the route does, then read back over real HTTP.
        stores = app.state.gateway_stores
        logsctl.ingest(stores, ENV, "/aws/lambda/fn1", "odin-lambda-default-fn1", ["shipped by an invoke"])
        resp = client.get("/logs", params={"group": "/aws/lambda/fn1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        assert body["sources"] == ["odin-lambda-default-fn1"]
        assert body["lines"].endswith(" shipped by an invoke")
