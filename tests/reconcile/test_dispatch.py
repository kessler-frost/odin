"""reconcile/dispatch.py -- the delivery half.

Every test here drives the REAL `Dispatcher` against REAL stores, and every
invoke goes through the REAL `lambdactl.invoke` wrapper down to a fake
`FunctionRuntime`. The substrate is the only fake, deliberately: what these
tests prove is that a trigger reaches the wrapper and that the wrapper's
outcome becomes a verdict, and a fake substrate cannot make that true when it
is false. Whether the RIE container answers is a different question, proven
with a real container in tests/reconcile/test_dispatch_e2e.py.

The clock IS moved here, and that is not the thing rule 1b forbids. A rule's
PERIOD is the user's own choice (`rate(1 day)`) and no test can wait it out;
the CADENCE -- how often a pass runs -- is what must never be shortened, and
`test_dispatch_cadence.py` pins that separately, at the real cadence.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from odin.compute.functions import InvokeResult
from odin.gateway.models import eventsctl
from odin.gateway.stores import SynthStores
from odin.reconcile import dispatch
from odin.reconcile.dispatch import Dispatcher

ENV = "evdisp-unit"
FUNCTION = "thumbnailer"
FUNCTION_ARN = f"arn:aws:lambda:us-east-1:000000000000:function:{FUNCTION}"
RULE = "nightly"


class FakeFunctions:
    """A `FunctionRuntime` stand-in with the two methods `lambdactl.invoke`
    reaches: `invoke` and `logs`. Records every payload it was handed, which is
    what the delivery assertions read."""

    def __init__(self, result: InvokeResult | None = None, raises: Exception | None = None) -> None:
        self.result = result or InvokeResult(payload=b'{"ok":true}', function_error=None)
        self.raises = raises
        self.payloads: list[bytes] = []

    async def invoke(self, env: str, name: str, payload: bytes) -> InvokeResult:
        self.payloads.append(payload)
        if self.raises is not None:
            raise self.raises
        return self.result

    async def logs(self, env: str, name: str, tail: int) -> str:
        return ""


class PortProvider:
    """The async callable the dispatcher asks for this env's goaws port, and a
    counter of how often it was asked.

    It is a CALLABLE rather than a number because resolving it in production
    shells out to `docker` once per backing -- see `_dispatch_mappings`. The
    count is what `test_an_env_with_no_mapping_never_looks_a_port_up` reads."""

    def __init__(self, port: int | None) -> None:
        self.port = port
        self.calls = 0

    async def __call__(self) -> int | None:
        self.calls += 1
        return self.port


def _port(port: int | None) -> PortProvider:
    return PortProvider(port)


class MovableClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


def seed_function(stores: SynthStores, state: str = "Active", label: str | None = None) -> None:
    stores.lambdactl.set(ENV, f"fn:{FUNCTION}", {
        "function_name": FUNCTION,
        "function_arn": FUNCTION_ARN,
        "state": state,
        "last_update_status": "Successful",
        "last_invocation_error": None,
        "timeout": 3,
    })
    if label:
        stores.tags.set(ENV, f"lambda:{FUNCTION_ARN}", {"odin:node": label})


def seed_rule(stores: SynthStores, schedule: str = "rate(5 minutes)", state: str = "ENABLED",
              target: dict | None = None) -> None:
    stores.eventsctl.set(ENV, f"rule:default:{RULE}", {
        "name": RULE,
        "arn": eventsctl.rule_arn("default", RULE),
        "event_bus_name": "default",
        "state": state,
        "description": None,
        "schedule_expression": schedule,
        "event_pattern": None,
        "role_arn": None,
        "created_at": 0.0,
    })
    stores.eventsctl.set(ENV, f"targets:default:{RULE}",
                         [target or {"Id": "t1", "Arn": FUNCTION_ARN}])


# --- the walking skeleton: tick -> due? -> invoke -> verdict -----------------


async def test_a_scheduled_rule_does_not_fire_on_the_pass_that_first_sees_it(stores):
    """Real EventBridge starts a schedule's clock when the rule is created, so
    a `rate(5 minutes)` rule fires five minutes later -- not immediately.

    Firing on first sight would make every single apply trigger an unrequested
    invocation, which is the kind of surprise that gets a feature turned off."""
    seed_function(stores)
    seed_rule(stores)
    functions = FakeFunctions()
    clock = MovableClock()

    assert await Dispatcher(functions, clock).verdicts(stores, ENV) == {}
    assert functions.payloads == [], "a rule must not fire on the pass that first anchors it"
    assert stores.dispatch.get(ENV, f"fired:default:{RULE}") == {"at": clock.now}


async def test_a_scheduled_rule_fires_once_its_period_has_elapsed(stores):
    seed_function(stores)
    seed_rule(stores, schedule="rate(5 minutes)")
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)          # anchor
    clock.advance(299)
    await dispatcher.verdicts(stores, ENV)
    assert functions.payloads == [], "299s into a 300s period is not due"

    clock.advance(1)
    assert await dispatcher.verdicts(stores, ENV) == {}, "a delivered trigger reports no verdict"
    assert len(functions.payloads) == 1, "the rule must fire at exactly its period"

    event = json.loads(functions.payloads[0])
    assert event["detail-type"] == "Scheduled Event"
    assert event["source"] == "aws.events"
    assert event["resources"] == [eventsctl.rule_arn("default", RULE)]


async def test_the_anchor_advances_to_now_so_an_outage_does_not_backfill(stores):
    """AWS does not backfill a missed schedule and neither does odin. Advancing
    the anchor by one PERIOD instead of to NOW would make a machine asleep for
    an hour wake up and fire a `rate(1 minute)` rule sixty times."""
    seed_function(stores)
    seed_rule(stores, schedule="rate(1 minute)")
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(3600)                       # an hour asleep
    await dispatcher.verdicts(stores, ENV)
    assert len(functions.payloads) == 1, "one pass fires once, however long the gap"
    assert stores.dispatch.get(ENV, f"fired:default:{RULE}") == {"at": clock.now}

    clock.advance(59)
    await dispatcher.verdicts(stores, ENV)
    assert len(functions.payloads) == 1, "the anchor moved to now, so the next fire is a full period away"


async def test_a_disabled_rule_delivers_nothing_and_reports_nothing(stores):
    """The one non-delivering trigger that is not a lie: the user asked for it."""
    seed_function(stores)
    seed_rule(stores, state="DISABLED")
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(3600)
    assert await dispatcher.verdicts(stores, ENV) == {}
    assert functions.payloads == []


async def test_a_target_input_overrides_the_scheduled_envelope(stores):
    """`aws_cloudwatch_event_target`'s `input` argument, which is real
    EventBridge's own behaviour."""
    seed_function(stores)
    seed_rule(stores, target={"Id": "t1", "Arn": FUNCTION_ARN, "Input": '{"mine":1}'})
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(400)
    await dispatcher.verdicts(stores, ENV)
    assert functions.payloads == [b'{"mine":1}']


