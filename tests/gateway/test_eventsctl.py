"""gateway/models/eventsctl.py: the EventBridge control plane
(`aws_cloudwatch_event_rule` / `_target` / `_bus`) -- rules, targets, buses and
tag CRUD, plus the deliberate hole where delivery isn't built yet.

Same test method as W2.1's logsctl and W2.4's ssmctl: REAL boto3-signed
captures, every response round-tripped through botocore's OWN parser for the
REAL `events` service model, every call routed through classify() -> (await
synth.pure_answer()). A response this module invents is not a response botocore
can necessarily read, and that is the difference this harness exists to catch.
"""
from __future__ import annotations

import json
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.models import eventsctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "evgw-default"
RULE = "nightly-report"
BUS = "orders"
RULE_ARN = f"arn:aws:events:us-east-1:000000000000:rule/{RULE}"
BUS_RULE_ARN = f"arn:aws:events:us-east-1:000000000000:rule/{BUS}/{RULE}"
BUS_ARN = f"arn:aws:events:us-east-1:000000000000:event-bus/{BUS}"
TARGET = {"Id": "thumbnailer", "Arn": "arn:aws:lambda:us-east-1:000000000000:function:thumbnailer"}
OTHER_TARGET = {"Id": "archiver", "Arn": "arn:aws:sqs:us-east-1:000000000000:archive"}


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("events")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300, "an error response must not carry a 2xx status"
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


