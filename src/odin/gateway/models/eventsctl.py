"""The gateway's EventBridge model: the `aws_cloudwatch_event_rule` /
`aws_cloudwatch_event_target` / `aws_cloudwatch_event_bus` CONTROL plane -- the
half of "an edge that renders as a trigger" that `tofu apply` and `tofu
destroy` actually drive.

Like iamctl/logsctl/secretsctl/ssmctl, `events` has no backing container to
forward to and no container substrate of its own: this module is the whole
answer for every `events:*` action (classified by `classify.py`'s
`_classify_events` -- ECR's JSON-target wire shape, `X-Amz-Target:
AWSEvents.*`, verified against botocore's own `events` service model: protocol
`json`, jsonVersion 1.1, targetPrefix `AWSEvents`, endpointPrefix `events`).
Its substrate is the per-env JSON sidecar `.odin/{env}/gateway/eventsctl.json`,
which is what makes rules and targets survive a reload -- the whole reason a
`plan` after an `apply` is clean.

**THE DELIVERY HALF IS NOT BUILT, AND THIS MODULE SAYS SO ON THE WIRE.**
odin can now record that "rule R fires target T"; nothing yet READS that record
and invokes T. So `PutEvents` -- the one `events` action a workload makes --
does NOT answer `{"FailedEntryCount": 0}`, because an accepted-and-never-
delivered event is precisely the "reports success it did not achieve" shape
this repo's honesty rules exist for. It answers a protocol-correct
`InternalException` naming the missing half instead (`_put_events`), so a
caller finds out at the call site rather than by waiting for a lambda that will
never run. Every control-plane op below is real; that one is honest about
being absent. See the DISPATCHER section of ROADMAP/the design note for where
the delivery half lands and what it must call.

WHAT IS MODELED, and why each one is load-bearing rather than an extra --
these are exactly the calls terraform-provider-aws drives for the three
resources above:
  - rule:    PutRule (create AND update -- the provider uses one call for both),
             DescribeRule, DeleteRule, ListRules, EnableRule, DisableRule
  - targets: PutTargets, RemoveTargets, ListTargetsByRule
  - bus:     CreateEventBus, DeleteEventBus, DescribeEventBus, ListEventBuses
  - tags:    TagResource, UntagResource, ListTagsForResource -- without these
             every `plan` drifts on `tags`, the same gap `logsctl`/`ecsctl`
             close.

TARGETS ARE STORED VERBATIM. `PutTargets` keeps each entry's whole dict
(`Id`, `Arn`, `Input`, `InputTransformer`, `EcsParameters`, ...) and
`ListTargetsByRule` echoes it back unexamined, for `ecsctl`'s
`container_definitions` reason: the provider compares what it sent against what
it reads, so anything this module normalizes is a permanent diff. It also means
the dispatcher's input (a target's `Arn`) is the ARN terraform really wrote,
not a re-derivation of it.

THE DEFAULT EVENT BUS IS IMPLICIT. Real EventBridge always has a `default`
bus and it cannot be created or deleted; here it needs no record, so a rule
lands on it with no `CreateEventBus` first. A rule or target naming any OTHER
bus requires that bus to exist (`_bus_missing`) -- a typo'd `event_bus_name`
otherwise creates a rule on a phantom bus that nothing can ever route, which is
a silently wrong state rather than an error.

NOT MODELED (each a real EventBridge feature deliberately absent, never
silently reinterpreted): archives/replays, schemas, API destinations and
connections, partner event sources, `PutPermission`/`RemovePermission`
(cross-account bus policy), `TestEventPattern`, `ListRuleNamesByTarget`, and
rule/target pagination (`NextToken` is never emitted; `Limit` truncates). An
unmodeled op gets a protocol-correct `InternalAction` error from `pure_answer`,
never a 503 and never a silent forward.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.stores import SynthStores

SERVICE = "events"
# The bus every rule lands on when a caller names none. Kept in lock-step with
# `classify.py::EVENTS_DEFAULT_BUS`.
DEFAULT_BUS = "default"
DEFAULT_STATE = "ENABLED"
STATE_DISABLED = "DISABLED"
# `ListRules`/`ListEventBuses`/`ListTargetsByRule` all default to 100 in real
# EventBridge; nothing here paginates, so this only truncates.
DEFAULT_LIMIT = 100


def bare_name(value: str) -> str:
    """The bare RULE or EVENT BUS name out of either an ARN or a plain name --
    kept in lock-step with `classify.py::_events_bare_name` (same rule, same
    reasoning: the name is the last `/`-segment of any events ARN, and a real
    rule/bus name contains no `/`)."""
    return value.rsplit("/", 1)[-1] if value.startswith("arn:") else value


def rule_arn(bus: str, name: str) -> str:
    """A rule's ARN. The DEFAULT bus is elided from the resource path
    (`…:rule/nightly`) and any other bus is part of it (`…:rule/{bus}/nightly`)
    -- real EventBridge's own two forms, which is what makes `bare_name` above
    correct for both."""
    scope = name if bus == DEFAULT_BUS else f"{bus}/{name}"
    return f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{scope}"


def bus_arn(name: str) -> str:
    return f"arn:aws:events:{REGION}:{ACCOUNT}:event-bus/{name}"


def _json(payload: dict) -> Response:
    return Response(json.dumps(payload), media_type="application/x-amz-json-1.1")


def _not_found(message: str) -> Response:
    return errors.synth_error(SERVICE, "ResourceNotFoundException", message, 400)


def _already_exists(message: str) -> Response:
    return errors.synth_error(SERVICE, "ResourceAlreadyExistsException", message, 400)


def _invalid(message: str) -> Response:
    """EventBridge's `ValidationException` is NOT one of the exception shapes in
    botocore's `events` model (checked: the model declares 13, and that is not
    among them), so a boto3 caller sees it as a plain `ClientError` with
    `Code == "ValidationException"` -- which is what real EventBridge sends for
    a rule deleted with targets still attached, and what the wire code has to be
    for a Go-SDK caller's string comparison to match (the same
    shape-name-is-not-the-wire-code lesson `synth.py`'s
    `_SQS_QUEUE_DOES_NOT_EXIST` records)."""
    return errors.synth_error(SERVICE, "ValidationException", message, 400)


def _drop_none(payload: dict) -> dict:
    """Omit unset optional members entirely, the way real EventBridge does.

    WEAKER THAN THE SAME HELPER IN `logsctl`/`lambdactl`, and worth saying so
    rather than copying their justification: those docstrings warn that a null
    is read back as a REAL value and drifts, which is true on the wires they
    serve. Probed here against botocore's own `events` (JSON) parser, both
    forms parse identically:

        {"Name":"r","Arn":"a","ScheduleExpression":null}
            -> {'Name': 'r', 'Arn': 'a', ...}
        {"Name":"r","Arn":"a"}
            -> {'Name': 'r', 'Arn': 'a', ...}

    So for THIS service a null would not have caused the drift -- Go's
    `encoding/json` leaves a `*string` nil for both too. What this keeps is
    byte-fidelity with real AWS, which is the standard the rest of the gateway
    is held to; it is not a fix for a measured bug. The thing that really would
    drift is a member DROPPED from the record altogether, which is what
    `_rule_json`'s round-trip is about."""
    return {k: v for k, v in payload.items() if v is not None}


