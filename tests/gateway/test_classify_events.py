"""classify() for EventBridge: (action, resource) from REAL boto3-signed
`events` requests, where `resource` is the bare RULE NAME (== the canvas label
of an `events` node) -- exactly what `policy.compile_policies` puts in an iam
edge's statement, and what `gateway/models/eventsctl.py` keys its records by.

Same capture method as test_classify_ecr/ecs/logs: the requests are whatever
boto3 actually put on the wire, never hand-built. That is load-bearing for the
central test here (`test_no_events_call_carries_dynamodbs_resourcearn_spelling`):
a hand-built body would have whatever spelling the test author believed in, and
the whole point is that odin's belief and botocore's differ by one capital
letter.
"""
from __future__ import annotations

import json

import pytest

from odin.gateway.classify import classify
from odin.gateway.policy import Statement, evaluate

from .conftest import split_url

RULE = "nightly-report"
BUS = "orders"
RULE_ARN = f"arn:aws:events:us-east-1:000000000000:rule/{RULE}"
CUSTOM_BUS_RULE_ARN = f"arn:aws:events:us-east-1:000000000000:rule/{BUS}/{RULE}"
BUS_ARN = f"arn:aws:events:us-east-1:000000000000:event-bus/{BUS}"
TARGET = {"Id": "t1", "Arn": "arn:aws:lambda:us-east-1:000000000000:function:thumbnailer"}
ENTRY = {"Source": "odin.test", "DetailType": "thing.happened", "Detail": "{}"}


def _capture(sink, call):
    return sink.call(call)


def _classify(sink, call):
    req = _capture(sink, call)
    path, query = split_url(req.url)
    return classify("events", req.method, path, query, req.headers, req.body)


# (label, the boto3 call, expected action, expected resource). EVERY op
# `eventsctl._HANDLERS` models appears here, because the failure this table
# exists to catch is per-op: an op whose identifier member odin reads under the
# wrong name classifies to `"*"`, which the OPERATOR's wildcard still allows --
# so nothing breaks until an iam edge is drawn, and then the denial is
# indistinguishable from a policy denial.
def _cases(events):
    return [
        ("PutRule", lambda: events.put_rule(Name=RULE), "events:PutRule", RULE),
        ("DescribeRule", lambda: events.describe_rule(Name=RULE), "events:DescribeRule", RULE),
        ("DeleteRule", lambda: events.delete_rule(Name=RULE), "events:DeleteRule", RULE),
        ("EnableRule", lambda: events.enable_rule(Name=RULE), "events:EnableRule", RULE),
        ("DisableRule", lambda: events.disable_rule(Name=RULE), "events:DisableRule", RULE),
        ("PutTargets", lambda: events.put_targets(Rule=RULE, Targets=[TARGET]), "events:PutTargets", RULE),
        ("RemoveTargets", lambda: events.remove_targets(Rule=RULE, Ids=["t1"]), "events:RemoveTargets", RULE),
        ("ListTargetsByRule", lambda: events.list_targets_by_rule(Rule=RULE), "events:ListTargetsByRule", RULE),
        ("TagResource", lambda: events.tag_resource(ResourceARN=RULE_ARN, Tags=[{"Key": "a", "Value": "b"}]),
         "events:TagResource", RULE),
        ("UntagResource", lambda: events.untag_resource(ResourceARN=RULE_ARN, TagKeys=["a"]),
         "events:UntagResource", RULE),
        ("ListTagsForResource", lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN),
         "events:ListTagsForResource", RULE),
        ("CreateEventBus", lambda: events.create_event_bus(Name=BUS), "events:CreateEventBus", BUS),
        ("DescribeEventBus", lambda: events.describe_event_bus(Name=BUS), "events:DescribeEventBus", BUS),
        ("DeleteEventBus", lambda: events.delete_event_bus(Name=BUS), "events:DeleteEventBus", BUS),
        ("ListRules", lambda: events.list_rules(NamePrefix="nightly"), "events:ListRules", "nightly"),
        ("PutEvents", lambda: events.put_events(Entries=[{**ENTRY, "EventBusName": BUS}]), "events:PutEvents", BUS),
    ]


def test_every_modeled_op_names_a_real_resource(sink, events):
    """The failure mode this whole module is about: a classified action that
    cannot say WHAT it is about. `"*"` is a legal fallback for a list with no
    filter, and a silent authorization hole for anything else."""
    for label, call, expected_action, expected_resource in _cases(events):
        assert _classify(sink, call) == (expected_action, expected_resource), label


def test_no_modeled_op_falls_back_to_the_wildcard(sink, events):
    """Stated separately from the table above so the assertion is legible on
    its own: none of these is allowed to resolve to `"*"`."""
    for label, call, _action, _resource in _cases(events):
        _action_out, resource = _classify(sink, call)
        assert resource != "*", f"{label} could not name its resource"


def test_no_events_call_carries_dynamodbs_resourcearn_spelling(sink, events):
    """The trap, measured on the real wire rather than argued about.

    `classify._target_resource` reads `ResourceArn` for dynamodb. EventBridge
    spells it `ResourceARN`. If `_EVENTS_ID_MEMBERS` had inherited the dynamodb
    spelling, all three tag ops would classify to `"*"` -- allowed for the
    operator's wildcard, denied for every workload edge, and reported as an
    ordinary policy denial either way.
    """
    for call in (
        lambda: events.tag_resource(ResourceARN=RULE_ARN, Tags=[{"Key": "a", "Value": "b"}]),
        lambda: events.untag_resource(ResourceARN=RULE_ARN, TagKeys=["a"]),
        lambda: events.list_tags_for_resource(ResourceARN=RULE_ARN),
    ):
        payload = json.loads(_capture(sink, call).body)
        assert "ResourceARN" in payload
        assert "ResourceArn" not in payload, "botocore changed the spelling; classify.py must follow"


