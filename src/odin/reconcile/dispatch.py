"""The event DISPATCHER -- the half that reads "when X happens, run Y" and
actually runs Y.

odin could already RECORD a trigger: EventBridge rules and targets are a real
durable control plane (`gateway/models/eventsctl.py`), event source mappings
are one now too (`gateway/models/lambdactl.py`), and both survive a real `tofu
apply`. Nothing read those records. An edge that renders as a trigger and never
fires is the same "reports success it did not achieve" shape this repo's
honesty rules exist for, one layer up from a bad exit code -- so this module is
the other half, and the refusals in those two control planes are what keep the
half that is NOT built from looking built.

THREE SOURCES, ONE SINK, ONE PASS. The sink is a Lambda invoke; the sources are
a scheduled EventBridge rule, an SQS event source mapping, and an S3 bucket
notification. They are one dispatcher rather than three because the failure
path is the interesting part and it must be identical for all of them: an
outcome, a map from outcome to verdict, and an unmapped outcome that falls
through to failure (the `/destroy` lesson -- .claude/CLAUDE.md honesty rule 2).

CADENCE: EVERY TICK, and that default is the whole argument. `drift.py`'s sweep
runs every 10 ticks because a sweep is a REPORT -- ten seconds late means a
crashed resource is named ten seconds late. A dispatcher is an ACTION: ten
seconds late means a trigger the user calls broken. So `ODIN_DISPATCH_TICKS`
defaults to **1**, not 10, and a pass that finds nothing costs nothing --
arithmetic against a stored anchor for a schedule, and for a mapping one
`ReceiveMessage` against a local container. Neither is a `docker` shell-out,
which is what made the drift sweep expensive enough to need a cadence at all.

And no test shortens it. Field test 5 found the specific way that goes wrong:
two e2e tests set `ODIN_DRIFT_SWEEP_TICKS=1` and *waited for the sweep* before
asserting, which measured a guard only after its input had provably arrived and
stepped around the entire residual -- honest measurement was four times worse
than the prose disclosing it. `tests/reconcile/test_dispatch_cadence.py` is the
ratchet: it asserts the default is 1 AND that no file in this repo assigns
`ODIN_DISPATCH_TICKS`, because the only cadence that cannot be faked is the one
nobody can turn down.

SUSPEND DURING AN APPLY, exactly as the drift sweep does. `Reconciler._watch`
passes `dispatch=False` while tofu holds the daemon. Invoking a function while
`tofu apply` is mid-`UpdateFunctionCode` is a real hazard with teeth:
`lambdactl`'s deploy path deliberately `rm -f`s the old RIE container before
starting the new one, so there is a window where `State` reads `Active` and no
container exists. `invoke()` would answer `unreachable` and this module would
write a verdict for a function that is merely being redeployed. So a suspended
pass delivers nothing and advances no anchor -- a rule does not silently lose
its turn because an apply was running.

WHY THE VERDICT IS KEYED TO THE TARGET FUNCTION'S LABEL. A verdict has to reach
`/world` or it is a guard reading a signal nobody receives (honesty rule 1, in
reverse). EventBridge rules are NOT in `tf_status.TF_OWNED_KINDS` -- there is no
`events` node in the canvas catalog beyond a placeholder, and `hcl.py` has no
`aws_cloudwatch_event_rule` builder -- so a verdict keyed to a rule name would
land nowhere. The TARGET is a Lambda, lambdas ARE projected, and for odin a
function's name IS its canvas label (`hcl.py::_lambda` emits `function_name =
<label>`). So every verdict here names the trigger in its TEXT and is keyed by
the function, which is the label a user can actually see.

WHAT IT DELIBERATELY DOES NOT DO: it never retries by itself and it never
records success. A successful invoke is already visible -- `lambdactl.invoke`
writes `last_invocation_error=None`, which clears
`tf_status._invocation_verdict`. A failed HANDLER is visible the same way. This
module's verdicts cover only what the invoke wrapper cannot see: the trigger
could not run at all.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from functools import partial
from typing import NamedTuple

import httpx

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway.models import eventsctl, lambdactl
from odin.gateway.stores import SynthStores
from odin.settings import settings

log = logging.getLogger("odin.reconcile.dispatch")

# How long a receive/delete against the local goaws backing may take. Short
# because it IS local and short-polled; long enough that a busy daemon does not
# turn a healthy queue into a reported fault.
_SQS_TIMEOUT = 5.0

# `WaitTimeSeconds=0` -- SHORT-poll, deliberately. Real AWS long-polls for up to
# 20s and the equivalent here would park a coroutine for 20s per mapping. That
# is fine only if the client is genuinely async, and odin's existing SQS client
# (`aws/backings.py::client`) is a BLOCKING boto3 one, deliberately host-side
# and for tests -- using it here would put an `async def` whose body still
# blocks onto the shared loop and stall the gateway and the reconciler
# together. `httpx.AsyncClient` plus a short poll every tick is the same
# throughput at a 1s cadence and none of the risk.
#
# DO NOT "OPTIMISE" THIS INTO A LONG POLL now that the gateway supports one.
# Long polling THROUGH the gateway used to be broken outright (a wait of 5s or
# more came back as a 503 `ServiceUnavailable`, measured), and
# `gateway/app.py::_long_poll` fixed it -- at which point the obvious next thought
# is that this poller should use it. It should not, for reasons that fix does not
# touch:
#   - this is a RECONCILER TICK, not a request. One pass drains every mapping and
#     every pending S3 notification in turn, so a 20s park here is 20s that the
#     drift sweep, the scheduled rules and every other mapping also spend
#     waiting. A bounded pass is the whole design (see `_MAX_PENDING_PER_PASS`).
#   - it would also have to outlast `_SQS_TIMEOUT` below, which is sized for a
#     LOCAL round trip and is what turns a genuinely wedged goaws into a
#     `source_unavailable` verdict rather than a hang.
#   - and none of the gateway's work applies anyway: `_sqs_call` dials goaws's own
#     published port DIRECTLY (see `_queue_url`), so this path never sees the
#     derived read timeout at all.
# `tests/gateway/test_sqs_long_poll.py::test_the_event_dispatcher_still_short_polls`
# is the part of this comment that can fail a build.
_WAIT_TIME_SECONDS = 0

# How many pending S3 notifications one pass may deliver, and how many times one
# of them may fail before it is dropped with a verdict. Both are bounds on the
# same hazard from opposite sides -- an unbounded BATCH stalls the tick, an
# unbounded RETRY invokes a broken function forever -- and `_dispatch_pending`'s
# docstring argues each. 10 matches the SQS batch size for no deeper reason than
# that one tick should move a comparable amount of work whichever source it came
# from.
_MAX_PENDING_PER_PASS = 10
_MAX_DELIVERY_ATTEMPTS = 5


def _dispatch_ticks() -> int:
    """Ticks between passes. Read fresh on every call (never cached at import)
    so the override is real, the same convention `drift._sweep_ticks` uses --
    but defaulting to 1, for the reason in the module docstring. The default
    and its bound live in `settings.ReconcileSettings`."""
    return settings.reconcile.dispatch_ticks


class Delivery(NamedTuple):
    """One trigger that tried to run one function.

    `label` is the World label the verdict attaches to; `outcome` is the
    vocabulary `_VERDICT` maps; `detail` is the reason already worded. `fired`
    says whether the trigger actually reached the invoke -- which is NOT the
    same as success, and is what the SQS drain keys its delete on."""

    label: str
    outcome: str
    detail: str
    fired: bool = False


def _anchor_key(rule: dict) -> str:
    return f"fired:{rule['event_bus_name']}:{rule['name']}"


def _label_of(stores: SynthStores, env: str, function_name: str) -> str:
    """The World label for a function -- the SAME rule `tf_status.py::
    _lambda_functions` projects with, and `drift.py::_label` sweeps with.

    It has to be the same or a verdict would be keyed to a label that never
    appears in World, which is a guard that cannot fire (honesty rule 1). The
    fallback IS the function name, and every function odin knows about
    therefore has a label -- so the only unkeyable target is one naming a
    function that does not exist, which `_deliver` reports as its own outcome."""
    arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{function_name}"
    return stores.tags.get(env, f"lambda:{arn}", {}).get("odin:node") or function_name


# outcome -> how the verdict sentence is built, given (trigger, detail).
# DERIVED, never initialised, and an outcome nobody mapped raises a KeyError
# out of `_verdict` rather than inheriting a plausible-looking one. That is the
# `/destroy` fix's shape: four rounds of patching branches one at a time never
# got there, and "stop initialising the status at all" did.
_VERDICT = {
    "missing": lambda trigger, detail: f"{trigger} could not run: {detail}",
    "not_ready": lambda trigger, detail: f"{trigger} could not run: {detail} — re-Apply to recreate it",
    "unreachable": lambda trigger, detail: f"{trigger} could not run: {detail}",
    "unroutable": lambda trigger, detail: f"{trigger} will never fire: {detail}",
    "source_unavailable": lambda trigger, detail: f"{trigger} could not read its source: {detail}",
}


def _verdict(outcome: str, trigger: str, detail: str) -> str:
    return _VERDICT[outcome](trigger, detail)


def _mid_redeploy(stores: SynthStores, env: str, function_name: str) -> bool:
    """Is this function being replaced right now?

    `lambdactl`'s deploy path removes the old container BEFORE starting the new
    one, so there is a real window where an invoke cannot succeed and NOTHING is
    wrong. `drift.py` exempts that window and this file honoured nothing.

    IN FLIGHT, not merely not-running: `last_update_status == "InProgress"` or
    `state == "Pending"`. The first draft read `state != "Active"`, which also
    swallows `Failed` -- and a Failed function is genuinely broken, so its
    notifications must burn attempts and eventually be given up on rather than
    retried forever against a function that will never answer. The existing
    `test_a_failed_delivery_KEEPS_the_record_for_the_next_tick` caught that
    overreach immediately, which is the argument for narrow predicates over
    convenient ones.

    What that cost, before the fix: a redeploy burned one delivery attempt per
    tick, so a redeploy slower than `_MAX_DELIVERY_ATTEMPTS` ticks dropped every
    notification enqueued during it AND reported `GIVING UP after 5 attempts` --
    a lost event with a false verdict layered on top, which is both halves of
    this repo's worst bug class in one place.

    The caller must SKIP without counting the attempt and without consuming a
    batch slot. "Don't count the failure" alone is not enough and is worse than
    it looks: the drain is oldest-first, so records for a redeploying function
    would sit at the head forever and starve every healthy function behind them
    for the whole redeploy."""
    record = stores.lambdactl.get(env, f"fn:{function_name}")
    if record is None:
        return False
    return record.get("last_update_status") == "InProgress" or record.get("state") == "Pending"


async def _deliver(
    stores: SynthStores, env: str, function_name: str, payload: bytes, trigger: str,
    substrate=None,
) -> Delivery:
    """Invoke `function_name` for `trigger`, and report what happened.

    THROUGH `lambdactl.invoke`, never `FunctionRuntime.invoke` -- that is the
    execution seam, and everything that makes an invocation VISIBLE lives in
    the wrapper: the State guard, the CloudWatch log shipping, and the durable
    `last_invocation_error` that `tf_status._invocation_verdict` projects.
    Bypassing it gives a function that fails every dispatched invocation while
    reporting `healthy` with no verdict, which is field test 2 finding #4
    re-created through a new door.

    A handler that RAN and raised is `fired=True` with no verdict of our own:
    the wrapper already recorded it and World already shows it. Saying it twice,
    in two voices, is how five differently-worded versions of one fact happen."""
    label = _label_of(stores, env, function_name)
    result = await lambdactl.invoke(stores, env, function_name, payload, substrate)
    if result.outcome == "ran":
        return Delivery(label, "ran", result.detail, fired=True)
    return Delivery(label, result.outcome, _verdict(result.outcome, trigger, result.detail), fired=False)


# --- source 1: the scheduled EventBridge rule -------------------------------


def _due(rule: dict, anchor: dict | None, now: float) -> bool:
    """Is this rule due? Arithmetic against the stored anchor, no clock of its
    own -- `Dispatcher(clock=...)` is the seam a test moves time with.

    A rule with NO anchor is never due on the pass that first sees it: real
    EventBridge starts a schedule's clock when the rule is created, so a
    `rate(5 minutes)` rule fires five minutes later, not immediately. Firing on
    first sight would make every apply trigger an unrequested invocation."""
    period = eventsctl.schedule_seconds(rule["schedule_expression"] or "")
    return anchor is not None and period is not None and now - anchor["at"] >= period


async def _dispatch_rules(
    stores: SynthStores, env: str, now: float, substrate=None,
) -> list[Delivery]:
    """Every scheduled rule that is due, fired once.

    A DISABLED rule is skipped and gets no verdict -- it is the one
    non-delivering trigger that is not a lie, because the user asked for it.

    The anchor is advanced to `now`, NOT to `anchor + period`: odin does not
    backfill, because real EventBridge does not either. A machine asleep for an
    hour must not wake up and fire a `rate(1 minute)` rule sixty times."""
    out: list[Delivery] = []
    for rule in eventsctl.rules(stores, env):
        if rule["state"] != eventsctl.DEFAULT_STATE:
            continue
        targets = eventsctl.targets_of(stores, env, rule)
        anchor = stores.dispatch.get(env, _anchor_key(rule))
        if anchor is None:
            # First sight: start the clock and fire nothing (see `_due`).
            stores.dispatch.set(env, _anchor_key(rule), {"at": now})
            continue
        if not _due(rule, anchor, now):
            continue
        stores.dispatch.set(env, _anchor_key(rule), {"at": now})
        for target in targets:
            trigger = f"rule {rule['name']!r}"
            # `PutTargets` refuses a non-lambda target, so a stored one is
            # always deliverable -- this is the belt to that braces, and it
            # exists because a store written by an OLDER odin predates the
            # refusal. Such a target is reported unroutable rather than
            # silently skipped.
            if not eventsctl.is_lambda_target(target):
                out.append(Delivery(
                    _label_of(stores, env, eventsctl.target_function(target)), "unroutable",
                    _verdict("unroutable", trigger,
                             f"target {target['Id']!r} points at {target.get('Arn')!r}, which odin cannot invoke"),
                ))
                continue
            out.append(await _deliver(
                stores, env, eventsctl.target_function(target),
                _scheduled_event(rule, target), trigger, substrate,
            ))
    return out


def _scheduled_event(rule: dict, target: dict) -> bytes:
    """The payload a scheduled rule delivers.

    `Input` overrides it entirely when the target carries one -- that is real
    EventBridge's own rule and it is what `aws_cloudwatch_event_target`'s
    `input` argument drives. Otherwise it is the `Scheduled Event` envelope real
    EventBridge sends, which is the shape a handler written against AWS already
    destructures."""
    if target.get("Input") is not None:
        return str(target["Input"]).encode()
    return json.dumps({
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": ACCOUNT,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "region": REGION,
        "resources": [rule["arn"]],
        "detail": {},
    }).encode()


# --- source 2: the SQS event source mapping ---------------------------------
#
# The one trigger where odin's architecture and Amazon's are the same thing: a
# real event source mapping IS a poller, so odin polling goaws is the actual
# design rather than a substitute-shaped compromise. Its failure semantics come
# free and are AWS's own -- DO NOT DELETE a message whose invoke failed, and
# the queue's own visibility timeout redelivers it. That is why the receive
# sets a VisibilityTimeout at least as long as the function's timeout:
# otherwise the message is redelivered while the first invoke is still running
# and the function runs twice for one message.
#
# THE WIRE WAS MEASURED, not guessed (honesty rule 1). Captured from real boto3
# through `tests/gateway/harness.CaptureSink`:
#
#   POST /  content-type: application/x-amz-json-1.0
#   X-Amz-Target: AmazonSQS.ReceiveMessage
#   {"QueueUrl": "...", "MaxNumberOfMessages": 10, "WaitTimeSeconds": 0, ...}
#
# So SQS is the JSON 1.0 protocol here, NOT the legacy query protocol -- and
# `tests/aws/test_backings_e2e.py` drives `sqs.receive_message` through this
# same botocore against the real goaws, so the protocol is established by
# working code rather than by this comment.

_SQS_JSON = "application/x-amz-json-1.0"


def _queue_url(port: int, queue: str) -> str:
    """The QueueUrl goaws identifies a queue by. goaws mints its own URLs from
    the `Host`/`Port` in its mounted config, which odin deliberately points at
    the GATEWAY (`aws/backings.py::_goaws_config`) so a returned QueueUrl
    re-dials through the gateway. This dials goaws DIRECTLY on its published
    host port instead -- the receive/delete is odin's own bookkeeping, not a
    workload's call, the same boundary `logsctl.ingest` keeps by being reachable
    in-process only."""
    return f"http://127.0.0.1:{port}/{ACCOUNT}/{queue}"


async def _sqs_call(client: httpx.AsyncClient, port: int, op: str, payload: dict) -> dict:
    response = await client.post(
        f"http://127.0.0.1:{port}/",
        headers={"content-type": _SQS_JSON, "x-amz-target": f"AmazonSQS.{op}"},
        content=json.dumps(payload).encode(),
        timeout=_SQS_TIMEOUT,
    )
    response.raise_for_status()
    return json.loads(response.content or b"{}")


def _sqs_event(messages: list[dict], arn: str) -> bytes:
    """The `Records` envelope real Lambda hands an SQS-triggered handler --
    member names are SQS's own lowerCamel wire names, which is what a handler
    written against AWS destructures."""
    return json.dumps({"Records": [
        {
            "messageId": m.get("MessageId", ""),
            "receiptHandle": m.get("ReceiptHandle", ""),
            "body": m.get("Body", ""),
            "attributes": m.get("Attributes", {}),
            "messageAttributes": m.get("MessageAttributes", {}),
            "md5OfBody": m.get("MD5OfBody", ""),
            "eventSource": "aws:sqs",
            "eventSourceARN": arn,
            "awsRegion": REGION,
        }
        for m in messages
    ]}).encode()


async def _drain_mapping(
    stores: SynthStores, env: str, mapping: dict, client: httpx.AsyncClient, port: int,
    substrate=None,
) -> list[Delivery]:
    """One receive per mapping per pass, so a busy queue cannot starve the tick.

    Messages are deleted ONLY when the invoke actually ran. A failed invoke
    leaves them, goaws returns them after the visibility timeout, and that is
    SQS's own redrive -- free, and the same semantics a real event source
    mapping has."""
    queue = lambdactl.queue_of(mapping["event_source_arn"])
    trigger = f"the {queue!r} event source mapping"
    url = _queue_url(port, queue)
    try:
        received = await _sqs_call(client, port, "ReceiveMessage", {
            "QueueUrl": url,
            "MaxNumberOfMessages": mapping["batch_size"],
            "WaitTimeSeconds": _WAIT_TIME_SECONDS,
            "VisibilityTimeout": _visibility_timeout(stores, env, mapping),
        })
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        label = _label_of(stores, env, mapping["function_name"])
        return [Delivery(label, "source_unavailable",
                         _verdict("source_unavailable", trigger, f"queue {queue!r}: {exc!r}"))]
    messages = received.get("Messages") or []
    if not messages:
        return []
    delivery = await _deliver(
        stores, env, mapping["function_name"],
        _sqs_event(messages, mapping["event_source_arn"]), trigger, substrate,
    )
    if delivery.fired:
        await _sqs_call(client, port, "DeleteMessageBatch", {
            "QueueUrl": url,
            "Entries": [{"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]}
                        for i, m in enumerate(messages)],
        })
    return [delivery]


def _visibility_timeout(stores: SynthStores, env: str, mapping: dict) -> int:
    """At least as long as the function's own timeout, so a message cannot come
    back while the first invoke is still running (§4 of the design note). Read
    off the function record rather than assumed, and defaulted generously when
    the function is gone -- a wrong guess here duplicates work."""
    fn = stores.lambdactl.get(env, f"fn:{mapping['function_name']}") or {}
    return max(int(fn.get("timeout") or 0), 30)


async def _dispatch_mappings(
    stores: SynthStores, env: str, sqs_port, substrate=None,
) -> list[Delivery]:
    """Every ENABLED mapping drained once, concurrently.

    `asyncio.TaskGroup` (stdlib, per the concurrency directive -- not `anyio`).

    `sqs_port` is a CALLABLE, and that is not decoration. It resolves to
    `BackingAws.backing_ports()`, which shells out to `docker` once per backing
    on a cache miss -- so taking it as a plain number meant every tick paid that
    whether or not this env had a single mapping, at a 1-tick cadence, forever.
    Measured consequence: a real e2e saw the goaws backing restart under the
    load and the gateway briefly answer `ServiceUnavailable`. The module
    docstring always claimed "a pass that finds nothing must cost nothing"; this
    is the line that makes it true rather than aspirational.

    The store read above is the cheap question, and it is asked first: an env
    with no mapping never calls the callable at all."""
    mappings = [m for m in lambdactl.event_source_mappings(stores, env) if lambdactl.mapping_enabled(m)]
    if not mappings:
        return []
    port = await sqs_port() if sqs_port is not None else None
    if port is None:
        return [
            Delivery(_label_of(stores, env, m["function_name"]), "source_unavailable",
                     _verdict("source_unavailable", f"the {lambdactl.queue_of(m['event_source_arn'])!r} "
                              "event source mapping", "this env has no running SQS backing"))
            for m in mappings
        ]
    async with httpx.AsyncClient() as client, asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(_drain_mapping(stores, env, m, client, port, substrate))
            for m in mappings
        ]
    return [delivery for task in tasks for delivery in task.result()]


# --- source 3: the enqueued S3 bucket notification --------------------------
#
# `gateway/synth.py::postprocess` is SYNCHRONOUS -- it is called from
# `catch_all`'s async body and cannot await -- so the object-write hook
# ENQUEUES a `pending:` record and this drains it. Firing inline would mean
# making `postprocess` a coroutine (and `synth.pure_answer`'s own docstring
# records what happens when only SOME handlers in a table are coroutines: a
# branch that forgets its `await` returns a coroutine OBJECT, which is truthy,
# so the gateway answers with a coroutine instead of a Response). It would also
# block the object-write response for the whole handler duration, which real S3
# notifications never do -- they are asynchronous and at-least-once.


def _s3_event(pending: dict) -> bytes:
    """The `Records` envelope real S3 hands a notification handler."""
    return json.dumps({"Records": [{
        "eventVersion": "2.1",
        "eventSource": "aws:s3",
        "awsRegion": REGION,
        "eventName": pending["event_name"].removeprefix("s3:"),
        "s3": {
            "s3SchemaVersion": "1.0",
            "bucket": {"name": pending["bucket"], "arn": f"arn:aws:s3:::{pending['bucket']}"},
            "object": {"key": pending["key"], "size": pending.get("size", 0),
                       "eTag": pending.get("etag", "")},
        },
    }]}).encode()


async def _dispatch_pending(stores: SynthStores, env: str, substrate=None) -> list[Delivery]:
    """Pending S3 notifications, oldest first, at most `_MAX_PENDING_PER_PASS`
    ATTEMPTS per pass, each record retried at most `_MAX_DELIVERY_ATTEMPTS`
    times.

    "attempts per pass" rather than "records per pass" is load-bearing, and is
    why the sort below is not sliced: a record skipped because its function is
    mid-redeploy is not an attempt and must not consume a slot. Slicing first
    would let a few such records hold the ten oldest places and starve every
    healthy function behind them until the redeploy finished (`_mid_redeploy`).

    THE THREE PROPERTIES, because every one of them has a silent failure mode
    and the first version of this function got two of them wrong.

    1. **A DELIVERED record is deleted; a FAILED one is not.** The first version
       deleted unconditionally, before the invoke -- so a notification for a
       function that happened to be redeploying vanished with nothing anywhere
       recording that it was owed. Deleting only on success is what makes this
       at-least-once, which is what S3 notifications are.

    2. **...but not forever.** Keeping a failed record with no bound is the
       opposite bug and it is worse, because it LOOKS like work: a function that
       is down would be invoked once per tick, for every stuck notification,
       until someone noticed a busy machine reporting nothing wrong. There is no
       visibility timeout here to space the retries out (that is SQS's, and it
       is why the mapping drain needs no counter of its own). So the record
       carries `attempts`, and on the `_MAX_DELIVERY_ATTEMPTS`-th failure it is
       dropped with a verdict that NAMES the loss -- real AWS's max-receive-count
       into a dead-letter queue, with an honest report standing in for the DLQ
       odin does not have.

    3. **The pass is BOUNDED.** `aws s3 cp --recursive` over a few thousand
       objects enqueues a few thousand records in one burst, and draining them
       all inside one tick would stall the reconciler for everything else. The
       remainder is not lost -- these are durable records, and the next pass is
       one tick away. What that costs, stated rather than implied: at the
       production 1s poll the drain rate is ~`_MAX_PENDING_PER_PASS` per second,
       and a SLOW handler makes the pass itself exceed one tick, in which case
       ticks queue behind `Reconciler._tick_lock` rather than overlapping -- the
       reconciler falls behind, it does not double-run."""
    # NOT sliced here: the batch bounds ATTEMPTS, and a record skipped for a
    # redeploying function is not an attempt. Slicing first would let a handful
    # of such records hold the ten oldest slots and starve everything behind
    # them until the redeploy finished.
    pending = sorted(
        ((key, record) for key, record in stores.dispatch.items(env).items() if key.startswith("pending:")),
        key=lambda item: item[1]["at"],
    )
    out: list[Delivery] = []
    attempted = 0
    for key, record in pending:
        if attempted >= _MAX_PENDING_PER_PASS:
            break
        function_name = record["target_arn"].rsplit(":", 1)[-1]
        if _mid_redeploy(stores, env, function_name):
            continue
        attempted += 1
        trigger = f"the {record['bucket']!r} notification for {record['key']!r}"
        delivery = await _deliver(stores, env, function_name, _s3_event(record), trigger, substrate)
        if delivery.fired:
            stores.dispatch.delete(env, key)
            continue
        attempts = record.get("attempts", 0) + 1
        if attempts < _MAX_DELIVERY_ATTEMPTS:
            stores.dispatch.set(env, key, {**record, "attempts": attempts})
            out.append(delivery)
            continue
        # Given up on. The verdict says so IN the sentence, because "could not
        # run" and "will never run" are different things to a reader and only
        # one of them is worth acting on.
        stores.dispatch.delete(env, key)
        out.append(delivery._replace(detail=(
            f"{delivery.detail} — GIVING UP after {attempts} attempts; "
            f"the {record['bucket']}/{record['key']} notification was dropped and will not be retried"
        )))
    return out


class Dispatcher:
    """The cadence around one pass, and the seams that make it testable with no
    Docker at all -- the same shape `DriftSweeper` already has, down to the
    injectable substrate.

    `clock` is the seam a test moves TIME with, and it is separate from the
    cadence on purpose. The cadence (how often a pass runs) is the thing rule 1b
    forbids shortening, and it is never shortened anywhere. A rule's PERIOD is
    the user's own choice -- `rate(1 day)` -- and no test can wait a day for it,
    so the arithmetic is proven against a moved clock while the cadence is
    proven at the cadence a user actually gets. Two different guards, tested
    two different ways, neither faked."""

    def __init__(self, functions=None, clock=None) -> None:
        self._functions = functions
        self._clock = clock or time.time
        self._ticks: dict[str, int] = {}

    async def verdicts(
        self, stores: SynthStores, env: str, sqs_port=None, dispatch: bool = True,
    ) -> dict[str, str]:
        """`label -> verdict` for every trigger that could NOT run, after firing
        the ones that could.

        Deliberately NOT cached between passes, which is the opposite of
        `DriftSweeper` and the reason is that they answer opposite questions. A
        drift verdict describes a STATE that persists, so it must survive the
        ticks between sweeps or it would flap. A dispatch verdict describes an
        EVENT that just happened; re-reporting it on the next tick would claim a
        trigger failed again when it never ran again. `Reconciler._emit`
        already suppresses everything but a change, so a genuinely repeating
        failure still costs one delta rather than one per tick.

        `sqs_port` is an async CALLABLE returning this env's goaws port, not the
        port itself -- see `_dispatch_mappings` for the measured reason.

        `dispatch=False` is the in-flight-apply form (`Reconciler._watch`):
        nothing is delivered, no anchor moves and the cadence counter does not
        advance, so a suspended apply neither delays nor triggers the next
        pass."""
        if not dispatch:
            return {}
        count = self._ticks.get(env, 0)
        self._ticks[env] = count + 1
        if count % _dispatch_ticks() != 0:
            return {}
        return {d.label: d.detail for d in await self._pass(stores, env, sqs_port) if not d.fired
                and d.outcome != "ran"}

    async def _pass(self, stores: SynthStores, env: str, sqs_port: int | None) -> list[Delivery]:
        """One pass over all three sources.

        Each source is guarded separately and a failure in one must not stop
        the others -- nor be swallowed. A source that raises is logged with its
        traceback and contributes nothing this pass; the two that worked still
        deliver. This is the one broad `except` in the module and it exists
        because a dispatcher exception must not stop the reconciler tick."""
        now = self._clock()
        # `partial` of the coroutine FUNCTION, never the coroutine itself -- the
        # same shape `drift.py::_listing` takes its argument in, and
        # `tests/test_no_unawaited_coroutine.py` is the ratchet that insisted on
        # it. Building all three coroutines up front and awaiting them in the
        # loop below LEAKS the un-awaited ones the moment anything unwinds
        # early: "coroutine was never awaited" is a warning, not an error, so
        # the source that silently never ran would look exactly like a source
        # with nothing to do.
        sources = (
            ("scheduled rules", partial(_dispatch_rules, stores, env, now, self._functions)),
            ("event source mappings", partial(_dispatch_mappings, stores, env, sqs_port, self._functions)),
            ("bucket notifications", partial(_dispatch_pending, stores, env, self._functions)),
        )
        out: list[Delivery] = []
        for name, source in sources:
            try:
                out.extend(await source())
            except Exception:  # noqa: BLE001 -- a source must not stop the tick, and must not go quiet
                log.exception("dispatch pass failed for %s (env %s)", name, env)
        return out
