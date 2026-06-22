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

def generate_lima_yaml(
    config: VmConfig,
    cloud_init_script: str | None = None,
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
    if cloud_init_script:
        provision.append({"mode": "system", "script": cloud_init_script})
    doc["provision"] = provision

    return yaml.dump(doc, default_flow_style=False, sort_keys=False)
