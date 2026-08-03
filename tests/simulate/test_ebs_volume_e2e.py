"""The EBS claim, proven against a REAL Lima VM with `lsblk`.

Everything else about `ebs` — the builder, the importer, the gateway state
machine — can be green while the disk does not exist. `DescribeVolumes`
returning what odin itself wrote proves the store round-trips and nothing
more. So this test asks the GUEST, through `limactl shell`, and the answer it
reads is a second block device of the requested size that was not there
before.

What it proves, in order, each through the real gateway handlers with a real
`InstanceVm` (no fake anywhere in this file):

  1. a real VM boots with NO extra disk       -- baseline `lsblk`
  2. `CreateVolume` makes a real `limactl disk` of the requested size
  3. `AttachVolume` REBOOTS it in and the guest really sees it -- the size in
     bytes is exact, and the device's real name is printed rather than
     assumed (it is `/dev/vdb`, not the `/dev/sdf` the API was handed)
  4. `DetachVolume` really takes it away    -- `lsblk` shows it gone
  5. `reclaim_env_disks` gives the space back, verified by LISTING what
     limactl reports, not by an exit code

Hygiene is absolute (the V3 brief's rule): a finalizer force-deletes the ONE
VM and the disks THIS test created, by exact name, whether it passed, failed
or raised. It never lists or touches anything outside its own `ebs-` prefix.

Cost: one real VM boot plus two real restarts. MEASURED at 72s and 81s on an
idle Mac. It is `integration`-only for that reason.

RUN IT ON A QUIET MACHINE, and know what the failure looks like if you do
not. Once, with two other `pytest -n auto` suites running on the same Mac,
`limactl start` died with `level=fatal msg="host agent process has exited:
signal: killed"` -- Lima's own hostagent killed out from under a VM that had
already reached "guest agent is running". That is machine pressure, not odin:
the identical test passed on the same commit before and after, and nothing
here retries or hides it. Recorded because a killed hostagent reads exactly
like a boot bug, and it cost a diagnosis once already.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time

import pytest

from odin.compute.instances import InstanceVm, disk_name, vm_name
from odin.gateway.models import ec2compute
from odin.gateway.stores import SynthStores

pytestmark = pytest.mark.integration

ENV = "ebs-e2e"
_NO_LIMA = shutil.which("limactl") is None


def _params(action: str, **kwargs: str) -> bytes:
    pairs = [f"Action={action}", "Version=2016-11-15", *(f"{k}={v}" for k, v in kwargs.items())]
    return "&".join(pairs).encode()


async def _call(stores: SynthStores, vm: InstanceVm, action: str, **kwargs: str) -> str:
    response = await ec2compute.pure_answer(
        f"ec2:{action}", "", ENV, _params(action, **kwargs), stores, time.monotonic(), vm,
    )
    assert response is not None, f"{action} fell through to ec2net"
    body = response.body.decode()
    assert response.status_code == 200, f"{action} failed: {body}"
    return body


def _between(text: str, tag: str) -> str:
    opening = f"<{tag}>"
    return text.split(opening, 1)[1].split(f"</{tag}>", 1)[0]


async def _lsblk(vm: InstanceVm, name: str) -> list[dict]:
    """The guest's own block devices, as JSON. `lsblk -J -b` is structured
    output rather than a column-parsed table -- the repo's "structured over
    regex" rule, and here it is also what makes the size assertion exact."""
    proc = await vm._lima("shell", name, "--", "lsblk", "-J", "-b", "-o", "NAME,SIZE,MOUNTPOINT")
    return json.loads(proc.stdout)["blockdevices"]


