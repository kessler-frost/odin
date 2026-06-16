import pytest

from odin.simulator.engine import MotoEngine


def test_endpoint_url():
    engine = MotoEngine(port=4298)
    assert engine.endpoint_url == "http://127.0.0.1:4298"


@pytest.mark.tofu
def test_engine_serves_aws_calls(moto_engine):
    ec2 = moto_engine.get_client("ec2")
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    assert vpc_id in {v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"]}


@pytest.mark.tofu
def test_engine_reset_clears_state(moto_engine):
    ec2 = moto_engine.get_client("ec2")
    ec2.create_vpc(CidrBlock="10.123.0.0/16")
    moto_engine.reset()
    cidrs = {v["CidrBlock"] for v in ec2.describe_vpcs()["Vpcs"]}
    assert "10.123.0.0/16" not in cidrs
