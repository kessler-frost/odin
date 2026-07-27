"""W2.4 -- gateway/models/secretsctl.py: the Secrets Manager control plane
(`aws_secretsmanager_secret`) and value plane (Get/PutSecretValue,
UpdateSecretVersionStage).

Same test method as W2.1's logsctl: every request is a REAL boto3-signed
capture (tests/gateway/harness.py's CaptureSink + the `secretsmanager` client
fixture) and every response round-trips through botocore's OWN parser for the
REAL Secrets Manager service model -- proof the wire bytes are real-AWS-shaped,
not string-matched. Every call ALSO routes through classify() ->
await synth.pure_answer(), exercising the `secretsmanager` branch of the dispatch
pipeline end to end.
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
from odin.gateway.models import secretsctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
NAME = "db-password"
VALUE = "s3cr3t-value-42"


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("secretsmanager")
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
    classified = classify("secretsmanager", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a Secrets Manager request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "secretsmanager is all-synth: pure_answer must never fall through"
    return response


async def _create(stores, sink, secretsmanager, name=NAME, **kwargs) -> Response:
    return await _answer(stores, sink.call(lambda: secretsmanager.create_secret(Name=name, **kwargs)))


async def _describe(stores, sink, secretsmanager, secret_id=NAME) -> Response:
    return await _answer(stores, sink.call(lambda: secretsmanager.describe_secret(SecretId=secret_id)))


async def _get_value(stores, sink, secretsmanager, secret_id=NAME, **kwargs) -> Response:
    return await _answer(stores, sink.call(lambda: secretsmanager.get_secret_value(SecretId=secret_id, **kwargs)))


async def _put_value(stores, sink, secretsmanager, value, secret_id=NAME, **kwargs) -> Response:
    return await _answer(
        stores, sink.call(lambda: secretsmanager.put_secret_value(SecretId=secret_id, SecretString=value, **kwargs))
    )


# --- control plane ----------------------------------------------------------


async def test_create_secret_returns_the_arn_and_stores_the_record(stores, sink, secretsmanager):
    parsed = _parse("CreateSecret", await _create(stores, sink, secretsmanager, Description="the db password"))

    assert parsed["ARN"] == secretsctl.secret_arn(NAME)
    assert parsed["Name"] == NAME
    assert secretsctl.secret_exists(stores, ENV, NAME)


async def test_create_secret_with_a_value_creates_an_awscurrent_version(stores, sink, secretsmanager):
    parsed = _parse("CreateSecret", await _create(stores, sink, secretsmanager, SecretString=VALUE))

    assert parsed["VersionId"]
    assert secretsctl.current_value(stores, ENV, NAME) == VALUE


async def test_create_secret_twice_is_resource_exists(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager)
    parsed = _parse("CreateSecret", await _create(stores, sink, secretsmanager), error=True)

    assert parsed["Error"]["Code"] == "ResourceExistsException"


async def test_describe_secret_round_trips_description_and_tags(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager, Description="the db password", Tags=[{"Key": "odin:node", "Value": NAME}])
    parsed = _parse("DescribeSecret", await _describe(stores, sink, secretsmanager))

    assert parsed["Description"] == "the db password"
    assert parsed["Tags"] == [{"Key": "odin:node", "Value": NAME}]
    assert parsed["RotationEnabled"] is False


async def test_describe_secret_omits_an_unset_description_entirely(stores, sink, secretsmanager):
    """An unset optional member must be ABSENT, not null -- the TF provider
    reads a null back as a real value and drifts on every later plan."""
    await _create(stores, sink, secretsmanager)
    parsed = _parse("DescribeSecret", await _describe(stores, sink, secretsmanager))

    assert "Description" not in parsed
    assert "KmsKeyId" not in parsed


async def test_describe_secret_accepts_the_arn_as_a_secret_id(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager)
    parsed = _parse("DescribeSecret", await _describe(stores, sink, secretsmanager, secret_id=secretsctl.secret_arn(NAME)))

    assert parsed["Name"] == NAME


async def test_describe_a_missing_secret_is_resource_not_found(stores, sink, secretsmanager):
    parsed = _parse("DescribeSecret", await _describe(stores, sink, secretsmanager), error=True)

    assert parsed["Error"]["Code"] == "ResourceNotFoundException"


async def test_update_secret_changes_the_description(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager, Description="old")
    await _answer(stores, sink.call(lambda: secretsmanager.update_secret(SecretId=NAME, Description="new")))
    parsed = _parse("DescribeSecret", await _describe(stores, sink, secretsmanager))

    assert parsed["Description"] == "new"


async def test_delete_secret_is_immediate_and_takes_its_versions_with_it(stores, sink, secretsmanager):
    """Deviation 1: no recovery window -- the record is gone when the call
    returns, which is what makes empty-canvas Apply -> re-Apply converge."""
    await _create(stores, sink, secretsmanager, SecretString=VALUE)
    parsed = _parse(
        "DeleteSecret",
        await _answer(stores, sink.call(lambda: secretsmanager.delete_secret(SecretId=NAME, RecoveryWindowInDays=30))),
    )

    assert parsed["Name"] == NAME
    assert parsed["DeletionDate"] is not None
    assert not secretsctl.secret_exists(stores, ENV, NAME)
    assert stores.secretsctl.items(ENV) == {}
    # ...and the name is immediately re-creatable (real AWS would refuse).
    assert _parse("CreateSecret", await _create(stores, sink, secretsmanager))["Name"] == NAME


async def test_list_secrets_filters_by_name_substring(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager, name="db-password")
    await _create(stores, sink, secretsmanager, name="api-token")
    listed = _parse("ListSecrets", await _answer(stores, sink.call(lambda: secretsmanager.list_secrets())))
    filtered = _parse(
        "ListSecrets",
        await _answer(stores, sink.call(lambda: secretsmanager.list_secrets(Filters=[{"Key": "name", "Values": ["db"]}]))),
    )

    assert [s["Name"] for s in listed["SecretList"]] == ["api-token", "db-password"]
    assert [s["Name"] for s in filtered["SecretList"]] == ["db-password"]


async def test_list_secrets_fails_closed_on_an_unmodeled_filter_key(stores, sink, secretsmanager):
    """An unrecognized filter matches nothing rather than being dropped --
    dropping it would hand back secrets the caller asked to exclude."""
    await _create(stores, sink, secretsmanager)
    parsed = _parse(
        "ListSecrets",
        await _answer(
            stores,
            sink.call(lambda: secretsmanager.list_secrets(Filters=[{"Key": "owning-service", "Values": ["x"]}])),
        ),
    )

    assert parsed["SecretList"] == []


async def test_tag_and_untag_round_trip_through_describe(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager)
    await _answer(stores, sink.call(lambda: secretsmanager.tag_resource(
        SecretId=NAME, Tags=[{"Key": "env", "Value": "dev"}, {"Key": "team", "Value": "core"}],
    )))
    tagged = _parse("DescribeSecret", await _describe(stores, sink, secretsmanager))
    await _answer(stores, sink.call(lambda: secretsmanager.untag_resource(SecretId=NAME, TagKeys=["team"])))
    untagged = _parse("DescribeSecret", await _describe(stores, sink, secretsmanager))

    assert tagged["Tags"] == [{"Key": "env", "Value": "dev"}, {"Key": "team", "Value": "core"}]
    assert untagged["Tags"] == [{"Key": "env", "Value": "dev"}]


async def test_get_resource_policy_answers_that_there_is_none(stores, sink, secretsmanager):
    """Modeled for the provider's read path only: odin grants access via IAM
    edges, so a secret never has an (inert-looking) resource policy."""
    await _create(stores, sink, secretsmanager)
    parsed = _parse(
        "GetResourcePolicy", await _answer(stores, sink.call(lambda: secretsmanager.get_resource_policy(SecretId=NAME)))
    )

    assert parsed["Name"] == NAME
    assert "ResourcePolicy" not in parsed


async def test_an_unmodeled_action_is_a_protocol_correct_error(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager)
    parsed = _parse("RotateSecret", await _answer(stores, sink.call(lambda: secretsmanager.rotate_secret(SecretId=NAME))), error=True)

    assert parsed["Error"]["Code"] == "InvalidAction"


# --- value plane ------------------------------------------------------------


async def test_get_secret_value_returns_the_awscurrent_string(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager, SecretString=VALUE)
    parsed = _parse("GetSecretValue", await _get_value(stores, sink, secretsmanager))

    assert parsed["SecretString"] == VALUE
    assert parsed["VersionStages"] == ["AWSCURRENT"]


async def test_get_secret_value_with_no_version_yet_is_resource_not_found(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager)
    parsed = _parse("GetSecretValue", await _get_value(stores, sink, secretsmanager), error=True)

    assert parsed["Error"]["Code"] == "ResourceNotFoundException"


async def test_put_secret_value_moves_awscurrent_and_keeps_the_old_as_awsprevious(stores, sink, secretsmanager):
    first = _parse("CreateSecret", await _create(stores, sink, secretsmanager, SecretString="v1"))
    second = _parse("PutSecretValue", await _put_value(stores, sink, secretsmanager, "v2"))

    current = _parse("GetSecretValue", await _get_value(stores, sink, secretsmanager))
    previous = _parse("GetSecretValue", await _get_value(stores, sink, secretsmanager, VersionStage="AWSPREVIOUS"))
    by_id = _parse("GetSecretValue", await _get_value(stores, sink, secretsmanager, VersionId=first["VersionId"]))

    assert second["VersionStages"] == ["AWSCURRENT"]
    assert current["SecretString"] == "v2"
    assert previous["SecretString"] == "v1"
    assert by_id["SecretString"] == "v1"
    assert _parse("DescribeSecret", await _describe(stores, sink, secretsmanager))["VersionIdsToStages"] == {
        first["VersionId"]: ["AWSPREVIOUS"], second["VersionId"]: ["AWSCURRENT"],
    }


async def test_put_secret_value_uses_the_client_request_token_as_the_version_id(stores, sink, secretsmanager):
    """Real AWS's own rule, and terraform reads the returned id into state
    (the token shape here is literally what tofu sends -- botocore enforces
    the model's own 32-char minimum, so a short one never reaches the wire)."""
    await _create(stores, sink, secretsmanager)
    token = "terraform-20260725042516522800000002"
    parsed = _parse("PutSecretValue", await _put_value(stores, sink, secretsmanager, VALUE, ClientRequestToken=token))

    assert parsed["VersionId"] == token


async def test_a_secret_binary_round_trips_as_the_base64_it_arrived_as(stores, sink, secretsmanager):
    await _create(stores, sink, secretsmanager)
    await _answer(stores, sink.call(lambda: secretsmanager.put_secret_value(SecretId=NAME, SecretBinary=b"\x00\x01raw")))
    parsed = _parse("GetSecretValue", await _get_value(stores, sink, secretsmanager))

    assert parsed["SecretBinary"] == b"\x00\x01raw"
    assert "SecretString" not in parsed


async def test_update_secret_version_stage_moves_a_label(stores, sink, secretsmanager):
    first = _parse("CreateSecret", await _create(stores, sink, secretsmanager, SecretString="v1"))
    second = _parse("PutSecretValue", await _put_value(stores, sink, secretsmanager, "v2"))
    await _answer(stores, sink.call(lambda: secretsmanager.update_secret_version_stage(
        SecretId=NAME, VersionStage="AWSCURRENT",
        RemoveFromVersionId=second["VersionId"], MoveToVersionId=first["VersionId"],
    )))

    assert _parse("GetSecretValue", await _get_value(stores, sink, secretsmanager))["SecretString"] == "v1"


async def test_the_value_never_leaves_except_through_get_secret_value(stores, sink, secretsmanager):
    """The plaintext lives in exactly one place (the 0600 sidecar) and rides
    out on exactly one wire shape -- DescribeSecret/ListSecrets must never
    carry it, or a `secretsmanager:DescribeSecret`-only edge would leak it."""
    await _create(stores, sink, secretsmanager, SecretString=VALUE)
    described = await _describe(stores, sink, secretsmanager)
    listed = await _answer(stores, sink.call(lambda: secretsmanager.list_secrets()))

    assert VALUE.encode() not in described.body
    assert VALUE.encode() not in listed.body
    assert VALUE.encode() in (await _get_value(stores, sink, secretsmanager)).body


# --- a request that named NO secret (the nine vacuous answers) --------------
#
# logsctl's defect in this module's dialect: every handler reads
# `_secret_name(payload.get("SecretId") or "")` and exactly ONE of them
# (CreateSecret) used to check the result. MEASURED by calling the real handlers
# with `SecretId` omitted, nine ops answered
#
#   400 ResourceNotFoundException "Secrets Manager can't find the specified secret: "
#
# -- which blames a name the caller never sent. Fixed ONCE, in `pure_answer`'s
# `_missing_identifier` gate. The member lists are botocore's OWN `required`
# metadata for the `secretsmanager` model, and a real boto3 client refuses to
# send a request without them (`describe_secret()` -> `ParamValidationError:
# Missing required parameter in input: "SecretId"`), so only a raw HTTP client
# reaches here. ListSecrets is deliberately absent: a filter-less list is a
# legitimate call.

_NAMELESS = [
    ("DescribeSecret", {}, "SecretId"),
    ("UpdateSecret", {}, "SecretId"),
    ("DeleteSecret", {}, "SecretId"),
    ("TagResource", {"Tags": []}, "SecretId"),
    ("UntagResource", {"TagKeys": []}, "SecretId"),
    ("GetResourcePolicy", {}, "SecretId"),
    ("GetSecretValue", {}, "SecretId"),
    ("PutSecretValue", {"SecretString": "x"}, "SecretId"),
    ("UpdateSecretVersionStage", {}, "SecretId"),
    ("CreateSecret", {}, "Name"),
    ("DescribeSecret", {"SecretId": ""}, "SecretId"),
    ("DescribeSecret", {"SecretId": "   "}, "SecretId"),
]


@pytest.mark.parametrize("op,payload,expected", _NAMELESS, ids=lambda v: str(v)[:40])
async def test_a_request_that_named_no_secret_says_so_instead_of_blaming_the_name(op, payload, expected, stores):
    response = await synth.pure_answer(
        f"secretsmanager:{op}", "*", ENV, json.dumps(payload).encode(), stores, 0.0,
    )
    body = json.loads(response.body)
    assert response.status_code == 400
    assert body["__type"] == "InvalidParameterException", body
    assert body["message"] == f"{expected} is required"
    assert not body["message"].rstrip().endswith(":")
    assert "can't find" not in body["message"], "it must not blame a name that was never sent"


async def test_update_secret_version_stage_without_a_stage_no_longer_reports_a_false_success(stores, sink, secretsmanager):
    """One shade worse than a vacuous error, and the reason `VersionStage` is on
    the required list: an empty stage matched no version, so the call moved
    NOTHING and answered 200 -- odin reporting a success it had not performed.
    (botocore marks `VersionStage` required for this op; measured.)"""
    await _create(stores, sink, secretsmanager, SecretString="v1")
    response = await synth.pure_answer(
        "secretsmanager:UpdateSecretVersionStage", "*", ENV,
        json.dumps({"SecretId": NAME, "VersionStage": ""}).encode(), stores, 0.0,
    )
    assert response.status_code == 400, response.body
    assert json.loads(response.body)["message"] == "VersionStage is required"


async def test_a_named_secret_still_gets_the_real_not_found_answer(stores):
    """The gate must not swallow the genuine case: a caller who DID name a
    secret that doesn't exist still gets `ResourceNotFoundException` naming it,
    which is what terraform's read path drops the resource from state on."""
    response = await synth.pure_answer(
        "secretsmanager:DescribeSecret", "*", ENV,
        json.dumps({"SecretId": "no-such-secret"}).encode(), stores, 0.0,
    )
    body = json.loads(response.body)
    assert body["__type"] == "ResourceNotFoundException"
    assert "no-such-secret" in body["message"]