async def _answer(stores: SynthStores, req) -> Response:
    path, query = split_url(req.url)
    classified = classify("events", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an EventBridge request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "events is all-synth: pure_answer must never fall through"
    return response


async def _call(stores, sink, fn) -> Response:
    return await _answer(stores, sink.call(fn))


async def _put_rule(stores, sink, events, name=RULE, **kwargs) -> Response:
    return await _call(stores, sink, lambda: events.put_rule(Name=name, **kwargs))


# --- rules -------------------------------------------------------------------


async def test_put_rule_then_describe_round_trips_every_member(stores, sink, events):
    created = _parse("PutRule", await _put_rule(
        stores, sink, events,
        ScheduleExpression="rate(1 day)", Description="the nightly one", State="ENABLED",
    ))
    assert created["RuleArn"] == RULE_ARN

    described = _parse("DescribeRule", await _call(stores, sink, lambda: events.describe_rule(Name=RULE)))
    assert described["Name"] == RULE
    assert described["Arn"] == RULE_ARN
    assert described["ScheduleExpression"] == "rate(1 day)"
    assert described["Description"] == "the nightly one"
    assert described["State"] == "ENABLED"
    assert described["EventBusName"] == "default"


async def test_an_unset_optional_member_is_omitted_not_null(stores, sink, events):
    """Asserted on the RAW BODY, deliberately, and the reason is a measurement
    that contradicts the docstring this test was first written with.

    botocore's JSON parser DROPS an explicit null, so both wire forms parse to
    the same dict -- probed directly:

        {"Name":"r","Arn":"a","ScheduleExpression":null} -> {'Name': 'r', 'Arn': 'a'}
        {"Name":"r","Arn":"a"}                           -> {'Name': 'r', 'Arn': 'a'}

    A parsed-response assertion therefore could not tell `_drop_none` working
    from `_drop_none` deleted -- it did not (mutation M12 survived it). What
    differs is the BYTES, which is what this now reads, and the claim it makes
    is only what it can back: odin's wire matches real EventBridge's shape."""
    await _put_rule(stores, sink, events, EventPattern='{"source":["odin"]}')
    response = await _call(stores, sink, lambda: events.describe_rule(Name=RULE))
    body = json.loads(response.body)
    assert "ScheduleExpression" not in body
    assert "RoleArn" not in body
    assert body["EventPattern"] == '{"source":["odin"]}'
    # ...and the parsed view still carries what WAS set.
    described = _parse("DescribeRule", response)
    assert described["EventPattern"] == '{"source":["odin"]}'


async def test_put_rule_is_an_upsert_the_provider_can_use_for_updates(stores, sink, events):
    await _put_rule(stores, sink, events, Description="first")
    await _put_rule(stores, sink, events, Description="second")
    described = _parse("DescribeRule", await _call(stores, sink, lambda: events.describe_rule(Name=RULE)))
    assert described["Description"] == "second"
    listed = _parse("ListRules", await _call(stores, sink, lambda: events.list_rules()))
    assert len(listed["Rules"]) == 1, "an update must not create a second rule"


async def test_describe_a_rule_that_does_not_exist(stores, sink, events):
    response = await _call(stores, sink, lambda: events.describe_rule(Name="nope"))
    parsed = _parse("DescribeRule", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceNotFoundException"


async def test_enable_and_disable_move_the_rule_state(stores, sink, events):
    await _put_rule(stores, sink, events)
    await _call(stores, sink, lambda: events.disable_rule(Name=RULE))
    described = _parse("DescribeRule", await _call(stores, sink, lambda: events.describe_rule(Name=RULE)))
    assert described["State"] == "DISABLED"
    await _call(stores, sink, lambda: events.enable_rule(Name=RULE))
    described = _parse("DescribeRule", await _call(stores, sink, lambda: events.describe_rule(Name=RULE)))
    assert described["State"] == "ENABLED"


async def test_list_rules_is_scoped_by_bus_and_prefix(stores, sink, events):
    await _call(stores, sink, lambda: events.create_event_bus(Name=BUS))
    await _put_rule(stores, sink, events, name="nightly-a")
    await _put_rule(stores, sink, events, name="hourly-b")
    await _put_rule(stores, sink, events, name="nightly-c", EventBusName=BUS)

    default_bus = _parse("ListRules", await _call(stores, sink, lambda: events.list_rules()))
    assert [r["Name"] for r in default_bus["Rules"]] == ["hourly-b", "nightly-a"]

    prefixed = _parse("ListRules", await _call(stores, sink, lambda: events.list_rules(NamePrefix="nightly")))
    assert [r["Name"] for r in prefixed["Rules"]] == ["nightly-a"], "the custom bus's rule must not leak in"

    custom = _parse("ListRules", await _call(stores, sink, lambda: events.list_rules(EventBusName=BUS)))
    assert [r["Name"] for r in custom["Rules"]] == ["nightly-c"]


# --- the bus-existence guard -------------------------------------------------


async def test_a_rule_on_a_bus_that_does_not_exist_is_refused(stores, sink, events):
    """A typo'd `event_bus_name` would otherwise create a rule on a phantom bus
    -- a record that looks fine, lists fine, and can never be routed."""
    response = await _put_rule(stores, sink, events, EventBusName="typo")
    assert _parse("PutRule", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


async def test_a_rule_on_a_real_custom_bus_is_created_with_the_two_segment_arn(stores, sink, events):
    await _call(stores, sink, lambda: events.create_event_bus(Name=BUS))
    created = _parse("PutRule", await _put_rule(stores, sink, events, EventBusName=BUS))
    assert created["RuleArn"] == BUS_RULE_ARN


async def test_the_default_bus_needs_no_create(stores, sink, events):
    """It is synthesized rather than stored (`eventsctl._bus`), so a first
    PutRule on a fresh env works with no CreateEventBus in front of it."""
    assert _parse("PutRule", await _put_rule(stores, sink, events))["RuleArn"] == RULE_ARN


# --- targets -----------------------------------------------------------------


async def test_targets_round_trip_verbatim(stores, sink, events):
    """Stored exactly as terraform sent them -- anything normalized here is a
    permanent plan diff, and the dispatcher's input is the real ARN."""
    await _put_rule(stores, sink, events)
    rich = {**TARGET, "Input": '{"mode":"full"}', "RetryPolicy": {"MaximumRetryAttempts": 2}}
    put = _parse("PutTargets", await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[rich])))
    assert put["FailedEntryCount"] == 0

    listed = _parse("ListTargetsByRule", await _call(
        stores, sink, lambda: events.list_targets_by_rule(Rule=RULE)))
    assert listed["Targets"] == [rich]


async def test_put_targets_upserts_by_id_rather_than_duplicating(stores, sink, events):
    await _put_rule(stores, sink, events)
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET, OTHER_TARGET]))
    replacement = {**TARGET, "Input": '{"mode":"changed"}'}
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[replacement]))

    listed = _parse("ListTargetsByRule", await _call(
        stores, sink, lambda: events.list_targets_by_rule(Rule=RULE)))
    assert [t["Id"] for t in listed["Targets"]] == ["archiver", "thumbnailer"]
    assert listed["Targets"][1]["Input"] == '{"mode":"changed"}'


