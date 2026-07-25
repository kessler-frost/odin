"""W2.4 -- classify() for Secrets Manager + SSM: (action, resource) from REAL
boto3-signed requests, where `resource` is the bare SECRET NAME / canonical
PARAMETER NAME -- which is the canvas label of a `secret` / `ssm` node, and
therefore exactly what `policy.compile_policies` puts in an iam edge's
statement.

That identity IS the feature: these tests prove the edge-compiled policy path
gates secret access with no secrets-specific code in the policy layer -- an
edge granting `secretsmanager:GetSecretValue` on `db-password` allows the read,
and a workload with no edge is denied by ordinary default-deny.

Same capture method as test_classify_ecr/ecs/logs: the requests are whatever
boto3 actually put on the wire, never hand-built.
"""
from __future__ import annotations

from odin.gateway.classify import classify
from odin.gateway.models import secretsctl, ssmctl
from odin.gateway.policy import Statement, evaluate

from .conftest import split_url

SECRET = "db-password"
SECRET_ARN = secretsctl.secret_arn(SECRET)
PARAM = "/odin/api-key"
PARAM_ARN = ssmctl.parameter_arn(PARAM)


def _classify(sink, service, call):
    req = sink.call(call)
    path, query = split_url(req.url)
    return classify(service, req.method, path, query, req.headers, req.body)


# --- Secrets Manager --------------------------------------------------------


def test_create_secret_maps_to_the_secret_name(sink, secretsmanager):
    assert _classify(sink, "secretsmanager", lambda: secretsmanager.create_secret(Name=SECRET)) == (
        "secretsmanager:CreateSecret", SECRET,
    )


def test_value_plane_calls_map_to_the_secret_name(sink, secretsmanager):
    assert _classify(sink, "secretsmanager", lambda: secretsmanager.get_secret_value(SecretId=SECRET)) == (
        "secretsmanager:GetSecretValue", SECRET,
    )
    assert _classify(sink, "secretsmanager", lambda: secretsmanager.put_secret_value(
        SecretId=SECRET, SecretString="v",
    )) == ("secretsmanager:PutSecretValue", SECRET)


def test_an_arn_secret_id_reduces_to_the_same_label(sink, secretsmanager):
    """What terraform actually sends: `aws_secretsmanager_secret.x.id` is the
    ARN, so without this the operator's own calls would classify to an
    ARN-shaped resource no canvas label could ever match."""
    assert _classify(sink, "secretsmanager", lambda: secretsmanager.describe_secret(SecretId=SECRET_ARN)) == (
        "secretsmanager:DescribeSecret", SECRET,
    )


def test_tag_and_delete_calls_map_to_the_secret_name(sink, secretsmanager):
    assert _classify(sink, "secretsmanager", lambda: secretsmanager.delete_secret(SecretId=SECRET_ARN)) == (
        "secretsmanager:DeleteSecret", SECRET,
    )
    assert _classify(sink, "secretsmanager", lambda: secretsmanager.tag_resource(
        SecretId=SECRET, Tags=[{"Key": "k", "Value": "v"}],
    )) == ("secretsmanager:TagResource", SECRET)


def test_list_secrets_names_no_secret_so_it_falls_back_to_the_wildcard(sink, secretsmanager):
    """Never None: a None would deny the OPERATOR via `unmappable-action`
    before evaluate() ever ran (the ec2/iam/ecr reasoning)."""
    assert _classify(sink, "secretsmanager", lambda: secretsmanager.list_secrets()) == (
        "secretsmanager:ListSecrets", "*",
    )


def test_an_iam_edge_to_the_secret_node_is_what_allows_the_read(sink, secretsmanager):
    """THE payoff: compile_policies puts the edge's DST LABEL in `resources`,
    classify puts the same label in `resource` -- so the existing evaluate()
    path gates the value read with zero secrets-specific policy code."""
    edge = [Statement(actions=("secretsmanager:GetSecretValue",), resources=(SECRET,))]
    action, resource = _classify(sink, "secretsmanager", lambda: secretsmanager.get_secret_value(SecretId=SECRET_ARN))

    assert evaluate(edge, action, resource) is True
    # No edge at all -> default-deny; an edge to a DIFFERENT secret -> denied.
    assert evaluate([], action, resource) is False
    other = [Statement(actions=("secretsmanager:GetSecretValue",), resources=("some-other-secret",))]
    assert evaluate(other, action, resource) is False
    # An edge granting only Describe cannot read the value.
    describe_only = [Statement(actions=("secretsmanager:DescribeSecret",), resources=(SECRET,))]
    assert evaluate(describe_only, action, resource) is False


# --- SSM Parameter Store ----------------------------------------------------


def test_parameter_calls_map_to_the_canonical_parameter_name(sink, ssm):
    assert _classify(sink, "ssm", lambda: ssm.get_parameter(Name=PARAM)) == ("ssm:GetParameter", PARAM)
    assert _classify(sink, "ssm", lambda: ssm.put_parameter(Name=PARAM, Value="v", Type="SecureString")) == (
        "ssm:PutParameter", PARAM,
    )
    assert _classify(sink, "ssm", lambda: ssm.delete_parameter(Name=PARAM)) == ("ssm:DeleteParameter", PARAM)


def test_a_root_level_parameter_matches_with_or_without_the_leading_slash(sink, ssm):
    assert _classify(sink, "ssm", lambda: ssm.get_parameter(Name="/db-url")) == ("ssm:GetParameter", "db-url")
    assert _classify(sink, "ssm", lambda: ssm.get_parameter(Name="db-url")) == ("ssm:GetParameter", "db-url")


def test_batch_and_path_reads_map_to_a_real_name(sink, ssm):
    assert _classify(sink, "ssm", lambda: ssm.get_parameters(Names=[PARAM])) == ("ssm:GetParameters", PARAM)
    assert _classify(sink, "ssm", lambda: ssm.get_parameters_by_path(Path="/odin")) == (
        "ssm:GetParametersByPath", "odin",
    )


def test_the_tag_api_carries_the_bare_name_as_resource_id(sink, ssm):
    assert _classify(sink, "ssm", lambda: ssm.list_tags_for_resource(
        ResourceType="Parameter", ResourceId=PARAM,
    )) == ("ssm:ListTagsForResource", PARAM)


def test_a_bare_describe_parameters_falls_back_to_the_wildcard(sink, ssm):
    assert _classify(sink, "ssm", lambda: ssm.describe_parameters()) == ("ssm:DescribeParameters", "*")


def test_an_iam_edge_to_the_ssm_node_is_what_allows_the_read(sink, ssm):
    edge = [Statement(actions=("ssm:GetParameter",), resources=(PARAM,))]
    action, resource = _classify(sink, "ssm", lambda: ssm.get_parameter(Name=PARAM, WithDecryption=True))

    assert evaluate(edge, action, resource) is True
    assert evaluate([], action, resource) is False
    assert evaluate([Statement(actions=("ssm:GetParameter",), resources=("/odin/other",))], action, resource) is False


def test_an_arn_parameter_reference_reduces_to_the_same_label(sink, ssm):
    _prefix, _sep, path = PARAM_ARN.partition(":parameter")
    assert path == "/odin/api-key"
    assert _classify(sink, "ssm", lambda: ssm.get_parameter(Name=PARAM_ARN)) == ("ssm:GetParameter", PARAM)
