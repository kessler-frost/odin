"""W2.5 -- classify() for Elastic Load Balancing v2: (action, resource) from
REAL boto3-signed `elbv2` requests.

elbv2 is the QUERY protocol (the operation rides in the `Action` form param, not
an `X-Amz-Target` header), and its resource is the bare LOAD-BALANCER or
TARGET-GROUP name: `Name` on a create, otherwise whichever ARN the call carries,
reduced by `_elbv2_name`. A LISTENER ARN reduces to its load balancer's name on
purpose -- one `alb` canvas node expands to lb + target group + listener, so all
three belong to the same label.

Same capture method as test_classify_ecs/logs: the requests are whatever boto3
actually put on the wire, never hand-built. Note the SERVICE STRING: botocore
calls the model `elbv2`, but the SigV4 credential scope (which is what the
gateway dispatches on) is `elasticloadbalancing`.
"""
from __future__ import annotations

from odin.gateway.classify import classify
from odin.gateway.policy import Statement, evaluate

from .conftest import split_url

SERVICE = "elasticloadbalancing"
LB = "web-lb"
TG = "web-lb-tg"
LB_ARN = f"arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/{LB}/1234567890abcdef"
TG_ARN = f"arn:aws:elasticloadbalancing:us-east-1:000000000000:targetgroup/{TG}/abcdef1234567890"
LISTENER_ARN = f"arn:aws:elasticloadbalancing:us-east-1:000000000000:listener/app/{LB}/1234567890abcdef/fedcba0987654321"


def _classify(sink, call):
    req = sink.call(call)
    path, query = split_url(req.url)
    return classify(SERVICE, req.method, path, query, req.headers, req.body)


def test_the_credential_scope_service_is_elasticloadbalancing(sink, elbv2):
    """The one thing a hand-written test would get wrong: boto3's client name is
    `elbv2` but what it SIGNS with -- and so what the gateway sees -- is
    `elasticloadbalancing`."""
    req = sink.call(lambda: elbv2.describe_load_balancers(Names=[LB]))
    assert f"/{SERVICE}/aws4_request" in req.headers["Authorization"]


def test_create_calls_map_to_the_name_they_carry(sink, elbv2):
    assert _classify(sink, lambda: elbv2.create_load_balancer(Name=LB, Subnets=["subnet-1"])) == (
        f"{SERVICE}:CreateLoadBalancer", LB,
    )
    assert _classify(sink, lambda: elbv2.create_target_group(Name=TG, Port=80, Protocol="HTTP", VpcId="vpc-1")) == (
        f"{SERVICE}:CreateTargetGroup", TG,
    )


def test_load_balancer_scoped_calls_map_to_the_load_balancer_name(sink, elbv2):
    assert _classify(sink, lambda: elbv2.describe_load_balancers(LoadBalancerArns=[LB_ARN])) == (
        f"{SERVICE}:DescribeLoadBalancers", LB,
    )
    assert _classify(sink, lambda: elbv2.delete_load_balancer(LoadBalancerArn=LB_ARN)) == (
        f"{SERVICE}:DeleteLoadBalancer", LB,
    )
    assert _classify(sink, lambda: elbv2.describe_load_balancer_attributes(LoadBalancerArn=LB_ARN)) == (
        f"{SERVICE}:DescribeLoadBalancerAttributes", LB,
    )
    assert _classify(sink, lambda: elbv2.modify_load_balancer_attributes(
        LoadBalancerArn=LB_ARN, Attributes=[{"Key": "idle_timeout.timeout_seconds", "Value": "120"}])) == (
        f"{SERVICE}:ModifyLoadBalancerAttributes", LB,
    )


