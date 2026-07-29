"""THE walking skeleton, with nothing faked: a scheduled EventBridge rule
reaching a REAL Lambda in a REAL RIE container, on the REAL cadence, with the
outcome landing where a user looks.

Why this test and not another unit test. `reconcile/dispatch.py`'s unit suite
fakes exactly one thing -- the `FunctionRuntime` -- and that is the one thing a
dispatcher's whole value rests on. A fake substrate cannot tell you that the
tick actually reaches a container, that the Scheduled Event envelope is
something a real Python handler can destructure, or that the invocation's
outcome survives into `/world`. .claude/CLAUDE.md honesty rule 1: a unit test
that fabricates the upstream signal proves the parser, not the integration.

THE CADENCE IS NOT SHORTENED ANYWHERE IN THIS FILE, and that is deliberate to
the point of being expensive. `ODIN_DISPATCH_TICKS` is never set (a repo-wide
ratchet in tests/reconcile/test_dispatch_cadence.py asserts nobody sets it),
and the rule uses `rate(1 minute)` -- AWS's own minimum -- so this test really
does wait out a full minute for the first fire, exactly as a user would. Field
test 5 is why: the two drift e2e tests set `ODIN_DRIFT_SWEEP_TICKS=1` and
waited for the sweep before asserting, which measured the guard only after its
input had provably arrived and stepped around the entire residual.

Same substrate constraints as `test_lambda_failure_e2e.py`: the store root must
live under `$HOME` (Colima only mounts that tree, and an empty `/var/task` is a
real `Runtime.ImportModuleError`), and container hygiene is absolute -- every
container name is force-removed by exact name on teardown whatever the outcome.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient

from odin.compute.functions import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.models import Stack
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "evdisp-sched-e2e"
FUNCTION = "ticker"
GHOST = "never-deployed"
RULE = "every-minute"
GHOST_RULE = "points-at-nothing"
SCHEDULE = "rate(1 minute)"

# What the handler prints. `_ship_logs` puts the container's own stdout into
# `/aws/lambda/{name}`, so finding this string in CloudWatch Logs is durable
# proof the dispatcher reached a real container -- not a store field odin could
# have written without ever dialling anything.
MARKER = "ODIN-DISPATCH-FIRED"
_CODE = (
    "import json\n"
    "def lambda_handler(event, context):\n"
    f"    print('{MARKER} ' + json.dumps(event))\n"
    "    return {'saw': event.get('detail-type')}\n"
)

_ACTIVE_TIMEOUT = 240.0   # a cold public.ecr.aws/lambda/python:3.12 pull is a real fetch
# One rule period (60s) plus a whole poll interval plus real slack. The thing
# being waited for is the RULE's own period, which is AWS's minimum and not
# something odin may shorten.
_FIRE_TIMEOUT = 150.0


def _zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", code)
    return buf.getvalue()


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


@pytest.fixture
def lambda_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-dispatch-sched-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _await_active(client, name: str) -> None:
    deadline = time.monotonic() + _ACTIVE_TIMEOUT
    while time.monotonic() < deadline:
        state = client.get_function(FunctionName=name)["Configuration"]["State"]
        assert state != "Failed", client.get_function(FunctionName=name)["Configuration"]
        if state == "Active":
            return
        time.sleep(1.0)
    raise AssertionError(f"{name} never reached Active within {_ACTIVE_TIMEOUT}s")


def _await(predicate, timeout: float, what: str):
    """Poll `predicate` until it returns something truthy. Never a bare sleep of
    the expected duration: that asserts the timing rather than the outcome, and
    it passes for a dispatcher that fired for an entirely different reason."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(1.0)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def test_a_scheduled_rule_invokes_a_real_lambda_and_the_outcome_reaches_world(
    store_root, lambda_cleanup,
):
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.append(container_name(ENV, FUNCTION))

    store = SpecStore(store_root)
    store.apply(Stack(env=ENV))       # so the lifespan starts this env's reconciler loop
    app = create_app(store=store)
    with TestClient(app) as http:
        gateway_port = http.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)

        def _client(service: str):
            return boto3.client(
                service, endpoint_url=f"http://127.0.0.1:{gateway_port}",
                aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                region_name="us-east-1",
            )

        awslambda, events, logs = _client("lambda"), _client("events"), _client("logs")

        awslambda.create_function(
            FunctionName=FUNCTION, Runtime="python3.12",
            Role=f"arn:aws:iam::000000000000:role/{FUNCTION}-exec",
            Handler="lambda_function.lambda_handler", Code={"ZipFile": _zip(_CODE)},
        )
        _await_active(awslambda, FUNCTION)

        # THE proof this is a real container, not a model fiction.
        ps = _docker("ps", "--filter", f"name={container_name(ENV, FUNCTION)}", "--format", "{{.Image}}")
        assert ps.stdout.strip() == "public.ecr.aws/lambda/python:3.12", ps.stdout

        arn = awslambda.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]
        events.put_rule(Name=RULE, ScheduleExpression=SCHEDULE, State="ENABLED")
        events.put_targets(Rule=RULE, Targets=[{"Id": "t1", "Arn": arn}])

        # --- the wait a user really has: one rule period, nothing shortened ---
        started = time.monotonic()
        found = _await(
            lambda: _marked_events(logs, FUNCTION),
            _FIRE_TIMEOUT,
            f"the {SCHEDULE} rule never invoked {FUNCTION}",
        )
        elapsed = time.monotonic() - started
        print(f"\nMEASURED first fire of a {SCHEDULE} rule: {elapsed:.1f}s after PutTargets")

        # The handler really ran, and what it received is the EventBridge
        # envelope -- read out of the container's own stdout, shipped to
        # CloudWatch by `lambdactl.invoke`'s wrapper.
        event = json.loads(found[0]["message"].split(MARKER, 1)[1].strip())
        assert event["detail-type"] == "Scheduled Event"
        assert event["source"] == "aws.events"
        assert event["resources"] == [f"arn:aws:events:us-east-1:000000000000:rule/{RULE}"]

        # A rule fires ONCE per period, not once per tick. With a 1s poll and a
        # 60s period, a per-tick bug would show ~60 invocations by now.
        assert len(found) == 1, f"the rule fired {len(found)} times in one period"

        # ...and a healthy dispatched invocation leaves World clean: the phase
        # stays healthy and NO verdict is invented for a trigger that worked.
        node = _world_node(http, FUNCTION)
        assert node["phase"] == "healthy", node
        assert not node.get("verdict"), node

        awslambda.delete_function(FunctionName=FUNCTION)

    name = container_name(ENV, FUNCTION)
    ps = _docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}")
    assert ps.stdout.strip() == "", f"lambda container survived teardown: {ps.stdout}"


