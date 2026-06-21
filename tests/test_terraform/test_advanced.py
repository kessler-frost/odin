"""ALB on Moto. It depends on a VPC + two subnets in different AZs; the
`timeouts { create }` makes a Moto that never reports "active" fail fast rather
than hang on the default ~40m wait.
"""
import pytest

pytestmark = pytest.mark.tofu

ALB_HCL = """
resource "aws_vpc" "v" {
  cidr_block = "10.0.0.0/16"
}
resource "aws_subnet" "a" {
  vpc_id            = aws_vpc.v.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = "us-east-1a"
}
resource "aws_subnet" "b" {
  vpc_id            = aws_vpc.v.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = "us-east-1b"
}
resource "aws_lb" "lb" {
  name               = "odin-lb"
  load_balancer_type = "application"
  subnets            = [aws_subnet.a.id, aws_subnet.b.id]
  timeouts { create = "90s" }
}
"""


async def test_alb_applies_to_moto(moto_engine, tofu_runner):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(ALB_HCL)
    result = await tofu_runner.apply()
    assert result.ok, result.raw
    lbs = moto_engine.get_client("elbv2").describe_load_balancers()["LoadBalancers"]
    assert any(lb["LoadBalancerName"] == "odin-lb" for lb in lbs)
    await tofu_runner.destroy()
