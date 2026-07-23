"""V1a -- gateway/models/ec2net.py: the EC2-network model (VPC / Subnet /
Security Group), built to research-coverage.md §2a's captured call surface.

S1's test method, reused exactly: every request is a REAL boto3-signed
capture (tests/gateway/harness.py CaptureSink + the ec2 fixture), and every
response round-trips through botocore's OWN parser for the REAL EC2 service
model (`create_parser("ec2")` -- the EC2 protocol, not "query") -- proof
the wire bytes are real-AWS-shaped, not string-matched.

Every call below ALSO routes through classify() -> synth.pure_answer(),
so the ec2 branch of the dispatch pipeline is exercised on every test, not
just the model functions in isolation.
"""
from __future__ import annotations

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


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("ec2")
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


def _answer(stores: SynthStores, req) -> Response:
    """classify -> synth.pure_answer, exactly app.py's allowed-request path."""
    path, query = split_url(req.url)
    classified = classify("ec2", req.method, path, query, req.headers, req.body)
    assert classified is not None, "an EC2 request must never be unmappable"
    action, resource = classified
    response = synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "ec2 is all-synth: pure_answer must never fall through"
    return response


def _create_vpc(stores, sink, ec2, cidr="10.0.0.0/16", **kwargs) -> str:
    req = sink.call(lambda: ec2.create_vpc(CidrBlock=cidr, **kwargs))
    parsed = _parse("CreateVpc", _answer(stores, req))
    return parsed["Vpc"]["VpcId"]


def _create_sg(stores, sink, ec2, vpc_id: str, name="web") -> str:
    req = sink.call(lambda: ec2.create_security_group(GroupName=name, Description=f"{name} sg", VpcId=vpc_id))
    return _parse("CreateSecurityGroup", _answer(stores, req))["GroupId"]


def _describe_sg(stores, sink, ec2, group_id: str) -> dict:
    req = sink.call(lambda: ec2.describe_security_groups(GroupIds=[group_id]))
    return _parse("DescribeSecurityGroups", _answer(stores, req))["SecurityGroups"][0]


# --- VPC ----------------------------------------------------------------------


def test_create_vpc_mints_aws_shaped_id_and_echoes_request(sink, ec2, stores):
    req = sink.call(lambda: ec2.create_vpc(
        CidrBlock="10.0.0.0/16", InstanceTenancy="default",
        TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
    ))
    parsed = _parse("CreateVpc", _answer(stores, req))
    vpc = parsed["Vpc"]
    assert vpc["VpcId"].startswith("vpc-") and len(vpc["VpcId"]) == len("vpc-") + 17
    assert vpc["CidrBlock"] == "10.0.0.0/16"
    assert vpc["State"] == "available"
    assert vpc["InstanceTenancy"] == "default"
    assert vpc["IsDefault"] is False
    assert vpc["Tags"] == [{"Key": "Name", "Value": "main"}]
    assert vpc["CidrBlockAssociationSet"][0]["CidrBlock"] == "10.0.0.0/16"


def test_describe_vpcs_all_and_by_id(sink, ec2, stores):
    vpc_a = _create_vpc(stores, sink, ec2, "10.0.0.0/16")
    vpc_b = _create_vpc(stores, sink, ec2, "10.1.0.0/16")

    all_req = sink.call(lambda: ec2.describe_vpcs())
    parsed = _parse("DescribeVpcs", _answer(stores, all_req))
    assert {v["VpcId"] for v in parsed["Vpcs"]} == {vpc_a, vpc_b}

    one_req = sink.call(lambda: ec2.describe_vpcs(VpcIds=[vpc_b]))
    parsed = _parse("DescribeVpcs", _answer(stores, one_req))
    assert [v["VpcId"] for v in parsed["Vpcs"]] == [vpc_b]
    assert parsed["Vpcs"][0]["CidrBlock"] == "10.1.0.0/16"


