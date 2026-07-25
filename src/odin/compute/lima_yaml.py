from __future__ import annotations

import yaml

from odin.compute.models import VmConfig
from odin.fabric.nebula import NEBULA_PORT

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
        # vzNAT, NOT socket_vmnet's "lima: shared" network (prior-odin's own
        # lesson, carried into the V3 brief verbatim): `{"lima": "shared"}`
        # is Lima's OLDER vmnet-framework network, which requires an
        # external `socket_vmnet` binary installed at a fixed system path --
        # not present on a stock Colima/Lima setup, and a real V3 flagship
        # boot fails hard on it ("paths.socketVMNet has to be installed").
        # vzNAT is macOS Virtualization.framework's OWN NAT device (`vmType:
        # vz`, already Colima's default driver here) -- host-reachable, no
        # extra binary, verified against limactl's own bundled
        # templates/default.yaml.
        doc["vmType"] = "vz"
        doc["networks"] = [{"vzNAT": True}]

    # W2.6 -- a REAL finding, found live: Lima automatically forwards every
    # port a guest listens on to the HOST's 127.0.0.1, and that includes the
    # `nebula` daemon's own UDP 4242 inside an EC2 VM. `limactl` then HOLDS
    # 127.0.0.1:4242 on the host (confirmed with lsof: `limactl ... UDP
    # 127.0.0.1:4242` right next to the host lighthouse's own `[::]:4242`),
    # which silently steals the exact address a CONTAINER reaches the
    # lighthouse at: Colima's user-mode network maps `host.docker.internal`
    # (192.168.5.2) onto the host's loopback, so every backing sidecar's
    # handshake packet was being forwarded INTO a VM instead of to the
    # lighthouse -- backing↔VM mesh traffic could never work while an EC2 VM
    # existed, even though VM↔VM (which rides the vzNAT address, not
    # loopback) was fine. Forwarding a mesh data-plane port to the host was
    # never wanted in the first place, so it is ignored explicitly.
    doc["portForwards"] = [
        {"guestPort": NEBULA_PORT, "proto": proto, "ignore": True} for proto in ("udp", "tcp")
    ]

    provision = []
    if cloud_init_script:
        provision.append({"mode": "system", "script": cloud_init_script})
    doc["provision"] = provision

    return yaml.dump(doc, default_flow_style=False, sort_keys=False)
