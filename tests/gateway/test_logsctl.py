"""W2.1 -- gateway/models/logsctl.py: the CloudWatch Logs control plane
(`aws_cloudwatch_log_group`) and data plane (Put/Get/FilterLogEvents,
DescribeLogStreams, CreateLogStream) + the internal substrate ingestion API.

Same test method as V2b/V5a: every request is a REAL boto3-signed capture
(tests/gateway/harness.py's CaptureSink + the `logs` client fixture) and every
response round-trips through botocore's OWN parser for the REAL CloudWatch
Logs service model (`create_parser("json")`) -- proof the wire bytes are
real-AWS-shaped, not string-matched. Every call ALSO routes through
classify() -> synth.pure_answer(), exercising the `logs` branch of the
dispatch pipeline end to end.
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
from odin.gateway.models import logsctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
GROUP = "/aws/lambda/echo"
STREAM = "odin-lambda-default-echo"


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("logs")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


def _answer(stores: SynthStores, req) -> Response:
    path, query = split_url(req.url)
    classified = classify("logs", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a CloudWatch Logs request must never be unmappable"
    action, resource = classified
    response = synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "logs is all-synth: pure_answer must never fall through"
    return response


def _create_group(stores, sink, logs, name=GROUP, **kwargs) -> Response:
    req = sink.call(lambda: logs.create_log_group(logGroupName=name, **kwargs))
    return _answer(stores, req)


def _create_stream(stores, sink, logs, group=GROUP, stream=STREAM) -> Response:
    req = sink.call(lambda: logs.create_log_stream(logGroupName=group, logStreamName=stream))
    return _answer(stores, req)


def _put(stores, sink, logs, events, group=GROUP, stream=STREAM) -> dict:
    req = sink.call(lambda: logs.put_log_events(logGroupName=group, logStreamName=stream, logEvents=events))
    return _parse("PutLogEvents", _answer(stores, req))


# --- control plane: CreateLogGroup / Describe / Delete / retention -----------


def test_create_then_describe_log_group_reports_the_wildcard_arn(sink, logs, stores):
    assert _create_group(stores, sink, logs).status_code == 200
    req = sink.call(lambda: logs.describe_log_groups())
    (group,) = _parse("DescribeLogGroups", _answer(stores, req))["logGroups"]
    assert group["logGroupName"] == GROUP
    # Real CloudWatch reports the `:*` suffix; the TF provider trims it.
    assert group["arn"] == f"arn:aws:logs:us-east-1:000000000000:log-group:{GROUP}:*"
    assert group["logGroupClass"] == "STANDARD"
    assert group["metricFilterCount"] == 0
    assert group["storedBytes"] == 0
    assert "retentionInDays" not in group  # unset is OMITTED, never null


def test_create_log_group_twice_is_already_exists(sink, logs, stores):
    _create_group(stores, sink, logs)
    response = _create_group(stores, sink, logs)
    assert response.status_code == 400
    assert _parse("CreateLogGroup", response, error=True)["Error"]["Code"] == "ResourceAlreadyExistsException"


def test_create_log_group_adopts_a_substrate_auto_created_group(sink, logs, stores):
    """Deviation 2 (logsctl's docstring): a lambda invoked BEFORE its log
    group was drawn auto-creates the group -- a later Apply must adopt it, not
    wedge forever on AlreadyExists. The already-ingested events survive."""
    logsctl.ingest(stores, ENV, GROUP, STREAM, ["from the substrate"])
    assert _create_group(stores, sink, logs).status_code == 200
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 10)] == ["from the substrate"]
    # ...and now it's a REAL group: a second create errors like AWS.
    assert _create_group(stores, sink, logs).status_code == 400


def test_describe_log_groups_filters_by_prefix(sink, logs, stores):
    _create_group(stores, sink, logs, name="/aws/lambda/a")
    _create_group(stores, sink, logs, name="/ecs/web")
    req = sink.call(lambda: logs.describe_log_groups(logGroupNamePrefix="/aws/"))
    parsed = _parse("DescribeLogGroups", _answer(stores, req))
    assert [g["logGroupName"] for g in parsed["logGroups"]] == ["/aws/lambda/a"]


def test_put_and_delete_retention_policy_round_trip(sink, logs, stores):
    _create_group(stores, sink, logs)
    put = sink.call(lambda: logs.put_retention_policy(logGroupName=GROUP, retentionInDays=14))
    assert _answer(stores, put).status_code == 200
    req = sink.call(lambda: logs.describe_log_groups())
    (group,) = _parse("DescribeLogGroups", _answer(stores, req))["logGroups"]
    assert group["retentionInDays"] == 14

    delete = sink.call(lambda: logs.delete_retention_policy(logGroupName=GROUP))
    assert _answer(stores, delete).status_code == 200
    req = sink.call(lambda: logs.describe_log_groups())
    (group,) = _parse("DescribeLogGroups", _answer(stores, req))["logGroups"]
    assert "retentionInDays" not in group


def test_retention_policy_on_unknown_group_is_not_found(sink, logs, stores):
    req = sink.call(lambda: logs.put_retention_policy(logGroupName="/ghost", retentionInDays=1))
    response = _answer(stores, req)
    assert response.status_code == 400
    assert _parse("PutRetentionPolicy", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


def test_delete_log_group_removes_streams_events_and_tags(sink, logs, stores):
    _create_group(stores, sink, logs, tags={"team": "infra"})
    _create_stream(stores, sink, logs)
    _put(stores, sink, logs, [{"timestamp": 1000, "message": "hello"}])

    req = sink.call(lambda: logs.delete_log_group(logGroupName=GROUP))
    assert _answer(stores, req).status_code == 200
    assert stores.logsctl.items(ENV) == {}
    assert stores.tags.get(ENV, f"logs:{logsctl.group_arn(GROUP)}") == {}

    confirm = sink.call(lambda: logs.describe_log_streams(logGroupName=GROUP))
    response = _answer(stores, confirm)
    assert _parse("DescribeLogStreams", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


def test_delete_unknown_log_group_is_not_found(sink, logs, stores):
    req = sink.call(lambda: logs.delete_log_group(logGroupName="/ghost"))
    response = _answer(stores, req)
    assert response.status_code == 400
    assert _parse("DeleteLogGroup", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


# --- tags (the zero-drift echo: ecr/ecsctl's solved approach) ---------------


def test_create_log_group_seeds_tags_and_list_tags_for_resource_echoes_them(sink, logs, stores):
    _create_group(stores, sink, logs, tags={"env": "prod"})
    # The provider passes the TRIMMED (wildcard-less) arn as resourceArn.
    req = sink.call(lambda: logs.list_tags_for_resource(resourceArn=logsctl.group_arn(GROUP)))
    assert _parse("ListTagsForResource", _answer(stores, req))["tags"] == {"env": "prod"}


def test_tag_and_untag_resource_round_trip(sink, logs, stores):
    _create_group(stores, sink, logs)
    arn = logsctl.group_arn(GROUP)
    tag = sink.call(lambda: logs.tag_resource(resourceArn=arn, tags={"team": "infra"}))
    assert _answer(stores, tag).status_code == 200
    listed = sink.call(lambda: logs.list_tags_for_resource(resourceArn=arn))
    assert _parse("ListTagsForResource", _answer(stores, listed))["tags"] == {"team": "infra"}

    untag = sink.call(lambda: logs.untag_resource(resourceArn=arn, tagKeys=["team"]))
    assert _answer(stores, untag).status_code == 200
    listed = sink.call(lambda: logs.list_tags_for_resource(resourceArn=arn))
    assert _parse("ListTagsForResource", _answer(stores, listed))["tags"] == {}


def test_list_tags_accepts_the_wildcard_arn_form_too(sink, logs, stores):
    _create_group(stores, sink, logs, tags={"a": "b"})
    req = sink.call(lambda: logs.list_tags_for_resource(resourceArn=f"{logsctl.group_arn(GROUP)}:*"))
    assert _parse("ListTagsForResource", _answer(stores, req))["tags"] == {"a": "b"}


def test_legacy_list_tags_log_group_answers_by_group_name(sink, logs, stores):
    _create_group(stores, sink, logs, tags={"a": "b"})
    req = sink.call(lambda: logs.list_tags_log_group(logGroupName=GROUP))
    assert _parse("ListTagsLogGroup", _answer(stores, req))["tags"] == {"a": "b"}


def test_tag_resource_on_unknown_group_is_not_found(sink, logs, stores):
    req = sink.call(lambda: logs.tag_resource(resourceArn=logsctl.group_arn("/ghost"), tags={"a": "b"}))
    response = _answer(stores, req)
    assert response.status_code == 400
    assert _parse("TagResource", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


# --- data plane: streams + events ------------------------------------------


def test_create_log_stream_then_describe_log_streams(sink, logs, stores):
    _create_group(stores, sink, logs)
    assert _create_stream(stores, sink, logs).status_code == 200
    req = sink.call(lambda: logs.describe_log_streams(logGroupName=GROUP))
    (stream,) = _parse("DescribeLogStreams", _answer(stores, req))["logStreams"]
    assert stream["logStreamName"] == STREAM
    assert stream["arn"] == f"{logsctl.group_arn(GROUP)}:log-stream:{STREAM}"
    assert stream["storedBytes"] == 0


def test_create_log_stream_twice_is_already_exists(sink, logs, stores):
    _create_group(stores, sink, logs)
    _create_stream(stores, sink, logs)
    response = _create_stream(stores, sink, logs)
    assert _parse("CreateLogStream", response, error=True)["Error"]["Code"] == "ResourceAlreadyExistsException"


def test_create_log_stream_without_its_group_is_not_found(sink, logs, stores):
    response = _create_stream(stores, sink, logs)
    assert _parse("CreateLogStream", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


def test_put_then_get_log_events_round_trips_messages_in_timestamp_order(sink, logs, stores):
    _create_group(stores, sink, logs)
    _create_stream(stores, sink, logs)
    put = _put(stores, sink, logs, [
        {"timestamp": 1000, "message": "first"},
        {"timestamp": 2000, "message": "second"},
    ])
    assert put["nextSequenceToken"] == "2"

    req = sink.call(lambda: logs.get_log_events(logGroupName=GROUP, logStreamName=STREAM))
    parsed = _parse("GetLogEvents", _answer(stores, req))
    assert [(e["timestamp"], e["message"]) for e in parsed["events"]] == [(1000, "first"), (2000, "second")]
    assert parsed["nextForwardToken"] == "f/2"
    assert parsed["nextBackwardToken"] == "b/0"


def test_get_log_events_honors_start_and_end_time_and_limit(sink, logs, stores):
    _create_group(stores, sink, logs)
    _create_stream(stores, sink, logs)
    _put(stores, sink, logs, [{"timestamp": t, "message": f"m{t}"} for t in (1000, 2000, 3000)])

    windowed = sink.call(lambda: logs.get_log_events(
        logGroupName=GROUP, logStreamName=STREAM, startTime=2000, endTime=3000))
    parsed = _parse("GetLogEvents", _answer(stores, windowed))
    assert [e["message"] for e in parsed["events"]] == ["m2000"]

    # AWS's default (startFromHead=False) hands back the MOST RECENT `limit`.
    limited = sink.call(lambda: logs.get_log_events(logGroupName=GROUP, logStreamName=STREAM, limit=1))
    parsed = _parse("GetLogEvents", _answer(stores, limited))
    assert [e["message"] for e in parsed["events"]] == ["m3000"]

    from_head = sink.call(lambda: logs.get_log_events(
        logGroupName=GROUP, logStreamName=STREAM, limit=1, startFromHead=True))
    parsed = _parse("GetLogEvents", _answer(stores, from_head))
    assert [e["message"] for e in parsed["events"]] == ["m1000"]


def test_get_log_events_next_forward_token_pages_and_then_terminates(sink, logs, stores):
    _create_group(stores, sink, logs)
    _create_stream(stores, sink, logs)
    _put(stores, sink, logs, [{"timestamp": t, "message": f"m{t}"} for t in (1000, 2000)])

    first = sink.call(lambda: logs.get_log_events(
        logGroupName=GROUP, logStreamName=STREAM, limit=1, startFromHead=True))
    parsed = _parse("GetLogEvents", _answer(stores, first))
    assert [e["message"] for e in parsed["events"]] == ["m1000"]
    token = parsed["nextForwardToken"]

    second = sink.call(lambda: logs.get_log_events(
        logGroupName=GROUP, logStreamName=STREAM, limit=1, nextToken=token))
    parsed = _parse("GetLogEvents", _answer(stores, second))
    assert [e["message"] for e in parsed["events"]] == ["m2000"]

    third = sink.call(lambda: logs.get_log_events(
        logGroupName=GROUP, logStreamName=STREAM, limit=1, nextToken=parsed["nextForwardToken"]))
    parsed = _parse("GetLogEvents", _answer(stores, third))
    assert parsed["events"] == []  # a paginator stops here rather than looping


def test_put_log_events_to_a_missing_group_or_stream_is_not_found(sink, logs, stores):
    missing_group = sink.call(lambda: logs.put_log_events(
        logGroupName="/ghost", logStreamName=STREAM, logEvents=[{"timestamp": 1, "message": "x"}]))
    response = _answer(stores, missing_group)
    assert _parse("PutLogEvents", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"

    _create_group(stores, sink, logs)
    missing_stream = sink.call(lambda: logs.put_log_events(
        logGroupName=GROUP, logStreamName="nope", logEvents=[{"timestamp": 1, "message": "x"}]))
    response = _answer(stores, missing_stream)
    assert _parse("PutLogEvents", response, error=True)["Error"]["Code"] == "ResourceNotFoundException"


def test_filter_log_events_across_streams_with_a_substring_pattern(sink, logs, stores):
    _create_group(stores, sink, logs)
    _create_stream(stores, sink, logs, stream="task-a")
    _create_stream(stores, sink, logs, stream="task-b")
    _put(stores, sink, logs, [{"timestamp": 1000, "message": "INFO started"}], stream="task-a")
    _put(stores, sink, logs, [{"timestamp": 2000, "message": "ERROR boom"}], stream="task-b")

    every = sink.call(lambda: logs.filter_log_events(logGroupName=GROUP))
    parsed = _parse("FilterLogEvents", _answer(stores, every))
    assert [(e["logStreamName"], e["message"]) for e in parsed["events"]] == [
        ("task-a", "INFO started"), ("task-b", "ERROR boom"),
    ]
    assert all(e["eventId"] for e in parsed["events"])

    matched = sink.call(lambda: logs.filter_log_events(logGroupName=GROUP, filterPattern='"ERROR"'))
    parsed = _parse("FilterLogEvents", _answer(stores, matched))
    assert [e["message"] for e in parsed["events"]] == ["ERROR boom"]

    by_stream = sink.call(lambda: logs.filter_log_events(logGroupName=GROUP, logStreamNames=["task-a"]))
    parsed = _parse("FilterLogEvents", _answer(stores, by_stream))
    assert [e["message"] for e in parsed["events"]] == ["INFO started"]


def test_filter_and_describe_on_an_unknown_group_are_not_found(sink, logs, stores):
    filtered = sink.call(lambda: logs.filter_log_events(logGroupName="/ghost"))
    assert _parse("FilterLogEvents", _answer(stores, filtered), error=True)["Error"]["Code"] == "ResourceNotFoundException"
    described = sink.call(lambda: logs.describe_log_streams(logGroupName="/ghost"))
    assert _parse("DescribeLogStreams", _answer(stores, described), error=True)["Error"]["Code"] == "ResourceNotFoundException"


def test_describe_log_streams_orders_by_last_event_time_when_asked(sink, logs, stores):
    _create_group(stores, sink, logs)
    _create_stream(stores, sink, logs, stream="b-newer")
    _create_stream(stores, sink, logs, stream="a-older")
    _put(stores, sink, logs, [{"timestamp": 1000, "message": "old"}], stream="a-older")
    _put(stores, sink, logs, [{"timestamp": 9000, "message": "new"}], stream="b-newer")

    by_name = sink.call(lambda: logs.describe_log_streams(logGroupName=GROUP))
    parsed = _parse("DescribeLogStreams", _answer(stores, by_name))
    assert [s["logStreamName"] for s in parsed["logStreams"]] == ["a-older", "b-newer"]

    by_time = sink.call(lambda: logs.describe_log_streams(
        logGroupName=GROUP, orderBy="LastEventTime", descending=True))
    parsed = _parse("DescribeLogStreams", _answer(stores, by_time))
    assert [s["logStreamName"] for s in parsed["logStreams"]] == ["b-newer", "a-older"]


# --- the internal substrate ingestion API ----------------------------------


def test_ingest_auto_creates_group_and_stream_then_reads_back_over_the_wire(sink, logs, stores):
    assert logsctl.ingest(stores, ENV, GROUP, STREAM, ["line one", "line two"]) == 2
    req = sink.call(lambda: logs.get_log_events(logGroupName=GROUP, logStreamName=STREAM))
    parsed = _parse("GetLogEvents", _answer(stores, req))
    assert [e["message"] for e in parsed["events"]] == ["line one", "line two"]


def test_ingest_tail_dedups_by_cursor_across_repeated_sweeps(stores):
    text = "one\ntwo\n"
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, text) == 2
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, text) == 0  # same tail, nothing new
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, text + "three\n") == 1
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 10)] == ["one", "two", "three"]


def test_ingest_tail_is_per_stream(stores):
    logsctl.ingest_tail(stores, ENV, "/ecs/web", "task-a", "a1\n")
    logsctl.ingest_tail(stores, ENV, "/ecs/web", "task-b", "b1\n")
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, "/ecs/web", 10)] == ["a1", "b1"]


# --- ingest_tail re-synchronises on CONTENT, not on a line count -----------
#
# v0.7.1. The cursor used to be "how many lines of this stream have I seen",
# which silently assumed line N of this tail is line N of the last one. Two
# real events break that assumption, and both are covered below.


def test_ingest_tail_does_not_duplicate_when_earlier_lines_gain_neighbours(stores):
    """The exact shift v0.7.1's stderr fix caused: `docker logs` used to be
    read stdout-only, so a stream's history is stdout lines alone; the next
    read of the SAME container now returns both streams merged, and the
    already-shipped lines are no longer at the offsets a line count recorded.

    Nothing already shipped may be duplicated, and output produced AFTER the
    last ingest must still land."""
    logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "out1\nout2\nout3\n")

    merged = "out1\nERR-a\nout2\nERR-b\nout3\nERR-c\n"
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, merged) == 1

    # `ERR-c` is genuinely new (it follows the last line we had). `ERR-a`/
    # `ERR-b` are BACKLOG the merged read revealed retroactively -- history
    # odin structurally never had, deliberately not spliced in at "now".
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 10)] == [
        "out1", "out2", "out3", "ERR-c",
    ]
    # ...and the re-read is still idempotent afterwards.
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, merged) == 0


def test_ingest_tail_keeps_shipping_after_the_tail_window_slides(stores):
    """A bounded `docker logs --tail N` read slides once a container has
    printed more than N lines: the tail's FIRST line is no longer the
    stream's first line. A line-count cursor reads `lines[N:]` of an N-line
    tail -- empty -- and the stream goes permanently deaf. Anchoring on
    content re-synchronises on the overlap instead."""
    logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "l1\nl2\nl3\n")

    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "l2\nl3\nl4\n") == 1
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "l3\nl4\nl5\n") == 1
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 10)] == [
        "l1", "l2", "l3", "l4", "l5",
    ]


def test_ingest_tail_ships_everything_when_a_replacement_shares_no_lines(stores):
    logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "old1\nold2\n")
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "new1\nnew2\n") == 2
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 10)] == [
        "old1", "old2", "new1", "new2",
    ]


def test_reset_cursor_lets_a_replaced_container_reship_identical_output(stores):
    """A redeploy's fresh container often prints a byte-identical banner, which
    content anchoring would otherwise read as "already seen". `reset_cursor` is
    the explicit signal that the history before it belongs to a container that
    is gone -- it voids the ANCHOR, never the stored log."""
    logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "boot\nready\n")
    logsctl.reset_cursor(stores, ENV, GROUP, STREAM)

    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "boot\nready\n") == 2
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 10)] == [
        "boot", "ready", "boot", "ready",
    ]
    # ...and the fresh container's own re-reads dedup normally again.
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "boot\nready\nserved\n") == 1


def test_reset_cursor_on_a_stream_that_never_shipped_anything_is_harmless(stores):
    logsctl.reset_cursor(stores, ENV, GROUP, STREAM)
    assert logsctl.ingest_tail(stores, ENV, GROUP, STREAM, "first\n") == 1


def test_ring_buffer_caps_stored_events_per_group_dropping_the_oldest(stores, monkeypatch):
    monkeypatch.setattr(logsctl, "MAX_EVENTS_PER_GROUP", 3)
    logsctl.ingest(stores, ENV, GROUP, STREAM, [f"m{i}" for i in range(5)])
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 10)] == ["m2", "m3", "m4"]


def test_stored_events_returns_the_last_tail_events(stores):
    logsctl.ingest(stores, ENV, GROUP, STREAM, [f"m{i}" for i in range(5)])
    assert [e["message"] for e in logsctl.stored_events(stores, ENV, GROUP, 2)] == ["m3", "m4"]
    assert logsctl.stored_events(stores, ENV, GROUP, 0) == []
    assert logsctl.group_exists(stores, ENV, GROUP)
    assert not logsctl.group_exists(stores, ENV, "/nope")


# --- dispatch edges + persistence ------------------------------------------


def test_unmodeled_logs_action_gets_invalid_action_not_a_503(stores):
    response = synth.pure_answer("logs:PutMetricFilter", "*", ENV, b"{}", stores, 0.0)
    assert response is not None and response.status_code == 400
    assert b"InvalidAction" in response.body


def test_create_log_group_without_a_name_is_invalid_parameter(stores):
    response = synth.pure_answer("logs:CreateLogGroup", "*", ENV, json.dumps({}).encode(), stores, 0.0)
    assert response.status_code == 400
    assert b"InvalidParameterException" in response.body


def test_state_persists_at_the_logsctl_sidecar_path(sink, logs, stores, tmp_path):
    _create_group(stores, sink, logs)
    assert (tmp_path / ENV / "gateway" / "logsctl.json").exists()
