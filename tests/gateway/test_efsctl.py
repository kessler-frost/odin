"""gateway/models/efsctl.py -- EFS as a real host directory.

Same test method as kmsctl/logsctl/secretsctl: every request is a REAL
boto3-signed capture (`harness.CaptureSink` + the `efs` client fixture), every
response round-trips through botocore's OWN parser for the REAL EFS service
model, and every call routes through `classify()` ->
`await synth.pure_answer()` -- so these exercise the `elasticfilesystem` branch
of the dispatch pipeline end to end rather than calling handlers directly.

WHAT THIS FILE DOES NOT PROVE, stated up front because the gap is the whole
point of the kind: that two containers really see the same directory. Nothing
here mounts anything. `tests/simulate/test_efs_mount_e2e.py` is where a real
task writes a file and a second real task reads it back; this file is about the
API being real and the guards being able to fire.

WHAT IT DOES PROVE beyond the wire shapes, and each of these was mutation-tested
(break the guard, watch a NAMED test below fail):
  * the substrate is a real directory on disk, created 0700 and really removed
  * a mount whose directory is gone is REFUSED, not quietly made empty
  * the post-delete poll answers 404 with the `x-amzn-errortype` header that a
    real `tofu destroy` was MEASURED needing (efsctl's module docstring)
  * DeleteFileSystem reports the OUTCOME: a directory that will not go is a 500
    naming it, never a 204
"""
from __future__ import annotations

import json
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.models import efsctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
LABEL = "shared-data"

# The SigV4 credential scope, which is what `app.py` hands `classify` -- NOT
# botocore's model name (`efs`). Every call below goes through this one.
SCOPE = "elasticfilesystem"


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("efs")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300, f"expected an error, got {response.status_code}"
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


async def _answer(stores: SynthStores, req) -> Response:
    """One request through the REAL pipeline: classify -> synth.pure_answer."""
    path, query = split_url(req.url)
    classified = classify(SCOPE, req.method, path, query, req.headers, req.body)
    assert classified is not None, f"an EFS request must never be unmappable: {req.method} {path}"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0, query=query)
    assert response is not None, "EFS is all-synth: pure_answer must never fall through"
    return response


async def _create_fs(stores, sink, efs, label: str = LABEL) -> dict:
    response = await _answer(stores, sink.call(lambda: efs.create_file_system(
        CreationToken=label, Tags=[{"Key": "Name", "Value": label}, {"Key": "odin:node", "Value": label}],
    )))
    assert response.status_code == 201, response.body
    return _parse("CreateFileSystem", response)


async def _create_ap(stores, sink, efs, file_system_id: str) -> dict:
    response = await _answer(stores, sink.call(lambda: efs.create_access_point(
        ClientToken="t-1", FileSystemId=file_system_id, RootDirectory={"Path": "/"},
    )))
    assert response.status_code == 200, response.body
    return _parse("CreateAccessPoint", response)


# --- the substrate: a real directory, not a record ---------------------------


def test_the_substrate_path_is_the_one_the_design_specifies(tmp_path):
    """The path SPELLED OUT, not derived from `host_dir`.

    Honesty rule 5: every other test in this file locates the directory by
    calling `efsctl.host_dir`, which is the same expression the source uses --
    so if `host_dir` started answering `/tmp/whatever`, the source and every one
    of those tests would move together and none could fail. This is the one
    place the expectation comes from somewhere the subject cannot reach.

    It is not a stylistic constraint either. `.odin/` lives under the repo
    checkout and so under `$HOME`, which is the ONLY tree Colima shares into its
    VM: a `-v` of a path outside it mounts an EMPTY directory and reports no
    error (measured -- `runtime/colima.py::copy_in`). Moving this path silently
    turns every EFS mount into an empty one."""
    assert efsctl.host_dir(tmp_path, "prod", "fs-abc") == tmp_path / "prod" / "gateway" / "efs" / "fs-abc"


