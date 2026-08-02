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

def additional_disks(names: tuple[str, ...] | list[str]) -> list[dict]:
    """Lima's `additionalDisks` entries for named `limactl disk` volumes --
    the EBS substrate (`gateway/models/ec2compute.py`'s volume actions).

    MEASURED against real limactl 2.1.3 + a real vz VM (2026-08-02), because
    every claim below is one this repo would otherwise be guessing at:

    - the guest device is `/dev/vdb` (virtio), NOT the `/dev/sdf` an
      `aws_volume_attachment` asks for -- an AWS device name is ADVISORY here
      and `docs/limits.md` says so;
    - Lima FORMATS and MOUNTS the disk itself: it arrives partitioned
      (`vdb1`, ext4) and mounted at `/mnt/lima-<disk name>`, where AWS hands
      you a raw device to `mkfs` yourself;
    - the cloud-init `cidata` ISO SHIFTS from `vdb` to `vdc` once an extra
      disk exists, so device letters are positional and not a contract;
    - a disk can only be attached to a STOPPED instance (`limactl edit` on a
      running one is `fatal: cannot edit a running instance`, exit 1), which
      is why `InstanceVm.attach_disk` reboots.

    The dict form (`- name: X`) is used rather than the bare-string shorthand
    because it is the form probed live.
    """
    return [{"name": name} for name in names]


def generate_lima_yaml(
    config: VmConfig,
    cloud_init_script: str | None = None,
    shared_network: bool = False,
    disks: tuple[str, ...] | list[str] = (),
) -> str:
    doc: dict = {
        "cpus": config.cpus,
        "memory": config.memory,
        "disk": config.disk,
        "images": UBUNTU_IMAGES,
        "mounts": [],
        # Always emitted, empty or not (like `mounts`): `InstanceVm.set_disks`
        # REPLACES this key wholesale on every attach/detach, so a document
        # whose shape depends on whether a disk happened to exist at birth
        # would make the two paths disagree.
        "additionalDisks": additional_disks(disks),
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
