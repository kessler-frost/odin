"""Orchestrator tests: a stubbed agent writes HCL, real tofu runs it on Moto."""
import pytest

from odin.api.canvas import CanvasGraph
from odin.orchestrator import Orchestrator

pytestmark = pytest.mark.tofu

VALID_HCL = """
resource "aws_vpc" "prodvpc" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_s3_bucket" "datalake" {
  bucket = "odin-test-datalake-bucket"
}
"""

BAD_HCL = """
resource "aws_subnet" "s" {
  vpc_id     = aws_vpc.does_not_exist.id
  cidr_block = "10.0.1.0/24"
}
"""


class FakeAgent:
    """Stands in for the Claude agent — writes a fixed main.tf on validate."""

    def __init__(self, runner, hcl):
        self._runner = runner
        self._hcl = hcl

    @property
    def is_running(self):
        return True

    async def validate(self, graph):
        self._runner.work_dir.mkdir(parents=True, exist_ok=True)
        (self._runner.work_dir / "main.tf").write_text(self._hcl)
        return
        yield  # async generator


def _graph():
    return CanvasGraph(
        nodes=[
            {"id": "vpc-1", "type": "vpc", "position": {"x": 0, "y": 0}, "data": {"label": "prodvpc"}},
            {"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "datalake"}},
        ]
    )


async def _drain(gen):
    async for _ in gen:
        pass


async def test_validate_marks_nodes_validated(moto_engine, registry, tofu_runner):
    orch = Orchestrator(moto_engine, registry, tofu_runner, agent=FakeAgent(tofu_runner, VALID_HCL))
    await _drain(orch.validate(_graph()))
    assert registry.get("vpc_prodvpc").status == "validated"
    assert registry.get("s3_datalake").status == "validated"


async def test_validate_marks_error_on_bad_hcl(moto_engine, registry, tofu_runner):
    orch = Orchestrator(moto_engine, registry, tofu_runner, agent=FakeAgent(tofu_runner, BAD_HCL))
    graph = CanvasGraph(
        nodes=[{"id": "s-1", "type": "subnet", "position": {"x": 0, "y": 0}, "data": {"label": "s"}}]
    )
    await _drain(orch.validate(graph))
    assert registry.get("subnet_s").status == "error"


async def test_validate_without_agent_marks_error(moto_engine, registry, tofu_runner):
    orch = Orchestrator(moto_engine, registry, tofu_runner, agent=None)
    await _drain(orch.validate(_graph()))
    assert registry.get("vpc_prodvpc").status == "error"


async def test_deploy_all_applies_to_moto(moto_engine, registry, tofu_runner):
    orch = Orchestrator(moto_engine, registry, tofu_runner, agent=FakeAgent(tofu_runner, VALID_HCL))
    await _drain(orch.validate(_graph()))

    deployed = await orch.deploy_all()
    assert set(deployed) == {"vpc_prodvpc", "s3_datalake"}
    assert registry.get("vpc_prodvpc").status == "live"

    ec2 = moto_engine.get_client("ec2")
    assert "10.0.0.0/16" in {v["CidrBlock"] for v in ec2.describe_vpcs()["Vpcs"]}


async def test_destroy_all_resets_to_draft(moto_engine, registry, tofu_runner):
    orch = Orchestrator(moto_engine, registry, tofu_runner, agent=FakeAgent(tofu_runner, VALID_HCL))
    await _drain(orch.validate(_graph()))
    await orch.deploy_all()

    destroyed = await orch.destroy_all()
    assert set(destroyed) >= {"vpc_prodvpc", "s3_datalake"}
    assert registry.get("vpc_prodvpc").status == "draft"