async def test_create_file_system_makes_a_real_directory_on_disk(stores, sink, efs, tmp_path):
    created = await _create_fs(stores, sink, efs)
    file_system_id = created["FileSystemId"]

    directory = efsctl.host_dir(tmp_path, ENV, file_system_id)
    assert directory.is_dir(), f"CreateFileSystem answered 201 but {directory} is not on disk"
    # 0700, like every other directory under `.odin/<env>/gateway` -- asked of
    # the filesystem, not of `private_mkdir`'s return value.
    assert oct(directory.stat().st_mode)[-3:] == "700"
    # ...and the record NAMES the real path, which is what every mount resolves
    # through. A record pointing somewhere else is a mount of the wrong tree.
    record = stores.efsctl.get(ENV, f"fs:{file_system_id}")
    assert Path(record["host_dir"]) == directory.resolve()
    assert Path(record["host_dir"]).is_absolute(), (
        "a relative source is a NAMED VOLUME to docker, not a bind mount"
    )


async def test_delete_file_system_really_removes_the_directory(stores, sink, efs, tmp_path):
    created = await _create_fs(stores, sink, efs)
    file_system_id = created["FileSystemId"]
    directory = efsctl.host_dir(tmp_path, ENV, file_system_id)
    (directory / "payload.txt").write_text("written by a task")

    response = await _answer(stores, sink.call(lambda: efs.delete_file_system(FileSystemId=file_system_id)))
    assert response.status_code == 204
    assert not directory.exists(), "DeleteFileSystem answered 204 over a directory that is still there"
    assert stores.efsctl.get(ENV, f"fs:{file_system_id}") is None


async def test_delete_reports_the_outcome_when_the_directory_will_not_go(stores, sink, efs, tmp_path, monkeypatch):
    """Honesty rule 2: the status comes from the OUTCOME.

    A delete that cannot remove the directory must NOT answer 204 -- the file
    system is still there, holding the user's data, and a caller told "deleted"
    will recreate the resource around files that never left. The failure is
    injected at `remove_host_dir`, and the injection is verified by the
    directory still existing afterwards rather than by trusting the patch."""
    created = await _create_fs(stores, sink, efs)
    file_system_id = created["FileSystemId"]
    directory = efsctl.host_dir(tmp_path, ENV, file_system_id)

    monkeypatch.setattr(
        efsctl, "remove_host_dir",
        lambda stores, env, fs_id: f"{directory}: PermissionError: [Errno 13] Permission denied",
    )
    response = await _answer(stores, sink.call(lambda: efs.delete_file_system(FileSystemId=file_system_id)))

    assert directory.is_dir(), "the fault injection did nothing -- this test proves nothing about the guard"
    assert response.status_code == 500, "a file system that would not go was reported deleted"
    assert b"Permission denied" in response.body
    assert stores.efsctl.get(ENV, f"fs:{file_system_id}") is not None, (
        "the record was dropped for a file system that still exists, so nothing names it any more"
    )


# --- the wire: real botocore parses everything odin sends --------------------


async def test_create_file_system_round_trips_through_botocores_own_parser(stores, sink, efs):
    created = await _create_fs(stores, sink, efs)
    assert created["FileSystemId"].startswith("fs-")
    assert created["CreationToken"] == LABEL
    assert created["LifeCycleState"] == "available"
    assert created["Name"] == LABEL
    assert created["FileSystemArn"].endswith(f":file-system/{created['FileSystemId']}")
    assert created["NumberOfMountTargets"] == 0
    assert created["PerformanceMode"] == "generalPurpose"
    assert created["ThroughputMode"] == "bursting"
    assert {t["Key"]: t["Value"] for t in created["Tags"]}["odin:node"] == LABEL


async def test_describe_file_systems_filters_by_id_and_by_creation_token(stores, sink, efs):
    first = await _create_fs(stores, sink, efs, "shared-data")
    second = await _create_fs(stores, sink, efs, "other-data")

    by_id = _parse("DescribeFileSystems", await _answer(
        stores, sink.call(lambda: efs.describe_file_systems(FileSystemId=first["FileSystemId"])),
    ))
    assert [f["FileSystemId"] for f in by_id["FileSystems"]] == [first["FileSystemId"]]

    by_token = _parse("DescribeFileSystems", await _answer(
        stores, sink.call(lambda: efs.describe_file_systems(CreationToken="other-data")),
    ))
    assert [f["FileSystemId"] for f in by_token["FileSystems"]] == [second["FileSystemId"]]

    everything = _parse("DescribeFileSystems", await _answer(
        stores, sink.call(lambda: efs.describe_file_systems()),
    ))
    assert len(everything["FileSystems"]) == 2