def test_a_rule_whose_target_function_is_gone_reports_it_in_world(store_root, lambda_cleanup):
    """The verdict half. A rule that cannot run must SAY so on the node a user
    can see -- the alternative is a trigger that silently does nothing, which is
    the bug this whole feature exists to remove.

    The target here names a function that was never deployed, which is the one
    case `PutTargets` deliberately does NOT refuse: real EventBridge does not
    validate target existence either, and refusing would break a legitimate
    apply order. So it has to surface at dispatch time instead."""
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    lambda_cleanup.append(container_name(ENV, GHOST))

    store = SpecStore(store_root)
    store.apply(Stack(env=ENV))
    app = create_app(store=store)
    with TestClient(app) as http:
        gateway_port = http.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        events = boto3.client(
            "events", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        )
        ghost_arn = f"arn:aws:lambda:us-east-1:000000000000:function:{GHOST}"
        events.put_rule(Name=GHOST_RULE, ScheduleExpression=SCHEDULE, State="ENABLED")
        events.put_targets(Rule=GHOST_RULE, Targets=[{"Id": "t1", "Arn": ghost_arn}])

        # The dispatcher reports the failure; nothing else in odin would, since
        # a function that does not exist has no record to project.
        verdict = _await(
            lambda: _dispatch_log(store_root),
            _FIRE_TIMEOUT,
            f"the {GHOST_RULE} rule never reported that {GHOST} does not exist",
        )
        assert "could not run" in verdict
        assert GHOST_RULE in verdict or GHOST in verdict


def _marked_events(logs, function: str) -> list[dict]:
    """The handler's own marked stdout lines in CloudWatch, or [] while there
    are none yet.

    The group is created by the FIRST log shipment, so before the first invoke
    `FilterLogEvents` legitimately raises `ResourceNotFoundException`. Checked
    with `DescribeLogGroups` rather than caught: a bare `except` here would also
    swallow a genuinely broken log path and turn this test's whole premise into
    a silent timeout."""
    group = f"/aws/lambda/{function}"
    groups = logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups", [])
    if not any(g["logGroupName"] == group for g in groups):
        return []
    return [e for e in logs.filter_log_events(logGroupName=group).get("events", [])
            if MARKER in e["message"]]


def _world_node(http, label: str) -> dict:
    world = http.get(f"/world?env={ENV}").json()
    node = next((r for r in world["resources"] if r["id"] == label), None)
    assert node is not None, f"{label} is not in /world: {world}"
    return node


def _dispatch_log(root: Path) -> str:
    """The env's own event log, which is where a WorldDelta's verdict lands
    durably (`.odin/<env>/events.jsonl`). Read from disk rather than from an
    SSE stream so this asserts on what SURVIVES, not on a transient broadcast."""
    path = root / ENV / "events.jsonl"
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if "could not run" in json.dumps(event):
            return json.dumps(event)
    return ""
