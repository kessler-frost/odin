"""CloudWatch Logs, EventBridge, and EBS on Moto.

Logs and EventBridge exercise the `provider_key` override (their AWS-provider
endpoint keys — cloudwatchlogs / cloudwatchevents — differ from the boto3
service names logs / events). A wrong key would break provider.tf and fail the
apply, so this test guards that mapping too.
"""
import pytest

pytestmark = pytest.mark.tofu

HCL = """
resource "aws_cloudwatch_log_group" "logs" {
  name = "/odin/test-logs"
}

resource "aws_cloudwatch_event_rule" "rule" {
  name                = "odin-rule"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_ebs_volume" "vol" {
  availability_zone = "us-east-1a"
  size              = 10
}
"""


async def test_logs_events_ebs_apply_to_moto(moto_engine, tofu_runner):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(HCL)

    result = await tofu_runner.apply()
    assert result.ok, result.raw

    c = moto_engine.get_client
    assert any(g["logGroupName"] == "/odin/test-logs" for g in c("logs").describe_log_groups()["logGroups"])
    assert any(r["Name"] == "odin-rule" for r in c("events").list_rules()["Rules"])
    assert any(v["Size"] == 10 for v in c("ec2").describe_volumes()["Volumes"])

    await tofu_runner.destroy()