async def test_size_in_bytes_is_read_off_the_real_directory(stores, sink, efs, tmp_path):
    """`SizeInBytes` is a required member, so the easy thing is a constant --
    which is how a field becomes decorative. It is the one number in the whole
    description a user could check with `du`, so it is read."""
    created = await _create_fs(stores, sink, efs)
    directory = efsctl.host_dir(tmp_path, ENV, created["FileSystemId"])
    assert created["SizeInBytes"]["Value"] == 0

    (directory / "a.txt").write_bytes(b"x" * 100)
    (directory / "nested").mkdir()
    (directory / "nested" / "b.txt").write_bytes(b"y" * 23)

    described = _parse("DescribeFileSystems", await _answer(
        stores, sink.call(lambda: efs.describe_file_systems(FileSystemId=created["FileSystemId"])),
    ))
    assert described["FileSystems"][0]["SizeInBytes"]["Value"] == 123


async def test_access_point_round_trips_and_names_its_file_system(stores, sink, efs):
    created = await _create_fs(stores, sink, efs)
    access_point = await _create_ap(stores, sink, efs, created["FileSystemId"])
    assert access_point["AccessPointId"].startswith("fsap-")
    assert access_point["FileSystemId"] == created["FileSystemId"]
    assert access_point["RootDirectory"] == {"Path": "/"}
    assert access_point["LifeCycleState"] == "available"

    described = _parse("DescribeAccessPoints", await _answer(
        stores, sink.call(lambda: efs.describe_access_points(AccessPointId=access_point["AccessPointId"])),
    ))
    assert [a["AccessPointId"] for a in described["AccessPoints"]] == [access_point["AccessPointId"]]


async def test_lifecycle_configuration_is_an_empty_policy_list(stores, sink, efs):
    created = await _create_fs(stores, sink, efs)
    parsed = _parse("DescribeLifecycleConfiguration", await _answer(
        stores, sink.call(lambda: efs.describe_lifecycle_configuration(FileSystemId=created["FileSystemId"])),
    ))
    assert parsed["LifecyclePolicies"] == []


# --- the delete contract: 404, with the header the probe proved is needed ----


async def test_post_delete_poll_answers_404_file_system_not_found(stores, sink, efs):
    """The measured contract (efsctl's module docstring): after DELETE, the
    provider polls DescribeFileSystems and real AWS answers 404
    FileSystemNotFound. A 200-with-an-empty-list would also satisfy the
    provider; odin sends the AWS-faithful one, so a workload SDK written
    against real EFS catches what it expects."""
    created = await _create_fs(stores, sink, efs)
    file_system_id = created["FileSystemId"]
    await _answer(stores, sink.call(lambda: efs.delete_file_system(FileSystemId=file_system_id)))

    response = await _answer(
        stores, sink.call(lambda: efs.describe_file_systems(FileSystemId=file_system_id)),
    )
    assert response.status_code == 404
    parsed = _parse("DescribeFileSystems", response, error=True)
    assert parsed["Error"]["Code"] == "FileSystemNotFound"
    assert file_system_id in parsed["Error"]["Message"]


async def test_post_delete_poll_answers_404_access_point_not_found(stores, sink, efs):
    created = await _create_fs(stores, sink, efs)
    access_point = await _create_ap(stores, sink, efs, created["FileSystemId"])
    await _answer(stores, sink.call(
        lambda: efs.delete_access_point(AccessPointId=access_point["AccessPointId"]),
    ))

    response = await _answer(stores, sink.call(
        lambda: efs.describe_access_points(AccessPointId=access_point["AccessPointId"]),
    ))
    assert response.status_code == 404
    parsed = _parse("DescribeAccessPoints", response, error=True)
    assert parsed["Error"]["Code"] == "AccessPointNotFound"


async def test_the_404_carries_the_errortype_header_the_go_sdk_reads(stores, sink, efs):
    """THE header, and this test is the ratchet on a MEASURED fact.

    EFS's own error shape makes `ErrorCode` a required BODY member, so sending
    only that is the natural mistake. It does not work. Measured against a real
    `tofu destroy` (hashicorp/aws 6.57.1) with the header removed:

        Error: waiting for EFS Access Point (fsap-...) delete: ... api error
        UnknownError: Access point fsap-... does not exist.

    -- exit 1, where the header form exits 0. botocore agrees: header ->
    `Code='FileSystemNotFound'`, body-only -> `Code='404'`. Both clients read
    `x-amzn-errortype`; neither reads `ErrorCode`. So this asserts on the header
    directly rather than only on what botocore made of it, because botocore's
    parse is downstream of the very thing being pinned."""
    created = await _create_fs(stores, sink, efs)
    file_system_id = created["FileSystemId"]
    await _answer(stores, sink.call(lambda: efs.delete_file_system(FileSystemId=file_system_id)))
    response = await _answer(
        stores, sink.call(lambda: efs.describe_file_systems(FileSystemId=file_system_id)),
    )
    assert response.headers["x-amzn-errortype"] == "FileSystemNotFound"
    # ...and the body still carries EFS's own documented shape, for a human
    # reading the raw response.
    assert json.loads(response.body)["ErrorCode"] == "FileSystemNotFound"


