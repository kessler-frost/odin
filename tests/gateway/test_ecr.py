"""V2b -- gateway/models/ecr.py: the ECR control-plane model, built to
research-coverage.md §2c's captured `aws_ecr_repository` call surface.

Same test method as V1a/V2a: every request is a REAL boto3-signed capture
(tests/gateway/harness.py CaptureSink + the ecr fixture), and every response
round-trips through botocore's OWN parser for the REAL ECR service model
(`create_parser("json")`) -- proof the wire bytes are real-AWS-shaped, not
string-matched. Every call ALSO routes through classify() ->
await synth.pure_answer(), exercising the ecr branch of the dispatch pipeline.
"""
from __future__ import annotations

import base64
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
ENV = "default"
PORT = 55123  # a fixed fake registry:2 port -- no real container in a unit test


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("ecr")
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


async def _answer(stores: SynthStores, req, backing_port: int | None = PORT) -> Response:
    path, query = split_url(req.url)
    classified = classify("ecr", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an ECR request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0, backing_port)
    assert response is not None, "ecr is all-synth: pure_answer must never fall through"
    return response


async def _create_repo(stores, sink, ecr, name="app", **kwargs) -> dict:
    req = sink.call(lambda: ecr.create_repository(repositoryName=name, **kwargs))
    return _parse("CreateRepository", await _answer(stores, req))["repository"]


# --- CreateRepository / DescribeRepositories / DeleteRepository ------------------


async def test_create_repository_builds_repository_uri_from_the_registry_port(sink, ecr, stores):
    repo = await _create_repo(stores, sink, ecr)
    assert repo["repositoryName"] == "app"
    assert repo["repositoryArn"] == "arn:aws:ecr:us-east-1:000000000000:repository/app"
    assert repo["repositoryUri"] == f"127.0.0.1:{PORT}/app"
    assert repo["registryId"] == "000000000000"
    assert repo["imageTagMutability"] == "MUTABLE"
    assert repo["encryptionConfiguration"] == {"encryptionType": "AES256"}


async def test_create_repository_on_existing_name_is_already_exists(sink, ecr, stores):
    await _create_repo(stores, sink, ecr)
    req = sink.call(lambda: ecr.create_repository(repositoryName="app"))
    response = await _answer(stores, req)
    assert response.status_code == 400
    assert _parse("CreateRepository", response, error=True)["Error"]["Code"] == "RepositoryAlreadyExistsException"


async def test_create_repository_seeds_tags_from_the_request(sink, ecr, stores):
    await _create_repo(stores, sink, ecr, tags=[{"Key": "env", "Value": "prod"}])
    req = sink.call(lambda: ecr.list_tags_for_resource(resourceArn="arn:aws:ecr:us-east-1:000000000000:repository/app"))
    parsed = _parse("ListTagsForResource", await _answer(stores, req))
    assert parsed["tags"] == [{"Key": "env", "Value": "prod"}]


async def test_describe_repositories_all_and_by_name(sink, ecr, stores):
    await _create_repo(stores, sink, ecr, name="app-a")
    await _create_repo(stores, sink, ecr, name="app-b")

    all_req = sink.call(lambda: ecr.describe_repositories())
    parsed = _parse("DescribeRepositories", await _answer(stores, all_req))
    assert {r["repositoryName"] for r in parsed["repositories"]} == {"app-a", "app-b"}

    one_req = sink.call(lambda: ecr.describe_repositories(repositoryNames=["app-b"]))
    parsed = _parse("DescribeRepositories", await _answer(stores, one_req))
    assert [r["repositoryName"] for r in parsed["repositories"]] == ["app-b"]


async def test_describe_repositories_unknown_name_is_not_found(sink, ecr, stores):
    req = sink.call(lambda: ecr.describe_repositories(repositoryNames=["ghost"]))
    response = await _answer(stores, req)
    assert response.status_code == 400
    assert _parse("DescribeRepositories", response, error=True)["Error"]["Code"] == "RepositoryNotFoundException"


