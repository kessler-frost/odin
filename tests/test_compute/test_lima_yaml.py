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
    # vzNAT (macOS Virtualization.framework's own NAT device), not
    # socket_vmnet's "lima: shared" -- see generate_lima_yaml's own comment;
    # a real V3 boot fails hard against "lima: shared" without an external
    # socket_vmnet binary installed.
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config, shared_network=True))
    assert parsed["vmType"] == "vz"
    assert parsed["networks"] == [{"vzNAT": True}]


def test_no_shared_network_by_default():
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config))
    assert "networks" not in parsed


def test_nebulas_port_is_never_forwarded_to_the_host():
    """W2.6, found live: Lima forwards every guest listener to the host's
    127.0.0.1, so a VM's `nebula` daemon made `limactl` hold UDP
    127.0.0.1:4242 -- the very address a backing CONTAINER reaches the host
    lighthouse at (Colima maps host.docker.internal onto the host loopback).
    Every backing↔VM handshake was silently forwarded INTO a VM instead. A
    mesh data-plane port must never be host-forwarded."""
    config = VmConfig(cpus=1, memory="1GiB", disk="10GiB")
    parsed = yaml.safe_load(generate_lima_yaml(config, shared_network=True))
    assert parsed["portForwards"] == [
        {"guestPort": 4242, "proto": "udp", "ignore": True},
        {"guestPort": 4242, "proto": "tcp", "ignore": True},
    ]
