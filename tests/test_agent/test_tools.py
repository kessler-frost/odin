
import pytest

from odin.agent.tools import create_odin_tools
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry


@pytest.fixture
def registry(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    return ResourceRegistry(registry_path)


@pytest.fixture
def engine():
    e = MotoEngine()
    e.start()
    yield e
    e.stop()


def test_validate_file_tool(engine, registry, tmp_path):
    tools = create_odin_tools(engine, registry)
    script = tmp_path / "s3_demo.py"
    script.write_text(
        'import boto3\n'
        'client = boto3.client("s3", region_name="us-east-1")\n'
        'client.create_bucket(Bucket="agent-bucket")\n'
    )
    result = tools["validate_file"](str(script))
    assert result["status"] == "validated"
    assert result["resource"] == "s3_demo"
    assert result["metadata"]["bucket"] == "agent-bucket"


def test_validate_file_tool_error(engine, registry, tmp_path):
    tools = create_odin_tools(engine, registry)
    script = tmp_path / "s3_broken.py"
    script.write_text("raise ValueError('intentional')\n")
    result = tools["validate_file"](str(script))
    assert result["status"] == "error"
    assert "intentional" in result["error"]


def test_get_infrastructure_state_empty(engine, registry):
    tools = create_odin_tools(engine, registry)
    result = tools["get_infrastructure_state"]()
    assert result["resources"] == []


def test_get_infrastructure_state_with_resources(engine, registry):
    registry.register("s3_test", service="s3", file_path=".odin/infra/s3_test.py")
    registry.update_status("s3_test", "validated")
    tools = create_odin_tools(engine, registry)

    result = tools["get_infrastructure_state"]()
    assert len(result["resources"]) == 1
    assert result["resources"][0]["name"] == "s3_test"
    assert result["resources"][0]["status"] == "validated"


def test_get_infrastructure_state_filtered(engine, registry):
    registry.register("s3_a", service="s3", file_path=".odin/infra/s3_a.py")
    registry.register("ec2_b", service="ec2", file_path=".odin/infra/ec2_b.py")
    tools = create_odin_tools(engine, registry)

    result = tools["get_infrastructure_state"](service="s3")
    assert len(result["resources"]) == 1
    assert result["resources"][0]["service"] == "s3"
