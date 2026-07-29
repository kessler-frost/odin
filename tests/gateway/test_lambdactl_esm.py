"""`aws_lambda_event_source_mapping` -- the SQS -> Lambda trigger's control
plane, which did not exist and so failed `tofu apply` outright.

Same method as the rest of the gateway suite: REAL boto3-signed captures
through the REAL classify() -> pure_answer path, every response round-tripped
through botocore's OWN rest-json parser for the REAL lambda service model. The
routes were taken from that same model rather than remembered.
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
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "evdisp-esm"
FUNCTION = "worker"
FUNCTION_ARN = f"arn:aws:lambda:us-east-1:000000000000:function:{FUNCTION}"
QUEUE_ARN = "arn:aws:sqs:us-east-1:000000000000:jobs"


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("lambda")
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, model.operation_model(operation).output_shape)
    if error:
        assert response.status_code >= 300, "an error response must not carry a 2xx status"
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, f"fn:{FUNCTION}", {
        "function_name": FUNCTION, "function_arn": FUNCTION_ARN, "state": "Active",
        "last_update_status": "Successful", "last_invocation_error": None, "timeout": 3,
    })
    return stores


async def _answer(stores: SynthStores, req) -> Response:
    path, query = split_url(req.url)
    classified = classify("lambda", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an event-source-mapping request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0, query=query)
    assert response is not None, "lambda is all-synth: pure_answer must never fall through"
    return response


async def _call(stores, sink, fn) -> Response:
    return await _answer(stores, sink.call(fn))


# --- the routes exist at all -------------------------------------------------


async def test_every_mapping_route_classifies_rather_than_denying(stores, sink, awslambda):
    """The wall this feature was behind: none of these five paths matched a
    route, so `classify` returned None and the gateway answered
    `unmappable-action` -- a `tofu apply` failure with no useful reason."""
    cases = [
        (lambda: awslambda.create_event_source_mapping(
            EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION), "CreateEventSourceMapping"),
        (lambda: awslambda.get_event_source_mapping(UUID="u-1"), "GetEventSourceMapping"),
        (lambda: awslambda.update_event_source_mapping(UUID="u-1", Enabled=False), "UpdateEventSourceMapping"),
        (lambda: awslambda.delete_event_source_mapping(UUID="u-1"), "DeleteEventSourceMapping"),
        (lambda: awslambda.list_event_source_mappings(), "ListEventSourceMappings"),
    ]
    for call, op in cases:
        req = sink.call(call)
        path, query = split_url(req.url)
        classified = classify("lambda", req.method, path, query, req.headers, req.body)
        assert classified is not None, f"{op} is unmappable"
        assert classified[0] == f"lambda:{op}"


async def test_a_mapping_uuid_is_never_classified_as_a_function_name(stores, sink, awslambda):
    """The `{uuid}` group is named `mapping`, not `name`. Called `name`, the
    UUID would be handed to `lambdactl` as a FUNCTION name and every one of
    these would 404 against a function that does not exist -- and a policy
    written for a function would silently also grant mapping management."""
    req = sink.call(lambda: awslambda.get_event_source_mapping(UUID="u-abc"))
    path, query = split_url(req.url)
    assert classify("lambda", req.method, path, query, req.headers, req.body) == (
        "lambda:GetEventSourceMapping", "u-abc")


async def test_create_resolves_a_full_function_arn_to_the_bare_name(stores, sink, awslambda):
    """terraform sends `aws_lambda_function.x.arn`. Without the reduction the
    same function is one resource under CreateFunction and a different one
    here, and no IAM policy could cover both."""
    req = sink.call(lambda: awslambda.create_event_source_mapping(
        EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION_ARN))
    path, query = split_url(req.url)
    assert classify("lambda", req.method, path, query, req.headers, req.body) == (
        "lambda:CreateEventSourceMapping", FUNCTION)


# --- the round trip ----------------------------------------------------------


async def test_create_then_get_round_trips_through_botocores_own_parser(stores, sink, awslambda):
    created = _parse("CreateEventSourceMapping", await _call(stores, sink, lambda: (
        awslambda.create_event_source_mapping(
            EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION_ARN, BatchSize=5, Enabled=True)
    )))
    assert created["EventSourceArn"] == QUEUE_ARN
    assert created["FunctionArn"] == FUNCTION_ARN
    assert created["State"] == "Enabled"
    assert created["BatchSize"] == 5
    # `LastModified` is a rest-json `timestamp` with no explicit format, so it
    # is a unix-epoch NUMBER on the wire -- unlike FunctionConfiguration's
    # formatted string. If odin sent the string form this parse would raise.
    assert created["LastModified"].year >= 2020

    uuid_ = created["UUID"]
    fetched = _parse("GetEventSourceMapping", await _call(
        stores, sink, lambda: awslambda.get_event_source_mapping(UUID=uuid_)))
    assert fetched["UUID"] == uuid_
    assert fetched["State"] == "Enabled"


async def test_update_disables_a_mapping_and_delete_removes_it(stores, sink, awslambda):
    created = _parse("CreateEventSourceMapping", await _call(stores, sink, lambda: (
        awslambda.create_event_source_mapping(EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION)
    )))
    uuid_ = created["UUID"]

    updated = _parse("UpdateEventSourceMapping", await _call(
        stores, sink, lambda: awslambda.update_event_source_mapping(UUID=uuid_, Enabled=False)))
    assert updated["State"] == "Disabled", "this is what stops the dispatcher draining it"

    deleted = _parse("DeleteEventSourceMapping", await _call(
        stores, sink, lambda: awslambda.delete_event_source_mapping(UUID=uuid_)))
    assert deleted["State"] == "Deleting"

    gone = await _call(stores, sink, lambda: awslambda.get_event_source_mapping(UUID=uuid_))
    assert _parse("GetEventSourceMapping", gone, error=True)["Error"]["Code"] == "ResourceNotFoundException"


async def test_list_is_filtered_by_function_name_from_the_query_string(stores, sink, awslambda):
    stores.lambdactl.set(ENV, "fn:other", {
        "function_name": "other", "function_arn": "arn:aws:lambda:us-east-1:000000000000:function:other",
        "state": "Active", "last_update_status": "Successful", "last_invocation_error": None, "timeout": 3,
    })
    await _call(stores, sink, lambda: awslambda.create_event_source_mapping(
        EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION))
    await _call(stores, sink, lambda: awslambda.create_event_source_mapping(
        EventSourceArn="arn:aws:sqs:us-east-1:000000000000:other-q", FunctionName="other"))

    listed = _parse("ListEventSourceMappings", await _call(
        stores, sink, lambda: awslambda.list_event_source_mappings(FunctionName=FUNCTION)))
    assert [m["EventSourceArn"] for m in listed["EventSourceMappings"]] == [QUEUE_ARN]


# --- the refusals ------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "arn:aws:kinesis:us-east-1:000000000000:stream/events",
    "arn:aws:dynamodb:us-east-1:000000000000:table/t/stream/2024",
    "arn:aws:kafka:us-east-1:000000000000:cluster/c/abc",
])
async def test_a_source_odin_cannot_poll_is_refused_not_stored(stores, sink, awslambda, source):
    """odin delivers a mapping by POLLING, and the only thing it can poll is an
    SQS queue in this env's backing. Stored, any of these would apply clean,
    plan clean, and never deliver a message -- the render-and-never-fire bug."""
    response = await _call(stores, sink, lambda: awslambda.create_event_source_mapping(
        EventSourceArn=source, FunctionName=FUNCTION))
    parsed = _parse("CreateEventSourceMapping", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidParameterValueException"
    assert "only source it can poll is an SQS queue" in parsed["Error"]["Message"]
    assert not [k for k in stores.lambdactl.items(ENV) if k.startswith("esm:")], "nothing may be stored"


async def test_a_mapping_for_a_function_that_does_not_exist_is_refused(stores, sink, awslambda):
    """Real AWS validates this too (`ResourceNotFoundException` is in
    CreateEventSourceMapping's own error list), so refusing is the AWS-shaped
    answer -- and it keeps every stored mapping's target resolvable to a World
    label."""
    response = await _call(stores, sink, lambda: awslambda.create_event_source_mapping(
        EventSourceArn=QUEUE_ARN, FunctionName="ghost"))
    parsed = _parse("CreateEventSourceMapping", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceNotFoundException"


async def test_a_duplicate_mapping_is_a_conflict_not_a_second_poller(stores, sink, awslambda):
    """Two mappings on one (queue, function) pair would drain the same queue
    twice per tick and invoke the function twice for one message."""
    await _call(stores, sink, lambda: awslambda.create_event_source_mapping(
        EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION))
    response = await _call(stores, sink, lambda: awslambda.create_event_source_mapping(
        EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION))
    parsed = _parse("CreateEventSourceMapping", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceConflictException"


async def test_the_stored_shape_is_the_one_records_py_validates(stores, sink, awslambda, tmp_path):
    """The written record must survive a reload through `records.validate` --
    a store this module writes and cannot read back is a bricked env."""
    await _call(stores, sink, lambda: awslambda.create_event_source_mapping(
        EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION))
    reloaded = SynthStores(tmp_path)
    mappings = [v for k, v in reloaded.lambdactl.items(ENV).items() if k.startswith("esm:")]
    assert len(mappings) == 1
    assert mappings[0]["event_source_arn"] == QUEUE_ARN


async def test_a_created_mapping_is_immediately_enabled_with_no_fake_creating_phase(stores, sink, awslambda):
    """Real AWS answers `Creating` and the provider polls, because AWS really is
    provisioning pollers. odin's poller is the reconciler tick, which already
    exists -- so `Enabled` is the truth, and inventing a transitional state
    would mean a status that has to be cleared on a timer."""
    created = _parse("CreateEventSourceMapping", await _call(stores, sink, lambda: (
        awslambda.create_event_source_mapping(EventSourceArn=QUEUE_ARN, FunctionName=FUNCTION)
    )))
    assert created["State"] == "Enabled"
    assert json.loads((await _call(
        stores, sink, lambda: awslambda.get_event_source_mapping(UUID=created["UUID"]),
    )).body)["State"] == "Enabled"