# --- store keys --------------------------------------------------------------


def _rule_key(bus: str, name: str) -> str:
    return f"rule:{bus}:{name}"


def _targets_key(bus: str, rule: str) -> str:
    return f"targets:{bus}:{rule}"


def _bus_key(name: str) -> str:
    return f"bus:{name}"


def _bus_of(payload: dict) -> str:
    """The bus a request names, canonicalized. `EventBusName` is documented as
    "the name OR ARN of the event bus", so both forms arrive and both reduce
    here."""
    value = payload.get("EventBusName")
    return bare_name(value) if isinstance(value, str) and value else DEFAULT_BUS


def _rule(stores: SynthStores, env: str, bus: str, name: str) -> dict | None:
    return stores.eventsctl.get(env, _rule_key(bus, name))


def _rules(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.eventsctl.items(env).items() if k.startswith("rule:")]


def _targets(stores: SynthStores, env: str, bus: str, rule: str) -> list[dict]:
    return stores.eventsctl.get(env, _targets_key(bus, rule), [])


def _buses(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.eventsctl.items(env).items() if k.startswith("bus:")]


def _bus(stores: SynthStores, env: str, name: str) -> dict | None:
    """A bus record, or None if there is no such bus. The DEFAULT bus is
    SYNTHESIZED rather than stored (module docstring): it always exists, so
    seeding a record for it would only create a way for it to be missing.
    Every "does this bus exist" question in this module goes through here, so
    that rule lives in exactly one place."""
    if name != DEFAULT_BUS:
        return stores.eventsctl.get(env, _bus_key(name))
    return {
        "name": DEFAULT_BUS, "arn": bus_arn(DEFAULT_BUS), "description": None,
        "creation_time": None, "last_modified_time": None,
    }


