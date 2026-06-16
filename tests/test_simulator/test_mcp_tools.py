from pathlib import Path

from odin.mcp.tools import OdinTools
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry


def _make_registry(tmp_path: Path) -> ResourceRegistry:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    return ResourceRegistry(registry_path)


def _write_s3_file(tmp_path: Path, name: str = "s3_test") -> Path:
    f = tmp_path / f"{name}.py"
    f.write_text(
        'import boto3\n'
        'client = boto3.client("s3", region_name="us-east-1")\n'
        'client.create_bucket(Bucket="test-bucket-123")\n'
    )
    return f


def test_validate_file_success(tmp_path):
    engine = MotoEngine()
    engine.start()
    registry = _make_registry(tmp_path)
    tools = OdinTools(engine, registry)

    script = _write_s3_file(tmp_path)
    result = tools.validate_file(str(script))

    assert result["resource"] == "s3_test"
    assert result["service"] == "s3"
    assert result["status"] == "validated"
    assert result["error"] is None
    assert "bucket" in result["metadata"]
    assert result["metadata"]["bucket"] == "test-bucket-123"
    engine.stop()


def test_validate_file_error(tmp_path):
    engine = MotoEngine()
    engine.start()
    registry = _make_registry(tmp_path)
    tools = OdinTools(engine, registry)

    script = tmp_path / "s3_bad.py"
    script.write_text(
        'import boto3\n'
        'client = boto3.client("s3", region_name="us-east-1")\n'
        'client.get_object(Bucket="nonexistent", Key="nope")\n'
    )
    result = tools.validate_file(str(script))

    assert result["resource"] == "s3_bad"
    assert result["service"] == "s3"
    assert result["status"] == "error"
    assert result["error"] is not None
    assert result["metadata"] == {}
    engine.stop()


def test_validate_file_updates_registry(tmp_path):
    engine = MotoEngine()
    engine.start()
    registry = _make_registry(tmp_path)
    tools = OdinTools(engine, registry)

    script = _write_s3_file(tmp_path)
    tools.validate_file(str(script))

    entry = registry.get("s3_test")
    assert entry is not None
    assert entry.status == "validated"
    assert "bucket" in entry.metadata
    engine.stop()


def test_validate_file_ec2(tmp_path):
    engine = MotoEngine()
    engine.start()
    registry = _make_registry(tmp_path)
    tools = OdinTools(engine, registry)

    script = tmp_path / "ec2_web.py"
    script.write_text(
        'import boto3\n'
        'ec2 = boto3.client("ec2", region_name="us-east-1")\n'
        'ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1, InstanceType="t2.micro")\n'
    )
    result = tools.validate_file(str(script))

    assert result["resource"] == "ec2_web"
    assert result["service"] == "ec2"
    assert result["status"] == "validated"
    assert "instance" in result["metadata"]
    engine.stop()


def test_get_infrastructure_state_empty(tmp_path):
    engine = MotoEngine()
    registry = _make_registry(tmp_path)
    tools = OdinTools(engine, registry)
    result = tools.get_infrastructure_state()
    assert result["resources"] == []


def test_get_infrastructure_state_with_resources(tmp_path):
    engine = MotoEngine()
    registry = _make_registry(tmp_path)
    registry.register("s3_test", service="s3", file_path=".odin/infra/s3_test.py")
    registry.update_status("s3_test", "validated")
    tools = OdinTools(engine, registry)
    result = tools.get_infrastructure_state()
    assert len(result["resources"]) == 1
    assert result["resources"][0]["name"] == "s3_test"
    assert result["resources"][0]["status"] == "validated"


def test_get_infrastructure_state_filtered_by_service(tmp_path):
    engine = MotoEngine()
    registry = _make_registry(tmp_path)
    registry.register("s3_a", service="s3", file_path=".odin/infra/s3_a.py")
    registry.register("ec2_b", service="ec2", file_path=".odin/infra/ec2_b.py")
    tools = OdinTools(engine, registry)
    result = tools.get_infrastructure_state(service="s3")
    assert len(result["resources"]) == 1
    assert result["resources"][0]["service"] == "s3"
