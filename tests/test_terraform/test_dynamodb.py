"""DynamoDB end-to-end: crafted HCL applied to Moto via tofu."""
import pytest

pytestmark = pytest.mark.tofu

DDB_HCL = """
resource "aws_dynamodb_table" "users" {
  name         = "users"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}
"""


async def test_dynamodb_table_applies_to_moto(moto_engine, tofu_runner):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(DDB_HCL)

    result = await tofu_runner.apply()
    assert result.ok, result.raw

    ddb = moto_engine.get_client("dynamodb")
    assert "users" in ddb.list_tables()["TableNames"]

    await tofu_runner.destroy()
    assert "users" not in moto_engine.get_client("dynamodb").list_tables()["TableNames"]