async def test_remove_targets_removes_only_the_named_ids(stores, sink, events):
    await _put_rule(stores, sink, events)
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET, OTHER_TARGET]))
    await _call(stores, sink, lambda: events.remove_targets(Rule=RULE, Ids=["archiver"]))
    listed = _parse("ListTargetsByRule", await _call(
        stores, sink, lambda: events.list_targets_by_rule(Rule=RULE)))
    assert [t["Id"] for t in listed["Targets"]] == ["thumbnailer"]


async def test_every_target_op_on_a_rule_that_does_not_exist_is_refused(stores, sink, events):
    """All THREE, not just the create. `RemoveTargets` is the one that matters
    most and was the one this file originally missed (mutation M13 survived):
    without the check it answers `FailedEntryCount: 0` for a rule that does not
    exist -- terraform reads that as "the target is gone", and a typo'd rule
    name destroys cleanly while the real targets stay attached. Same shape for
    `ListTargetsByRule`, which would report an empty target list, which is
    exactly what a rule with no targets reports."""
    for op, call in (
        ("PutTargets", lambda: events.put_targets(Rule="nope", Targets=[TARGET])),
        ("RemoveTargets", lambda: events.remove_targets(Rule="nope", Ids=["thumbnailer"])),
        ("ListTargetsByRule", lambda: events.list_targets_by_rule(Rule="nope")),
    ):
        parsed = _parse(op, await _call(stores, sink, call), error=True)
        assert parsed["Error"]["Code"] == "ResourceNotFoundException", op


# --- delete ------------------------------------------------------------------


async def test_deleting_a_rule_that_still_has_targets_is_refused(stores, sink, events):
    """Real EventBridge's own rule. Without it a rule vanishes and its target
    records survive as orphans nothing will ever read or clean up."""
    await _put_rule(stores, sink, events)
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET]))
    response = await _call(stores, sink, lambda: events.delete_rule(Name=RULE))
    parsed = _parse("DeleteRule", response, error=True)
    assert parsed["Error"]["Code"] == "ValidationException"
    assert "thumbnailer" in parsed["Error"]["Message"], "the message must name what is still attached"
    # ...and the rule really is still there.
    assert eventsctl.rule_exists(stores, ENV, RULE)


async def test_force_deletes_the_rule_and_sweeps_its_targets_and_tags(stores, sink, events):
    await _put_rule(stores, sink, events, Tags=[{"Key": "owner", "Value": "ops"}])
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET]))
    await _call(stores, sink, lambda: events.delete_rule(Name=RULE, Force=True))
    assert not eventsctl.rule_exists(stores, ENV, RULE)
    assert stores.eventsctl.get(ENV, f"targets:default:{RULE}") is None
    assert stores.tags.get(ENV, f"events:{RULE_ARN}", {}) == {}


async def test_the_ordinary_destroy_order_needs_no_force(stores, sink, events):
    """What terraform actually does: the target resource is destroyed before
    the rule it depends on, so the guard above never fires on a clean destroy."""
    await _put_rule(stores, sink, events)
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET]))
    await _call(stores, sink, lambda: events.remove_targets(Rule=RULE, Ids=[TARGET["Id"]]))
    await _call(stores, sink, lambda: events.delete_rule(Name=RULE))
    assert not eventsctl.rule_exists(stores, ENV, RULE)


# --- event buses -------------------------------------------------------------


async def test_create_describe_and_delete_a_custom_bus(stores, sink, events):
    created = _parse("CreateEventBus", await _call(stores, sink, lambda: events.create_event_bus(Name=BUS)))
    assert created["EventBusArn"] == BUS_ARN

    described = _parse("DescribeEventBus", await _call(
        stores, sink, lambda: events.describe_event_bus(Name=BUS)))
    assert (described["Name"], described["Arn"]) == (BUS, BUS_ARN)

    listed = _parse("ListEventBuses", await _call(stores, sink, lambda: events.list_event_buses()))
    assert [b["Name"] for b in listed["EventBuses"]] == [BUS]

    await _call(stores, sink, lambda: events.delete_event_bus(Name=BUS))
    gone = await _call(stores, sink, lambda: events.describe_event_bus(Name=BUS))
    assert _parse("DescribeEventBus", gone, error=True)["Error"]["Code"] == "ResourceNotFoundException"


async def test_describe_event_bus_with_no_name_answers_for_the_default(stores, sink, events):
    described = _parse("DescribeEventBus", await _call(stores, sink, lambda: events.describe_event_bus()))
    assert described["Name"] == "default"


