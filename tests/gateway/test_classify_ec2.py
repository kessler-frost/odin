"""V1a -- classify's ec2 branch, in test_classify.py's exact style: one test
per action asserting the exact (action, resource) tuple against a REAL
captured boto3 request. EC2's resource convention differs from the other
services (see classify.py's module docstring): the id param when the
request carries one, else "*" -- never a canvas label.
"""
from __future__ import annotations

from odin.gateway.classify import classify

from .conftest import split_url


def _classified(req):
    path, query = split_url(req.url)
    return classify("ec2", req.method, path, query, req.headers, req.body)


def test_create_vpc_has_no_id_yet(sink, ec2):
    req = sink.call(lambda: ec2.create_vpc(CidrBlock="10.0.0.0/16"))
    assert _classified(req) == ("ec2:CreateVpc", "*")


def test_describe_vpcs_unfiltered(sink, ec2):
    req = sink.call(lambda: ec2.describe_vpcs())
    assert _classified(req) == ("ec2:DescribeVpcs", "*")


def test_describe_vpcs_by_id_list(sink, ec2):
    req = sink.call(lambda: ec2.describe_vpcs(VpcIds=["vpc-123"]))
    assert _classified(req) == ("ec2:DescribeVpcs", "vpc-123")


def test_describe_vpc_attribute(sink, ec2):
    req = sink.call(lambda: ec2.describe_vpc_attribute(VpcId="vpc-123", Attribute="enableDnsSupport"))
    assert _classified(req) == ("ec2:DescribeVpcAttribute", "vpc-123")


def test_delete_vpc(sink, ec2):
    req = sink.call(lambda: ec2.delete_vpc(VpcId="vpc-123"))
    assert _classified(req) == ("ec2:DeleteVpc", "vpc-123")


def test_create_subnet_scopes_to_its_vpc(sink, ec2):
    req = sink.call(lambda: ec2.create_subnet(VpcId="vpc-123", CidrBlock="10.0.1.0/24"))
    assert _classified(req) == ("ec2:CreateSubnet", "vpc-123")


def test_describe_subnets_by_id_list(sink, ec2):
    req = sink.call(lambda: ec2.describe_subnets(SubnetIds=["subnet-123"]))
    assert _classified(req) == ("ec2:DescribeSubnets", "subnet-123")


def test_delete_subnet(sink, ec2):
    req = sink.call(lambda: ec2.delete_subnet(SubnetId="subnet-123"))
    assert _classified(req) == ("ec2:DeleteSubnet", "subnet-123")


def test_create_security_group_scopes_to_its_vpc(sink, ec2):
    req = sink.call(lambda: ec2.create_security_group(GroupName="web", Description="d", VpcId="vpc-123"))
    assert _classified(req) == ("ec2:CreateSecurityGroup", "vpc-123")


def test_describe_security_groups_by_id_list(sink, ec2):
    req = sink.call(lambda: ec2.describe_security_groups(GroupIds=["sg-123"]))
    assert _classified(req) == ("ec2:DescribeSecurityGroups", "sg-123")


def test_describe_security_groups_by_filter_has_no_id(sink, ec2):
    req = sink.call(lambda: ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": ["vpc-123"]}]))
    assert _classified(req) == ("ec2:DescribeSecurityGroups", "*")


def test_authorize_security_group_ingress(sink, ec2):
    req = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId="sg-123", IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]},
    ]))
    assert _classified(req) == ("ec2:AuthorizeSecurityGroupIngress", "sg-123")


def test_revoke_security_group_egress(sink, ec2):
    req = sink.call(lambda: ec2.revoke_security_group_egress(GroupId="sg-123", IpPermissions=[
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ]))
    assert _classified(req) == ("ec2:RevokeSecurityGroupEgress", "sg-123")


def test_delete_security_group(sink, ec2):
    req = sink.call(lambda: ec2.delete_security_group(GroupId="sg-123"))
    assert _classified(req) == ("ec2:DeleteSecurityGroup", "sg-123")


def test_describe_security_group_rules_by_filter(sink, ec2):
    req = sink.call(lambda: ec2.describe_security_group_rules(
        Filters=[{"Name": "group-id", "Values": ["sg-123"]}],
    ))
    assert _classified(req) == ("ec2:DescribeSecurityGroupRules", "*")


def test_create_tags_resolves_the_first_resource_id(sink, ec2):
    req = sink.call(lambda: ec2.create_tags(
        Resources=["vpc-123", "subnet-456"], Tags=[{"Key": "env", "Value": "prod"}],
    ))
    assert _classified(req) == ("ec2:CreateTags", "vpc-123")


def test_delete_tags_resolves_the_first_resource_id(sink, ec2):
    req = sink.call(lambda: ec2.delete_tags(Resources=["subnet-456"]))
    assert _classified(req) == ("ec2:DeleteTags", "subnet-456")


def test_describe_tags_by_filter_has_no_id(sink, ec2):
    req = sink.call(lambda: ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": ["vpc-123"]}]))
    assert _classified(req) == ("ec2:DescribeTags", "*")


def test_describe_network_interfaces(sink, ec2):
    req = sink.call(lambda: ec2.describe_network_interfaces(
        Filters=[{"Name": "vpc-id", "Values": ["vpc-123"]}],
    ))
    assert _classified(req) == ("ec2:DescribeNetworkInterfaces", "*")