def test_describe_vpcs_unknown_id_is_the_real_not_found_envelope(sink, ec2, stores):
    req = sink.call(lambda: ec2.describe_vpcs(VpcIds=["vpc-00000000000000000"]))
    response = _answer(stores, req)
    assert response.status_code == 400
    parsed = _parse("DescribeVpcs", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidVpcID.NotFound"


def test_describe_vpc_attribute_defaults(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)

    dns_req = sink.call(lambda: ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsSupport"))
    parsed = _parse("DescribeVpcAttribute", _answer(stores, dns_req))
    assert parsed["EnableDnsSupport"] == {"Value": True}

    host_req = sink.call(lambda: ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsHostnames"))
    parsed = _parse("DescribeVpcAttribute", _answer(stores, host_req))
    assert parsed["EnableDnsHostnames"] == {"Value": False}


# --- the 3 auto-created sidecars (research finding #1) ------------------------


def test_create_vpc_auto_creates_default_nacl(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    req = sink.call(lambda: ec2.describe_network_acls(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "default", "Values": ["true"]}],
    ))
    parsed = _parse("DescribeNetworkAcls", _answer(stores, req))
    (nacl,) = parsed["NetworkAcls"]
    assert nacl["NetworkAclId"].startswith("acl-")
    assert nacl["VpcId"] == vpc_id
    assert nacl["IsDefault"] is True


def test_create_vpc_auto_creates_main_route_table(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    req = sink.call(lambda: ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "association.main", "Values": ["true"]}],
    ))
    parsed = _parse("DescribeRouteTables", _answer(stores, req))
    (table,) = parsed["RouteTables"]
    assert table["RouteTableId"].startswith("rtb-")
    assert table["VpcId"] == vpc_id
    assert table["Associations"][0]["Main"] is True
    assert table["Routes"][0]["GatewayId"] == "local"


def test_create_vpc_auto_creates_default_security_group(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    req = sink.call(lambda: ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "group-name", "Values": ["default"]}],
    ))
    parsed = _parse("DescribeSecurityGroups", _answer(stores, req))
    (sg,) = parsed["SecurityGroups"]
    assert sg["GroupId"].startswith("sg-")
    assert sg["GroupName"] == "default"
    assert sg["VpcId"] == vpc_id


# --- Subnet -------------------------------------------------------------------


def test_create_subnet_describe_delete_round_trip(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    create_req = sink.call(lambda: ec2.create_subnet(
        VpcId=vpc_id, CidrBlock="10.0.1.0/24", AvailabilityZone="us-east-1a",
    ))
    created = _parse("CreateSubnet", _answer(stores, create_req))["Subnet"]
    subnet_id = created["SubnetId"]
    assert subnet_id.startswith("subnet-") and len(subnet_id) == len("subnet-") + 17
    assert created["CidrBlock"] == "10.0.1.0/24"
    assert created["VpcId"] == vpc_id
    assert created["AvailabilityZone"] == "us-east-1a"
    assert created["State"] == "available"

    by_vpc = sink.call(lambda: ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]))
    parsed = _parse("DescribeSubnets", _answer(stores, by_vpc))
    assert [s["SubnetId"] for s in parsed["Subnets"]] == [subnet_id]

    delete_req = sink.call(lambda: ec2.delete_subnet(SubnetId=subnet_id))
    assert _answer(stores, delete_req).status_code == 200

    confirm = sink.call(lambda: ec2.describe_subnets(SubnetIds=[subnet_id]))
    response = _answer(stores, confirm)
    assert response.status_code == 400
    assert _parse("DescribeSubnets", response, error=True)["Error"]["Code"] == "InvalidSubnetID.NotFound"


# --- Security Group -----------------------------------------------------------


def test_create_security_group_seeds_default_allow_all_egress(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    assert group_id.startswith("sg-") and len(group_id) == len("sg-") + 17

    sg = _describe_sg(stores, sink, ec2, group_id)
    assert sg["IpPermissions"] == []
    (egress,) = sg["IpPermissionsEgress"]
    assert egress["IpProtocol"] == "-1"
    assert egress["IpRanges"] == [{"CidrIp": "0.0.0.0/0"}]


def test_provider_can_revoke_the_seeded_default_egress(sink, ec2, stores):
    """The captured sequence (research §2a): the provider REVOKES the
    auto-created allow-all egress -- so the revoke's content hash must land
    on the seeded rule."""
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    revoke_req = sink.call(lambda: ec2.revoke_security_group_egress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ]))
    assert _answer(stores, revoke_req).status_code == 200
    assert _describe_sg(stores, sink, ec2, group_id)["IpPermissionsEgress"] == []