async def test_the_default_bus_can_neither_be_created_nor_deleted(stores, sink, events):
    created = await _call(stores, sink, lambda: events.create_event_bus(Name="default"))
    assert _parse("CreateEventBus", created, error=True)["Error"]["Code"] == "ResourceAlreadyExistsException"
    deleted = await _call(stores, sink, lambda: events.delete_event_bus(Name="default"))
    assert _parse("DeleteEventBus", deleted, error=True)["Error"]["Code"] == "ValidationException"


async def test_creating_a_bus_twice_is_refused(stores, sink, events):
    await _call(stores, sink, lambda: events.create_event_bus(Name=BUS))
    again = await _call(stores, sink, lambda: events.create_event_bus(Name=BUS))
    assert _parse("CreateEventBus", again, error=True)["Error"]["Code"] == "ResourceAlreadyExistsException"


# --- tags --------------------------------------------------------------------


async def test_rule_tags_round_trip_through_the_arn_only_tag_api(stores, sink, events):
    await _put_rule(stores, sink, events, Tags=[{"Key": "owner", "Value": "ops"}])
    listed = _parse("ListTagsForResource", await _call(
        stores, sink, lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN)))
    assert listed["Tags"] == [{"Key": "owner", "Value": "ops"}]

    await _call(stores, sink, lambda: events.tag_resource(
        ResourceARN=RULE_ARN, Tags=[{"Key": "team", "Value": "core"}]))
    listed = _parse("ListTagsForResource", await _call(
        stores, sink, lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN)))
    assert [t["Key"] for t in listed["Tags"]] == ["owner", "team"]

    await _call(stores, sink, lambda: events.untag_resource(ResourceARN=RULE_ARN, TagKeys=["owner"]))
    listed = _parse("ListTagsForResource", await _call(
        stores, sink, lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN)))
    assert [t["Key"] for t in listed["Tags"]] == ["team"]


async def test_a_bus_is_taggable_through_the_same_arn_lookup(stores, sink, events):
    await _call(stores, sink, lambda: events.create_event_bus(Name=BUS))
    await _call(stores, sink, lambda: events.tag_resource(
        ResourceARN=BUS_ARN, Tags=[{"Key": "env", "Value": "dev"}]))
    listed = _parse("ListTagsForResource", await _call(
        stores, sink, lambda: events.list_tags_for_resource(ResourceARN=BUS_ARN)))
    assert listed["Tags"] == [{"Key": "env", "Value": "dev"}]


async def test_a_custom_bus_rules_tags_are_keyed_by_its_own_two_segment_arn(stores, sink, events):
    """Two rules with the SAME name on two different buses must not share a tag
    set -- the ARN is what keeps them apart, and it is the ARN the record
    itself carries, not one re-derived from the classified label."""
    await _call(stores, sink, lambda: events.create_event_bus(Name=BUS))
    await _put_rule(stores, sink, events, Tags=[{"Key": "bus", "Value": "default"}])
    await _put_rule(stores, sink, events, EventBusName=BUS, Tags=[{"Key": "bus", "Value": BUS}])

    default_tags = _parse("ListTagsForResource", await _call(
        stores, sink, lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN)))
    custom_tags = _parse("ListTagsForResource", await _call(
        stores, sink, lambda: events.list_tags_for_resource(ResourceARN=BUS_RULE_ARN)))
    assert default_tags["Tags"] == [{"Key": "bus", "Value": "default"}]
    assert custom_tags["Tags"] == [{"Key": "bus", "Value": BUS}]


async def test_tagging_an_arn_no_record_claims(stores, sink, events):
    response = await _call(stores, sink, lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN))
    assert _parse("ListTagsForResource", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


# --- persistence -------------------------------------------------------------


async def test_rules_and_targets_survive_a_reload(stores, sink, events, tmp_path):
    """The whole reason this is a JsonStore and not a dict: the gateway is
    rebuilt on every tick, and a rule that vanished with it would make every
    `tofu plan` after a restart want to create it again."""
    await _put_rule(stores, sink, events, Description="durable", Tags=[{"Key": "k", "Value": "v"}])
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET]))

    reloaded = SynthStores(tmp_path)
    described = _parse("DescribeRule", await _answer(
        reloaded, sink.call(lambda: events.describe_rule(Name=RULE))))
    assert described["Description"] == "durable"
    listed = _parse("ListTargetsByRule", await _answer(
        reloaded, sink.call(lambda: events.list_targets_by_rule(Rule=RULE))))
    assert listed["Targets"] == [TARGET]
    tags = _parse("ListTagsForResource", await _answer(
        reloaded, sink.call(lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN))))
    assert tags["Tags"] == [{"Key": "k", "Value": "v"}]