# --- the verdicts: what the invoke wrapper cannot say for itself -------------


async def test_a_rule_whose_function_is_not_active_names_the_trigger_and_the_state(stores):
    seed_function(stores, state="Failed")
    seed_rule(stores)
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(400)
    verdicts = await dispatcher.verdicts(stores, ENV)

    assert list(verdicts) == [FUNCTION], "the verdict is keyed to the TARGET's World label"
    assert "rule 'nightly' could not run" in verdicts[FUNCTION]
    assert "state=Failed" in verdicts[FUNCTION]
    assert "re-Apply" in verdicts[FUNCTION]
    assert functions.payloads == [], "a non-Active function must not be dialled at all"


async def test_a_rule_whose_function_does_not_exist_says_so(stores):
    seed_rule(stores)                      # no function seeded
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(400)
    verdicts = await dispatcher.verdicts(stores, ENV)
    assert "Function not found" in verdicts[FUNCTION]


async def test_the_verdict_is_keyed_to_the_odin_node_tag_when_there_is_one(stores):
    """A verdict keyed to a label World never emits is a guard nobody receives
    -- honesty rule 1 in reverse. The label rule here must be the SAME one
    `tf_status._lambda_functions` projects with."""
    seed_function(stores, state="Failed", label="thumbs-canvas-label")
    seed_rule(stores)
    dispatcher = Dispatcher(FakeFunctions(), MovableClock())

    await dispatcher.verdicts(stores, ENV)
    dispatcher._clock.advance(400)
    verdicts = await dispatcher.verdicts(stores, ENV)
    assert list(verdicts) == ["thumbs-canvas-label"]


