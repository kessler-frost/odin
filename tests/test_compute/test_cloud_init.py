from __future__ import annotations

from odin.compute.cloud_init import generate_cloud_init


def test_cloud_init_sets_hostname():
    script = generate_cloud_init(hostname="ec2-web-server")
    assert "hostnamectl set-hostname ec2-web-server" in script


def test_cloud_init_includes_ssh_key():
    script = generate_cloud_init(
        hostname="ec2-test",
        ssh_pubkey="ssh-ed25519 AAAAC3Nz... user@host",
    )
    assert "ssh-ed25519 AAAAC3Nz... user@host" in script
    assert "authorized_keys" in script


def test_cloud_init_without_ssh_key():
    script = generate_cloud_init(hostname="ec2-bare")
    assert "authorized_keys" not in script


def test_cloud_init_is_valid_bash():
    script = generate_cloud_init(hostname="ec2-test")
    assert script.startswith("#!/bin/bash")
    # `set -ux` (no `-e`): a per-boot provision script must not hard-fail, or
    # `limactl start` hangs waiting for a readiness it never gets.
    assert "set -ux" in script
    assert "set -e" not in script


def test_cloud_init_with_nebula():
    script = generate_cloud_init(
        hostname="ec2-test",
        nebula_ca_crt="---CA CERT---",
        nebula_host_crt="---HOST CERT---",
        nebula_host_key="---HOST KEY---",
        nebula_config="pki:\n  ca: /etc/nebula/ca.crt\n",
    )
    assert "/etc/nebula" in script
    assert "---CA CERT---" in script
    assert "---HOST CERT---" in script
    assert "---HOST KEY---" in script
    assert "nebula.service" in script
    assert "systemctl" in script


def test_cloud_init_without_nebula_no_nebula_content():
    script = generate_cloud_init(hostname="ec2-plain")
    assert "/etc/nebula" not in script
    assert "nebula.service" not in script


def test_cloud_init_with_nerdctl():
    script = generate_cloud_init(hostname="container-host", install_nerdctl=True)
    assert "nerdctl" in script
    assert "containerd" in script
    assert "hostnamectl set-hostname container-host" in script