async def test_the_stored_shape_is_the_one_records_py_validates(stores, sink, events, tmp_path):
    """A round-trip through the real validator, not an assertion about it:
    `JsonStore._data` runs `records.validate` on every read, so a reload that
    succeeds IS the proof the writer and the schema agree."""
    await _call(stores, sink, lambda: events.create_event_bus(Name=BUS))
    await _put_rule(stores, sink, events, EventBusName=BUS)
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, EventBusName=BUS, Targets=[TARGET]))

    reloaded = SynthStores(tmp_path).eventsctl.items(ENV)
    assert set(reloaded) == {f"bus:{BUS}", f"rule:{BUS}:{RULE}", f"targets:{BUS}:{RULE}"}


# --- the reads odin's own dispatcher will use --------------------------------


async def test_the_internal_reads_reach_rules_and_their_targets(stores, sink, events):
    """`rules`/`targets_of` are the seam a dispatcher starts from, and they are
    deliberately not on any HTTP route."""
    await _put_rule(stores, sink, events, State="DISABLED")
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET]))
    records = eventsctl.rules(stores, ENV)
    assert [r["name"] for r in records] == [RULE]
    assert records[0]["state"] == "DISABLED", "a dispatcher has to be able to see a disabled rule"
    assert eventsctl.targets_of(stores, ENV, records[0]) == [TARGET]


# --- the hole, stated on the wire --------------------------------------------


async def test_put_events_refuses_rather_than_accepting_an_undeliverable_event(stores, sink, events):
    """The honesty rule, enforced. `{"FailedEntryCount": 0}` would be true of
    every field and false about the only thing the caller wants to know."""
    await _put_rule(stores, sink, events)
    await _call(stores, sink, lambda: events.put_targets(Rule=RULE, Targets=[TARGET]))
    response = await _call(stores, sink, lambda: events.put_events(
        Entries=[{"Source": "odin.test", "DetailType": "t", "Detail": "{}"}]))
    parsed = _parse("PutEvents", response, error=True)
    assert parsed["Error"]["Code"] == "InternalException"
    assert "does not deliver events yet" in parsed["Error"]["Message"]
    assert "FailedEntryCount" not in parsed


# --- malformed / unmodeled ---------------------------------------------------


async def test_an_unmodeled_op_gets_a_named_error_not_a_fallthrough(stores, sink, events):
    response = await _call(stores, sink, lambda: events.test_event_pattern(EventPattern="{}", Event="{}"))
    parsed = _parse("TestEventPattern", response, error=True)
    assert parsed["Error"]["Code"] == "InternalException"
    assert "events:TestEventPattern" in parsed["Error"]["Message"]


async def test_a_request_that_carries_no_identifier_says_which_one(stores):
    """Guarded once at dispatch rather than in thirteen handlers -- every one
    of them indexes its identifier directly, so without this the answer is the
    gateway's last-resort `InternalFailure` for what is really a malformed
    request. boto3 refuses to send this (`ParamValidationError`), so it can
    only arrive from a raw HTTP client."""
    for action, member in (
        ("events:PutRule", "Name"),
        ("events:PutTargets", "Rule"),
        ("events:TagResource", "ResourceARN"),
    ):
        response = await synth.pure_answer(action, "*", ENV, b"{}", stores, 0.0)
        parsed = _parse("PutRule", response, error=True)
        assert parsed["Error"]["Code"] == "ValidationException"
        assert parsed["Error"]["Message"] == f"{member} is required"


async def test_a_blank_identifier_counts_as_missing(stores):
    response = await synth.pure_answer("events:DescribeRule", "*", ENV, b'{"Name": "   "}', stores, 0.0)
    assert _parse("DescribeRule", response, error=True)["Error"]["Code"] == "ValidationException"


async def test_an_unparseable_body_does_not_reach_a_handler(stores):
    response = await synth.pure_answer("events:PutRule", "*", ENV, b"{not json", stores, 0.0)
    assert _parse("PutRule", response, error=True)["Error"]["Code"] == "ValidationException"