async def test_a_handler_that_raised_is_left_to_the_invoke_wrappers_own_verdict(stores):
    """`lambdactl.invoke` records `last_invocation_error`, which
    `tf_status._invocation_verdict` already projects. The dispatcher saying it
    again, in its own words, is how one fact ends up with five wordings."""
    seed_function(stores)
    seed_rule(stores)
    functions = FakeFunctions(InvokeResult(payload=b"{}", function_error="Unhandled"))
    clock = MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(400)
    assert await dispatcher.verdicts(stores, ENV) == {}, "the wrapper owns this verdict, not the dispatcher"
    assert stores.lambdactl.get(ENV, f"fn:{FUNCTION}")["last_invocation_error"] == "Unhandled"


async def test_a_stored_non_lambda_target_is_reported_unroutable(stores):
    """`PutTargets` refuses these now, so this can only come from a store an
    OLDER odin wrote. It must be reported, never silently skipped."""
    seed_function(stores)
    seed_rule(stores, target={"Id": "archiver", "Arn": "arn:aws:sqs:us-east-1:000000000000:archive"})
    clock = MovableClock()
    dispatcher = Dispatcher(FakeFunctions(), clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(400)
    verdicts = await dispatcher.verdicts(stores, ENV)
    assert "will never fire" in verdicts["archive"]


async def test_a_cron_schedule_is_never_fired_and_never_silently_dropped(stores):
    """`PutRule` refuses a cron expression, so this too can only come from an
    older store -- and it must not be treated as a rule that simply is not due
    yet, which is indistinguishable from a working rule."""
    seed_function(stores)
    seed_rule(stores, schedule="cron(0 12 * * ? *)")
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(86_400 * 7)
    await dispatcher.verdicts(stores, ENV)
    assert functions.payloads == [], "odin has no cron evaluator; it must not guess"


# --- suspension during an apply ---------------------------------------------


async def test_a_suspended_pass_delivers_nothing_and_loses_no_turn(stores):
    """`Reconciler._watch` passes `dispatch=False` while tofu holds the daemon.
    `lambdactl`'s deploy path removes the old RIE container before starting the
    new one, so invoking mid-`UpdateFunctionCode` would report a function
    unreachable that is merely being redeployed."""
    seed_function(stores)
    seed_rule(stores)
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    clock.advance(400)
    assert await dispatcher.verdicts(stores, ENV, dispatch=False) == {}
    assert functions.payloads == [], "nothing may be invoked while an apply holds the env"

    assert await dispatcher.verdicts(stores, ENV) == {}
    assert len(functions.payloads) == 1, "the rule was still due: a suspended pass must not cost it its turn"


# --- S3 pending notifications ------------------------------------------------


async def test_a_pending_notification_is_delivered_as_an_s3_records_envelope(stores):
    seed_function(stores)
    stores.dispatch.set(ENV, "pending:abc", {
        "bucket": "uploads", "key": "a/b.png", "event_name": "s3:ObjectCreated:Put",
        "target_arn": FUNCTION_ARN, "at": 1.0, "size": 42, "etag": "e1",
    })
    functions = FakeFunctions()
    assert await Dispatcher(functions, MovableClock()).verdicts(stores, ENV) == {}

    record = json.loads(functions.payloads[0])["Records"][0]
    assert record["eventSource"] == "aws:s3"
    assert record["eventName"] == "ObjectCreated:Put"
    assert record["s3"]["bucket"]["name"] == "uploads"
    assert record["s3"]["object"] == {"key": "a/b.png", "size": 42, "eTag": "e1"}
    assert stores.dispatch.get(ENV, "pending:abc") is None, "a delivered notification is consumed"


async def test_pending_notifications_are_delivered_oldest_first(stores):
    seed_function(stores)
    for name, at in (("pending:b", 2.0), ("pending:a", 1.0), ("pending:c", 3.0)):
        stores.dispatch.set(ENV, name, {
            "bucket": "uploads", "key": name, "event_name": "s3:ObjectCreated:Put",
            "target_arn": FUNCTION_ARN, "at": at,
        })
    functions = FakeFunctions()
    await Dispatcher(functions, MovableClock()).verdicts(stores, ENV)
    keys = [json.loads(p)["Records"][0]["s3"]["object"]["key"] for p in functions.payloads]
    assert keys == ["pending:a", "pending:b", "pending:c"]


def seed_pending(stores: SynthStores, store_key: str = "pending:abc", **overrides) -> None:
    """`store_key` names the STORE key; the record's own `key` (the S3 object
    key) is an override, since a `pending:` record has a field called `key` too
    and the two collide in a kwargs signature."""
    stores.dispatch.set(ENV, store_key, {
        "bucket": "uploads", "key": "a/b.png", "event_name": "s3:ObjectCreated:Put",
        "target_arn": FUNCTION_ARN, "at": 1.0, "size": 0, "etag": "", "attempts": 0,
        **overrides,
    })


async def test_a_failed_delivery_KEEPS_the_record_for_the_next_tick(stores):
    """THE at-least-once half, and the first version of this code got it wrong
    -- it deleted unconditionally, BEFORE the invoke. A notification for a
    function that happened to be mid-redeploy vanished with nothing anywhere
    recording that it was owed, which is the quietest possible way to lose
    work."""
    seed_function(stores, state="Failed")
    seed_pending(stores)
    verdicts = await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV)

    assert "could not run" in verdicts[FUNCTION]
    kept = stores.dispatch.get(ENV, "pending:abc")
    assert kept is not None, "an undelivered notification must survive for the next pass"
    assert kept["attempts"] == 1


