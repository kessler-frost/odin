from __future__ import annotations


def generate_cloud_init(
    hostname: str,
    ssh_pubkey: str | None = None,
    install_nerdctl: bool = False,
    extra_script: str | None = None,
    env_vars: dict[str, str] | None = None,
) -> str:
    lines = [
        "#!/bin/bash",
        # NOT `set -e`: this runs as a Lima per-boot provision script, and if any
        # one command fails the whole boot script fails — which leaves
        # `limactl start` waiting forever for a readiness it never gets.
        "set -ux",
        "",
        f"hostnamectl set-hostname {hostname} || true",
    ]

    if ssh_pubkey:
        # Detect the VM's regular user at runtime (UID 1000). Lima warns against
        # referencing LIMA_CIDATA_* in provision scripts (and they're undefined
        # there), which left provisioning unfinished so `limactl start` hung.
        lines.extend([
            "",
            "# Install SSH public key",
            'LIMA_USER="$(getent passwd 1000 | cut -d: -f1)"',
            'mkdir -p "/home/${LIMA_USER}/.ssh"',
            f'echo "{ssh_pubkey}" >> "/home/${{LIMA_USER}}/.ssh/authorized_keys"',
            'chown -R "${LIMA_USER}:${LIMA_USER}" "/home/${LIMA_USER}/.ssh"',
            'chmod 700 "/home/${LIMA_USER}/.ssh"',
            'chmod 600 "/home/${LIMA_USER}/.ssh/authorized_keys"',
        ])

    if env_vars:
        # Workload identity (gateway/keys.py::workload_env): the four AWS-SDK
        # env vars land system-wide in /etc/environment (overwritten, not
        # appended -- this is a per-boot provision script, appending would
        # duplicate lines every boot) AND the credential pair lands in the
        # default user's ~/.aws/credentials ([default] holds ONLY the two
        # standard credentials-file keys; endpoint/region aren't valid there).
        # Computes its OWN LIMA_USER: this section and the SSH-key one are
        # independent conditionals -- either can run without the other.
        environment_lines = "\n".join(f"{key}={value}" for key, value in env_vars.items())
        lines.extend([
            "",
            "# Inject AWS credentials + endpoint (odin gateway workload identity)",
            "cat > /etc/environment << 'ODIN_ETC_ENVIRONMENT'",
            environment_lines,
            "ODIN_ETC_ENVIRONMENT",
            'LIMA_USER="$(getent passwd 1000 | cut -d: -f1)"',
            'mkdir -p "/home/${LIMA_USER}/.aws"',
            'cat > "/home/${LIMA_USER}/.aws/credentials" << \'ODIN_AWS_CREDENTIALS\'',
            "[default]",
            f"aws_access_key_id={env_vars.get('AWS_ACCESS_KEY_ID', '')}",
            f"aws_secret_access_key={env_vars.get('AWS_SECRET_ACCESS_KEY', '')}",
            "ODIN_AWS_CREDENTIALS",
            'chown -R "${LIMA_USER}:${LIMA_USER}" "/home/${LIMA_USER}/.aws"',
            'chmod 600 "/home/${LIMA_USER}/.aws/credentials"',
        ])

    if install_nerdctl:
        lines.extend([
            "",
            "# Install containerd + nerdctl",
            "apt-get update -qq",
            "apt-get install -y -qq containerd",
            "systemctl enable --now containerd",
            'ARCH=$(uname -m)',
            'case $ARCH in aarch64|arm64) ARCH="arm64" ;; x86_64) ARCH="amd64" ;; esac',
            'NERDCTL_VERSION="2.0.3"',
            'curl -fsSL -o /tmp/nerdctl.tar.gz '
            '"https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${ARCH}.tar.gz"',
            'tar -xzf /tmp/nerdctl.tar.gz -C /usr/local/bin nerdctl',
            'chmod +x /usr/local/bin/nerdctl',
            'rm /tmp/nerdctl.tar.gz',
            '',
            '# Install buildkit for nerdctl build',
            'BUILDKIT_VERSION="0.19.0"',
            'curl -fsSL -o /tmp/buildkit.tar.gz '
            '"https://github.com/moby/buildkit/releases/download/v${BUILDKIT_VERSION}/buildkit-v${BUILDKIT_VERSION}.linux-${ARCH}.tar.gz"',
            'tar -xzf /tmp/buildkit.tar.gz -C /usr/local --strip-components=1 bin/buildkitd bin/buildctl',
            'rm /tmp/buildkit.tar.gz',
            '',
            '# Create and start buildkitd systemd service',
            'cat > /etc/systemd/system/buildkitd.service << \'ODIN_BUILDKIT_UNIT\'',
            '[Unit]',
            'Description=BuildKit Daemon',
            'After=containerd.service',
            '[Service]',
            'ExecStart=/usr/local/bin/buildkitd --oci-worker-no-process-sandbox',
            'Restart=always',
            '[Install]',
            'WantedBy=multi-user.target',
            'ODIN_BUILDKIT_UNIT',
            'systemctl daemon-reload',
            'systemctl enable --now buildkitd',
        ])

    if extra_script:
        # V3: compute/instances.py composes this from two sources -- an
        # instance's Nebula cert/config files (written verbatim, no download)
        # and/or its RunInstances `UserData` (already base64-decoded by the
        # caller) -- both are plain bash appended after the base provisioning
        # above, one script, one Lima `provision` entry.
        lines.extend(["", "# Extra provisioning (Nebula join / instance user_data)", extra_script])

    return "\n".join(lines) + "\n"
