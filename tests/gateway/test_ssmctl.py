"""W2.4 -- gateway/models/ssmctl.py: the SSM Parameter Store
(`aws_ssm_parameter`) -- Put/Get/GetParameters/GetParametersByPath/
DescribeParameters/Delete + tag CRUD, including `SecureString` (stored exactly
like `String`: odin has no KMS, and this file's own tests say so out loud).

Same test method as W2.1's logsctl: REAL boto3-signed captures, every response
round-tripped through botocore's OWN parser for the REAL `ssm` service model,
every call routed through classify() -> (await synth.pure_answer()).
"""
from __future__ import annotations

from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.models import ssmctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
NAME = "/odin/api-key"
VALUE = "param-s3cr3t-99"


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("ssm")
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


async def _answer(stores: SynthStores, req) -> Response:
    path, query = split_url(req.url)
    classified = classify("ssm", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an SSM request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "ssm is all-synth: pure_answer must never fall through"
    return response


async def _put(stores, sink, ssm, name=NAME, value=VALUE, param_type="SecureString", **kwargs) -> Response:
    return await _answer(stores, sink.call(lambda: ssm.put_parameter(Name=name, Value=value, Type=param_type, **kwargs)))


async def _get(stores, sink, ssm, name=NAME, **kwargs) -> Response:
    return await _answer(stores, sink.call(lambda: ssm.get_parameter(Name=name, **kwargs)))


async def _describe(stores, sink, ssm, **kwargs) -> Response:
    return await _answer(stores, sink.call(lambda: ssm.describe_parameters(**kwargs)))


# --- write / read -----------------------------------------------------------


async def test_put_parameter_returns_version_one_and_the_tier(stores, sink, ssm):
    parsed = _parse("PutParameter", await _put(stores, sink, ssm))

    assert parsed["Version"] == 1
    assert parsed["Tier"] == "Standard"
    assert ssmctl.parameter_exists(stores, ENV, NAME)


async def test_get_parameter_returns_the_value_arn_and_type(stores, sink, ssm):
    await _put(stores, sink, ssm)
    parameter = _parse("GetParameter", await _get(stores, sink, ssm, WithDecryption=True))["Parameter"]

    assert parameter["Value"] == VALUE
    assert parameter["Type"] == "SecureString"
    assert parameter["Name"] == NAME
    assert parameter["ARN"] == ssmctl.parameter_arn(NAME)
    assert parameter["DataType"] == "text"


async def test_a_securestring_is_stored_and_read_exactly_like_a_string(stores, sink, ssm):
    """The recorded limit, asserted rather than implied: there is no KMS in
    odin, so `SecureString` buys the file mode and nothing else -- the value
    sits in the 0600 sidecar as cleartext and reads back identically with or
    without WithDecryption."""
    await _put(stores, sink, ssm, name="/a/secure", param_type="SecureString", value="same-bytes")
    await _put(stores, sink, ssm, name="/a/plain", param_type="String", value="same-bytes")

    with_decryption = _parse("GetParameter", await _get(stores, sink, ssm, name="/a/secure", WithDecryption=True))
    without = _parse("GetParameter", await _get(stores, sink, ssm, name="/a/secure", WithDecryption=False))

    assert with_decryption["Parameter"]["Value"] == without["Parameter"]["Value"] == "same-bytes"
    assert ssmctl.parameter_value(stores, ENV, "/a/secure") == ssmctl.parameter_value(stores, ENV, "/a/plain")


async def test_put_parameter_without_overwrite_refuses_to_clobber(stores, sink, ssm):
    await _put(stores, sink, ssm)
    parsed = _parse("PutParameter", await _put(stores, sink, ssm, value="other"), error=True)

    assert parsed["Error"]["Code"] == "ParameterAlreadyExists"
    assert ssmctl.parameter_value(stores, ENV, NAME) == VALUE


async def test_overwrite_bumps_the_version(stores, sink, ssm):
    await _put(stores, sink, ssm)
    parsed = _parse("PutParameter", await _put(stores, sink, ssm, value="rotated", Overwrite=True))
    parameter = _parse("GetParameter", await _get(stores, sink, ssm))["Parameter"]

    assert parsed["Version"] == 2
    assert parameter["Value"] == "rotated"
    assert parameter["Version"] == 2


async def test_an_invalid_type_is_a_validation_exception(stores, sink, ssm):
    # botocore won't send an invalid enum, so this one request is hand-shaped
    # (the guard exists for a non-boto3 caller / a future direct call).
    response = await ssmctl.pure_answer(
        "ssm:PutParameter", NAME, ENV, b'{"Name": "x", "Value": "v", "Type": "Nope"}', stores, 0.0,
    )

    assert _parse("PutParameter", response, error=True)["Error"]["Code"] == "ValidationException"


async def test_get_a_missing_parameter_is_parameter_not_found(stores, sink, ssm):
    parsed = _parse("GetParameter", await _get(stores, sink, ssm), error=True)

    assert parsed["Error"]["Code"] == "ParameterNotFound"


async def test_a_root_level_leading_slash_is_optional_both_ways(stores, sink, ssm):
    """AWS's own equivalence for root-level parameters (`db-url` == `/db-url`)
    -- and the same rule classify.py applies, so an IAM edge to either form
    gates the other."""
    await _put(stores, sink, ssm, name="db-url", value="postgres://x")
    parameter = _parse("GetParameter", await _get(stores, sink, ssm, name="/db-url"))["Parameter"]

    # The NAME echoed back is what the writer sent, never a rewritten form
    # (a rewritten one reads as drift in terraform's state).
    assert parameter["Name"] == "db-url"
    assert parameter["Value"] == "postgres://x"
    assert ssmctl.canonical_name("/db-url") == ssmctl.canonical_name("db-url") == "db-url"
    assert ssmctl.canonical_name("odin/x") == ssmctl.canonical_name("/odin/x") == "/odin/x"


async def test_get_parameters_splits_found_from_invalid(stores, sink, ssm):
    await _put(stores, sink, ssm, name="/a/one", value="1")
    parsed = _parse(
        "GetParameters",
        await _answer(stores, sink.call(lambda: ssm.get_parameters(Names=["/a/one", "/a/missing"], WithDecryption=True))),
    )

    assert [p["Name"] for p in parsed["Parameters"]] == ["/a/one"]
    assert parsed["InvalidParameters"] == ["/a/missing"]


async def test_get_parameters_by_path_is_one_level_unless_recursive(stores, sink, ssm):
    await _put(stores, sink, ssm, name="/app/db", value="1")
    await _put(stores, sink, ssm, name="/app/nested/key", value="2")
    await _put(stores, sink, ssm, name="/other/x", value="3")

    shallow = _parse("GetParametersByPath", await _answer(stores, sink.call(lambda: ssm.get_parameters_by_path(Path="/app"))))
    deep = _parse(
        "GetParametersByPath",
        await _answer(stores, sink.call(lambda: ssm.get_parameters_by_path(Path="/app", Recursive=True))),
    )

    assert [p["Name"] for p in shallow["Parameters"]] == ["/app/db"]
    assert [p["Name"] for p in deep["Parameters"]] == ["/app/db", "/app/nested/key"]


async def test_delete_parameter_removes_it_and_its_tags(stores, sink, ssm):
    await _put(stores, sink, ssm, Tags=[{"Key": "odin:node", "Value": NAME}])
    await _answer(stores, sink.call(lambda: ssm.delete_parameter(Name=NAME)))

    assert not ssmctl.parameter_exists(stores, ENV, NAME)
    assert stores.tags.get(ENV, f"ssm:{NAME}") == {}


async def test_delete_a_missing_parameter_is_parameter_not_found(stores, sink, ssm):
    parsed = _parse("DeleteParameter", await _answer(stores, sink.call(lambda: ssm.delete_parameter(Name=NAME))), error=True)

    assert parsed["Error"]["Code"] == "ParameterNotFound"


# --- the metadata + tag reads the TF provider's refresh depends on ---------


async def test_describe_parameters_carries_metadata_but_never_the_value(stores, sink, ssm):
    """Real AWS's own boundary: DescribeParameters is where the provider reads
    description/tier/allowed_pattern/key_id from, and it carries NO value."""
    await _put(stores, sink, ssm, Description="the api key", AllowedPattern="", Tier="Standard")
    filters = [{"Key": "Name", "Option": "Equals", "Values": [NAME]}]
    parsed = _parse("DescribeParameters", await _describe(stores, sink, ssm, ParameterFilters=filters))
    metadata = parsed["Parameters"][0]

    assert metadata["Name"] == NAME
    assert metadata["Description"] == "the api key"
    assert metadata["Tier"] == "Standard"
    assert metadata["Version"] == 1
    assert VALUE.encode() not in (await _describe(stores, sink, ssm, ParameterFilters=filters)).body


async def test_describe_parameters_name_filter_is_exact_and_canonical(stores, sink, ssm):
    await _put(stores, sink, ssm, name="/odin/api-key", value="1")
    await _put(stores, sink, ssm, name="/odin/api-key-2", value="2")
    equals = _parse("DescribeParameters", await _describe(
        stores, sink, ssm, ParameterFilters=[{"Key": "Name", "Option": "Equals", "Values": ["/odin/api-key"]}],
    ))
    begins = _parse("DescribeParameters", await _describe(
        stores, sink, ssm, ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": ["/odin/api"]}],
    ))

    assert [p["Name"] for p in equals["Parameters"]] == ["/odin/api-key"]
    assert [p["Name"] for p in begins["Parameters"]] == ["/odin/api-key", "/odin/api-key-2"]


async def test_describe_parameters_fails_closed_on_an_unmodeled_filter(stores, sink, ssm):
    await _put(stores, sink, ssm)
    parsed = _parse("DescribeParameters", await _describe(
        stores, sink, ssm, ParameterFilters=[{"Key": "Label", "Option": "Equals", "Values": ["prod"]}],
    ))

    assert parsed["Parameters"] == []


async def test_tags_round_trip_from_put_through_list_and_removal(stores, sink, ssm):
    await _put(stores, sink, ssm, Tags=[{"Key": "odin:node", "Value": NAME}])
    listed = _parse("ListTagsForResource", await _answer(stores, sink.call(
        lambda: ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=NAME),
    )))
    await _answer(stores, sink.call(lambda: ssm.add_tags_to_resource(
        ResourceType="Parameter", ResourceId=NAME, Tags=[{"Key": "env", "Value": "dev"}],
    )))
    added = _parse("ListTagsForResource", await _answer(stores, sink.call(
        lambda: ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=NAME),
    )))
    await _answer(stores, sink.call(lambda: ssm.remove_tags_from_resource(
        ResourceType="Parameter", ResourceId=NAME, TagKeys=["env"],
    )))
    removed = _parse("ListTagsForResource", await _answer(stores, sink.call(
        lambda: ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=NAME),
    )))

    assert listed["TagList"] == [{"Key": "odin:node", "Value": NAME}]
    assert added["TagList"] == [{"Key": "env", "Value": "dev"}, {"Key": "odin:node", "Value": NAME}]
    assert removed["TagList"] == [{"Key": "odin:node", "Value": NAME}]


async def test_an_unmodeled_action_is_a_protocol_correct_error(stores, sink, ssm):
    await _put(stores, sink, ssm)
    parsed = _parse(
        "LabelParameterVersion",
        await _answer(stores, sink.call(lambda: ssm.label_parameter_version(Name=NAME, Labels=["prod"]))),
        error=True,
    )

    assert parsed["Error"]["Code"] == "InvalidAction"