async def test_a_live_file_system_is_not_404(stores, sink, efs):
    """The other half of the ratchet above: a guard that answers 404 for
    everything would pass every test in this section and break every apply."""
    created = await _create_fs(stores, sink, efs)
    response = await _answer(
        stores, sink.call(lambda: efs.describe_file_systems(FileSystemId=created["FileSystemId"])),
    )
    assert response.status_code == 200
    assert len(_parse("DescribeFileSystems", response)["FileSystems"]) == 1


# --- refusals that are real AWS's own -----------------------------------------


async def test_creation_token_is_idempotent_and_names_the_existing_file_system(stores, sink, efs):
    first = await _create_fs(stores, sink, efs)
    response = await _answer(stores, sink.call(lambda: efs.create_file_system(CreationToken=LABEL)))
    assert response.status_code == 409
    assert first["FileSystemId"] in json.loads(response.body)["Message"]
    assert len(stores.efsctl.items(ENV)) == 1, "a second file system was created for the same token"


async def test_a_creation_token_over_64_characters_is_refused_not_truncated(stores, sink, efs):
    """AWS caps `CreationToken` at 64 (botocore's own shape). Truncating would
    make a file system the next apply cannot find by the name it was created
    with -- silently, once, at some length nobody tested."""
    long_token = "x" * 65
    response = await _answer(stores, sink.call(lambda: efs.create_file_system(CreationToken=long_token)))
    assert response.status_code == 400
    assert "65" in json.loads(response.body)["Message"]
    assert stores.efsctl.items(ENV) == {}


async def test_deleting_a_file_system_that_still_has_an_access_point_is_refused(stores, sink, efs, tmp_path):
    """`FileSystemInUse` is EFS's own error and the ordering rule is real AWS's
    too. Deleting here would leave access-point records pointing at a directory
    that is gone."""
    created = await _create_fs(stores, sink, efs)
    access_point = await _create_ap(stores, sink, efs, created["FileSystemId"])
    response = await _answer(stores, sink.call(
        lambda: efs.delete_file_system(FileSystemId=created["FileSystemId"]),
    ))
    assert response.status_code == 409
    assert access_point["AccessPointId"] in json.loads(response.body)["Message"]
    assert efsctl.host_dir(tmp_path, ENV, created["FileSystemId"]).is_dir(), (
        "the refusal still removed the directory"
    )


async def test_an_access_point_on_an_unknown_file_system_is_404(stores, sink, efs):
    response = await _answer(stores, sink.call(lambda: efs.create_access_point(
        ClientToken="t-1", FileSystemId="fs-00000000000000000",
    )))
    assert response.status_code == 404
    assert json.loads(response.body)["ErrorCode"] == "FileSystemNotFound"


async def test_an_unmodeled_action_is_a_protocol_correct_error_not_a_503(stores):
    """The no-backing contract every model here keeps: EFS has nothing to
    forward to, so an action odin does not model must still come back in EFS's
    own wire shape."""
    response = await efsctl.pure_answer("elasticfilesystem:CreateMountTarget", "fs-1", ENV, b"{}", stores, 0.0)
    assert response.status_code == 400
    assert response.headers["x-amzn-errortype"] == "BadRequest"
    assert "CreateMountTarget" in json.loads(response.body)["Message"]


# --- the mount join: taskdef volumes + mountPoints ---------------------------


def _taskdef(file_system_id: str, name: str = "shared") -> dict:
    return {
        "family": "api", "revision": 1,
        "volumes": [{"name": name, "efsVolumeConfiguration": {"fileSystemId": file_system_id, "rootDirectory": "/"}}],
        "container_definitions": [{"name": "app", "image": "alpine"}],
    }