@pytest.mark.skipif(_NO_LIMA, reason="limactl is not installed")
async def test_a_drawn_volume_is_a_real_block_device_on_a_real_vm(tmp_path):
    stores = SynthStores(tmp_path)
    vm = InstanceVm()
    instance_id, volume_id, disk = "", "", ""
    try:
        # 1. A real VM, no extra disk. -----------------------------------
        run = await _call(stores, vm, "RunInstances", InstanceType="t3.micro", MinCount="1", MaxCount="1")
        instance_id = _between(run, "instanceId")
        name = vm_name(ENV, instance_id)
        # Waits for `running` but ALSO breaks on a terminal state, and that is
        # not a nicety. A boot that fails records `terminated` with the real
        # reason within seconds; a loop watching only for `running` then sits
        # out its entire deadline and reports a ten-minute timeout for a
        # twenty-second failure -- burying the one line that says what went
        # wrong under a wait that means nothing. Measured: exactly that, once.
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            state = (ec2compute._instance(stores, ENV, instance_id) or {}).get("state_name")
            if state in ("running", "terminated"):
                break
            await _sleep()
        record = ec2compute._instance(stores, ENV, instance_id)
        assert record["state_name"] == "running", f"the VM never booted: {record.get('state_reason')}"

        before = await _lsblk(vm, name)
        names_before = {d["name"] for d in before}

        # 2. A real disk. ------------------------------------------------
        created = await _call(stores, vm, "CreateVolume", Size="3", AvailabilityZone="us-east-1a")
        volume_id = _between(created, "volumeId")
        disk = disk_name(ENV, volume_id)
        assert _between(created, "status") == "available"
        on_machine = await vm.disk(disk)
        assert on_machine is not None, f"CreateVolume answered `available` but limactl has no disk {disk}"
        assert on_machine["size"] == 3 * 1024**3, on_machine

        # 3. Attach -- and ask the GUEST, not the record. -----------------
        await _call(stores, vm, "AttachVolume", VolumeId=volume_id, InstanceId=instance_id, Device="/dev/sdf")
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            state = (ec2compute._volume(stores, ENV, volume_id) or {})["state"]
            if state in ("in-use", "available"):
                break
            await _sleep()
        volume = ec2compute._volume(stores, ENV, volume_id)
        assert volume["state"] == "in-use", f"the attach did not hold: {volume.get('last_error')}"

        after = await _lsblk(vm, name)
        assert len(after) == len(before) + 1, f"before={before} after={after}"
        # KEYED ON THE MOUNT POINT, and never on the device name -- this test
        # got that wrong once and the real VM caught it, which is the best
        # possible argument for the limits.md entry it proves. Diffing device
        # NAMES finds `vdc`: the cloud-init `cidata` ISO is shoved along from
        # `vdb` to `vdc` by the arriving disk, so the "new" name belongs to
        # the ISO while the volume quietly TAKES the name `vdb` that was
        # already in the before-set. Device letters here are positional.
        # `mountPoint` is limactl's own answer, so this asks the component.
        mount = on_machine["mountPoint"]
        assert mount not in json.dumps(before), "the mount point existed before the attach"
        device = next((d for d in after if mount in json.dumps(d)), None)
        assert device is not None, f"{disk} is not mounted at {mount} in the guest: {after}"

        # THE assertion this whole file exists for: a real block device, of
        # exactly the size that was asked for, in a real running machine.
        assert device["size"] == 3 * 1024**3, device
        # ...and the honest half. `/dev/sdf` went in; this is what came out.
        assert device["name"] != "sdf"
        partition = next(c for c in device.get("children", []) if c.get("mountpoint") == mount)
        print(
            f"\nMEASURED: requested device_name=/dev/sdf; the guest reports /dev/{device['name']} "
            f"({device['size']} bytes), partitioned /dev/{partition['name']} and mounted by Lima "
            f"at {mount}.\nlsblk before attach: {json.dumps(before)}"
            f"\nlsblk after attach:  {json.dumps(after)}"
        )

        # 4. Detach -- really gone from the guest. ------------------------
        await _call(stores, vm, "DetachVolume", VolumeId=volume_id)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if (ec2compute._volume(stores, ENV, volume_id) or {})["state"] == "available":
                break
            await _sleep()
        assert ec2compute._volume(stores, ENV, volume_id)["state"] == "available"
        detached = await _lsblk(vm, name)
        assert mount not in json.dumps(detached), f"{disk} is STILL mounted after DetachVolume: {detached}"
        assert {d["name"] for d in detached} == names_before, detached

        # 5. Reclaim -- verified by LISTING, never by an exit code. -------
        await vm.delete(name)
        result = await ec2compute.reclaim_env_disks(stores, ENV, vm)
        assert result.reclaimed == (disk,), result
        assert not result.failed
        assert await vm.disk(disk) is None, "limactl still lists a disk odin reported reclaimed"
    finally:
        # By EXACT name, and only what this test made.
        if instance_id:
            await vm.delete(vm_name(ENV, instance_id))
        if disk:
            await vm._lima("disk", "delete", disk, check=False)


async def _sleep() -> None:
    await asyncio.sleep(2.0)
