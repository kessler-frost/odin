"""V2a -- gateway/models/iamctl.py: the IAM control-plane model, built to
research-coverage.md §2d's captured Lambda+IAM call surface.

Same test method as V1a's test_ec2net.py: every request is a REAL
boto3-signed capture (tests/gateway/harness.py CaptureSink + the iam
fixture), and every response round-trips through botocore's OWN parser for
the REAL IAM service model (`create_parser("query")`) -- proof the wire
bytes are real-AWS-shaped, not string-matched. Every call ALSO routes
through classify() -> await synth.pure_answer(), exercising the iam branch of the
dispatch pipeline, not just the model functions in isolation.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"

_TRUST_DOC = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
})


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("iam")
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
    classified = classify("iam", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an IAM request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "iam is all-synth: pure_answer must never fall through"
    return response


async def _create_role(stores, sink, iam, name="lambda-role", **kwargs) -> dict:
    req = sink.call(lambda: iam.create_role(RoleName=name, AssumeRolePolicyDocument=_TRUST_DOC, **kwargs))
    return _parse("CreateRole", await _answer(stores, req))["Role"]


async def _create_policy(stores, sink, iam, name="lambda-basic", document=None, **kwargs) -> dict:
    doc = document or json.dumps({"Version": "2012-10-17", "Statement": []})
    req = sink.call(lambda: iam.create_policy(PolicyName=name, PolicyDocument=doc, **kwargs))
    return _parse("CreatePolicy", await _answer(stores, req))["Policy"]


# --- Role: create / get / delete / list ----------------------------------------


async def test_create_role_mints_aws_shaped_id_and_echoes_request(sink, iam, stores):
    role = await _create_role(stores, sink, iam, Tags=[{"Key": "env", "Value": "prod"}])
    assert role["RoleId"].startswith("AROA") and len(role["RoleId"]) == 21
    assert role["RoleName"] == "lambda-role"
    assert role["Arn"] == "arn:aws:iam::000000000000:role/lambda-role"
    assert role["Path"] == "/"
    assert role["MaxSessionDuration"] == 3600
    assert unquote(role["AssumeRolePolicyDocument"]) == _TRUST_DOC
    assert role["Tags"] == [{"Key": "env", "Value": "prod"}]


async def test_create_role_honors_custom_path_and_max_session_duration(sink, iam, stores):
    role = await _create_role(stores, sink, iam, Path="/service-role/", MaxSessionDuration=7200)
    assert role["Path"] == "/service-role/"
    assert role["Arn"] == "arn:aws:iam::000000000000:role/service-role/lambda-role"
    assert role["MaxSessionDuration"] == 7200


async def test_create_role_on_existing_name_is_entity_already_exists(sink, iam, stores):
    await _create_role(stores, sink, iam)
    req = sink.call(lambda: iam.create_role(RoleName="lambda-role", AssumeRolePolicyDocument=_TRUST_DOC))
    response = await _answer(stores, req)
    assert response.status_code == 409
    assert _parse("CreateRole", response, error=True)["Error"]["Code"] == "EntityAlreadyExists"


async def test_get_role_round_trip(sink, iam, stores):
    await _create_role(stores, sink, iam)
    req = sink.call(lambda: iam.get_role(RoleName="lambda-role"))
    role = _parse("GetRole", await _answer(stores, req))["Role"]
    assert role["RoleName"] == "lambda-role"


async def test_get_role_unknown_name_is_no_such_entity(sink, iam, stores):
    req = sink.call(lambda: iam.get_role(RoleName="ghost"))
    response = await _answer(stores, req)
    assert response.status_code == 404
    assert _parse("GetRole", response, error=True)["Error"]["Code"] == "NoSuchEntity"


async def test_delete_role_then_get_confirms_gone(sink, iam, stores):
    await _create_role(stores, sink, iam)
    delete_req = sink.call(lambda: iam.delete_role(RoleName="lambda-role"))
    assert (await _answer(stores, delete_req)).status_code == 200

    confirm = sink.call(lambda: iam.get_role(RoleName="lambda-role"))
    response = await _answer(stores, confirm)
    assert response.status_code == 404
    assert _parse("GetRole", response, error=True)["Error"]["Code"] == "NoSuchEntity"


async def test_delete_role_blocked_by_inline_then_attached_then_succeeds(sink, iam, stores):
    await _create_role(stores, sink, iam)
    put_req = sink.call(lambda: iam.put_role_policy(
        RoleName="lambda-role", PolicyName="inline", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
    ))
    await _answer(stores, put_req)

    blocked = await _answer(stores, sink.call(lambda: iam.delete_role(RoleName="lambda-role")))
    assert blocked.status_code == 409
    assert _parse("DeleteRole", blocked, error=True)["Error"]["Code"] == "DeleteConflict"

    await _answer(stores, sink.call(lambda: iam.delete_role_policy(RoleName="lambda-role", PolicyName="inline")))
    policy = await _create_policy(stores, sink, iam)
    await _answer(stores, sink.call(lambda: iam.attach_role_policy(RoleName="lambda-role", PolicyArn=policy["Arn"])))

    still_blocked = await _answer(stores, sink.call(lambda: iam.delete_role(RoleName="lambda-role")))
    assert _parse("DeleteRole", still_blocked, error=True)["Error"]["Code"] == "DeleteConflict"

    await _answer(stores, sink.call(lambda: iam.detach_role_policy(RoleName="lambda-role", PolicyArn=policy["Arn"])))
    assert (await _answer(stores, sink.call(lambda: iam.delete_role(RoleName="lambda-role")))).status_code == 200


async def test_list_roles_returns_all_created(sink, iam, stores):
    await _create_role(stores, sink, iam, name="role-a")
    await _create_role(stores, sink, iam, name="role-b")
    req = sink.call(lambda: iam.list_roles())
    parsed = _parse("ListRoles", await _answer(stores, req))
    assert {r["RoleName"] for r in parsed["Roles"]} == {"role-a", "role-b"}


# --- Inline role policies --------------------------------------------------------


async def test_put_get_delete_list_role_policy_round_trip(sink, iam, stores):
    await _create_role(stores, sink, iam)
    doc = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]})
    put_req = sink.call(lambda: iam.put_role_policy(RoleName="lambda-role", PolicyName="read-s3", PolicyDocument=doc))
    assert (await _answer(stores, put_req)).status_code == 200

    get_req = sink.call(lambda: iam.get_role_policy(RoleName="lambda-role", PolicyName="read-s3"))
    parsed = _parse("GetRolePolicy", await _answer(stores, get_req))
    assert unquote(parsed["PolicyDocument"]) == doc

    list_req = sink.call(lambda: iam.list_role_policies(RoleName="lambda-role"))
    assert _parse("ListRolePolicies", await _answer(stores, list_req))["PolicyNames"] == ["read-s3"]

    delete_req = sink.call(lambda: iam.delete_role_policy(RoleName="lambda-role", PolicyName="read-s3"))
    assert (await _answer(stores, delete_req)).status_code == 200

    missing = await _answer(stores, sink.call(lambda: iam.get_role_policy(RoleName="lambda-role", PolicyName="read-s3")))
    assert missing.status_code == 404


# --- Managed (customer) policies --------------------------------------------------


async def test_create_policy_mints_id_and_default_version(sink, iam, stores):
    policy = await _create_policy(stores, sink, iam)
    assert policy["PolicyId"].startswith("ANPA") and len(policy["PolicyId"]) == 21
    assert policy["Arn"] == "arn:aws:iam::000000000000:policy/lambda-basic"
    assert policy["DefaultVersionId"] == "v1"
    assert policy["AttachmentCount"] == 0


async def test_get_policy_and_get_policy_version_round_trip(sink, iam, stores):
    doc = json.dumps({"Version": "2012-10-17", "Statement": []})
    policy = await _create_policy(stores, sink, iam, document=doc)

    get_req = sink.call(lambda: iam.get_policy(PolicyArn=policy["Arn"]))
    assert _parse("GetPolicy", await _answer(stores, get_req))["Policy"]["Arn"] == policy["Arn"]

    version_req = sink.call(lambda: iam.get_policy_version(PolicyArn=policy["Arn"], VersionId="v1"))
    parsed = _parse("GetPolicyVersion", await _answer(stores, version_req))["PolicyVersion"]
    assert unquote(parsed["Document"]) == doc
    assert parsed["IsDefaultVersion"] is True

    list_req = sink.call(lambda: iam.list_policy_versions(PolicyArn=policy["Arn"]))
    versions = _parse("ListPolicyVersions", await _answer(stores, list_req))["Versions"]
    assert [v["VersionId"] for v in versions] == ["v1"]


async def test_delete_policy_blocked_while_attached_then_succeeds(sink, iam, stores):
    await _create_role(stores, sink, iam)
    policy = await _create_policy(stores, sink, iam)
    await _answer(stores, sink.call(lambda: iam.attach_role_policy(RoleName="lambda-role", PolicyArn=policy["Arn"])))

    blocked = await _answer(stores, sink.call(lambda: iam.delete_policy(PolicyArn=policy["Arn"])))
    assert blocked.status_code == 409
    assert _parse("DeletePolicy", blocked, error=True)["Error"]["Code"] == "DeleteConflict"

    await _answer(stores, sink.call(lambda: iam.detach_role_policy(RoleName="lambda-role", PolicyArn=policy["Arn"])))
    assert (await _answer(stores, sink.call(lambda: iam.delete_policy(PolicyArn=policy["Arn"])))).status_code == 200


# --- Attachments -------------------------------------------------------------------


async def test_attach_detach_list_attached_role_policies_is_idempotent(sink, iam, stores):
    await _create_role(stores, sink, iam)
    policy = await _create_policy(stores, sink, iam)

    for _ in range(2):  # a repeated attach must not duplicate
        req = sink.call(lambda: iam.attach_role_policy(RoleName="lambda-role", PolicyArn=policy["Arn"]))
        assert (await _answer(stores, req)).status_code == 200

    list_req = sink.call(lambda: iam.list_attached_role_policies(RoleName="lambda-role"))
    attached = _parse("ListAttachedRolePolicies", await _answer(stores, list_req))["AttachedPolicies"]
    assert [a["PolicyArn"] for a in attached] == [policy["Arn"]]

    detach_req = sink.call(lambda: iam.detach_role_policy(RoleName="lambda-role", PolicyArn=policy["Arn"]))
    assert (await _answer(stores, detach_req)).status_code == 200
    list_req2 = sink.call(lambda: iam.list_attached_role_policies(RoleName="lambda-role"))
    assert _parse("ListAttachedRolePolicies", await _answer(stores, list_req2))["AttachedPolicies"] == []


# --- Instance profiles ---------------------------------------------------------------


async def test_instance_profile_create_add_remove_delete_round_trip(sink, iam, stores):
    await _create_role(stores, sink, iam)
    create_req = sink.call(lambda: iam.create_instance_profile(InstanceProfileName="lambda-profile"))
    profile = _parse("CreateInstanceProfile", await _answer(stores, create_req))["InstanceProfile"]
    assert profile["InstanceProfileId"].startswith("AIPA")
    assert profile["Roles"] == []

    add_req = sink.call(lambda: iam.add_role_to_instance_profile(InstanceProfileName="lambda-profile", RoleName="lambda-role"))
    assert (await _answer(stores, add_req)).status_code == 200

    get_req = sink.call(lambda: iam.get_instance_profile(InstanceProfileName="lambda-profile"))
    got = _parse("GetInstanceProfile", await _answer(stores, get_req))["InstanceProfile"]
    assert [r["RoleName"] for r in got["Roles"]] == ["lambda-role"]

    for_role_req = sink.call(lambda: iam.list_instance_profiles_for_role(RoleName="lambda-role"))
    for_role = _parse("ListInstanceProfilesForRole", await _answer(stores, for_role_req))["InstanceProfiles"]
    assert [p["InstanceProfileName"] for p in for_role] == ["lambda-profile"]

    blocked = await _answer(stores, sink.call(lambda: iam.delete_instance_profile(InstanceProfileName="lambda-profile")))
    assert blocked.status_code == 409

    remove_req = sink.call(lambda: iam.remove_role_from_instance_profile(InstanceProfileName="lambda-profile", RoleName="lambda-role"))
    assert (await _answer(stores, remove_req)).status_code == 200
    delete_req = sink.call(lambda: iam.delete_instance_profile(InstanceProfileName="lambda-profile"))
    assert (await _answer(stores, delete_req)).status_code == 200


# --- Tags ----------------------------------------------------------------------------


async def test_tag_role_untag_role_list_role_tags_round_trip(sink, iam, stores):
    await _create_role(stores, sink, iam)
    tag_req = sink.call(lambda: iam.tag_role(RoleName="lambda-role", Tags=[{"Key": "team", "Value": "infra"}]))
    assert (await _answer(stores, tag_req)).status_code == 200

    list_req = sink.call(lambda: iam.list_role_tags(RoleName="lambda-role"))
    assert _parse("ListRoleTags", await _answer(stores, list_req))["Tags"] == [{"Key": "team", "Value": "infra"}]

    untag_req = sink.call(lambda: iam.untag_role(RoleName="lambda-role", TagKeys=["team"]))
    assert (await _answer(stores, untag_req)).status_code == 200
    list_req2 = sink.call(lambda: iam.list_role_tags(RoleName="lambda-role"))
    assert _parse("ListRoleTags", await _answer(stores, list_req2))["Tags"] == []


async def test_tag_policy_untag_policy_list_policy_tags_round_trip(sink, iam, stores):
    policy = await _create_policy(stores, sink, iam)
    tag_req = sink.call(lambda: iam.tag_policy(PolicyArn=policy["Arn"], Tags=[{"Key": "env", "Value": "prod"}]))
    assert (await _answer(stores, tag_req)).status_code == 200

    list_req = sink.call(lambda: iam.list_policy_tags(PolicyArn=policy["Arn"]))
    assert _parse("ListPolicyTags", await _answer(stores, list_req))["Tags"] == [{"Key": "env", "Value": "prod"}]

    untag_req = sink.call(lambda: iam.untag_policy(PolicyArn=policy["Arn"], TagKeys=["env"]))
    await _answer(stores, untag_req)
    list_req2 = sink.call(lambda: iam.list_policy_tags(PolicyArn=policy["Arn"]))
    assert _parse("ListPolicyTags", await _answer(stores, list_req2))["Tags"] == []


# --- dispatch edges + persistence -----------------------------------------------------


async def test_unmodeled_iam_action_gets_invalid_action_not_a_503(stores):
    body = b"Action=GenerateCredentialReport&Version=2010-05-08"
    response = await synth.pure_answer("iam:GenerateCredentialReport", "*", ENV, body, stores, 0.0)
    assert response is not None and response.status_code == 400
    assert b"InvalidAction" in response.body


async def test_state_persists_at_the_iamctl_sidecar_path(sink, iam, stores, tmp_path):
    await _create_role(stores, sink, iam)
    assert (tmp_path / ENV / "gateway" / "iamctl.json").exists()