def test_authorize_ingress_is_idempotent_by_content(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    permissions = [
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]},
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "UserIdGroupPairs": [{"GroupId": group_id}]},
    ]
    first = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=permissions))
    parsed = _parse("AuthorizeSecurityGroupIngress", _answer(stores, first))
    first_ids = {r["SecurityGroupRuleId"] for r in parsed["SecurityGroupRules"]}
    assert len(first_ids) == 2
    assert all(rid.startswith("sgr-") and len(rid) == len("sgr-") + 17 for rid in first_ids)

    again = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=permissions))
    parsed = _parse("AuthorizeSecurityGroupIngress", _answer(stores, again))
    assert {r["SecurityGroupRuleId"] for r in parsed["SecurityGroupRules"]} == first_ids

    rules_req = sink.call(lambda: ec2.describe_security_group_rules(
        Filters=[{"Name": "group-id", "Values": [group_id]}],
    ))
    rules = _parse("DescribeSecurityGroupRules", _answer(stores, rules_req))["SecurityGroupRules"]
    ingress = [r for r in rules if not r["IsEgress"]]
    assert len(ingress) == 2  # no duplicates from the double authorize
    assert {r["SecurityGroupRuleId"] for r in ingress} == first_ids


def test_revoke_ingress_matches_by_content_hash(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    perm = [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}]
    auth_req = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=perm))
    _answer(stores, auth_req)

    revoke_req = sink.call(lambda: ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=perm))
    assert _answer(stores, revoke_req).status_code == 200
    assert _describe_sg(stores, sink, ec2, group_id)["IpPermissions"] == []


def test_describe_security_groups_aggregates_ranges_per_permission(sink, ec2, stores):
    """Two cidrs on one (proto, port span) come back as ONE IpPermission with
    two IpRanges -- the aggregation the provider diffs against its config."""
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    auth_req = sink.call(lambda: ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
         "IpRanges": [{"CidrIp": "10.0.0.0/16"}, {"CidrIp": "192.168.0.0/24"}]},
    ]))
    _answer(stores, auth_req)
    (ingress,) = _describe_sg(stores, sink, ec2, group_id)["IpPermissions"]
    assert ingress["FromPort"] == 443 and ingress["ToPort"] == 443
    assert [r["CidrIp"] for r in ingress["IpRanges"]] == ["10.0.0.0/16", "192.168.0.0/24"]


