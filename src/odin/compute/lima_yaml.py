from __future__ import annotations

import yaml

from odin.compute.models import VmConfig

UBUNTU_IMAGES = [
    {
        "location": "https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img",
        "arch": "x86_64",
    },
    {
        "location": "https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-arm64.img",
        "arch": "aarch64",
    },
]

NEBULA_INSTALL_SCRIPT = """#!/bin/bash
set -eux -o pipefail
NEBULA_VERSION="1.9.5"
ARCH=$(uname -m)
case $ARCH in
  aarch64|arm64) ARCH="arm64" ;;
  x86_64) ARCH="amd64" ;;
esac
curl -fsSL -o /tmp/nebula.tar.gz \
  "https://github.com/slackhq/nebula/releases/download/v${NEBULA_VERSION}/nebula-linux-${ARCH}.tar.gz"
tar -xzf /tmp/nebula.tar.gz -C /usr/local/bin nebula nebula-cert
chmod +x /usr/local/bin/nebula /usr/local/bin/nebula-cert
rm /tmp/nebula.tar.gz
"""


def generate_lima_yaml(
    config: VmConfig,
    cloud_init_script: str | None = None,
    install_nebula: bool = False,
    shared_network: bool = False,
) -> str:
    doc: dict = {
        "cpus": config.cpus,
        "memory": config.memory,
        "disk": config.disk,
        "images": UBUNTU_IMAGES,
        "mounts": [],
        "ssh": {
            "forwardAgent": False,
            "loadDotSSHPubKeys": False,
        },
    }

    if shared_network:
        doc["networks"] = [{"lima": "shared"}]

    provision = []
    if install_nebula:
        provision.append({"mode": "system", "script": NEBULA_INSTALL_SCRIPT})
    if cloud_init_script:
        provision.append({"mode": "system", "script": cloud_init_script})
    doc["provision"] = provision

    return yaml.dump(doc, default_flow_style=False, sort_keys=False)
