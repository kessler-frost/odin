"""Fast unit tests for orchestrator status mapping (no tofu/agent needed).

These drive `Orchestrator._finalize` with crafted tofu diagnostics via a stub
runner, so they exercise the address→node mapping logic precisely and quickly.
"""
import pytest

from odin.api.canvas import CanvasGraph
from odin.orchestrator import Orchestrator
from odin.simulator.registry import ResourceRegistry
from odin.terraform.runner import PlanResult


@pytest.fixture
def registry(tmp_path) -> ResourceRegistry:
    path = tmp_path / "registry.json"
    path.write_text('{"resources": {}}')
    return ResourceRegistry(path)


class StubRunner:
    """Returns crafted validate/plan results without touching tofu."""

    def __init__(self, tmp_path, validate_res=None, plan_res=None):
        self.work_dir = tmp_path
        self._v = validate_res or PlanResult(ok=True)
        self._p = plan_res or PlanResult(ok=True)

    async def validate(self):
        return self._v

    async def plan(self):
        return self._p


async def _run(registry, tmp_path, graph, *, validate_res=None, plan_res=None):
    runner = StubRunner(tmp_path, validate_res=validate_res, plan_res=plan_res)
    orch = Orchestrator(engine=None, registry=registry, runner=runner)
    reg_names = orch._register_validating(graph)
    await orch._finalize(graph, reg_names)
    return registry


async def test_prefix_address_does_not_taint_sibling(registry, tmp_path):
    """A failing `aws_instance.web2` must NOT mark `aws_instance.web` as error."""
    graph = CanvasGraph(nodes=[
        {"id": "1", "type": "ec2", "data": {"label": "web"}},
        {"id": "2", "type": "ec2", "data": {"label": "web2"}},
    ])
    plan = PlanResult(ok=False, diagnostics=[
        {"severity": "error", "summary": "bad web2", "address": "aws_instance.web2"},
    ])
    reg = await _run(registry, tmp_path, graph, plan_res=plan)
    assert reg.get("ec2_web").status == "validated"
    assert reg.get("ec2_web2").status == "error"
    assert "bad web2" in (reg.get("ec2_web2").error or "")


async def test_per_node_error_messages(registry, tmp_path):
    """Each errored node gets only its own diagnostic, not every error concatenated."""
    graph = CanvasGraph(nodes=[
        {"id": "1", "type": "s3", "data": {"label": "alpha"}},
        {"id": "2", "type": "s3", "data": {"label": "beta"}},
    ])
    plan = PlanResult(ok=False, diagnostics=[
        {"severity": "error", "summary": "alpha boom", "address": "aws_s3_bucket.alpha"},
        {"severity": "error", "summary": "beta boom", "address": "aws_s3_bucket.beta"},
    ])
    reg = await _run(registry, tmp_path, graph, plan_res=plan)
    assert reg.get("s3_alpha").error == "alpha boom"
    assert reg.get("s3_beta").error == "beta boom"


async def test_indexed_address_matches(registry, tmp_path):
    """count/for_each addresses like `aws_instance.web[0]` map to the node."""
    graph = CanvasGraph(nodes=[{"id": "1", "type": "ec2", "data": {"label": "web"}}])
    plan = PlanResult(ok=False, diagnostics=[
        {"severity": "error", "summary": "count err", "address": "aws_instance.web[0]"},
    ])
    reg = await _run(registry, tmp_path, graph, plan_res=plan)
    assert reg.get("ec2_web").status == "error"


async def test_global_error_taints_all(registry, tmp_path):
    """A diagnostic with no address (syntax error) fails every node."""
    graph = CanvasGraph(nodes=[
        {"id": "1", "type": "vpc", "data": {"label": "v"}},
        {"id": "2", "type": "s3", "data": {"label": "b"}},
    ])
    val = PlanResult(ok=False, diagnostics=[{"severity": "error", "summary": "syntax", "address": ""}])
    reg = await _run(registry, tmp_path, graph, validate_res=val)
    assert reg.get("vpc_v").status == "error"
    assert reg.get("s3_b").status == "error"


async def test_all_validated_when_no_errors(registry, tmp_path):
    graph = CanvasGraph(nodes=[
        {"id": "1", "type": "vpc", "data": {"label": "v"}},
        {"id": "2", "type": "s3", "data": {"label": "b"}},
    ])
    reg = await _run(registry, tmp_path, graph, plan_res=PlanResult(ok=True))
    assert reg.get("vpc_v").status == "validated"
    assert reg.get("s3_b").status == "validated"
    assert reg.get("vpc_v").error is None