def test_delete_security_group_then_describe_confirms_gone(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    group_id = _create_sg(stores, sink, ec2, vpc_id)
    delete_req = sink.call(lambda: ec2.delete_security_group(GroupId=group_id))
    assert _answer(stores, delete_req).status_code == 200

    confirm = sink.call(lambda: ec2.describe_security_groups(GroupIds=[group_id]))
    response = _answer(stores, confirm)
    assert response.status_code == 400
    assert _parse("DescribeSecurityGroups", response, error=True)["Error"]["Code"] == "InvalidGroup.NotFound"


# --- DeleteVpc: DependencyViolation semantics ---------------------------------


def test_delete_vpc_blocked_by_subnet_then_sg_then_succeeds(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    subnet_req = sink.call(lambda: ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24"))
    subnet_id = _parse("CreateSubnet", _answer(stores, subnet_req))["Subnet"]["SubnetId"]
    group_id = _create_sg(stores, sink, ec2, vpc_id)

    blocked = _answer(stores, sink.call(lambda: ec2.delete_vpc(VpcId=vpc_id)))
    assert blocked.status_code == 400
    assert _parse("DeleteVpc", blocked, error=True)["Error"]["Code"] == "DependencyViolation"

    _answer(stores, sink.call(lambda: ec2.delete_subnet(SubnetId=subnet_id)))
    still_blocked = _answer(stores, sink.call(lambda: ec2.delete_vpc(VpcId=vpc_id)))
    assert _parse("DeleteVpc", still_blocked, error=True)["Error"]["Code"] == "DependencyViolation"

    _answer(stores, sink.call(lambda: ec2.delete_security_group(GroupId=group_id)))
    assert _answer(stores, sink.call(lambda: ec2.delete_vpc(VpcId=vpc_id))).status_code == 200

    confirm = _answer(stores, sink.call(lambda: ec2.describe_vpcs(VpcIds=[vpc_id])))
    assert _parse("DescribeVpcs", confirm, error=True)["Error"]["Code"] == "InvalidVpcID.NotFound"


def test_delete_vpc_removes_its_default_security_group(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    _answer(stores, sink.call(lambda: ec2.delete_vpc(VpcId=vpc_id)))
    req = sink.call(lambda: ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
    ))
    assert _parse("DescribeSecurityGroups", _answer(stores, req))["SecurityGroups"] == []


# --- Tags (EC2's own wire shape) ------------------------------------------------


def test_create_tags_describe_tags_round_trip(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    tag_req = sink.call(lambda: ec2.create_tags(
        Resources=[vpc_id], Tags=[{"Key": "env", "Value": "prod"}, {"Key": "team", "Value": "infra"}],
    ))
    assert _answer(stores, tag_req).status_code == 200

    describe_req = sink.call(lambda: ec2.describe_tags(
        Filters=[{"Name": "resource-id", "Values": [vpc_id]}],
    ))
    parsed = _parse("DescribeTags", _answer(stores, describe_req))
    entries = {(t["Key"], t["Value"], t["ResourceType"]) for t in parsed["Tags"]}
    assert entries == {("env", "prod", "vpc"), ("team", "infra", "vpc")}
    assert all(t["ResourceId"] == vpc_id for t in parsed["Tags"])


def test_create_tags_can_tag_multiple_resources_at_once(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    subnet_req = sink.call(lambda: ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24"))
    subnet_id = _parse("CreateSubnet", _answer(stores, subnet_req))["Subnet"]["SubnetId"]

    tag_req = sink.call(lambda: ec2.create_tags(
        Resources=[vpc_id, subnet_id], Tags=[{"Key": "env", "Value": "prod"}],
    ))
    _answer(stores, tag_req)
    assert stores.tags.get(ENV, f"ec2:{vpc_id}") == {"env": "prod"}
    assert stores.tags.get(ENV, f"ec2:{subnet_id}") == {"env": "prod"}


def test_delete_tags_keyed_removes_only_matching(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    stores.tags.set(ENV, f"ec2:{vpc_id}", {"env": "prod", "team": "infra"})
    delete_req = sink.call(lambda: ec2.delete_tags(Resources=[vpc_id], Tags=[{"Key": "env"}]))
    _answer(stores, delete_req)
    assert stores.tags.get(ENV, f"ec2:{vpc_id}") == {"team": "infra"}


def test_delete_tags_without_keys_removes_all(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2)
    stores.tags.set(ENV, f"ec2:{vpc_id}", {"env": "prod", "team": "infra"})
    delete_req = sink.call(lambda: ec2.delete_tags(Resources=[vpc_id]))
    _answer(stores, delete_req)
    assert stores.tags.get(ENV, f"ec2:{vpc_id}") == {}


def test_tag_specification_at_create_shows_in_describes(sink, ec2, stores):
    vpc_id = _create_vpc(stores, sink, ec2, TagSpecifications=[
        {"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]},
    ])
    describe_req = sink.call(lambda: ec2.describe_vpcs(VpcIds=[vpc_id]))
    parsed = _parse("DescribeVpcs", _answer(stores, describe_req))
    assert parsed["Vpcs"][0]["Tags"] == [{"Key": "Name", "Value": "main"}]


# --- destroy-sweep support + dispatch edges -------------------------------------


def test_describe_network_interfaces_is_always_empty(sink, ec2, stores):
    req = sink.call(lambda: ec2.describe_network_interfaces(
        Filters=[{"Name": "vpc-id", "Values": ["vpc-123"]}],
    ))
    parsed = _parse("DescribeNetworkInterfaces", _answer(stores, req))
    assert parsed["NetworkInterfaces"] == []


def test_unmodeled_ec2_action_gets_invalid_action_not_a_503(stores):
    """EC2 has no backing: returning None would 503. An unmodeled action
    must answer the InvalidAction envelope the provider tolerates
    (research §2b's ModifyInstanceAttribute observation)."""
    body = b"Action=DescribeVpcClassicLink&Version=2016-11-15"
    response = synth.pure_answer("ec2:DescribeVpcClassicLink", "*", ENV, body, stores, 0.0)
    assert response is not None and response.status_code == 400
    assert b"InvalidAction" in response.body


def test_state_persists_at_the_ec2net_sidecar_path(sink, ec2, stores, tmp_path):
    _create_vpc(stores, sink, ec2)
    assert (tmp_path / ENV / "gateway" / "ec2net.json").exists()