def _by_arn(stores: SynthStores, env: str, arn: str) -> dict | None:
    """The rule OR event-bus record whose ARN is `arn` -- the tag ops' only
    identifier is `ResourceARN`, and both kinds are taggable.

    A scan rather than an ARN parser, deliberately: every record already stores
    its own `arn`, so matching on it cannot disagree with what the wire reported,
    while a second parser for EventBridge's two rule-ARN forms could. The
    `isinstance` guard skips the `targets:` records, which are LISTS."""
    return next(
        (record for record in stores.eventsctl.items(env).values()
         if isinstance(record, dict) and record.get("arn") == arn),
        None,
    )


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, f"{SERVICE}:{arn}", {})


def _set_tags(stores: SynthStores, env: str, arn: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"{SERVICE}:{arn}", tags)


def _tags_from_list(items: object) -> dict[str, str]:
    entries = items if isinstance(items, list) else []
    return {e["Key"]: e.get("Value", "") for e in entries if isinstance(e, dict) and e.get("Key")}


def _tags_to_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]


# --- wire shapes (member names verified against botocore's `events` model) ---


def _rule_json(record: dict) -> dict:
    """A `Rule` -- `ListRules`' member shape, and a subset of `DescribeRule`'s
    (which adds `CreatedBy`). Every optional member the provider reads has to
    come BACK with the value it was given, or that argument reads as unset on
    the next refresh and every plan is dirty against it -- which is why the
    record stores all six and this builds all six, rather than only the ones a
    scheduled rule happens to use."""
    return _drop_none({
        "Name": record["name"],
        "Arn": record["arn"],
        "EventPattern": record["event_pattern"],
        "State": record["state"],
        "Description": record["description"],
        "ScheduleExpression": record["schedule_expression"],
        "RoleArn": record["role_arn"],
        "EventBusName": record["event_bus_name"],
    })


def _bus_json(record: dict) -> dict:
    return _drop_none({
        "Name": record["name"],
        "Arn": record["arn"],
        "Description": record["description"],
        "CreationTime": record["creation_time"],
        "LastModifiedTime": record["last_modified_time"],
    })


# --- rules -------------------------------------------------------------------


