from __future__ import annotations


def generate_cloud_init(
    hostname: str,
    ssh_pubkey: str | None = None,
    nebula_ca_crt: str | None = None,
    nebula_host_crt: str | None = None,
    nebula_host_key: str | None = None,
    nebula_config: str | None = None,
    install_nerdctl: bool = False,
) -> str:
    lines = [
        "#!/bin/bash",
        "set -eux -o pipefail",
        "",
        f"hostnamectl set-hostname {hostname}",
    ]

    if ssh_pubkey:
        lines.extend([
            "",
            "# Install SSH public key",
            "mkdir -p /home/${LIMA_CIDATA_USER}/.ssh",
            f'echo "{ssh_pubkey}" >> /home/${{LIMA_CIDATA_USER}}/.ssh/authorized_keys',
            "chown -R ${LIMA_CIDATA_USER}:${LIMA_CIDATA_USER} /home/${LIMA_CIDATA_USER}/.ssh",
            "chmod 700 /home/${LIMA_CIDATA_USER}/.ssh",
            "chmod 600 /home/${LIMA_CIDATA_USER}/.ssh/authorized_keys",
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

    if nebula_ca_crt and nebula_host_crt and nebula_host_key and nebula_config:
        lines.extend([
            "",
            "# Configure Nebula overlay network",
            "mkdir -p /etc/nebula",
            "cat > /etc/nebula/ca.crt << 'ODIN_NEBULA_CA'",
            nebula_ca_crt,
            "ODIN_NEBULA_CA",
            "cat > /etc/nebula/host.crt << 'ODIN_NEBULA_CRT'",
            nebula_host_crt,
            "ODIN_NEBULA_CRT",
            "cat > /etc/nebula/host.key << 'ODIN_NEBULA_KEY'",
            nebula_host_key,
            "ODIN_NEBULA_KEY",
            "cat > /etc/nebula/config.yaml << 'ODIN_NEBULA_CFG'",
            nebula_config,
            "ODIN_NEBULA_CFG",
            "",
            "# Create and start Nebula systemd service",
            "cat > /etc/systemd/system/nebula.service << 'ODIN_NEBULA_UNIT'",
            "[Unit]",
            "Description=Nebula Overlay Network",
            "After=network.target",
            "[Service]",
            "ExecStart=/usr/local/bin/nebula -config /etc/nebula/config.yaml",
            "Restart=always",
            "[Install]",
            "WantedBy=multi-user.target",
            "ODIN_NEBULA_UNIT",
            "systemctl daemon-reload",
            "systemctl enable --now nebula",
        ])

    return "\n".join(lines) + "\n"
