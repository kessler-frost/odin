"""V4a -- classify's lambda branch: one test per action asserting the exact
(action, resource) tuple against a REAL captured boto3 request. Lambda is a
FOURTH wire shape (REST method+path, verified live) distinct from ec2/iam's
query protocol and ecr's X-Amz-Target JSON -- `_classify_lambda` matches the
captured route table instead. Same OPERATOR-only reasoning as ec2/iam/ecr:
extraction never returns None for a recognized route.
"""
from __future__ import annotations

from odin.gateway.classify import classify

from .conftest import split_url

_ROLE_ARN = "arn:aws:iam::000000000000:role/lambda-exec"
_FUNCTION_ARN = "arn:aws:lambda:us-east-1:000000000000:function:fn1"


def _classified(req):
    path, query = split_url(req.url)
    return classify("lambda", req.method, path, query, req.headers, req.body)


def test_create_function_resolves_the_name_from_the_body(sink, lambda_):
    req = sink.call(lambda: lambda_.create_function(
        FunctionName="fn1", Role=_ROLE_ARN, Runtime="python3.12", Handler="lambda_function.lambda_handler",
        Code={"ZipFile": b"PK\x03\x04fake-zip-bytes"},
    ))
    assert _classified(req) == ("lambda:CreateFunction", "fn1")


def test_get_function_resolves_the_name_from_the_path(sink, lambda_):
    req = sink.call(lambda: lambda_.get_function(FunctionName="fn1"))
    assert _classified(req) == ("lambda:GetFunction", "fn1")


def test_get_function_configuration(sink, lambda_):
    req = sink.call(lambda: lambda_.get_function_configuration(FunctionName="fn1"))
    assert _classified(req) == ("lambda:GetFunctionConfiguration", "fn1")


def test_delete_function(sink, lambda_):
    req = sink.call(lambda: lambda_.delete_function(FunctionName="fn1"))
    assert _classified(req) == ("lambda:DeleteFunction", "fn1")


def test_update_function_code(sink, lambda_):
    req = sink.call(lambda: lambda_.update_function_code(FunctionName="fn1", ZipFile=b"PK\x03\x04new-bytes"))
    assert _classified(req) == ("lambda:UpdateFunctionCode", "fn1")


def test_update_function_configuration(sink, lambda_):
    req = sink.call(lambda: lambda_.update_function_configuration(FunctionName="fn1", Timeout=10))
    assert _classified(req) == ("lambda:UpdateFunctionConfiguration", "fn1")


def test_list_versions_by_function(sink, lambda_):
    req = sink.call(lambda: lambda_.list_versions_by_function(FunctionName="fn1"))
    assert _classified(req) == ("lambda:ListVersionsByFunction", "fn1")


def test_get_function_code_signing_config(sink, lambda_):
    req = sink.call(lambda: lambda_.get_function_code_signing_config(FunctionName="fn1"))
    assert _classified(req) == ("lambda:GetFunctionCodeSigningConfig", "fn1")


def test_invoke_resolves_the_name_from_the_path_not_the_payload(sink, lambda_):
    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b'{"key": "value"}'))
    assert _classified(req) == ("lambda:Invoke", "fn1")


def test_list_tags_resolves_the_name_from_the_arn(sink, lambda_):
    req = sink.call(lambda: lambda_.list_tags(Resource=_FUNCTION_ARN))
    assert _classified(req) == ("lambda:ListTags", "fn1")


def test_tag_resource_resolves_the_name_from_the_arn(sink, lambda_):
    req = sink.call(lambda: lambda_.tag_resource(Resource=_FUNCTION_ARN, Tags={"env": "test"}))
    assert _classified(req) == ("lambda:TagResource", "fn1")


def test_untag_resource_resolves_the_name_from_the_arn(sink, lambda_):
    req = sink.call(lambda: lambda_.untag_resource(Resource=_FUNCTION_ARN, TagKeys=["env"]))
    assert _classified(req) == ("lambda:UntagResource", "fn1")


def test_unrecognized_path_is_unmappable(sink, lambda_):
    req = sink.call(lambda: lambda_.list_functions())
    assert _classified(req) is None
