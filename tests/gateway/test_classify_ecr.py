"""V2b -- classify's ecr branch: one test per action asserting the exact
(action, resource) tuple against a REAL captured boto3 request. ECR is
JSON-target protocol (like dynamodb/sqs) but gets its own dedicated
extractor (classify.py's `_classify_ecr`) since its resource shapes
(repositoryName / repositoryNames / resourceArn) don't fit
`_target_resource`'s dynamodb/sqs-specific branches, and -- like ec2/iam --
the only caller in v1 is the OPERATOR, so extraction never returns None.
"""
from __future__ import annotations

from odin.gateway.classify import classify

from .conftest import split_url


def _classified(req):
    path, query = split_url(req.url)
    return classify("ecr", req.method, path, query, req.headers, req.body)


def test_create_repository_resolves_its_own_name(sink, ecr):
    req = sink.call(lambda: ecr.create_repository(repositoryName="app"))
    assert _classified(req) == ("ecr:CreateRepository", "app")


def test_describe_repositories_by_name_list(sink, ecr):
    req = sink.call(lambda: ecr.describe_repositories(repositoryNames=["app"]))
    assert _classified(req) == ("ecr:DescribeRepositories", "app")


def test_describe_repositories_unfiltered_has_no_id(sink, ecr):
    req = sink.call(lambda: ecr.describe_repositories())
    assert _classified(req) == ("ecr:DescribeRepositories", "*")


def test_delete_repository(sink, ecr):
    req = sink.call(lambda: ecr.delete_repository(repositoryName="app"))
    assert _classified(req) == ("ecr:DeleteRepository", "app")


def test_list_tags_for_resource_resolves_the_name_from_the_arn(sink, ecr):
    req = sink.call(lambda: ecr.list_tags_for_resource(resourceArn="arn:aws:ecr:us-east-1:000000000000:repository/app"))
    assert _classified(req) == ("ecr:ListTagsForResource", "app")


def test_get_authorization_token_has_no_id(sink, ecr):
    req = sink.call(lambda: ecr.get_authorization_token())
    assert _classified(req) == ("ecr:GetAuthorizationToken", "*")
