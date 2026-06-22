from __future__ import annotations

import yaml

from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import VmConfig


def test_generates_valid_yaml():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    result = generate_lima_yaml(config)
    parsed = yaml.safe_load(result)
    assert parsed["cpus"] == 1
    assert parsed["memory"] == "1GiB"
    assert parsed["disk"] == "10GiB"


def test_includes_ubuntu_images():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config))
    assert len(parsed["images"]) >= 2
    arches = [img["arch"] for img in parsed["images"]]
    assert "x86_64" in arches
    assert "aarch64" in arches


def test_no_mounts():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config))
    assert parsed["mounts"] == []


def test_ssh_config():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config))
    assert parsed["ssh"]["forwardAgent"] is False
    assert parsed["ssh"]["loadDotSSHPubKeys"] is False


def test_includes_provision_script():
    config = VmConfig(cpus=2, memory="4GiB", disk="20GiB")
    cloud_init = "#!/bin/bash\nset -eux\nhostnamectl set-hostname test\n"
    parsed = yaml.safe_load(generate_lima_yaml(config, cloud_init_script=cloud_init))
    assert len(parsed["provision"]) == 1
    assert parsed["provision"][0]["mode"] == "system"
    assert "hostnamectl" in parsed["provision"][0]["script"]


def test_no_provision_without_cloud_init():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config))
    assert parsed.get("provision") is None or parsed["provision"] == []


def test_shared_network():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config, shared_network=True))
    assert parsed["networks"] == [{"lima": "shared"}]


def test_no_shared_network_by_default():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config))
    assert "networks" not in parsed
