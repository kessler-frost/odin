"""EBS volumes as REAL Lima disks — the gateway state machine over them.

Every assertion here is about the thing this repo keeps getting wrong: a
record that says `in-use` when nothing is attached, a `destroyed` over disk
that is still on the machine, an `available` over a disk no `DeleteVolume`
can ever remove again. The substrate is faked (`FakeInstanceVm`) so the state
machine is testable without a 60-second VM reboot per case; that the REAL
substrate does what the fake claims is proven separately, and only there, by
`tests/simulate/test_ebs_volume_e2e.py` driving a real VM and reading
`lsblk`. A fake can prove the machine; only the VM can prove the disk.

The helpers are imported from `test_ec2compute` rather than re-spelled — the
same arrangement `test_empty_reasons.py` uses, and for the same reason: two
copies of `_answer` would drift.
"""
from __future__ import annotations

import asyncio

import pytest

from odin.iac import hcl
from odin.compute.instances import disk_name
from odin.gateway.models import ec2compute
from odin.gateway.stores import SynthStores
from odin.spec.models import ResourceDesired

from tests.gateway.test_ec2compute import (
    ENV,
    FakeInstanceVm,
    _answer,
    _parse,
    _raw,
    _run_instance,
    _subnet,
    _wait_for_state,
)


@pytest.fixture
def stores(tmp_path) -> SynthStores:
    # Same one-liner as `test_ec2compute.py`'s, and deliberately NOT imported:
    # a fixture imported by name is a fixture two modules share a `tmp_path`
    # namespace through, which is the sort of cross-file coupling that turns
    # into a phantom flake under `-n auto`.
    return SynthStores(tmp_path)


async def _create_volume(stores, sink, ec2, vm, **kwargs) -> dict:
    params = {"Size": 10, "AvailabilityZone": "us-east-1a", **kwargs}
    req = sink.call(lambda: ec2.create_volume(**params))
    return _parse("CreateVolume", await _answer(stores, req, vm))


async def _volumes(stores, sink, ec2, vm) -> list[dict]:
    req = sink.call(lambda: ec2.describe_volumes())
    return _parse("DescribeVolumes", await _answer(stores, req, vm))["Volumes"]


