from odin.simulator.engine import MotoEngine


def test_engine_starts_and_provides_clients():
    engine = MotoEngine()
    engine.start()
    s3 = engine.get_client("s3")
    s3.create_bucket(Bucket="test-bucket")
    buckets = s3.list_buckets()["Buckets"]
    assert any(b["Name"] == "test-bucket" for b in buckets)
    engine.stop()


def test_engine_ec2_client():
    engine = MotoEngine()
    engine.start()
    ec2 = engine.get_client("ec2")
    vpcs = ec2.describe_vpcs()["Vpcs"]
    assert len(vpcs) >= 1
    engine.stop()


def test_engine_iam_client():
    engine = MotoEngine()
    engine.start()
    iam = engine.get_client("iam")
    iam.create_role(RoleName="test-role", AssumeRolePolicyDocument="{}", Path="/")
    roles = iam.list_roles()["Roles"]
    assert any(r["RoleName"] == "test-role" for r in roles)
    engine.stop()


def test_engine_reset_clears_state():
    engine = MotoEngine()
    engine.start()
    s3 = engine.get_client("s3")
    s3.create_bucket(Bucket="will-be-gone")
    engine.reset()
    s3 = engine.get_client("s3")
    buckets = s3.list_buckets()["Buckets"]
    assert not any(b["Name"] == "will-be-gone" for b in buckets)
    engine.stop()


def test_engine_supported_services():
    engine = MotoEngine()
    assert "s3" in engine.supported_services
    assert "ec2" in engine.supported_services
    assert "iam" in engine.supported_services
    assert "lambda" in engine.supported_services