def test_put_events_reads_the_bus_out_of_the_entries_not_the_top_level(sink, events):
    """`PutEvents` is the one `events` action a WORKLOAD makes, and its
    identifier is nested. A top-level `EventBusName` scan finds nothing."""
    payload = json.loads(_capture(sink, lambda: events.put_events(
        Entries=[{**ENTRY, "EventBusName": BUS}])).body)
    assert "EventBusName" not in payload, "the bus is per-entry, not top-level"
    assert payload["Entries"][0]["EventBusName"] == BUS


def test_put_events_without_a_bus_is_the_default_bus(sink, events):
    assert _classify(sink, lambda: events.put_events(Entries=[ENTRY])) == ("events:PutEvents", "default")


def test_put_events_accepts_a_bus_arn_as_well_as_a_name(sink, events):
    """`EventBusName` is documented as "the name OR ARN of the event bus", so
    both forms have to reduce to the same label an iam edge names."""
    assert _classify(sink, lambda: events.put_events(
        Entries=[{**ENTRY, "EventBusName": BUS_ARN}])) == ("events:PutEvents", BUS)


def test_a_custom_bus_rule_arn_reduces_to_the_rule_not_the_bus(sink, events):
    """`…:rule/{bus}/{rule}` is EventBridge's second rule-ARN form. Taking the
    FIRST path segment would name the bus, and every tag call on a custom-bus
    rule would then be authorized against the wrong resource."""
    assert _classify(sink, lambda: events.list_tags_for_resource(ResourceARN=CUSTOM_BUS_RULE_ARN)) == (
        "events:ListTagsForResource", RULE,
    )


def test_rule_ops_on_a_custom_bus_still_classify_to_the_rule_name(sink, events):
    """The bus scopes the STORE key; the iam resource stays the rule label, the
    same way an ecs service classifies to its own name and not its cluster's."""
    assert _classify(sink, lambda: events.put_rule(Name=RULE, EventBusName=BUS)) == ("events:PutRule", RULE)
    assert _classify(sink, lambda: events.put_targets(
        Rule=RULE, EventBusName=BUS, Targets=[TARGET])) == ("events:PutTargets", RULE)


def test_a_bare_list_falls_back_to_the_wildcard_never_none(sink, events):
    """Never None: an unmappable action denies the OPERATOR before evaluate()
    ever runs, which would kill a `tofu apply` outright."""
    assert _classify(sink, lambda: events.list_rules()) == ("events:ListRules", "*")
    assert _classify(sink, lambda: events.list_event_buses()) == ("events:ListEventBuses", "*")


def test_an_unmodeled_events_op_still_classifies(sink, events):
    """classify() is deliberately wider than eventsctl's handler table: an op
    odin does not model must still reach evaluate() (and then eventsctl's own
    named error), never the closed-world `unmappable-action` deny."""
    assert _classify(sink, lambda: events.test_event_pattern(
        EventPattern="{}", Event="{}")) == ("events:TestEventPattern", "*")


def test_an_events_request_without_an_amz_target_is_unmappable(sink, events):
    req = _capture(sink, lambda: events.describe_rule(Name=RULE))
    path, query = split_url(req.url)
    headers = {k: v for k, v in req.headers.items() if k.lower() != "x-amz-target"}
    assert classify("events", req.method, path, query, headers, req.body) is None


def test_a_body_that_is_not_json_is_unmappable(sink, events):
    req = _capture(sink, lambda: events.describe_rule(Name=RULE))
    path, query = split_url(req.url)
    assert classify("events", req.method, path, query, req.headers, b"{not json") is None


@pytest.mark.parametrize("target", ["AWSEvents", "", "PutRule"])
def test_a_malformed_amz_target_is_unmappable(sink, events, target):
    req = _capture(sink, lambda: events.describe_rule(Name=RULE))
    path, query = split_url(req.url)
    headers = {**{k: v for k, v in req.headers.items() if k.lower() != "x-amz-target"}, "X-Amz-Target": target}
    assert classify("events", req.method, path, query, headers, req.body) is None


def test_an_iam_edge_to_an_events_node_gates_the_classified_call(sink, events):
    """The whole point of the bare-rule-name convention: the statement an edge
    compiles to (resources=(<events node label>,)) matches classify()'s
    resource with no events-specific plumbing in the policy layer."""
    statements = [Statement(actions=("events:PutEvents", "events:DescribeRule"), resources=(RULE,))]
    action, resource = _classify(sink, lambda: events.describe_rule(Name=RULE))
    assert evaluate(statements, action, resource)

    other_action, other_resource = _classify(sink, lambda: events.describe_rule(Name="someone-elses-rule"))
    assert not evaluate(statements, other_action, other_resource)


def test_a_tag_call_that_could_not_name_its_rule_would_be_denied(sink, events):
    """The consequence of the trap, spelled out as an authorization outcome
    rather than as a string comparison -- this is what the user would see.

    Mutation check: point `_EVENTS_ID_MEMBERS` at dynamodb's `ResourceArn` and
    the first assertion below flips to a denial that reads exactly like "your
    edge does not grant this"."""
    statements = [Statement(actions=("events:TagResource",), resources=(RULE,))]
    action, resource = _classify(sink, lambda: events.tag_resource(
        ResourceARN=RULE_ARN, Tags=[{"Key": "a", "Value": "b"}]))
    assert evaluate(statements, action, resource) is True
    # ...and this is what a `"*"` resource would produce instead.
    assert evaluate(statements, "events:TagResource", "*") is False