def _container_def(volume_name: str = "shared", container_path: str = "/mnt/efs") -> dict:
    return {
        "name": "app", "image": "alpine",
        "mountPoints": [{"sourceVolume": volume_name, "containerPath": container_path, "readOnly": False}],
    }


async def test_task_mounts_join_taskdef_volumes_with_container_mount_points(stores, sink, efs, tmp_path):
    """The join AWS itself splits across two places: the file system id is on
    the TASK DEFINITION's `volumes[]`, the container path is on the CONTAINER
    DEFINITION's `mountPoints[]`, and they meet on the volume NAME."""
    created = await _create_fs(stores, sink, efs)
    file_system_id = created["FileSystemId"]

    mounts = efsctl.task_mounts(stores, ENV, _taskdef(file_system_id), _container_def())
    assert mounts == {str(efsctl.host_dir(tmp_path, ENV, file_system_id).resolve()): "/mnt/efs"}


async def test_task_mounts_ignore_a_non_efs_volume(stores, sink, efs):
    """A `host{sourcePath}` volume is a real ECS concept odin does not model.
    Refusing it here would break task definitions that merely mention one."""
    taskdef = {"volumes": [{"name": "scratch", "host": {"sourcePath": "/tmp/x"}}]}
    assert efsctl.task_mounts(stores, ENV, taskdef, _container_def("scratch")) == {}


async def test_a_task_mounting_a_deleted_file_system_is_refused(stores, sink, efs, tmp_path):
    """THE GUARD, and the reason it reads the FILESYSTEM rather than the record:
    `docker -v` CREATES a missing source path (empty, as root) instead of
    failing, so without this the task starts, mounts nothing, reads nothing, and
    every status stays green."""
    created = await _create_fs(stores, sink, efs)
    file_system_id = created["FileSystemId"]
    directory = efsctl.host_dir(tmp_path, ENV, file_system_id)
    directory.rmdir()  # the record survives; the substrate does not
    assert not directory.exists(), "the injection did nothing -- this test proves nothing"

    with pytest.raises(efsctl.MountUnavailable) as raised:
        efsctl.task_mounts(stores, ENV, _taskdef(file_system_id), _container_def())
    assert file_system_id in str(raised.value)
    assert str(directory) in str(raised.value)


async def test_a_read_only_mount_is_refused_not_silently_made_writable(stores, sink, efs, tmp_path):
    """odin's bind-mount renderer has no `:ro` form, so a `readOnly: true`
    mountPoint cannot be honoured -- and mounting it WRITABLE would leave a user
    believing their data was protected when it is not. `agent/hcl.py` hardcodes
    `readOnly: false`, so this bites only a hand-written or IMPORTED taskdef.

    Mutation-test: delete the `_refuse_read_only` call and this fails."""
    created = await _create_fs(stores, sink, efs)
    container_def = _container_def()
    container_def["mountPoints"][0]["readOnly"] = True

    with pytest.raises(efsctl.MountUnavailable) as raised:
        efsctl.task_mounts(stores, ENV, _taskdef(created["FileSystemId"]), container_def)
    assert "readOnly" in str(raised.value)
    assert ":ro" in str(raised.value), "the refusal does not say WHY, so a user cannot check it"


async def test_a_scoped_root_directory_is_refused_not_widened_to_the_whole_share(stores, sink, efs, tmp_path):
    """`rootDirectory` means "expose THIS subtree". odin mounts the whole file
    system, so accepting it would hand a consumer scoped to `/data` every other
    consumer's files -- a confidentiality answer, not a cosmetic one.

    Mutation-test: delete the `_refuse_subdirectory` call and this fails."""
    created = await _create_fs(stores, sink, efs)
    taskdef = _taskdef(created["FileSystemId"])
    taskdef["volumes"][0]["efsVolumeConfiguration"]["rootDirectory"] = "/data"

    with pytest.raises(efsctl.MountUnavailable) as raised:
        efsctl.task_mounts(stores, ENV, taskdef, _container_def())
    assert "/data" in str(raised.value)


async def test_the_root_directory_odin_really_emits_is_accepted(stores, sink, efs):
    """The other half: `agent/hcl.py` emits `rootDirectory = "/"`, and a guard
    that refused THAT would break every generated project."""
    created = await _create_fs(stores, sink, efs)
    taskdef = _taskdef(created["FileSystemId"])
    taskdef["volumes"][0]["efsVolumeConfiguration"]["rootDirectory"] = "/"
    assert efsctl.task_mounts(stores, ENV, taskdef, _container_def())


