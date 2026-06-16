import pytest

from odin.mcp.tools import OdinTools, _error_summaries
from odin.terraform.runner import TofuRunner


def test_error_summaries_keeps_only_errors():
    diags = [
        {"severity": "error", "summary": "bad cidr"},
        {"severity": "warning", "summary": "deprecated"},
    ]
    assert _error_summaries(diags) == ["bad cidr"]


def test_get_infrastructure_state_reads_main_tf(tmp_path, registry):
    tf_dir = tmp_path / "tf"
    tf_dir.mkdir()
    (tf_dir / "main.tf").write_text('resource "aws_vpc" "v" {}')
    registry.register("vpc_prod", service="vpc", file_path="")
    tools = OdinTools(TofuRunner(tf_dir, "http://127.0.0.1:4298"), registry)

    state = tools.get_infrastructure_state()
    assert "aws_vpc" in state["main_tf"]
    assert any(r["name"] == "vpc_prod" for r in state["resources"])


@pytest.mark.tofu
async def test_validate_infrastructure_ok(tofu_runner, registry):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(
        'resource "aws_vpc" "v" {\n  cidr_block = "10.0.0.0/16"\n}\n'
    )
    tools = OdinTools(tofu_runner, registry)
    result = await tools.validate_infrastructure()
    assert result["valid"], result


@pytest.mark.tofu
async def test_validate_infrastructure_reports_errors(tofu_runner, registry):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(
        'resource "aws_subnet" "s" {\n  vpc_id     = aws_vpc.nope.id\n  cidr_block = "10.0.1.0/24"\n}\n'
    )
    tools = OdinTools(tofu_runner, registry)
    result = await tools.validate_infrastructure()
    assert not result["valid"]
    assert result["errors"]
