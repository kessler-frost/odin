"""W2.1 -- classify() for CloudWatch Logs: (action, resource) from REAL
boto3-signed `logs` requests, where `resource` is the bare LOG GROUP NAME
(== the canvas label of a `logs` node), which is exactly what
`policy.compile_policies` puts in an iam edge's statement.

Same capture method as test_classify_ecr/ecs: the requests are whatever boto3
actually put on the wire, never hand-built.
"""
from __future__ import annotations

from odin.gateway.classify import classify
from odin.gateway.policy import Statement, evaluate

from .conftest import split_url

GROUP = "/aws/lambda/echo"
ARN = f"arn:aws:logs:us-east-1:000000000000:log-group:{GROUP}"


def _classify(sink, call):
    req = sink.call(call)
    path, query = split_url(req.url)
    return classify("logs", req.method, path, query, req.headers, req.body)


def test_create_log_group_maps_to_the_group_name(sink, logs):
    assert _classify(sink, lambda: logs.create_log_group(logGroupName=GROUP)) == ("logs:CreateLogGroup", GROUP)


def test_delete_and_retention_calls_map_to_the_group_name(sink, logs):
    assert _classify(sink, lambda: logs.delete_log_group(logGroupName=GROUP)) == ("logs:DeleteLogGroup", GROUP)
    assert _classify(sink, lambda: logs.put_retention_policy(logGroupName=GROUP, retentionInDays=7)) == (
        "logs:PutRetentionPolicy", GROUP,
    )
    assert _classify(sink, lambda: logs.delete_retention_policy(logGroupName=GROUP)) == (
        "logs:DeleteRetentionPolicy", GROUP,
    )


def test_data_plane_calls_map_to_the_group_name(sink, logs):
    assert _classify(sink, lambda: logs.create_log_stream(logGroupName=GROUP, logStreamName="s")) == (
        "logs:CreateLogStream", GROUP,
    )
    assert _classify(sink, lambda: logs.put_log_events(
        logGroupName=GROUP, logStreamName="s", logEvents=[{"timestamp": 1, "message": "m"}])) == (
        "logs:PutLogEvents", GROUP,
    )
    assert _classify(sink, lambda: logs.get_log_events(logGroupName=GROUP, logStreamName="s")) == (
        "logs:GetLogEvents", GROUP,
    )
    assert _classify(sink, lambda: logs.filter_log_events(logGroupName=GROUP)) == ("logs:FilterLogEvents", GROUP)
    assert _classify(sink, lambda: logs.describe_log_streams(logGroupName=GROUP)) == (
        "logs:DescribeLogStreams", GROUP,
    )


def test_tag_calls_reduce_either_arn_form_to_the_bare_group_name(sink, logs):
    assert _classify(sink, lambda: logs.list_tags_for_resource(resourceArn=ARN)) == (
        "logs:ListTagsForResource", GROUP,
    )
    assert _classify(sink, lambda: logs.list_tags_for_resource(resourceArn=f"{ARN}:*")) == (
        "logs:ListTagsForResource", GROUP,
    )
    assert _classify(sink, lambda: logs.tag_resource(resourceArn=ARN, tags={"a": "b"})) == (
        "logs:TagResource", GROUP,
    )
    assert _classify(sink, lambda: logs.untag_resource(resourceArn=ARN, tagKeys=["a"])) == (
        "logs:UntagResource", GROUP,
    )


def test_log_group_identifier_is_reduced_the_same_way(sink, logs):
    assert _classify(sink, lambda: logs.get_log_events(logGroupIdentifier=ARN, logStreamName="s")) == (
        "logs:GetLogEvents", GROUP,
    )


def test_describe_log_groups_falls_back_to_prefix_then_star(sink, logs):
    assert _classify(sink, lambda: logs.describe_log_groups(logGroupNamePrefix="/aws/")) == (
        "logs:DescribeLogGroups", "/aws/",
    )
    # No group identifier at all -> "*", never None: an unmappable action would
    # deny the OPERATOR before evaluate() ever runs.
    assert _classify(sink, lambda: logs.describe_log_groups()) == ("logs:DescribeLogGroups", "*")


def test_a_logs_request_without_an_amz_target_is_unmappable(sink, logs):
    req = sink.call(lambda: logs.describe_log_groups())
    path, query = split_url(req.url)
    headers = {k: v for k, v in req.headers.items() if k.lower() != "x-amz-target"}
    assert classify("logs", req.method, path, query, headers, req.body) is None


def test_an_iam_edge_to_a_logs_node_gates_the_classified_call(sink, logs):
    """The whole point of the bare-group-name convention: the statement an
    edge compiles to (resources=(<logs node label>,)) matches classify()'s
    resource with no logs-specific plumbing in the policy layer."""
    statements = [Statement(actions=("logs:GetLogEvents", "logs:PutLogEvents"), resources=(GROUP,))]
    action, resource = _classify(sink, lambda: logs.get_log_events(logGroupName=GROUP, logStreamName="s"))
    assert evaluate(statements, action, resource)

    other, other_resource = _classify(sink, lambda: logs.get_log_events(logGroupName="/other", logStreamName="s"))
    assert not evaluate(statements, other, other_resource)