async def test_a_function_on_a_scoped_access_point_is_refused(stores, sink, efs):
    """The Lambda-side twin: an access point's `RootDirectory.Path` scopes it
    the same way, and is refused the same way."""
    created = await _create_fs(stores, sink, efs)
    response = await _answer(stores, sink.call(lambda: efs.create_access_point(
        ClientToken="t-1", FileSystemId=created["FileSystemId"], RootDirectory={"Path": "/data"},
    )))
    access_point = _parse("CreateAccessPoint", response)
    configs = [{"Arn": access_point["AccessPointArn"], "LocalMountPath": "/mnt/efs"}]

    with pytest.raises(efsctl.MountUnavailable) as raised:
        efsctl.function_mounts(stores, ENV, configs)
    assert "/data" in str(raised.value)


async def test_a_task_mounting_a_file_system_with_no_record_is_refused(stores):
    with pytest.raises(efsctl.MountUnavailable) as raised:
        efsctl.task_mounts(stores, ENV, _taskdef("fs-00000000000000000"), _container_def())
    assert "fs-00000000000000000" in str(raised.value)


# --- the mount join: lambda's FileSystemConfigs ------------------------------


async def test_function_mounts_resolve_arn_to_access_point_to_directory(stores, sink, efs, tmp_path):
    """Lambda names an ACCESS POINT, never a file system -- botocore's own
    `FileSystemArn` pattern demands `access-point/fsap-[a-f0-9]{17}$` -- so the
    resolution is Arn -> access point -> file system -> directory."""
    created = await _create_fs(stores, sink, efs)
    access_point = await _create_ap(stores, sink, efs, created["FileSystemId"])
    configs = [{"Arn": access_point["AccessPointArn"], "LocalMountPath": "/mnt/efs"}]

    mounts = efsctl.function_mounts(stores, ENV, configs)
    assert mounts == {str(efsctl.host_dir(tmp_path, ENV, created["FileSystemId"]).resolve()): "/mnt/efs"}


async def test_a_function_mounting_an_unknown_access_point_is_refused(stores):
    configs = [{"Arn": efsctl.access_point_arn("fsap-00000000000000000"), "LocalMountPath": "/mnt/efs"}]
    with pytest.raises(efsctl.MountUnavailable) as raised:
        efsctl.function_mounts(stores, ENV, configs)
    assert "fsap-00000000000000000" in str(raised.value)


@pytest.mark.parametrize("bad_path", ["/mnt/efs/data", "/data", "/mnt/", "/var/task"])
async def test_a_local_mount_path_real_lambda_refuses_is_refused_here(stores, sink, efs, bad_path):
    """AWS's own `LocalMountPath` pattern is `/mnt/[a-zA-Z0-9-_.]+` -- ONE
    segment under `/mnt`, with no second slash in it. `/mnt/efs/data` looks
    reasonable and real Lambda rejects it, so accepting it would make odin the
    more permissive of the two: a canvas that works here and fails on Amazon."""
    created = await _create_fs(stores, sink, efs)
    access_point = await _create_ap(stores, sink, efs, created["FileSystemId"])
    configs = [{"Arn": access_point["AccessPointArn"], "LocalMountPath": bad_path}]
    with pytest.raises(efsctl.MountUnavailable) as raised:
        efsctl.function_mounts(stores, ENV, configs)
    assert bad_path in str(raised.value)


async def test_a_legal_local_mount_path_is_accepted(stores, sink, efs):
    """The other half of the ratchet: a validator that refused everything would
    pass the parametrized test above and break every real mount."""
    created = await _create_fs(stores, sink, efs)
    access_point = await _create_ap(stores, sink, efs, created["FileSystemId"])
    configs = [{"Arn": access_point["AccessPointArn"], "LocalMountPath": "/mnt/a-b_c.d"}]
    assert list(efsctl.function_mounts(stores, ENV, configs).values()) == ["/mnt/a-b_c.d"]


# --- ids odin mints are ids real AWS accepts ----------------------------------


