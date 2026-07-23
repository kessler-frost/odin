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


def test_cloud_init_with_nerdctl():
    script = generate_cloud_init(hostname="container-host", install_nerdctl=True)
    assert "nerdctl" in script
    assert "containerd" in script
    assert "hostnamectl set-hostname container-host" in script


_ENV_VARS = {
    "AWS_ACCESS_KEY_ID": "AKtest",
    "AWS_SECRET_ACCESS_KEY": "sec",
    "AWS_ENDPOINT_URL": "http://host.docker.internal:4266",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def test_cloud_init_env_vars_land_in_etc_environment_and_aws_credentials():
    script = generate_cloud_init(hostname="ec2-env", env_vars=_ENV_VARS)
    # /etc/environment: every KEY=value, no `export`
    assert "/etc/environment" in script
    assert "AWS_ACCESS_KEY_ID=AKtest" in script
    assert "AWS_SECRET_ACCESS_KEY=sec" in script
    assert "AWS_ENDPOINT_URL=http://host.docker.internal:4266" in script
    assert "AWS_DEFAULT_REGION=us-east-1" in script
    # ~/.aws/credentials: the two standard credential keys only
    assert ".aws/credentials" in script
    assert "[default]" in script
    assert "aws_access_key_id=AKtest" in script
    assert "aws_secret_access_key=sec" in script
    assert "aws_endpoint_url" not in script  # not a standard credentials-file key
    assert "aws_default_region" not in script
    assert "chmod 600" in script


def test_cloud_init_env_vars_work_without_ssh_pubkey():
    # env_vars is an independent section: it must compute its OWN LIMA_USER
    # even when the SSH-key section (which also computes it) never runs.
    script = generate_cloud_init(hostname="ec2-env", ssh_pubkey=None, env_vars=_ENV_VARS)
    assert "authorized_keys" not in script
    assert 'LIMA_USER="$(getent passwd 1000 | cut -d: -f1)"' in script
    assert "aws_access_key_id=AKtest" in script


def test_cloud_init_without_env_vars_has_no_credentials_section():
    script = generate_cloud_init(hostname="ec2-bare")
    assert ".aws/credentials" not in script
    assert "/etc/environment" not in script