async def test_a_retried_notification_is_delivered_once_the_function_recovers(stores):
    """The point of keeping it: the retry has to actually work."""
    seed_function(stores, state="Failed")
    seed_pending(stores)
    functions, clock = FakeFunctions(), MovableClock()
    dispatcher = Dispatcher(functions, clock)

    await dispatcher.verdicts(stores, ENV)
    assert functions.payloads == []

    stores.lambdactl.update(ENV, f"fn:{FUNCTION}", lambda fn: {**fn, "state": "Active"})
    assert await dispatcher.verdicts(stores, ENV) == {}
    assert len(functions.payloads) == 1
    assert stores.dispatch.get(ENV, "pending:abc") is None, "a delivered notification is consumed"


async def test_delivery_is_given_up_on_LOUDLY_rather_than_retried_forever(stores):
    """The opposite bug, and the worse one because it LOOKS like work: an
    unbounded retry invokes a broken function once per tick, for every stuck
    notification, forever -- a busy machine reporting nothing wrong.

    There is no visibility timeout here to space retries out (that is SQS's own,
    which is why the mapping drain needs no counter). So the record counts its
    attempts and is dropped with a verdict that NAMES the loss -- real AWS's
    max-receive-count into a DLQ, with an honest report standing in for the DLQ
    odin does not have."""
    seed_function(stores, state="Failed")
    seed_pending(stores)
    dispatcher = Dispatcher(FakeFunctions(), MovableClock())

    for _ in range(dispatch._MAX_DELIVERY_ATTEMPTS - 1):
        verdicts = await dispatcher.verdicts(stores, ENV)
        assert stores.dispatch.get(ENV, "pending:abc") is not None
        assert "GIVING UP" not in verdicts[FUNCTION]

    final = await dispatcher.verdicts(stores, ENV)
    assert stores.dispatch.get(ENV, "pending:abc") is None, "it must not be retried forever"
    assert "GIVING UP" in final[FUNCTION]
    assert "uploads/a/b.png" in final[FUNCTION], "the verdict must name what was dropped"