async def _wait_for_volume(stores, sink, ec2, vm, volume_id: str, want: str, timeout: float = 2.0) -> dict:
    """Poll DescribeVolumes until the background attach/detach lands.

    A poll and not a sleep: an `asyncio.Task` has no head start (the repo's
    own de-threading lesson), so reading the store straight after the call
    would be asserting on a coincidence."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        volume = next(v for v in await _volumes(stores, sink, ec2, vm) if v["VolumeId"] == volume_id)
        if volume["State"] == want:
            return volume
        await asyncio.sleep(0.02)
    raise AssertionError(f"{volume_id} never reached {want!r}; last state was {volume['State']!r}")


# --- CreateVolume: a real disk exists before any record claims one ----------


async def test_create_volume_creates_a_real_disk_before_recording_it(sink, ec2, stores):
    vm = FakeInstanceVm()
    volume = await _create_volume(stores, sink, ec2, vm, Size=25)

    assert volume["VolumeId"].startswith("vol-")
    assert volume["Size"] == 25
    assert volume["State"] == "available"
    assert volume["AvailabilityZone"] == "us-east-1a"
    # The claim that matters: a REAL disk of that size, named for this env.
    assert vm.created_disks == [(disk_name(ENV, volume["VolumeId"]), 25)]


async def test_a_volume_nothing_could_create_is_an_error_and_not_a_record(sink, ec2, stores):
    """Mutation-test: make `_create_volume` write the record before awaiting
    `create_disk` (or ignore its failure) and this fails -- which is the whole
    point. A store entry over a disk that does not exist is the decorative
    bug, and it is worse here than usual because `DescribeVolumes` would then
    report it `available` forever."""
    vm = FakeInstanceVm(fail_disk_create=True)
    req = sink.call(lambda: ec2.create_volume(Size=10, AvailabilityZone="us-east-1a"))
    parsed = _parse("CreateVolume", await _answer(stores, req, vm), error=True)

    assert parsed["Error"]["Code"] == "InternalError"
    assert "no space left on device" in parsed["Error"]["Message"]
    assert await _volumes(stores, sink, ec2, vm) == []


async def test_a_volume_with_no_size_is_refused_by_name(sink, ec2, stores):
    vm = FakeInstanceVm()
    parsed = _parse("CreateVolume", await _raw(stores, "CreateVolume", {"AvailabilityZone": "us-east-1a"}, vm), error=True)
    assert parsed["Error"]["Code"] == "InvalidParameterValue"
    assert vm.created_disks == []


async def test_a_volume_with_no_availability_zone_is_refused_by_name(sink, ec2, stores):
    vm = FakeInstanceVm()
    parsed = _parse("CreateVolume", await _raw(stores, "CreateVolume", {"Size": "10"}, vm), error=True)
    assert parsed["Error"]["Code"] == "MissingParameter"
    assert vm.created_disks == []


# --- AttachVolume: the reboot, and what it must not claim -------------------


async def test_attach_reboots_the_instance_and_only_then_says_in_use(sink, ec2, stores):
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    volume = await _create_volume(stores, sink, ec2, vm)

    req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device="/dev/sdf"))
    attachment = _parse("AttachVolume", await _answer(stores, req, vm))

    # AWS answers `attaching`, and here that is not a formality: a whole VM
    # restart is really about to happen.
    assert attachment["State"] == "attaching"
    assert attachment["Device"] == "/dev/sdf"

    landed = await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")
    assert landed["Attachments"][0]["InstanceId"] == instance_id
    assert landed["Attachments"][0]["State"] == "attached"
    # The REAL disk was handed to the VM, and its arrival was verified in the
    # guest rather than assumed from a successful yaml edit.
    (name, disks), = vm.disk_sets
    assert disks == [disk_name(ENV, volume["VolumeId"])]
    assert vm.verified == [disk_name(ENV, volume["VolumeId"])]


async def test_an_attach_that_never_reached_the_guest_does_not_report_in_use(sink, ec2, stores):
    """The single most important test in this file. `attach_disk` raises when
    the disk is not mounted in the booted guest; the volume must fall back to
    `available` and carry the reason, NOT sit at `in-use` over a machine where
    `lsblk` shows nothing.

    Mutation-test: drop the `except` in `_finish_attach` (or set `in-use`
    unconditionally) and this fails."""
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    volume = await _create_volume(stores, sink, ec2, vm)
    vm.fail_attach = True

    req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device="/dev/sdf"))
    await _answer(stores, req, vm)

    back = await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "available")
    assert back["Attachments"] == []
    record = ec2compute._volume(stores, ENV, volume["VolumeId"])
    assert "is not mounted at" in record["last_error"]


async def test_the_providers_own_attachment_waiter_query_is_answerable(sink, ec2, stores):
    """`aws_volume_attachment` waits by polling DescribeVolumes with THREE
    filters at once and reading `Attachments[0].State`, with `attaching` as
    its only pending value. Two ways that silently fails, both fixed here and
    neither visible to a test that filters on one name:

      * `_matches` requires EVERY named filter to match, so an unmodelled
        name does not narrow the result -- it EMPTIES it. `attachment.device`
        was missing, which would have returned nothing to every poll.
      * an `attaching` volume rendered an empty `<attachmentSet/>`, so the
        waiter had no attachment to read at all.

    Mutation-test: remove `attachment.device` from `_volume_filter_attrs`, or
    take `attaching` out of `_VOLUME_ATTACHED`, and this fails."""
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    volume = await _create_volume(stores, sink, ec2, vm)

    # Held mid-reboot, which is the state the waiter actually polls against.
    ec2compute._update_volume(
        stores, ENV, volume["VolumeId"], state="attaching", instance_id=instance_id,
        device="/dev/sdf", attach_time="2026-08-02T00:00:00.000Z",
    )
    req = sink.call(lambda: ec2.describe_volumes(Filters=[
        {"Name": "volume-id", "Values": [volume["VolumeId"]]},
        {"Name": "attachment.instance-id", "Values": [instance_id]},
        {"Name": "attachment.device", "Values": ["/dev/sdf"]},
    ]))
    (found,) = _parse("DescribeVolumes", await _answer(stores, req, vm))["Volumes"]

    assert found["State"] == "attaching"
    assert found["Attachments"][0]["State"] == "attaching"
    assert found["Attachments"][0]["Device"] == "/dev/sdf"


async def test_attaching_a_volume_that_is_already_attached_is_refused(sink, ec2, stores):
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    volume = await _create_volume(stores, sink, ec2, vm)
    req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device="/dev/sdf"))
    await _answer(stores, req, vm)
    await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")

    again = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device="/dev/sdg"))
    parsed = _parse("AttachVolume", await _answer(stores, again, vm), error=True)
    assert parsed["Error"]["Code"] == "VolumeInUse"


async def test_attaching_to_an_unknown_instance_or_volume_is_named(sink, ec2, stores):
    vm = FakeInstanceVm()
    volume = await _create_volume(stores, sink, ec2, vm)

    req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId="i-0000", Device="/dev/sdf"))
    assert _parse("AttachVolume", await _answer(stores, req, vm), error=True)["Error"]["Code"] == "InvalidInstanceID.NotFound"

    req = sink.call(lambda: ec2.attach_volume(VolumeId="vol-0000", InstanceId="i-0000", Device="/dev/sdf"))
    assert _parse("AttachVolume", await _answer(stores, req, vm), error=True)["Error"]["Code"] == "InvalidVolume.NotFound"


async def test_two_volumes_on_one_instance_keep_a_stable_disk_order(sink, ec2, stores):
    """The guest's `/dev/vd*` letters are POSITIONAL (measured), so the list
    handed to Lima must be ordered by volume id and stay that way -- otherwise
    adding a second disk renumbers the first one under a mounted filesystem.

    Mutation-test: drop the `sorted(...)` in `_attached_disks` and this fails
    for any pair whose insertion order differs from their id order."""
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    ids = []
    for device in ("/dev/sdf", "/dev/sdg"):
        volume = await _create_volume(stores, sink, ec2, vm)
        ids.append(volume["VolumeId"])
        req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device=device))
        await _answer(stores, req, vm)
        await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")

    expected = sorted(disk_name(ENV, i) for i in ids)
    assert vm.disk_sets[-1][1] == expected
    # ...and the first attach saw only the first disk, so nothing was ever
    # handed a list that would have reordered a live device.
    assert vm.disk_sets[0][1] == [disk_name(ENV, ids[0])]


async def test_two_attachments_planned_in_parallel_both_land(sink, ec2, stores):
    """Terraform plans the `aws_volume_attachment`s for one instance in
    PARALLEL, so both AttachVolume calls arrive before either reboot
    finishes. Two ways that used to break, both real:

      * the second call found the instance already `pending` (the first
        attach put it there) and was refused `IncorrectState` -- odin
        declining an ordinary two-disk canvas;
      * with that relaxed, the two stop/start cycles interleaved on ONE VM,
        and a `limactl edit` landing during a `limactl start` is refused
        outright.

    So the API accepts `pending` and the VM work is serialised per VM name.
    Both volumes must end `in-use`, and the LAST list handed to Lima must
    contain BOTH disks -- a second reboot that dropped the first disk would
    detach a live volume by omission.

    Mutation-test: restore `!= "running"`, or drop the `_attach_lock`
    around `attach_disk`, and this fails."""
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    first = await _create_volume(stores, sink, ec2, vm)
    second = await _create_volume(stores, sink, ec2, vm)
    reqs = [
        sink.call(lambda: ec2.attach_volume(VolumeId=first["VolumeId"], InstanceId=instance_id, Device="/dev/sdf")),
        sink.call(lambda: ec2.attach_volume(VolumeId=second["VolumeId"], InstanceId=instance_id, Device="/dev/sdg")),
    ]
    # Both dispatched before either background reboot is awaited -- the real
    # arrival pattern, not one-after-the-other.
    for req in reqs:
        parsed = _parse("AttachVolume", await _answer(stores, req, vm))
        assert parsed["State"] == "attaching", parsed

    for volume in (first, second):
        await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")

    expected = sorted(disk_name(ENV, v["VolumeId"]) for v in (first, second))
    assert vm.disk_sets[-1][1] == expected
    # The serialisation itself, from a witness inside the fake's own reboot:
    # two tasks must never have been in it at once. `FakeInstanceVm._reboot`
    # really suspends for this reason -- a straight-through fake cannot
    # interleave, so this assertion would have passed with the lock removed.
    assert vm.overlapped is False


def test_the_disk_order_is_sorted_and_not_merely_insertion_order(stores):
    """The mutation-testable half of the test above, and it needs its own
    case because volume ids are RANDOM: with `sorted()` removed, insertion
    order happens to equal id order about half the time, so the end-to-end
    test alone would be a coin flip rather than a ratchet. Here the records
    are seeded in deliberately REVERSE order, so the two orders can never
    agree by luck.

    Mutation-test: drop the `sorted(...)` in `_attached_disks` and this
    fails every run."""
    for volume_id in ("vol-zzz", "vol-aaa"):
        stores.ec2compute.set(ENV, f"volume:{volume_id}", {
            "volume_id": volume_id, "state": "in-use", "instance_id": "i-1",
            "disk": disk_name(ENV, volume_id), "size": 1,
            "availability_zone": "us-east-1a", "create_time": "2026-08-02T00:00:00.000Z",
        })

    assert ec2compute._attached_disks(stores, ENV, "i-1") == [
        disk_name(ENV, "vol-aaa"), disk_name(ENV, "vol-zzz"),
    ]


def test_a_detaching_volume_is_not_handed_back_to_lima(stores):
    """`detaching` means "on its way out", so it must NOT be in the list that
    becomes the VM's `additionalDisks:` -- otherwise the detach reboot would
    put the disk straight back in."""
    stores.ec2compute.set(ENV, "volume:vol-1", {
        "volume_id": "vol-1", "state": "detaching", "instance_id": "i-1",
        "disk": disk_name(ENV, "vol-1"), "size": 1,
        "availability_zone": "us-east-1a", "create_time": "2026-08-02T00:00:00.000Z",
    })

    assert ec2compute._attached_disks(stores, ENV, "i-1") == []


# --- Detach and Delete -----------------------------------------------------


async def test_detach_frees_the_volume_and_a_failed_detach_keeps_it_attached(sink, ec2, stores):
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    volume = await _create_volume(stores, sink, ec2, vm)
    req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device="/dev/sdf"))
    await _answer(stores, req, vm)
    await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")

    # A detach that could not restart the VM leaves the disk exactly where it
    # is, so the record must keep saying so -- an `available` here would let
    # `DeleteVolume` try, and limactl would refuse.
    vm.fail_attach = True
    req = sink.call(lambda: ec2.detach_volume(VolumeId=volume["VolumeId"]))
    await _answer(stores, req, vm)
    still = await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")
    assert still["Attachments"][0]["InstanceId"] == instance_id

    vm.fail_attach = False
    req = sink.call(lambda: ec2.detach_volume(VolumeId=volume["VolumeId"]))
    assert _parse("DetachVolume", await _answer(stores, req, vm))["State"] == "detaching"
    freed = await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "available")
    assert freed["Attachments"] == []
    assert vm.disk_sets[-1][1] == []


async def test_delete_removes_the_real_disk_and_refuses_an_attached_one(sink, ec2, stores):
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    volume = await _create_volume(stores, sink, ec2, vm)
    req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device="/dev/sdf"))
    await _answer(stores, req, vm)
    await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")

    req = sink.call(lambda: ec2.delete_volume(VolumeId=volume["VolumeId"]))
    assert _parse("DeleteVolume", await _answer(stores, req, vm), error=True)["Error"]["Code"] == "VolumeInUse"
    assert vm.deleted_disks == []

    req = sink.call(lambda: ec2.detach_volume(VolumeId=volume["VolumeId"]))
    await _answer(stores, req, vm)
    await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "available")
    req = sink.call(lambda: ec2.delete_volume(VolumeId=volume["VolumeId"]))
    await _answer(stores, req, vm)

    assert vm.deleted_disks == [disk_name(ENV, volume["VolumeId"])]
    # The instance's own root volume is still listed, and correctly so -- the
    # instance is still running. Only the drawn volume went.
    listed = [v["VolumeId"] for v in await _volumes(stores, sink, ec2, vm)]
    assert volume["VolumeId"] not in listed
    assert len(listed) == 1


async def test_a_delete_limactl_refused_keeps_the_record_so_the_disk_is_findable(sink, ec2, stores):
    """A record dropped over a disk that is still on the machine is a leak
    nothing can name afterwards -- the exact orphan `odin env rm` exists to
    stop leaving behind.

    Mutation-test: move the `stores.ec2compute.delete` above the `try` (or
    delete regardless of the outcome) and this fails."""
    vm = FakeInstanceVm(fail_disk_delete=True)
    volume = await _create_volume(stores, sink, ec2, vm)

    req = sink.call(lambda: ec2.delete_volume(VolumeId=volume["VolumeId"]))
    parsed = _parse("DeleteVolume", await _answer(stores, req, vm), error=True)

    assert parsed["Error"]["Code"] == "InternalError"
    assert "was NOT deleted" in parsed["Error"]["Message"]
    assert disk_name(ENV, volume["VolumeId"]) in parsed["Error"]["Message"]
    assert [v["VolumeId"] for v in await _volumes(stores, sink, ec2, vm)] == [volume["VolumeId"]]


# --- Lifecycle: a terminated instance frees its volumes ---------------------


async def test_terminating_an_instance_frees_its_volumes_rather_than_stranding_them(sink, ec2, stores):
    """`limactl disk delete` refuses a disk an instance still holds, so a
    volume left claiming `in-use` after its VM is deleted could never be
    deleted again -- a permanent leak created by a stale field. AWS keeps a
    non-root volume too, so `available` is also the AWS-correct answer.

    Mutation-test: remove the `_release_instance_volumes` call in
    `_finish_terminate` and this fails."""
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm()
    instance_id = (await _run_instance(stores, sink, ec2, vm, SubnetId=subnet_id))["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)
    volume = await _create_volume(stores, sink, ec2, vm)
    req = sink.call(lambda: ec2.attach_volume(VolumeId=volume["VolumeId"], InstanceId=instance_id, Device="/dev/sdf"))
    await _answer(stores, req, vm)
    await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "in-use")

    req = sink.call(lambda: ec2.terminate_instances(InstanceIds=[instance_id]))
    await _answer(stores, req, vm)
    await _wait_for_state(stores, sink, ec2, instance_id, "terminated", vm)

    freed = await _wait_for_volume(stores, sink, ec2, vm, volume["VolumeId"], "available")
    assert freed["Attachments"] == []


async def test_describe_volumes_survives_an_instance_record_with_no_root_volume(sink, ec2, stores):
    """A `KeyError` behind a 200. Several test fixtures and any store written
    before the field existed seed an `instance:` record without `root_volume`;
    `_all_volumes` must list less, not raise.

    Mutation-test: put the bare `i["root_volume"]` index back and this fails."""
    stores.ec2compute.set(ENV, "instance:i-old", {"instance_id": "i-old", "state_name": "running"})
    assert await _volumes(stores, sink, ec2, FakeInstanceVm()) == []


# --- Reclaim: the disk-leak backstop ---------------------------------------


async def test_reclaim_deletes_every_env_disk_including_one_no_record_names(sink, ec2, stores):
    """The machine sweep is the point. A disk whose `CreateVolume` was
    interrupted between `limactl disk create` and the record write exists with
    nothing naming it, and would otherwise hold space forever.

    Mutation-test: drop the `| on_machine` union and this fails on the orphan."""
    vm = FakeInstanceVm()
    volume = await _create_volume(stores, sink, ec2, vm)
    orphan = disk_name(ENV, "vol-nobody-remembers")
    await vm.create_disk(orphan, 5)
    # ...and a disk belonging to a DIFFERENT env, which must survive untouched.
    other = disk_name("someone-elses-env", "vol-theirs")
    await vm.create_disk(other, 5)

    result = await ec2compute.reclaim_env_disks(stores, ENV, vm)

    assert set(result.reclaimed) == {disk_name(ENV, volume["VolumeId"]), orphan}
    assert result.standing == ()
    assert other not in vm.deleted_disks
    assert await _volumes(stores, sink, ec2, vm) == []


async def test_reclaim_reports_a_disk_it_could_not_delete_rather_than_success(sink, ec2, stores):
    """Mutation-test: swallow the per-disk `except` and this fails -- which is
    the whole contract. `/destroy` turns a non-empty `standing` into a
    `ReclaimFailed`, so a silent skip here becomes a `destroyed` over
    gigabytes odin did not give back."""
    vm = FakeInstanceVm()
    volume = await _create_volume(stores, sink, ec2, vm)
    vm.fail_disk_delete = True

    result = await ec2compute.reclaim_env_disks(stores, ENV, vm)

    assert result.reclaimed == ()
    assert result.failed
    assert result.standing[0]["disk"] == disk_name(ENV, volume["VolumeId"])
    assert "in use by instance" in result.standing[0]["reason"]
    # The record survives, so the next attempt can still find it.
    assert [v["VolumeId"] for v in await _volumes(stores, sink, ec2, vm)] == [volume["VolumeId"]]

    failure = ec2compute.disks_standing(ENV, result.standing)
    assert isinstance(failure, ec2compute.ReclaimFailed)
    assert "is NOT destroyed" in str(failure)
    assert disk_name(ENV, volume["VolumeId"]) in str(failure)


async def test_reclaim_on_an_env_that_never_had_a_volume_is_a_quiet_success(stores):
    result = await ec2compute.reclaim_env_disks(stores, "never-had-a-disk", FakeInstanceVm())
    assert result.reclaimed == ()
    assert not result.failed


async def test_the_type_odin_WRITES_is_the_type_the_gateway_ANSWERS(sink, ec2, stores):
    """A cross-half ratchet, and it exists because the halves were agreeing
    by coincidence. `iac/hcl.py` writes `type = "gp3"` into every
    `aws_ebs_volume`; `import_tf._FIXED_VALUES` claims odin always emits
    `gp3` and is tested against the EMITTER; this gateway answers
    `<volumeType>` on the wire. Nothing compared the third to the first, so
    changing either one alone would have made odin's generated file and
    odin's own API disagree about the same volume with every suite green.

    Mutation-test: change `hcl._EBS_VOLUME_TYPE` (or the gateway's default)
    and this fails."""
    vm = FakeInstanceVm()
    volume = await _create_volume(stores, sink, ec2, vm)

    emitted, _nested = hcl._ebs(ResourceDesired(id="data", kind="ebs"), {})

    assert emitted["type"] == hcl.quote(volume["VolumeType"])
    assert volume["VolumeType"] == hcl._EBS_VOLUME_TYPE


@pytest.mark.parametrize("action", ["CreateVolume", "AttachVolume", "DetachVolume", "DeleteVolume"])
def test_every_volume_action_is_registered(action: str):
    """Guards the guard: an unregistered action falls through to ec2net and
    answers `InvalidAction`, which every test above would then read as a
    plain error rather than a missing feature."""
    assert action in ec2compute._HANDLERS
