"""The gateway's EFS model -- and, unlike every other model in this package,
the thing it describes is not a container: **an EFS file system IS a real host
directory**, at `.odin/{env}/gateway/efs/{fs-id}/`, bind-mounted into the ECS
tasks and Lambda containers that were drawn with a mount edge to it. That
directory is the whole substrate. If it is not there, nothing here is true, and
this module's job is to make sure odin never says otherwise.

The path convention is `compute/functions.py`'s Lambda code-dir pattern, and it
is under `.odin/` for a MEASURED reason, not a stylistic one: a `-v` of a path
under macOS's per-user temp dir (`/private/var/folders/...`) silently mounts an
EMPTY directory under Colima's virtiofs -- the path exists inside the VM, so
nothing errors (`runtime/colima.py::copy_in`, `compute/proxy.py`). `.odin/`
lives under the repo checkout, itself under `$HOME`, which is the only tree
Colima shares in. A test that put the EFS root under `tmp_path` would pass
against a fake runtime and mount nothing at all against a real one.

WIRE PROTOCOL: EFS is `rest-json` (botocore's own model:
`protocol: rest-json | endpointPrefix: elasticfilesystem | apiVersion:
2015-02-01`, no `targetPrefix`), so it routes like Lambda -- method+path
through `classify.py::_EFS_ROUTES` -- and not like SQS/EC2. The SigV4
credential scope on the wire is `.../us-east-1/elasticfilesystem/aws4_request`,
which is the `service` string `app.py` hands to `classify` and `errors`.

THE CALL SEQUENCE THIS ANSWERS was measured, not guessed: a real `tofu init` +
`apply` + `plan` + `destroy` over `aws_efs_file_system` + `aws_efs_access_point`
on OpenTofu 1.12.3 / hashicorp/aws 6.57.1, pointed at a recording endpoint.
Seven operations are called and no others; `DescribeBackupPolicy`,
`DescribeFileSystemPolicy`, `ListTagsForResource`, `DescribeMountTargets` and
`TagResource` are NEVER called, so implementing them would be dead code.

    CREATE   201 POST   /2015-02-01/file-systems
             200 GET    /2015-02-01/file-systems?FileSystemId=      (x2)
             200 GET    /2015-02-01/file-systems/{id}/lifecycle-configuration
             200 POST   /2015-02-01/access-points
             200 GET    /2015-02-01/access-points?AccessPointId=    (x2)
    REFRESH  the three GETs above
    DESTROY  204 DELETE /2015-02-01/access-points/{id}
             ... GET    /2015-02-01/access-points?AccessPointId=    (poll)
             204 DELETE /2015-02-01/file-systems/{id}
             ... GET    /2015-02-01/file-systems?FileSystemId=      (poll)

THE POST-DELETE POLL ANSWERS 404, AND THAT WAS PROBED BEFORE IT WAS CODED.
Botocore's own `DeleteFileSystem` documentation states it ("If you pass file
system ID or creation token for the deleted file system, the
DescribeFileSystems returns a `404 FileSystemNotFound` error"), and a
200-with-an-empty-list would ALSO have satisfied the provider -- so the choice
had to be measured rather than reasoned about. Both forms were run against a
real `tofu destroy`; both exit 0. odin returns the AWS-faithful 404
(`_fs_not_found` / `_ap_not_found`), because that is what a workload SDK
written against real EFS expects to catch.

What the same probe found, and what would otherwise have been a silent
breakage: **the `x-amzn-errortype` HEADER is load-bearing, and EFS's own body
member name is not enough.** The error shape botocore models carries
`ErrorCode` as a REQUIRED member -- but neither SDK reads it. Measured:

    404 + header + body -> destroy exit 0; botocore Code='FileSystemNotFound'
    404 + body only     -> destroy exit 1:
        "Error: waiting for EFS Access Point (fsap-...) delete: ...
         api error UnknownError: Access point fsap-... does not exist."
      ...and botocore parses Code='404'.

So `gateway/errors.py`'s `elasticfilesystem` branch sends BOTH: the header both
SDKs actually read, and EFS's own `{"ErrorCode", "Message"}` body for a human
reading the raw response. That is the same split `_lambda_response` already
makes for the other rest-json service, for the same reason.

Persistence: one `JsonStore` at `.odin/{env}/gateway/efsctl.json`
(`stores.efsctl`), flat keys `"fs:{FileSystemId}"` and `"ap:{AccessPointId}"`.
Tags live in the shared `stores.tags` store, keyed `"elasticfilesystem:{arn}"`.

ID SHAPES ARE CONSTRAINED BY REAL AWS, and getting them wrong makes a generated
project real AWS rejects. From botocore's own patterns:
`FileSystemId` is `fs-[0-9a-f]{8,40}`; `AccessPointId` is `fsap-[0-9a-f]{8,40}`
-- but Lambda's `FileSystemArn` demands `access-point/fsap-[a-f0-9]{17}$`,
EXACTLY 17. Both are minted at 17 lowercase hex here, which satisfies all
three.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.stores import SynthStores
from odin.util import private_mkdir

NODE_TAG = "odin:node"

# 17 lowercase hex characters -- see the module docstring's ID SHAPES note.
# `secrets.token_hex(9)` is 18, so one character comes off.
_ID_HEX = 17

# AWS's own `LocalMountPath` pattern, copied from botocore's lambda model
# rather than remembered: `/mnt/` plus ONE path segment. `/mnt/efs` is legal,
# `/mnt/efs/data` is NOT -- there is no second slash in it. The tile's `path`
# is validated against this by `iac/hcl.py` before a project is generated;
# it is repeated here because a mount that reaches this module by any other
# route (an imported project, a hand-written taskdef) has never been through
# that check.
LOCAL_MOUNT_PATH = re.compile(r"^/mnt/[a-zA-Z0-9\-_.]+$")

_LIFECYCLE_EMPTY: dict[str, list] = {"LifecyclePolicies": []}


class MountUnavailable(RuntimeError):
    """This container was drawn with an EFS mount that cannot be made real.

    Raised, never swallowed and never downgraded to "mount nothing": both call
    sites (`ecsctl._launch_task`, `lambdactl._finish_deploy`) already turn an
    exception into that workload's terminal failure with the real reason
    attached, which is what puts it on the canvas as a `crashed` node with a
    verdict AND fails the apply. Silently starting the container with an empty
    directory where the shared file system should be is the failure mode this
    whole class exists to prevent -- the workload comes up, reads nothing,
    writes into a directory nobody else can see, and every status is green.
    """


# --- ids, arns, paths --------------------------------------------------------


def _mint(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(9)[:_ID_HEX]}"


def file_system_arn(file_system_id: str) -> str:
    return f"arn:aws:elasticfilesystem:{REGION}:{ACCOUNT}:file-system/{file_system_id}"


def access_point_arn(access_point_id: str) -> str:
    return f"arn:aws:elasticfilesystem:{REGION}:{ACCOUNT}:access-point/{access_point_id}"


def host_dir(root: Path, env: str, file_system_id: str) -> Path:
    """THE file system. One definition, so nothing can mount a second one.

    Not `.resolve()`d here: `root` is `stores.root`, which a caller may have
    handed in relative (`Path(".odin")` is the production default), and a
    relative source in a `docker -v` is not a bind mount at all -- docker reads
    a source with no leading `/` as a NAMED VOLUME (`runtime/colima.py`'s
    `ContainerSpec.volumes` says so in as many words). `_create` resolves at
    creation time and the mount builders resolve at mount time, which is where
    an absolute path is actually required."""
    return root / env / "gateway" / "efs" / file_system_id


def _fs_key(file_system_id: str) -> str:
    return f"fs:{file_system_id}"


def _ap_key(access_point_id: str) -> str:
    return f"ap:{access_point_id}"


def _file_system(stores: SynthStores, env: str, file_system_id: str) -> dict | None:
    return stores.efsctl.get(env, _fs_key(file_system_id))


def _access_point(stores: SynthStores, env: str, access_point_id: str) -> dict | None:
    return stores.efsctl.get(env, _ap_key(access_point_id))


def _records(stores: SynthStores, env: str, prefix: str) -> list[dict]:
    return [value for key, value in stores.efsctl.items(env).items() if key.startswith(prefix)]


def bare_id(value: str) -> str:
    """`arn:aws:elasticfilesystem:...:access-point/fsap-x` -> `fsap-x`; a bare
    id passes through. Both forms are legal on the wire for every id member
    EFS models (its own patterns spell out the ARN alternative), and Lambda's
    `FileSystemConfig.Arn` is always the ARN form."""
    return value.rpartition("/")[2] or value


# --- wire helpers ------------------------------------------------------------


def _json(status: int, payload: dict) -> Response:
    return Response(json.dumps(payload), status_code=status, media_type="application/json")


def _fs_not_found(file_system_id: str) -> Response:
    return errors.synth_error(
        "elasticfilesystem", "FileSystemNotFound",
        f"File system {file_system_id} does not exist.", 404,
    )


def _ap_not_found(access_point_id: str) -> Response:
    return errors.synth_error(
        "elasticfilesystem", "AccessPointNotFound",
        f"Access point {access_point_id} does not exist.", 404,
    )


def _bad_request(message: str) -> Response:
    return errors.synth_error("elasticfilesystem", "BadRequest", message, 400)


def _tags_key(arn: str) -> str:
    return f"elasticfilesystem:{arn}"


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, _tags_key(arn), {})


def _tags_from_list(items: object) -> dict[str, str]:
    return {
        item["Key"]: item.get("Value", "")
        for item in (items or []) if isinstance(item, dict) and "Key" in item
    }


def _tags_to_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def _fs_wire(record: dict, tags: dict[str, str]) -> dict:
    """`FileSystemDescription`, with every member botocore marks required
    present: OwnerId, CreationToken, FileSystemId, CreationTime, LifeCycleState,
    NumberOfMountTargets, SizeInBytes, PerformanceMode, Tags."""
    return {
        "OwnerId": ACCOUNT,
        "CreationToken": record["creation_token"],
        "FileSystemId": record["file_system_id"],
        "FileSystemArn": file_system_arn(record["file_system_id"]),
        "CreationTime": record["created_at"],
        "LifeCycleState": "available",
        "Name": tags.get("Name"),
        "NumberOfMountTargets": 0,
        # Real, not invented: what the directory actually holds right now.
        # `_size_bytes` walks it, so a file written by a task is visible here.
        "SizeInBytes": {"Value": record.get("size_bytes", 0)},
        "PerformanceMode": record["performance_mode"],
        "Encrypted": False,
        "ThroughputMode": record["throughput_mode"],
        "Tags": _tags_to_list(tags),
    }


def _ap_wire(record: dict, tags: dict[str, str]) -> dict:
    return {
        "ClientToken": record["client_token"],
        "Name": tags.get("Name"),
        "Tags": _tags_to_list(tags),
        "AccessPointId": record["access_point_id"],
        "AccessPointArn": access_point_arn(record["access_point_id"]),
        "FileSystemId": record["file_system_id"],
        "PosixUser": record.get("posix_user"),
        "RootDirectory": record.get("root_directory") or {"Path": "/"},
        "OwnerId": ACCOUNT,
        "LifeCycleState": "available",
    }


def _size_bytes(directory: Path) -> int:
    """What the file system really holds, off the real directory.

    `SizeInBytes` is a required member and the obvious thing is to store a
    constant, which is how a field becomes decorative. It is cheap here (an
    EFS-backed canvas holds shared config and small artefacts, not a data lake)
    and it is the one number in the whole `FileSystemDescription` that a user
    could check against `du` -- so it is read, not remembered. A file that
    disappears mid-walk (a task writing while tofu refreshes) is skipped rather
    than raising: this is a size, not a guarantee."""
    return sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())


# --- handlers ----------------------------------------------------------------


async def _create_file_system(
    resource: str, env: str, body: bytes, query: dict[str, str], stores: SynthStores, now: float,
) -> Response:
    payload = _payload(body)
    token = payload.get("CreationToken") or ""
    if not token:
        # CreationToken is `required` in botocore's own CreateFileSystemRequest
        # (min 1, max 64). Refusing beats recording a file system whose identity
        # the caller never chose -- the same reason `lambdactl._create_function`
        # refuses a nameless function rather than minting one nothing can name.
        return _bad_request("CreationToken is required and must be 1-64 characters")
    if len(token) > 64:
        return _bad_request(
            f"CreationToken is capped at 64 characters by EFS itself and this one is {len(token)}. "
            f"odin will not truncate it: a silently shortened token is a file system that the next "
            f"apply cannot find by the name it was created with."
        )
    existing = next((r for r in _records(stores, env, "fs:") if r["creation_token"] == token), None)
    if existing is not None:
        # Real CreateFileSystem is IDEMPOTENT on the creation token and answers
        # `FileSystemAlreadyExists` carrying the existing id (botocore models
        # that member as required on the error shape) -- which is what lets a
        # retried create learn it already succeeded.
        return errors.synth_error(
            "elasticfilesystem", "FileSystemAlreadyExists",
            f"File system already exists with creation token {token}: {existing['file_system_id']}", 409,
        )

    file_system_id = _mint("fs")
    directory = host_dir(stores.root, env, file_system_id).resolve()
    # THE SUBSTRATE. 0700 like the rest of `.odin/<env>/gateway`, created before
    # the record exists so a record can never name a directory that is not
    # there. `.resolve()` because a relative source is a NAMED VOLUME to
    # docker/nerdctl, not a bind mount -- see `host_dir`.
    private_mkdir(directory)
    record = {
        "file_system_id": file_system_id,
        "creation_token": token,
        "created_at": now,
        "performance_mode": payload.get("PerformanceMode") or "generalPurpose",
        "throughput_mode": payload.get("ThroughputMode") or "bursting",
        "host_dir": str(directory),
        "size_bytes": 0,
    }
    stores.efsctl.set(env, _fs_key(file_system_id), record)
    stores.tags.set(env, _tags_key(file_system_arn(file_system_id)), _tags_from_list(payload.get("Tags")))
    return _json(201, _fs_wire(record, _tags_for(stores, env, file_system_arn(file_system_id))))


async def _describe_file_systems(
    resource: str, env: str, body: bytes, query: dict[str, str], stores: SynthStores, now: float,
) -> Response:
    wanted_id = bare_id(query.get("FileSystemId", ""))
    wanted_token = query.get("CreationToken", "")
    # THE POST-DELETE ANSWER, and the one branch in this module that a probe
    # rather than a preference decided (module docstring). A poll naming a file
    # system that is gone gets 404 FileSystemNotFound -- real AWS's own answer,
    # measured accepted by a real `tofu destroy`.
    if wanted_id and _file_system(stores, env, wanted_id) is None:
        return _fs_not_found(wanted_id)
    found = [
        record for record in _records(stores, env, "fs:")
        if wanted_id in ("", record["file_system_id"])
        and wanted_token in ("", record["creation_token"])
    ]
    return _json(200, {"FileSystems": [
        _fs_wire(_with_size(stores, env, record), _tags_for(stores, env, file_system_arn(record["file_system_id"])))
        for record in found
    ]})


def _with_size(stores: SynthStores, env: str, record: dict) -> dict:
    """The record with `size_bytes` re-read off the real directory.

    Read at DESCRIBE time rather than written at mount time, because the writer
    is a container odin does not sit between: an ECS task appending to a shared
    file is not a call this gateway ever sees. Asking the filesystem is the only
    way this number can be true."""
    directory = Path(record["host_dir"])
    if not directory.is_dir():
        return record
    updated = {**record, "size_bytes": _size_bytes(directory)}
    stores.efsctl.set(env, _fs_key(record["file_system_id"]), updated)
    return updated


async def _delete_file_system(
    resource: str, env: str, body: bytes, query: dict[str, str], stores: SynthStores, now: float,
) -> Response:
    record = _file_system(stores, env, resource)
    if record is None:
        return _fs_not_found(resource)
    holders = [
        ap["access_point_id"] for ap in _records(stores, env, "ap:")
        if ap["file_system_id"] == resource
    ]
    if holders:
        # `FileSystemInUse` (409) is EFS's OWN error for this, and the real API
        # has the same ordering rule: mount targets must go before the file
        # system. The provider already deletes the access point first (measured
        # DESTROY sequence above), so this fires only for a caller that skips
        # that -- and refusing is the honest answer, because deleting here would
        # leave access-point records pointing at a directory that is gone.
        return errors.synth_error(
            "elasticfilesystem", "FileSystemInUse",
            f"File system {resource} still has access points: {', '.join(sorted(holders))}. "
            f"Delete them first.", 409,
        )
    standing = remove_host_dir(stores, env, resource)
    if standing:
        # Honesty rule 2: the status comes from the OUTCOME. A directory that
        # would not go means the file system is STILL THERE, so this must not
        # answer 204 -- a caller told "deleted" over a live directory will
        # recreate the resource around data that never left.
        return errors.synth_error(
            "elasticfilesystem", "InternalServerError",
            f"odin could not remove the directory behind file system {resource}, so it still exists: "
            f"{standing}", 500,
        )
    stores.efsctl.delete(env, _fs_key(resource))
    stores.tags.set(env, _tags_key(file_system_arn(resource)), {})
    return Response(status_code=204)


async def _describe_lifecycle_configuration(
    resource: str, env: str, body: bytes, query: dict[str, str], stores: SynthStores, now: float,
) -> Response:
    """Always the empty policy list, and that is the true answer rather than a
    stub: odin implements no lifecycle transitions at all, so a file system here
    HAS no policies. botocore's own documentation names this exact shape for the
    case ("For a file system without a LifecycleConfiguration object, the call
    returns an empty array"). The 404 for an unknown file system is the same
    honest half -- `FileSystemNotFound` is in this operation's own error list."""
    if _file_system(stores, env, resource) is None:
        return _fs_not_found(resource)
    return _json(200, _LIFECYCLE_EMPTY)


async def _create_access_point(
    resource: str, env: str, body: bytes, query: dict[str, str], stores: SynthStores, now: float,
) -> Response:
    payload = _payload(body)
    file_system_id = bare_id(payload.get("FileSystemId") or "")
    if _file_system(stores, env, file_system_id) is None:
        return _fs_not_found(file_system_id)
    access_point_id = _mint("fsap")
    record = {
        "access_point_id": access_point_id,
        "file_system_id": file_system_id,
        "client_token": payload.get("ClientToken") or "",
        "created_at": now,
        "posix_user": payload.get("PosixUser"),
        "root_directory": payload.get("RootDirectory") or {"Path": "/"},
    }
    stores.efsctl.set(env, _ap_key(access_point_id), record)
    stores.tags.set(env, _tags_key(access_point_arn(access_point_id)), _tags_from_list(payload.get("Tags")))
    return _json(200, _ap_wire(record, _tags_for(stores, env, access_point_arn(access_point_id))))


async def _describe_access_points(
    resource: str, env: str, body: bytes, query: dict[str, str], stores: SynthStores, now: float,
) -> Response:
    wanted_ap = bare_id(query.get("AccessPointId", ""))
    wanted_fs = bare_id(query.get("FileSystemId", ""))
    if wanted_ap and _access_point(stores, env, wanted_ap) is None:
        return _ap_not_found(wanted_ap)
    if wanted_fs and _file_system(stores, env, wanted_fs) is None:
        return _fs_not_found(wanted_fs)
    found = [
        record for record in _records(stores, env, "ap:")
        if wanted_ap in ("", record["access_point_id"])
        and wanted_fs in ("", record["file_system_id"])
    ]
    return _json(200, {"AccessPoints": [
        _ap_wire(record, _tags_for(stores, env, access_point_arn(record["access_point_id"])))
        for record in found
    ]})


async def _delete_access_point(
    resource: str, env: str, body: bytes, query: dict[str, str], stores: SynthStores, now: float,
) -> Response:
    record = _access_point(stores, env, resource)
    if record is None:
        return _ap_not_found(resource)
    stores.efsctl.delete(env, _ap_key(resource))
    stores.tags.set(env, _tags_key(access_point_arn(resource)), {})
    return Response(status_code=204)


def _payload(body: bytes) -> dict:
    """The request body as a JSON object, or `{}`.

    ONE try/except, at the boundary where non-JSON bytes can arrive -- the same
    shape `lambdactl._payload` keeps. A body that is valid JSON but not an
    object (a bare list, a string) reduces to `{}` too, so every `.get` below is
    safe without a second type check per field."""
    try:
        parsed = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- the mount join: taskdef volumes + mountPoints -> real host paths --------


def _efs_volume_ids(taskdef: dict) -> dict[str, str]:
    """`{volume name -> file system id}` for the EFS volumes of a task
    definition.

    `taskdef["volumes"]` has been STORED and echoed since ECS landed
    (`ecsctl.py`) and read by absolutely nothing -- this function is what
    finally reads it. A `volume` with no `efsVolumeConfiguration` (a
    `host{sourcePath}` or `dockerVolumeConfiguration` volume) is skipped and not
    an error: those are other kinds of volume odin does not model, and refusing
    them here would break task definitions that merely mention one."""
    return {
        volume["name"]: bare_id(str((volume.get("efsVolumeConfiguration") or {}).get("fileSystemId") or ""))
        for volume in taskdef.get("volumes") or []
        if isinstance(volume, dict) and volume.get("name") and volume.get("efsVolumeConfiguration")
    }


def task_mounts(stores: SynthStores, env: str, taskdef: dict, container_def: dict) -> dict[str, str]:
    """`{host path -> container path}` for every EFS volume this container
    mounts -- the join of `taskdef["volumes"]` with
    `container_def["mountPoints"]`, which is the pair AWS itself splits the
    information across.

    `containerDefinitions` is stored VERBATIM under ecsctl's zero-drift mandate
    and is NOT mutated here: this reads `mountPoints` and builds a separate
    `ContainerSpec.volumes` dict, so what tofu reads back is byte-for-byte what
    it sent.

    Raises `MountUnavailable` rather than skipping, in both of the ways this can
    fail -- see the class docstring for why a quiet skip is the worse outcome.
    """
    by_name = _efs_volume_ids(taskdef)
    roots = _efs_volume_roots(taskdef)
    mounts: dict[str, str] = {}
    for point in container_def.get("mountPoints") or []:
        source_volume = point.get("sourceVolume", "")
        file_system_id = by_name.get(source_volume)
        if not file_system_id:
            continue  # a mountPoint onto a non-EFS volume: not this module's business
        _refuse_read_only(point, source_volume)
        _refuse_subdirectory(roots.get(source_volume, "/"), source_volume, "rootDirectory")
        mounts[str(_mount_source(stores, env, file_system_id, source_volume))] = (
            point.get("containerPath") or ""
        )
    return mounts


def _refuse_read_only(point: dict, referrer: str) -> None:
    """`readOnly: true` is REFUSED, not quietly downgraded to a writable mount.

    `ContainerSpec.volumes` is `dict[source -> container_path]` and the renderer
    has no `:ro` form (`runtime/colima.py`), so odin cannot make this true. It
    would be easy to accept it and mount read-WRITE -- and that is the worst of
    the three options: a user who asked for read-only protection would not get
    it, would not be told, and would find out by having data overwritten. Real
    ECS honours the flag, so accepting it here would also be a canvas that
    behaves differently on Amazon.

    `iac/hcl.py` hardcodes `readOnly: false`, so no odin-generated project can
    reach this. It bites only a hand-written or IMPORTED task definition -- which
    is exactly the case v0.8.12 exists to honour, so it is worth refusing loudly
    rather than leaving as a trap."""
    if point.get("readOnly"):
        raise MountUnavailable(
            f"{referrer!r} is mounted with `readOnly: true`, and odin cannot enforce that: its "
            f"container runtime renders a bind mount as `-v source:target` with no `:ro` form. "
            f"Nothing was mounted, rather than mounting it WRITABLE and letting you believe the "
            f"data was protected. Set `readOnly: false` if a writable share is acceptable."
        )


def _refuse_subdirectory(path: str, referrer: str, field: str) -> None:
    """A `rootDirectory` other than `/` is REFUSED for the same reason.

    EFS's `rootDirectory` (on an ECS volume) and an access point's
    `RootDirectory.Path` both mean "expose THIS subtree, not the whole file
    system" -- a scoping control. odin mounts the file system's own directory,
    so honouring a subpath would need a different source path per consumer.
    Until it does, accepting the field would mean a consumer scoped to `/data`
    silently receiving the WHOLE file system, including every other consumer's
    files. That is a confidentiality answer, not a cosmetic one.

    `iac/hcl.py` always emits `/`, so no odin-generated project reaches this;
    a hand-written or imported one can."""
    if path not in ("", "/"):
        raise MountUnavailable(
            f"{referrer!r} sets `{field}` to {path!r}, and odin mounts the whole file system rather "
            f"than a subtree -- so honouring this would need a source path odin does not build yet. "
            f"Nothing was mounted, rather than silently exposing the ENTIRE file system to something "
            f"that asked to see only {path!r}. Use `/` to mount the whole share deliberately."
        )


def _efs_volume_roots(taskdef: dict) -> dict[str, str]:
    """`{volume name -> its declared rootDirectory}`, defaulting to `/`."""
    return {
        volume["name"]: str((volume.get("efsVolumeConfiguration") or {}).get("rootDirectory") or "/")
        for volume in taskdef.get("volumes") or []
        if isinstance(volume, dict) and volume.get("name") and volume.get("efsVolumeConfiguration")
    }


def function_mounts(stores: SynthStores, env: str, file_system_configs: list) -> dict[str, str]:
    """`{host path -> container path}` for a Lambda's `FileSystemConfigs`.

    Lambda names an ACCESS POINT, never a file system -- `FileSystemConfig.Arn`
    is documented as an access-point ARN and botocore's own pattern demands
    `access-point/fsap-[a-f0-9]{17}$` -- so this resolves Arn -> access point ->
    file system -> directory. Handing it a file-system ARN would be a project
    real AWS rejects, which is exactly why `iac/hcl.py` emits a companion
    `aws_efs_access_point` for a canvas with a lambda mount edge."""
    mounts: dict[str, str] = {}
    for config in file_system_configs or []:
        access_point_id = bare_id(str(config.get("Arn") or ""))
        record = _access_point(stores, env, access_point_id)
        if record is None:
            raise MountUnavailable(
                f"this function is drawn with an EFS mount at {config.get('LocalMountPath')!r}, but odin "
                f"has no access point {access_point_id!r}. Nothing was mounted and the function was NOT "
                f"started with an empty directory in its place -- apply the file system first."
            )
        # An access point's own RootDirectory scopes what it exposes, exactly as
        # an ECS volume's `rootDirectory` does -- and odin honours neither, so it
        # refuses both rather than handing the whole file system to a function
        # scoped to a subtree. See `_refuse_subdirectory`.
        _refuse_subdirectory(
            str((record.get("root_directory") or {}).get("Path") or "/"),
            access_point_id, "RootDirectory.Path",
        )
        local_path = str(config.get("LocalMountPath") or "")
        if not LOCAL_MOUNT_PATH.match(local_path):
            # AWS's own `LocalMountPath` pattern, checked against the real value
            # on the wire. Real Lambda refuses this outright, so accepting it
            # would make odin the more permissive of the two -- a canvas that
            # works here and fails on Amazon.
            raise MountUnavailable(
                f"{local_path!r} is not a mount path real Lambda accepts: it must be `/mnt/` followed by "
                f"exactly ONE segment (AWS's own pattern is `/mnt/[a-zA-Z0-9-_.]+`, which has no second "
                f"slash in it). Nothing was mounted."
            )
        mounts[str(_mount_source(stores, env, record["file_system_id"], access_point_id))] = local_path
    return mounts


def _mount_source(stores: SynthStores, env: str, file_system_id: str, referrer: str) -> Path:
    """The absolute host directory to bind-mount, or `MountUnavailable`.

    THE GUARD READS THE FILESYSTEM, not the record. A record saying a file
    system exists is exactly the kind of self-report this repo has been burned
    by; what makes the mount real is the directory, so that is what is checked.
    It is not theoretical either: `docker -v` CREATES a missing source path
    (as root, empty) rather than failing, so without this check a workload
    mounting a destroyed file system starts happily, reads nothing, and every
    status stays green."""
    record = _file_system(stores, env, file_system_id)
    if record is None:
        raise MountUnavailable(
            f"{referrer!r} mounts EFS file system {file_system_id!r}, which odin has no record of. "
            f"Nothing was mounted."
        )
    directory = Path(record["host_dir"])
    if not directory.is_dir():
        raise MountUnavailable(
            f"{referrer!r} mounts EFS file system {file_system_id!r}, whose directory {directory} is "
            f"NOT on disk. odin refused to start this workload rather than bind-mount an empty "
            f"directory in place of the shared file system -- a container that mounts a missing source "
            f"path gets a fresh empty one and reports no error at all, which would have looked exactly "
            f"like a working mount holding no data."
        )
    return directory


# --- teardown ----------------------------------------------------------------


def remove_host_dir(stores: SynthStores, env: str, file_system_id: str) -> str:
    """Remove one file system's directory. Returns "" on success, or WHY it is
    still standing.

    `shutil.rmtree` with a collecting error handler rather than
    `ignore_errors=True`, which is the whole point: `ignore_errors` is how a
    delete path reports success over a directory that never went. The return
    value is a reason string precisely so the caller cannot accidentally treat
    a failure as a success -- an empty string is the only falsy outcome.

    `onexc`, not the older `onerror`: `onerror` is deprecated from Python 3.12
    and this tree runs 3.13, so it would raise a DeprecationWarning on the one
    path that must not be noisy. The final `exists()` check is not redundant
    either -- it is what catches a removal that reported nothing and did
    nothing, which is the failure this function is named after."""
    record = _file_system(stores, env, file_system_id)
    directory = Path(record["host_dir"]) if record else host_dir(stores.root, env, file_system_id)
    failures: list[str] = []

    def _collect(_function, path, exc: BaseException) -> None:
        failures.append(f"{path}: {errors.exc_text(exc)}")

    shutil.rmtree(directory, onexc=_collect)
    if failures:
        return "; ".join(failures)
    return f"{directory} still exists after rmtree reported no error" if directory.exists() else ""


def reclaim_env_file_systems(stores: SynthStores, env: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(reclaimed, standing)` -- every EFS directory this env owns, removed.

    The teardown half of honesty rule 2, and it is needed for the same reason
    ebs's disk sweep is: `/destroy` runs `tofu destroy`, which reaches
    DeleteFileSystem only if the apply that created the state got that far. An
    interrupted apply leaves a directory with a record; a deleted store leaves
    one with neither. So this sweeps `.odin/{env}/gateway/efs/` ITSELF -- the
    real directory on disk -- rather than iterating records, and it is scoped to
    the one env directory by construction, so it can never reach another env's
    data.

    `standing` is what the caller must refuse to report success over --
    `file_systems_standing` below is the sentence for it."""
    root = stores.root / env / "gateway" / "efs"
    if not root.is_dir():
        return (), ()
    reclaimed: list[str] = []
    standing: list[str] = []
    for directory in sorted(root.iterdir()):
        reason = remove_host_dir(stores, env, directory.name)
        (standing if reason else reclaimed).append(f"{directory.name}: {reason}" if reason else directory.name)
    return tuple(reclaimed), tuple(standing)


def file_systems_standing(env: str, standing: tuple[str, ...]) -> str:
    """The `/destroy` half of the sentence, kept next to the sweep that
    produces it rather than in the route -- `ec2compute.disks_standing`'s own
    convention, for the same reason.

    It names USER DATA specifically. An EBS disk that will not go is space; an
    EFS directory that will not go is whatever the workloads wrote into it, so
    "destroyed" over one is a claim about somebody's files."""
    return (
        f"env {env!r} is NOT destroyed: {len(standing)} EFS file system director(ies) are still on "
        f"this disk and could not be removed -- {'; '.join(standing)}. Each one holds whatever the "
        f"workloads that mounted it wrote, so odin will not report the env destroyed over them. "
        f"Retry `odin destroy`, or remove them by exact path."
    )


# --- dispatch ----------------------------------------------------------------

_Handler = Callable[[str, str, bytes, dict[str, str], SynthStores, float], Awaitable[Response]]

_HANDLERS: dict[str, _Handler] = {
    "CreateFileSystem": _create_file_system,
    "DescribeFileSystems": _describe_file_systems,
    "DeleteFileSystem": _delete_file_system,
    "DescribeLifecycleConfiguration": _describe_lifecycle_configuration,
    "CreateAccessPoint": _create_access_point,
    "DescribeAccessPoints": _describe_access_points,
    "DeleteAccessPoint": _delete_access_point,
}


async def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    query: dict[str, str] | None = None,
) -> Response:
    """The whole EFS answer -- same no-backing contract as the other models: an
    unmodeled action gets a protocol-correct error, never a 503.

    `query` is app.py's already-parsed query dict, and unlike most models this
    one genuinely needs it: `DescribeFileSystems`/`DescribeAccessPoints` carry
    their entire filter in the query string (rest-json puts a `querystring`
    location on those members), so without it every describe would be an
    unfiltered list-all and the post-delete 404 could never fire."""
    op = action.removeprefix("elasticfilesystem:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return _bad_request(f"The action {op} is not valid.")
    return await handler(resource, env, body, query or {}, stores, now)
