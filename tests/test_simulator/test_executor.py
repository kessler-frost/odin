from pathlib import Path

from odin.simulator.engine import MotoEngine
from odin.simulator.executor import Executor


def test_execute_s3_resource(tmp_path):
    resource_file = tmp_path / "s3_test-bucket.py"
    resource_file.write_text(
        'import boto3\n'
        'client = boto3.client("s3", region_name="us-east-1")\n'
        'client.create_bucket(Bucket="test-bucket")\n'
    )
    engine = MotoEngine()
    engine.start()
    executor = Executor(engine)
    result = executor.execute(resource_file)
    assert result.success
    assert result.error is None
    s3 = engine.get_client("s3")
    buckets = s3.list_buckets()["Buckets"]
    assert any(b["Name"] == "test-bucket" for b in buckets)
    engine.stop()


def test_execute_ec2_resource(tmp_path):
    resource_file = tmp_path / "ec2_web.py"
    resource_file.write_text(
        'import boto3\n'
        'ec2 = boto3.client("ec2", region_name="us-east-1")\n'
        'ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1, InstanceType="t2.micro")\n'
    )
    engine = MotoEngine()
    engine.start()
    executor = Executor(engine)
    result = executor.execute(resource_file)
    assert result.success
    ec2 = engine.get_client("ec2")
    instances = ec2.describe_instances()["Reservations"]
    assert len(instances) >= 1
    engine.stop()


def test_execute_bad_resource_returns_error(tmp_path):
    resource_file = tmp_path / "s3_bad.py"
    resource_file.write_text(
        'import boto3\n'
        'client = boto3.client("s3", region_name="us-east-1")\n'
        'client.get_object(Bucket="nonexistent", Key="nope")\n'
    )
    engine = MotoEngine()
    engine.start()
    executor = Executor(engine)
    result = executor.execute(resource_file)
    assert not result.success
    assert result.error is not None
    assert "NoSuchBucket" in result.error
    engine.stop()


def test_execute_syntax_error_returns_error(tmp_path):
    resource_file = tmp_path / "broken.py"
    resource_file.write_text("def foo(\n")
    engine = MotoEngine()
    engine.start()
    executor = Executor(engine)
    result = executor.execute(resource_file)
    assert not result.success
    assert result.error is not None
    engine.stop()


def test_detect_service_from_filename():
    assert Executor.detect_service(Path(".odin/infra/s3_my-bucket.py")) == "s3"
    assert Executor.detect_service(Path(".odin/infra/ec2_web-server.py")) == "ec2"
    assert Executor.detect_service(Path(".odin/infra/lambda_handler.py")) == "lambda"
    assert Executor.detect_service(Path(".odin/infra/iam_role.py")) == "iam"
    assert Executor.detect_service(Path(".odin/infra/vpc_main.py")) == "vpc"
    assert Executor.detect_service(Path(".odin/infra/unknown.py")) == "unknown"