async def test_one_pass_drains_a_bounded_number_of_notifications(stores):
    """`aws s3 cp --recursive` over a few thousand objects enqueues a few
    thousand records in one burst. Draining them all inside one tick would stall
    the reconciler for every other resource -- the same shape as a blocking call
    on the shared loop. The remainder is not lost: these are durable records and
    the next pass is one tick away."""
    seed_function(stores)
    for i in range(dispatch._MAX_PENDING_PER_PASS * 3):
        seed_pending(stores, store_key=f"pending:{i:03d}", at=float(i), key=f"obj-{i:03d}")
    functions = FakeFunctions()

    await Dispatcher(functions, MovableClock()).verdicts(stores, ENV)
    assert len(functions.payloads) == dispatch._MAX_PENDING_PER_PASS
    remaining = [k for k in stores.dispatch.items(ENV) if k.startswith("pending:")]
    assert len(remaining) == dispatch._MAX_PENDING_PER_PASS * 2, "the rest wait, they are not dropped"

    # ...and the OLDEST were the ones taken, so nothing starves at the back.
    delivered = [json.loads(p)["Records"][0]["s3"]["object"]["key"] for p in functions.payloads]
    assert delivered == [f"obj-{i:03d}" for i in range(dispatch._MAX_PENDING_PER_PASS)]


# --- SQS event source mappings ----------------------------------------------


def seed_mapping(stores: SynthStores, state: str = "Enabled") -> None:
    stores.lambdactl.set(ENV, "esm:m-1", {
        "uuid": "m-1",
        "event_source_arn": "arn:aws:sqs:us-east-1:000000000000:jobs",
        "function_name": FUNCTION,
        "function_arn": FUNCTION_ARN,
        "state": state,
        "batch_size": 10,
        "maximum_batching_window_in_seconds": 0,
        "state_transition_reason": "USER_INITIATED",
        "last_processing_result": None,
        "function_response_types": [],
        "last_modified": 0.0,
    })


async def test_a_mapping_with_no_running_backing_reports_it_rather_than_going_quiet(stores):
    seed_function(stores)
    seed_mapping(stores)
    verdicts = await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=_port(None))
    assert "could not read its source" in verdicts[FUNCTION]
    assert "no running SQS backing" in verdicts[FUNCTION]


async def test_a_disabled_mapping_is_not_drained_and_reports_nothing(stores):
    seed_function(stores)
    seed_mapping(stores, state="Disabled")
    assert await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=_port(None)) == {}


async def test_an_env_with_no_mapping_never_looks_a_port_up(stores):
    """"A pass that finds nothing must cost nothing" -- what makes a 1-tick
    cadence affordable, and a promise the first version of this code did not
    keep.

    Resolving the port calls `BackingAws.backing_ports()`, which shells out to
    `docker` once per backing on a cache miss. Doing that eagerly every tick was
    enough, measured in a real e2e, to make the goaws backing restart and the
    gateway answer `ServiceUnavailable`. So the store read comes first and the
    callable is not touched at all when there is nothing to drain."""
    seed_function(stores)
    provider = _port(4599)
    assert await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=provider) == {}
    assert provider.calls == 0, "an env with no event source mapping must not shell out to docker"


async def test_an_env_with_a_mapping_asks_for_the_port_exactly_once(stores):
    """...and when there IS something to drain, the lookup happens once for the
    whole pass rather than once per mapping."""
    seed_function(stores)
    seed_mapping(stores)
    stores.lambdactl.set(ENV, "esm:m-2", {
        **stores.lambdactl.get(ENV, "esm:m-1"),
        "uuid": "m-2", "event_source_arn": "arn:aws:sqs:us-east-1:000000000000:second",
    })
    provider = _port(None)
    await Dispatcher(FakeFunctions(), MovableClock()).verdicts(stores, ENV, sqs_port=provider)
    assert provider.calls == 1


# --- one source must not stop the others ------------------------------------


