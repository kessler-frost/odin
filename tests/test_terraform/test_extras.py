"""Elastic IP and Internet Gateway on Moto."""
import pytest

pytestmark = pytest.mark.tofu

HCL = """
resource "aws_eip" "eip" {
  domain = "vpc"
}

resource "aws_internet_gateway" "igw" {}
"""


async def test_eip_igw_apply_to_moto(moto_engine, tofu_runner):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(HCL)

    result = await tofu_runner.apply()
    assert result.ok, result.raw

    c = moto_engine.get_client
    assert c("ec2").describe_addresses()["Addresses"]
    assert c("ec2").describe_internet_gateways()["InternetGateways"]

    await tofu_runner.destroy()
