"""SQS + SNS end-to-end: crafted HCL applied to Moto via tofu."""
import pytest

pytestmark = pytest.mark.tofu

MESSAGING_HCL = """
resource "aws_sqs_queue" "jobs" {
  name = "jobs"
}

resource "aws_sns_topic" "alerts" {
  name = "alerts"
}
"""


async def test_sqs_and_sns_apply_to_moto(moto_engine, tofu_runner):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(MESSAGING_HCL)

    result = await tofu_runner.apply()
    assert result.ok, result.raw

    sqs = moto_engine.get_client("sqs")
    assert any(url.endswith("/jobs") for url in sqs.list_queues().get("QueueUrls", []))

    sns = moto_engine.get_client("sns")
    assert any(t["TopicArn"].endswith(":alerts") for t in sns.list_topics()["Topics"])

    await tofu_runner.destroy()