def _put_rule(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """Create OR update -- one call for both, which is what the provider drives
    for a create and for every in-place change."""
    bus, name = _bus_of(payload), payload["Name"]
    if _bus(stores, env, bus) is None:
        return _not_found(f"Event bus {bus} does not exist.")
    existing = _rule(stores, env, bus, name)
    record = {
        "name": name,
        "arn": rule_arn(bus, name),
        "event_bus_name": bus,
        "state": payload.get("State") or DEFAULT_STATE,
        "description": payload.get("Description"),
        "schedule_expression": payload.get("ScheduleExpression"),
        "event_pattern": payload.get("EventPattern"),
        "role_arn": payload.get("RoleArn"),
        "created_at": (existing or {}).get("created_at") or now,
    }
    stores.eventsctl.set(env, _rule_key(bus, name), record)
    tags = _tags_from_list(payload.get("Tags"))
    if tags:
        _set_tags(stores, env, record["arn"], {**_tags_for(stores, env, record["arn"]), **tags})
    return _json({"RuleArn": record["arn"]})


def _describe_rule(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    bus, name = _bus_of(payload), payload["Name"]
    record = _rule(stores, env, bus, name)
    if record is None:
        return _not_found(f"Rule {name} does not exist on EventBus {bus}.")
    return _json({**_rule_json(record), "CreatedBy": ACCOUNT})


def _delete_rule(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """Real EventBridge refuses to delete a rule that still has targets unless
    `Force` is set, and terraform's own graph removes the targets first -- so
    honouring it costs a clean destroy nothing and stops a hand-rolled caller
    from orphaning target records that nothing would ever clean up."""
    bus, name = _bus_of(payload), payload["Name"]
    if _rule(stores, env, bus, name) is None:
        return _not_found(f"Rule {name} does not exist on EventBus {bus}.")
    targets = _targets(stores, env, bus, name)
    if targets and not payload.get("Force"):
        return _invalid(f"Rule can't be deleted since it has targets: {sorted(t['Id'] for t in targets)}")
    arn = rule_arn(bus, name)
    stores.eventsctl.delete(env, _rule_key(bus, name))
    stores.eventsctl.delete(env, _targets_key(bus, name))
    _set_tags(stores, env, arn, {})
    return _json({})


def _list_rules(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    bus, prefix = _bus_of(payload), payload.get("NamePrefix") or ""
    matched = [
        r for r in _rules(stores, env)
        if r["event_bus_name"] == bus and r["name"].startswith(prefix)
    ]
    matched.sort(key=lambda r: r["name"])
    limit = int(payload.get("Limit") or DEFAULT_LIMIT)
    return _json({"Rules": [_rule_json(r) for r in matched[:limit]]})


def _set_state(payload: dict, env: str, stores: SynthStores, state: str) -> Response:
    bus, name = _bus_of(payload), payload["Name"]
    record = _rule(stores, env, bus, name)
    if record is None:
        return _not_found(f"Rule {name} does not exist on EventBus {bus}.")
    stores.eventsctl.set(env, _rule_key(bus, name), {**record, "state": state})
    return _json({})


def _enable_rule(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _set_state(payload, env, stores, DEFAULT_STATE)


def _disable_rule(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _set_state(payload, env, stores, STATE_DISABLED)


# --- targets -----------------------------------------------------------------


def _put_targets(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """Upsert by target `Id`, exactly like real PutTargets -- re-sending an
    existing id REPLACES that target rather than duplicating it, which is what
    makes a re-apply of an unchanged `aws_cloudwatch_event_target` a no-op."""
    bus, rule = _bus_of(payload), payload["Rule"]
    if _rule(stores, env, bus, rule) is None:
        return _not_found(f"Rule {rule} does not exist on EventBus {bus}.")
    incoming = [t for t in (payload.get("Targets") or []) if isinstance(t, dict) and t.get("Id")]
    replaced = {t["Id"] for t in incoming}
    kept = [t for t in _targets(stores, env, bus, rule) if t["Id"] not in replaced]
    stores.eventsctl.set(env, _targets_key(bus, rule), [*kept, *incoming])
    return _json({"FailedEntryCount": 0, "FailedEntries": []})


def _remove_targets(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    bus, rule = _bus_of(payload), payload["Rule"]
    if _rule(stores, env, bus, rule) is None:
        return _not_found(f"Rule {rule} does not exist on EventBus {bus}.")
    remove = set(payload.get("Ids") or [])
    kept = [t for t in _targets(stores, env, bus, rule) if t["Id"] not in remove]
    stores.eventsctl.set(env, _targets_key(bus, rule), kept)
    return _json({"FailedEntryCount": 0, "FailedEntries": []})


def _list_targets_by_rule(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    bus, rule = _bus_of(payload), payload["Rule"]
    if _rule(stores, env, bus, rule) is None:
        return _not_found(f"Rule {rule} does not exist on EventBus {bus}.")
    limit = int(payload.get("Limit") or DEFAULT_LIMIT)
    return _json({"Targets": _targets(stores, env, bus, rule)[:limit]})


# --- event buses -------------------------------------------------------------


def _create_event_bus(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload["Name"]
    # `_bus` answers for the default bus too, so "you cannot create `default`"
    # needs no case of its own.
    if _bus(stores, env, name) is not None:
        return _already_exists(f"Event bus {name} already exists.")
    record = {
        "name": name,
        "arn": bus_arn(name),
        "description": payload.get("Description"),
        "creation_time": now,
        "last_modified_time": now,
    }
    stores.eventsctl.set(env, _bus_key(name), record)
    _set_tags(stores, env, record["arn"], _tags_from_list(payload.get("Tags")))
    return _json({"EventBusArn": record["arn"], **_drop_none({"Description": record["description"]})})


def _describe_event_bus(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """`Name` is OPTIONAL here (botocore's own `required` list is empty) and
    omitting it means the default bus -- which is never stored, so it is
    synthesized rather than looked up."""
    name = bare_name(str(payload.get("Name") or DEFAULT_BUS))
    record = _bus(stores, env, name)
    if record is None:
        return _not_found(f"Event bus {name} does not exist.")
    return _json(_bus_json(record))


def _delete_event_bus(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """The default bus cannot be deleted (real EventBridge's own rule) -- and
    here that is load-bearing rather than cosmetic: it is the bus every rule
    lands on, and it has no record to delete, so a "successful" delete would
    report a teardown that never happened."""
    name = payload["Name"]
    if name == DEFAULT_BUS:
        return _invalid("Cannot delete the default event bus.")
    record = _bus(stores, env, name)
    if record is None:
        return _not_found(f"Event bus {name} does not exist.")
    stores.eventsctl.delete(env, _bus_key(name))
    _set_tags(stores, env, record["arn"], {})
    return _json({})


def _list_event_buses(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    prefix = payload.get("NamePrefix") or ""
    matched = sorted(
        (b for b in _buses(stores, env) if b["name"].startswith(prefix)),
        key=lambda b: b["name"],
    )
    limit = int(payload.get("Limit") or DEFAULT_LIMIT)
    return _json({"EventBuses": [_bus_json(b) for b in matched[:limit]]})


# --- tags (ARN-only, like elbv2's) -------------------------------------------


def _tagged(payload: dict, env: str, stores: SynthStores) -> tuple[str, dict | None]:
    arn = str(payload["ResourceARN"])
    return arn, _by_arn(stores, env, arn)


def _list_tags(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    arn, record = _tagged(payload, env, stores)
    if record is None:
        return _not_found(f"Rule {bare_name(arn)} does not exist.")
    return _json({"Tags": _tags_to_list(_tags_for(stores, env, arn))})


def _tag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    arn, record = _tagged(payload, env, stores)
    if record is None:
        return _not_found(f"Rule {bare_name(arn)} does not exist.")
    _set_tags(stores, env, arn, {**_tags_for(stores, env, arn), **_tags_from_list(payload.get("Tags"))})
    return _json({})


def _untag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    arn, record = _tagged(payload, env, stores)
    if record is None:
        return _not_found(f"Rule {bare_name(arn)} does not exist.")
    remove = set(payload.get("TagKeys") or [])
    _set_tags(stores, env, arn, {k: v for k, v in _tags_for(stores, env, arn).items() if k not in remove})
    return _json({})


# --- the data plane, which is honest about not existing yet ------------------

PUT_EVENTS_UNBUILT = (
    "odin's gateway records EventBridge rules and targets but does not deliver events yet: "
    "nothing reads a rule's targets and invokes them, so accepting this PutEvents would report "
    "a delivery that never happens. The rule/target control plane is real; the dispatcher is not "
    "built. Invoke the target directly (for a lambda target, lambda:Invoke) until it is."
)


def _put_events(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """Deliberately NOT `{"FailedEntryCount": 0}`.

    That answer is what real EventBridge sends for an ACCEPTED batch, and it
    would be true of the bytes -- odin could store these entries and report
    zero failures without lying about a single field. It would still be the
    wrong answer, because the only reason anyone calls PutEvents is to make a
    target run, and no target runs. `FailedEntryCount: 0` is read as "it will
    fire". So this fails, loudly, naming the missing half."""
    return errors.synth_error(SERVICE, "InternalException", PUT_EVENTS_UNBUILT, 500)


# --- reads odin's own control plane uses (never the SigV4 wire) --------------


def rules(stores: SynthStores, env: str) -> list[dict]:
    """Every rule record in this env -- the read a DISPATCHER would start from.
    Deliberately here rather than on any HTTP route, the same boundary
    `ssmctl.parameter_value` keeps."""
    return _rules(stores, env)


def targets_of(stores: SynthStores, env: str, record: dict) -> list[dict]:
    """The targets attached to `record` (a rule record from `rules` above), in
    the order they were registered and exactly as terraform wrote them."""
    return _targets(stores, env, record["event_bus_name"], record["name"])


def rule_exists(stores: SynthStores, env: str, name: str, bus: str = DEFAULT_BUS) -> bool:
    return _rule(stores, env, bus, name) is not None


# --- dispatch ----------------------------------------------------------------
#
# WHICH identifiers, measured rather than guessed: exactly botocore's OWN
# `required` lists for the `events` model, restricted to the members that
# IDENTIFY a resource (`Name`/`Rule`/`ResourceARN`) -- the payload members
# (`Targets`, `Ids`, `Tags`, `TagKeys`) are not here, because an empty one is a
# legitimate no-op rather than a missing identifier.
#
# This is `logsctl._missing_identifier`'s guard, and it is here for the same
# measured reason: every handler above reads its identifier as `payload["Name"]`
# or `payload["Rule"]`, so a request that carried neither would raise a KeyError
# out of the handler and leave the gateway's last-resort 500 to answer -- an
# `InternalFailure` for what is really a malformed request. A real boto3 client
# refuses to send one (`ParamValidationError: Missing required parameter in
# input: "Name"`), so a request without one came from a raw HTTP client; it
# still gets a sentence that names what was missing.
#
# The LIST ops (ListRules, ListEventBuses) and DescribeEventBus are deliberately
# absent: a prefix-less list and a bus-less describe are both legitimate calls.
_REQUIRED: dict[str, str] = {
    "PutRule": "Name",
    "DescribeRule": "Name",
    "DeleteRule": "Name",
    "EnableRule": "Name",
    "DisableRule": "Name",
    "PutTargets": "Rule",
    "RemoveTargets": "Rule",
    "ListTargetsByRule": "Rule",
    "CreateEventBus": "Name",
    "DeleteEventBus": "Name",
    "TagResource": "ResourceARN",
    "UntagResource": "ResourceARN",
    "ListTagsForResource": "ResourceARN",
}


def _missing_identifier(op: str, payload: dict) -> str | None:
    member = _REQUIRED.get(op)
    return member if member and not str(payload.get(member) or "").strip() else None


_Handler = Callable[[dict, str, SynthStores, float], Response]

_HANDLERS: dict[str, _Handler] = {
    "PutRule": _put_rule,
    "DescribeRule": _describe_rule,
    "DeleteRule": _delete_rule,
    "ListRules": _list_rules,
    "EnableRule": _enable_rule,
    "DisableRule": _disable_rule,
    "PutTargets": _put_targets,
    "RemoveTargets": _remove_targets,
    "ListTargetsByRule": _list_targets_by_rule,
    "CreateEventBus": _create_event_bus,
    "DescribeEventBus": _describe_event_bus,
    "DeleteEventBus": _delete_event_bus,
    "ListEventBuses": _list_event_buses,
    "TagResource": _tag_resource,
    "UntagResource": _untag_resource,
    "ListTagsForResource": _list_tags,
    "PutEvents": _put_events,
}


async def pure_answer(action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """The whole EventBridge answer -- same no-backing contract as
    iamctl/logsctl/ssmctl: an unmodeled action (archives, replays, API
    destinations, PutPermission, every non-rule EventBridge API) gets a
    protocol-correct error, never a 503 and never a silent forward.

    `time.time()` rather than the `now` the caller threads in: `now` is
    `time.monotonic()` (app.py), which is an arbitrary offset from an
    unspecified epoch -- correct for the SQS delete-grace arithmetic it exists
    for, and wrong for a `CreationTime` a caller reads as a wall-clock
    timestamp. The same reason `logsctl` mints its own `_now_ms()`."""
    op = action.removeprefix(f"{SERVICE}:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error(SERVICE, "InternalException", f"odin's gateway does not model {action}.", 400)
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    missing = _missing_identifier(op, payload)
    return _invalid(f"{missing} is required") if missing else handler(payload, env, stores, time.time())