async def test_a_source_that_raises_is_logged_and_the_others_still_deliver(stores, caplog):
    """A dispatcher exception must not stop the tick, and must not be swallowed
    either. The corrupt record here is one `records.py` would reject on a fresh
    load, so it stands for any in-memory shape a future writer gets wrong."""
    seed_function(stores)
    stores.dispatch.set(ENV, "pending:ok", {
        "bucket": "uploads", "key": "k", "event_name": "s3:ObjectCreated:Put",
        "target_arn": FUNCTION_ARN, "at": 1.0,
    })
    stores.eventsctl.set(ENV, f"rule:default:{RULE}", {"name": RULE})   # missing every other member
    functions = FakeFunctions()

    verdicts = await Dispatcher(functions, MovableClock()).verdicts(stores, ENV)
    assert verdicts == {}
    assert len(functions.payloads) == 1, "the healthy source still delivered"
    assert "dispatch pass failed for scheduled rules" in caplog.text


# --- the mid-redeploy exemption --------------------------------------------
#
# Found by `s3notify` reviewing this file's source rather than agreeing from a
# summary, and verified before fixing: `drift.py` exempts a redeploying function
# on BOTH signals (`state != "Active"`, `last_update_status == "InProgress"`) and
# `dispatch.py` honoured neither. `lambdactl`'s deploy path removes the old
# container before starting the new one, so that window is real and nothing is
# wrong during it.


async def test_a_redeploying_function_does_not_burn_delivery_attempts(stores):
    """The bug, in the direction that loses data. A redeploy slower than
    `_MAX_DELIVERY_ATTEMPTS` ticks used to drop every notification enqueued
    during it AND report `GIVING UP after 5 attempts` — a lost event with a
    false verdict on top."""
    seed_function(stores, state="Active")
    stores.lambdactl.set(ENV, f"fn:{FUNCTION}", {
        **stores.lambdactl.get(ENV, f"fn:{FUNCTION}"), "last_update_status": "InProgress",
    })
    stores.dispatch.set(ENV, "pending:abc", {
        "bucket": "uploads", "key": "a/b.png", "event_name": "s3:ObjectCreated:Put",
        "target_arn": FUNCTION_ARN, "at": 1.0, "size": 42, "etag": "e1",
    })
    functions = FakeFunctions()

    for _ in range(8):  # comfortably past the give-up bound
        await Dispatcher(functions, MovableClock()).verdicts(stores, ENV)

    record = stores.dispatch.get(ENV, "pending:abc")
    assert record is not None, "the notification was dropped while its function was redeploying"
    assert record.get("attempts", 0) == 0, f"a redeploy burned attempts: {record}"
    assert functions.payloads == [], "nothing should have been invoked mid-redeploy"


async def test_a_redeploying_function_does_not_starve_the_healthy_ones(stores):
    """The interaction, which is why 'do not count the failure' alone is wrong.
    The drain is oldest-first, so records for a redeploying function would hold
    the head of the queue and block everything behind them for the whole
    redeploy. They must not consume a batch slot either."""
    seed_function(stores, state="Active")
    stores.lambdactl.set(ENV, f"fn:{FUNCTION}", {
        **stores.lambdactl.get(ENV, f"fn:{FUNCTION}"), "last_update_status": "InProgress",
    })
    other, other_arn = "healthy-fn", "arn:aws:lambda:us-east-1:000000000000:function:healthy-fn"
    stores.lambdactl.set(ENV, f"fn:{other}", {
        "function_name": other, "function_arn": other_arn, "state": "Active",
        "last_update_status": "Successful", "last_invocation_error": None,
    })
    # 12 older records for the redeploying function -- more than the batch of 10.
    for i in range(12):
        stores.dispatch.set(ENV, f"pending:old{i:02d}", {
            "bucket": "uploads", "key": f"old{i}.png", "event_name": "s3:ObjectCreated:Put",
            "target_arn": FUNCTION_ARN, "at": float(i), "size": 1, "etag": "e",
        })
    stores.dispatch.set(ENV, "pending:new", {
        "bucket": "uploads", "key": "new.png", "event_name": "s3:ObjectCreated:Put",
        "target_arn": other_arn, "at": 99.0, "size": 1, "etag": "e",
    })
    functions = FakeFunctions()

    await Dispatcher(functions, MovableClock()).verdicts(stores, ENV)

    assert stores.dispatch.get(ENV, "pending:new") is None, (
        "the healthy function's notification was starved behind 12 older records "
        "for a function that is merely redeploying"
    )