def test_target_group_scoped_calls_map_to_the_target_group_name(sink, elbv2):
    assert _classify(sink, lambda: elbv2.describe_target_groups(TargetGroupArns=[TG_ARN])) == (
        f"{SERVICE}:DescribeTargetGroups", TG,
    )
    assert _classify(sink, lambda: elbv2.delete_target_group(TargetGroupArn=TG_ARN)) == (
        f"{SERVICE}:DeleteTargetGroup", TG,
    )
    assert _classify(sink, lambda: elbv2.describe_target_group_attributes(TargetGroupArn=TG_ARN)) == (
        f"{SERVICE}:DescribeTargetGroupAttributes", TG,
    )
    assert _classify(sink, lambda: elbv2.register_targets(
        TargetGroupArn=TG_ARN, Targets=[{"Id": "i-1", "Port": 80}])) == (
        f"{SERVICE}:RegisterTargets", TG,
    )
    assert _classify(sink, lambda: elbv2.deregister_targets(
        TargetGroupArn=TG_ARN, Targets=[{"Id": "i-1", "Port": 80}])) == (
        f"{SERVICE}:DeregisterTargets", TG,
    )
    assert _classify(sink, lambda: elbv2.describe_target_health(TargetGroupArn=TG_ARN)) == (
        f"{SERVICE}:DescribeTargetHealth", TG,
    )


def test_a_listener_arn_maps_to_its_load_balancers_name(sink, elbv2):
    # A listener is not a canvas node of its own -- it belongs to the alb node's
    # label, same as the load balancer and the target group.
    assert _classify(sink, lambda: elbv2.describe_listeners(ListenerArns=[LISTENER_ARN])) == (
        f"{SERVICE}:DescribeListeners", LB,
    )
    assert _classify(sink, lambda: elbv2.delete_listener(ListenerArn=LISTENER_ARN)) == (
        f"{SERVICE}:DeleteListener", LB,
    )
    # The read that broke the first real apply until it was modeled.
    assert _classify(sink, lambda: elbv2.describe_listener_attributes(ListenerArn=LISTENER_ARN)) == (
        f"{SERVICE}:DescribeListenerAttributes", LB,
    )


def test_create_listener_maps_to_its_load_balancer(sink, elbv2):
    assert _classify(sink, lambda: elbv2.create_listener(
        LoadBalancerArn=LB_ARN, Protocol="HTTP", Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": TG_ARN}])) == (
        f"{SERVICE}:CreateListener", LB,
    )


def test_the_arn_only_tag_api_maps_to_the_first_resource_arn(sink, elbv2):
    # elbv2's tag API is ARN-only (`ResourceArns`), never a typed id -- so the
    # resource has to come out of the ARN itself.
    assert _classify(sink, lambda: elbv2.describe_tags(ResourceArns=[LB_ARN])) == (
        f"{SERVICE}:DescribeTags", LB,
    )
    assert _classify(sink, lambda: elbv2.add_tags(
        ResourceArns=[TG_ARN], Tags=[{"Key": "team", "Value": "core"}])) == (
        f"{SERVICE}:AddTags", TG,
    )
    assert _classify(sink, lambda: elbv2.remove_tags(ResourceArns=[TG_ARN], TagKeys=["team"])) == (
        f"{SERVICE}:RemoveTags", TG,
    )


def test_a_call_naming_nothing_falls_back_to_star_never_none(sink, elbv2):
    """The OPERATOR-only rule ec2/iam/ecr/ecs already follow: a None here would
    deny tofu via `unmappable-action` before evaluate() ever ran."""
    assert _classify(sink, lambda: elbv2.describe_load_balancers()) == (
        f"{SERVICE}:DescribeLoadBalancers", "*",
    )
    assert _classify(sink, lambda: elbv2.describe_target_groups()) == (
        f"{SERVICE}:DescribeTargetGroups", "*",
    )


def test_a_body_with_no_action_is_unmappable():
    assert classify(SERVICE, "POST", "/", {}, {}, b"Version=2015-12-01") is None


def test_the_operator_wildcard_allows_every_classified_elbv2_action(sink, elbv2):
    """What actually matters in production: tofu is the only principal that ever
    reaches these actions (an ALB is not an IAM data-plane target -- you send it
    plain HTTP), so the operator's full-allow statement must cover them all."""
    operator = [Statement(actions=("*",), resources=("*",))]
    for call in (
        lambda: elbv2.create_load_balancer(Name=LB, Subnets=["subnet-1"]),
        lambda: elbv2.describe_load_balancer_attributes(LoadBalancerArn=LB_ARN),
        lambda: elbv2.describe_listener_attributes(ListenerArn=LISTENER_ARN),
        lambda: elbv2.describe_tags(ResourceArns=[LB_ARN]),
    ):
        classified = _classify(sink, call)
        assert classified is not None
        assert evaluate(operator, *classified)