def test_minted_ids_match_aws_own_patterns():
    """Read from botocore's live models, never from memory. The tight one is
    Lambda's `FileSystemArn`, which demands EXACTLY 17 lowercase hex after
    `fsap-`: an access-point id in any other shape makes a generated project
    that real AWS rejects, and nothing else in odin would ever notice."""
    import gzip
    import re

    import botocore

    def _model(service: str, version: str) -> dict:
        path = Path(botocore.__file__).parent / "data" / service / version / "service-2.json.gz"
        return json.loads(gzip.decompress(path.read_bytes()))

    efs_model = _model("efs", "2015-02-01")
    lambda_model = _model("lambda", "2015-03-31")

    file_system_id = efsctl._mint("fs")
    access_point_id = efsctl._mint("fsap")
    assert re.fullmatch(efs_model["shapes"]["FileSystemId"]["pattern"], file_system_id)
    assert re.fullmatch(efs_model["shapes"]["AccessPointId"]["pattern"], access_point_id)
    assert re.fullmatch(
        lambda_model["shapes"]["FileSystemArn"]["pattern"], efsctl.access_point_arn(access_point_id),
    ), "an access-point ARN Lambda's own pattern rejects"
    # ...and odin's mount-path check IS AWS's, not a lookalike.
    aws_pattern = lambda_model["shapes"]["LocalMountPath"]["pattern"]
    for candidate in ("/mnt/efs", "/mnt/efs/data", "/data", "/mnt/", "/mnt/a-b_c.d"):
        assert bool(re.fullmatch(aws_pattern, candidate)) == bool(efsctl.LOCAL_MOUNT_PATH.match(candidate)), (
            f"odin and AWS disagree about {candidate!r}"
        )


# --- teardown -----------------------------------------------------------------


async def test_reclaim_sweeps_the_directory_tree_not_the_records(stores, sink, efs, tmp_path):
    """`/destroy`'s backstop. It walks `.odin/{env}/gateway/efs/` itself, so it
    also finds a directory whose RECORD was lost -- which is exactly what an
    interrupted apply leaves, and what `tofu destroy` can never reach."""
    created = await _create_fs(stores, sink, efs)
    orphan = efsctl.host_dir(tmp_path, ENV, "fs-0badc0ffee0000000")
    orphan.mkdir(parents=True)
    (orphan / "left-behind.txt").write_text("an interrupted apply")

    reclaimed, standing = efsctl.reclaim_env_file_systems(stores, ENV)
    assert not standing
    assert set(reclaimed) == {created["FileSystemId"], "fs-0badc0ffee0000000"}
    assert not orphan.exists()
    assert not efsctl.host_dir(tmp_path, ENV, created["FileSystemId"]).exists()


async def test_reclaim_on_an_env_that_never_had_efs_is_empty_and_quiet(stores):
    assert efsctl.reclaim_env_file_systems(stores, "never-existed") == ((), ())


async def test_reclaim_reports_what_would_not_go_rather_than_claiming_success(stores, sink, efs, tmp_path, monkeypatch):
    """The sentence `/destroy` refuses to say `destroyed` over. Injected at
    `shutil.rmtree` so the REAL `remove_host_dir` failure path runs -- and the
    directory still standing afterwards is what proves the injection landed."""
    created = await _create_fs(stores, sink, efs)
    directory = efsctl.host_dir(tmp_path, ENV, created["FileSystemId"])

    def _refuse(path, onexc=None, **kwargs):
        onexc(None, str(path), PermissionError(13, "Permission denied"))

    monkeypatch.setattr(efsctl.shutil, "rmtree", _refuse)
    reclaimed, standing = efsctl.reclaim_env_file_systems(stores, ENV)

    assert directory.is_dir(), "the fault injection did nothing -- this test proves nothing"
    assert not reclaimed
    assert len(standing) == 1 and "Permission denied" in standing[0]
    sentence = efsctl.file_systems_standing(ENV, standing)
    assert "is NOT destroyed" in sentence
    assert created["FileSystemId"] in sentence


async def test_a_removal_that_silently_does_nothing_is_still_reported_standing(stores, sink, efs, tmp_path, monkeypatch):
    """The failure `ignore_errors=True` hides, and the reason `remove_host_dir`
    re-checks `exists()` after a clean-looking rmtree: a removal that raises
    NOTHING and removes NOTHING must not read as success."""
    created = await _create_fs(stores, sink, efs)
    monkeypatch.setattr(efsctl.shutil, "rmtree", lambda path, onexc=None, **kwargs: None)

    reason = efsctl.remove_host_dir(stores, ENV, created["FileSystemId"])
    assert reason, "a no-op removal was reported as a success"
    assert "still exists" in reason
