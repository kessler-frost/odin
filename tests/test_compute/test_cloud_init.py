from __future__ import annotations

import pytest

from odin.compute.cloud_init import (
    HOSTS_BEGIN,
    HOSTS_END,
    generate_cloud_init,
    hosts_block_script,
)


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


# --- route53: the odin-owned /etc/hosts block --------------------------------


def test_hosts_block_deletes_before_it_appends():
    """The whole point. This is a PER-BOOT provision script, so an append-only
    writer would duplicate every entry on every boot -- the same trap the
    `/etc/environment` section documents. Delete-then-append also makes
    REMOVAL work, which no appender can do."""
    script = hosts_block_script({"api.internal": "10.42.0.5"})
    assert script.index(f"sed -i '/^{HOSTS_BEGIN}$/,/^{HOSTS_END}$/d' /etc/hosts") < script.index("cat >> /etc/hosts")
    assert "10.42.0.5\tapi.internal" in script


def test_hosts_block_never_truncates_the_file():
    """`/etc/hosts` is NOT odin's file -- it arrives with the image's loopback
    entries and Lima's hostname line, and a `cat >` would destroy them and
    break resolution for the whole guest. Only `cat >>` is allowed."""
    script = hosts_block_script({"api.internal": "10.42.0.5"})
    assert "cat > /etc/hosts" not in script
    assert "cat >> /etc/hosts" in script


def test_hosts_block_is_sorted_and_order_independent():
    """Identical record SETS must render identical BYTES, or the no-churn
    comparison in `InstanceVm.push_hosts` fires on every Apply."""
    one = hosts_block_script({"web.internal": "10.42.0.9", "api.internal": "10.42.0.5"})
    two = hosts_block_script({"api.internal": "10.42.0.5", "web.internal": "10.42.0.9"})
    assert one == two
    assert one.index("api.internal") < one.index("web.internal")


def test_an_empty_record_set_still_deletes_the_block():
    """How the LAST record being removed reaches the guest. An empty map is an
    instruction ("resolve nothing of odin's"), not a no-op -- if this emitted
    nothing, a deleted record would keep resolving until the VM was rebuilt."""
    script = hosts_block_script({})
    assert f"sed -i '/^{HOSTS_BEGIN}$/,/^{HOSTS_END}$/d' /etc/hosts" in script
    assert HOSTS_BEGIN in script and HOSTS_END in script


@pytest.mark.parametrize("name,address", [
    ("api internal", "10.42.0.5"),      # a space makes the next field an alias
    ("api.internal", "10.42.0.5 evil"), # ...and here, a second name for that IP
    ("api\ninternal", "10.42.0.5"),     # a newline writes an arbitrary extra line
    ("", "10.42.0.5"),
    ("api.internal", ""),
])
def test_a_malformed_entry_is_refused_rather_than_mangled(name, address):
    """This is the boundary where canvas-shaped text becomes a file the
    resolver obeys, so a value that would mean something else must raise
    rather than be written."""
    with pytest.raises(ValueError, match="whitespace-free"):
        hosts_block_script({name: address})


def test_cloud_init_places_hosts_before_user_data():
    """A UserData script that dials a drawn name must find it already
    resolving, or the record is real everywhere except where the user's own
    code runs."""
    script = generate_cloud_init(
        hostname="ec2-test", hosts={"api.internal": "10.42.0.5"},
        extra_script="curl http://api.internal",
    )
    assert script.index("10.42.0.5\tapi.internal") < script.index("curl http://api.internal")


def test_cloud_init_without_hosts_has_no_block():
    """`None` means "this caller has nothing to say about hosts" and must
    leave the guest's file completely alone -- unchanged behaviour for every
    existing caller, including `LimaRuntime.ensure_host`."""
    script = generate_cloud_init(hostname="ec2-bare")
    assert "/etc/hosts" not in script
    assert HOSTS_BEGIN not in script


def test_cloud_init_with_an_empty_hosts_map_still_emits_the_block():
    """`is not None`, not truthiness -- see `test_an_empty_record_set...`."""
    script = generate_cloud_init(hostname="ec2-bare", hosts={})
    assert HOSTS_BEGIN in script
