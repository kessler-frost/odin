
from odin.simulator.registry import ResourceRegistry


def test_register_resource(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg = ResourceRegistry(registry_path)
    reg.register("s3_my-bucket", service="s3", file_path=".odin/infra/s3_my-bucket.py")
    entry = reg.get("s3_my-bucket")
    assert entry.name == "s3_my-bucket"
    assert entry.service == "s3"
    assert entry.status == "draft"


def test_update_status(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg = ResourceRegistry(registry_path)
    reg.register("s3_test", service="s3", file_path=".odin/infra/s3_test.py")
    reg.update_status("s3_test", "live")
    assert reg.get("s3_test").status == "live"


def test_update_status_with_error(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg = ResourceRegistry(registry_path)
    reg.register("ec2_bad", service="ec2", file_path=".odin/infra/ec2_bad.py")
    reg.update_status("ec2_bad", "error", error="InvalidAMI")
    entry = reg.get("ec2_bad")
    assert entry.status == "error"
    assert entry.error == "InvalidAMI"


def test_remove_resource(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg = ResourceRegistry(registry_path)
    reg.register("s3_tmp", service="s3", file_path=".odin/infra/s3_tmp.py")
    reg.remove("s3_tmp")
    assert reg.get("s3_tmp") is None


def test_list_resources(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg = ResourceRegistry(registry_path)
    reg.register("s3_a", service="s3", file_path=".odin/infra/s3_a.py")
    reg.register("ec2_b", service="ec2", file_path=".odin/infra/ec2_b.py")
    assert len(reg.list_all()) == 2


def test_list_by_service(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg = ResourceRegistry(registry_path)
    reg.register("s3_a", service="s3", file_path=".odin/infra/s3_a.py")
    reg.register("s3_b", service="s3", file_path=".odin/infra/s3_b.py")
    reg.register("ec2_c", service="ec2", file_path=".odin/infra/ec2_c.py")
    s3_resources = reg.list_by_service("s3")
    assert len(s3_resources) == 2
    assert all(r.service == "s3" for r in s3_resources)


def test_persistence(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg1 = ResourceRegistry(registry_path)
    reg1.register("s3_persist", service="s3", file_path=".odin/infra/s3_persist.py")
    reg1.save()
    reg2 = ResourceRegistry(registry_path)
    entry = reg2.get("s3_persist")
    assert entry is not None
    assert entry.name == "s3_persist"


def test_get_errors(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"resources": {}}')
    reg = ResourceRegistry(registry_path)
    reg.register("s3_ok", service="s3", file_path=".odin/infra/s3_ok.py")
    reg.update_status("s3_ok", "live")
    reg.register("ec2_bad", service="ec2", file_path=".odin/infra/ec2_bad.py")
    reg.update_status("ec2_bad", "error", error="Boom")
    errors = reg.get_errors()
    assert len(errors) == 1
    assert errors[0].name == "ec2_bad"