async def test_delete_repository_then_describe_confirms_gone(sink, ecr, stores):
    await _create_repo(stores, sink, ecr)
    delete_req = sink.call(lambda: ecr.delete_repository(repositoryName="app"))
    deleted = _parse("DeleteRepository", await _answer(stores, delete_req))["repository"]
    assert deleted["repositoryName"] == "app"

    confirm = sink.call(lambda: ecr.describe_repositories(repositoryNames=["app"]))
    response = await _answer(stores, confirm)
    assert response.status_code == 400
    assert _parse("DescribeRepositories", response, error=True)["Error"]["Code"] == "RepositoryNotFoundException"


async def test_delete_repository_unknown_name_is_not_found(sink, ecr, stores):
    req = sink.call(lambda: ecr.delete_repository(repositoryName="ghost"))
    response = await _answer(stores, req)
    assert response.status_code == 400
    assert _parse("DeleteRepository", response, error=True)["Error"]["Code"] == "RepositoryNotFoundException"


# --- Tags --------------------------------------------------------------------------


async def test_tag_resource_untag_resource_round_trip(sink, ecr, stores):
    repo = await _create_repo(stores, sink, ecr)
    tag_req = sink.call(lambda: ecr.tag_resource(resourceArn=repo["repositoryArn"], tags=[{"Key": "team", "Value": "infra"}]))
    assert (await _answer(stores, tag_req)).status_code == 200

    list_req = sink.call(lambda: ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"]))
    assert _parse("ListTagsForResource", await _answer(stores, list_req))["tags"] == [{"Key": "team", "Value": "infra"}]

    untag_req = sink.call(lambda: ecr.untag_resource(resourceArn=repo["repositoryArn"], tagKeys=["team"]))
    assert (await _answer(stores, untag_req)).status_code == 200
    list_req2 = sink.call(lambda: ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"]))
    assert _parse("ListTagsForResource", await _answer(stores, list_req2))["tags"] == []


async def test_tag_resource_on_unknown_arn_is_not_found(sink, ecr, stores):
    req = sink.call(lambda: ecr.tag_resource(resourceArn="arn:aws:ecr:us-east-1:000000000000:repository/ghost", tags=[]))
    response = await _answer(stores, req)
    assert response.status_code == 400
    assert _parse("TagResource", response, error=True)["Error"]["Code"] == "RepositoryNotFoundException"


# --- GetAuthorizationToken ------------------------------------------------------------


async def test_get_authorization_token_returns_a_docker_login_compatible_synthetic_token(sink, ecr, stores):
    req = sink.call(lambda: ecr.get_authorization_token())
    parsed = _parse("GetAuthorizationToken", await _answer(stores, req))
    (entry,) = parsed["authorizationData"]
    assert base64.b64decode(entry["authorizationToken"]) == b"AWS:odin"
    assert entry["proxyEndpoint"] == f"http://127.0.0.1:{PORT}"


# --- dispatch edges + persistence -----------------------------------------------------


async def test_unmodeled_ecr_action_gets_invalid_action_not_a_503(stores):
    body = json.dumps({}).encode()
    response = await synth.pure_answer("ecr:DescribeImages", "*", ENV, body, stores, 0.0, PORT)
    assert response is not None and response.status_code == 400
    assert b"InvalidAction" in response.body


async def test_missing_backing_port_falls_back_to_port_zero_rather_than_crashing(sink, ecr, stores):
    """ensure_backings() always boots the registry ahead of Apply, but this
    stays defensive rather than raising if it somehow hasn't yet."""
    req = sink.call(lambda: ecr.create_repository(repositoryName="app"))
    repo = _parse("CreateRepository", await _answer(stores, req, backing_port=None))["repository"]
    assert repo["repositoryUri"] == "127.0.0.1:0/app"


async def test_state_persists_at_the_ecr_sidecar_path(sink, ecr, stores, tmp_path):
    await _create_repo(stores, sink, ecr)
    assert (tmp_path / ENV / "gateway" / "ecr.json").exists()
