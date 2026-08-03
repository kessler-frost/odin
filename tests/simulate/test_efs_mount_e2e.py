"""The EFS claim, proven by two REAL containers sharing one REAL directory.

Everything else about `efs` -- the builder, the importer, the gateway model,
the World projection -- can be green while nothing is mounted.
`DescribeFileSystems` returning what odin itself wrote proves the store
round-trips and nothing more, and `tests/gateway/test_efsctl.py` says so in its
own docstring. So this test asks the only question that matters: **does task B
see what task A wrote?**

What it proves, in order, each through the real gateway handlers with a real
`TaskRuntime` on real Colima (no fake anywhere in this file):

  1. `CreateFileSystem` through the real classify -> synth path makes a real
     0700 directory on the host -- and it is EMPTY to begin with, asserted, so
     step 3 cannot pass on a file that was already there
  2. task A boots with that directory bind-mounted and writes a file into it
  3. the file appears ON THE HOST                      -- container -> host
  4. task B, a SEPARATE container from a SEPARATE service, boots with the same
     file system and `cat`s the file back out           -- host -> container
  5. `DeleteFileSystem` really removes the directory, verified by looking

Steps 3 and 4 are the two halves that a single container could fake on its own.
Neither container ever talks to the other; the directory is the only thing
between them.

WHY `.odin/` AND NOT `tmp_path`, and this is load-bearing rather than tidy: a
`-v` of a path under macOS's per-user temp dir (`/private/var/folders/...`)
silently mounts an EMPTY directory under Colima's virtiofs -- the path exists
inside the VM, so nothing errors (`runtime/colima.py::copy_in`,
`compute/proxy.py`, measured on this repo). A version of this test using
`tmp_path` would pass against a fake runtime and mount NOTHING against a real
one -- which is exactly the false green the whole file exists to prevent. So
the store root is a real directory under the repo checkout, itself under
`$HOME`, which is the only tree Colima shares in.

Hygiene is absolute: a `finally` removes the containers THIS test created, by
exact name, and its own store directory -- whether it passed, failed or raised.
It never lists or filters anything machine-wide, and every name it touches
carries the `efs-e2e` env prefix.

Cost: two real `alpine` container boots (the image is a few MB and is usually
already local). `integration`-only for that reason.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from odin.compute.tasks import TaskRuntime, container_name
from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.models import ecsctl
from odin.gateway.stores import SynthStores
from odin.runtime.colima import ColimaRuntime

# PER-TEST, not `pytestmark`, and deliberately: the two container-free ratchets
# at the bottom of this file guard the premises the two real tests rest on, and
# a ratchet that only runs when somebody remembers `-m integration` is a ratchet
# that will be discovered broken by the thing it was meant to prevent.
_needs_containers = pytest.mark.integration

ENV = "efs-e2e"
CLUSTER = "efs-e2e-cluster"
LABEL = "efs-e2e-shared"
MOUNT = "/mnt/efs"
FILENAME = "written-by-task-a.txt"
CONTENT = "task A wrote this through a real bind mount"

# NOT `tmp_path` -- see the module docstring. Under the repo checkout (and so
# under $HOME), which is the only tree Colima shares into its VM.
_STORE_ROOT = Path(__file__).resolve().parents[2] / ".odin" / "efs-e2e-store"

_IMAGE = "alpine:3.20"
_BOOT_TIMEOUT = 180.0
_POLL = 1.0


def _split(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return parts.path, dict(parse_qsl(parts.query, keep_blank_values=True))


async def _efs(stores: SynthStores, method: str, path: str, body: bytes = b"", **query: str):
    """One EFS call through the REAL pipeline: classify -> synth.pure_answer.
    Hand-built rather than boto3-signed on purpose -- `tests/gateway/
    test_efsctl.py` already proves the wire shapes against botocore's own
    parser, and what this file is for is the substrate underneath them."""
    classified = classify("elasticfilesystem", method, path, query, {}, body)
    assert classified is not None, f"unmappable EFS request: {method} {path}"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, body, stores, time.time(), query=query)
    assert response is not None, "EFS is all-synth"
    return response


async def _ecs(stores: SynthStores, runtime: TaskRuntime, target: str, payload: dict):
    """One ECS call through the REAL pipeline. ECS is the AWS-JSON protocol, so
    the operation rides in the `X-Amz-Target` header."""
    body = json.dumps(payload).encode()
    headers = {"x-amz-target": f"AmazonEC2ContainerServiceV20141113.{target}"}
    classified = classify("ecs", "POST", "/", {}, headers, body)
    assert classified is not None, f"unmappable ECS request: {target}"
    action, resource = classified
    response = await ecsctl.pure_answer(action, resource, ENV, body, stores, time.time(), runtime)
    assert response.status_code == 200, f"{target} failed: {response.body!r}"
    return json.loads(response.body)


def _taskdef(family: str, file_system_id: str, command: list[str]) -> dict:
    """A single-container task definition that mounts the file system. The two
    halves AWS splits this across: the file system id on the TASK
    DEFINITION's `volumes[]`, the container path on the CONTAINER
    DEFINITION's `mountPoints[]`, joined on the volume name."""
    return {
        "family": family,
        "volumes": [{"name": "shared", "efsVolumeConfiguration": {
            "fileSystemId": file_system_id, "rootDirectory": "/",
        }}],
        "containerDefinitions": [{
            "name": "app",
            "image": _IMAGE,
            "essential": True,
            "command": ["sh", "-c", " ".join(command)],
            "mountPoints": [{"sourceVolume": "shared", "containerPath": MOUNT, "readOnly": False}],
        }],
    }


async def _wait_for_running(stores: SynthStores, service_name: str) -> dict:
    """Poll until this service has a RUNNING task, or a STOPPED one -- and
    break on BOTH, for `test_ebs_volume_e2e.py`'s reason: a launch that fails
    records its terminal state within seconds, and a loop watching only for
    RUNNING then burns its whole deadline and reports a three-minute timeout
    for a two-second failure, burying the one line that says what went wrong."""
    deadline = time.monotonic() + _BOOT_TIMEOUT
    while time.monotonic() < deadline:
        tasks = [
            record for key, record in stores.ecsctl.items(ENV).items()
            if key.startswith("task:") and record["service_name"] == service_name
        ]
        terminal = [t for t in tasks if t["last_status"] in ("RUNNING", "STOPPED")]
        if terminal:
            return terminal[0]
        await asyncio.sleep(_POLL)
    raise AssertionError(f"service {service_name} never launched a task within {_BOOT_TIMEOUT:g}s")


async def _wait_for_file(path: Path) -> str:
    """The container's write landing on the HOST. A real `docker run` returns
    as soon as the process starts, so the write is genuinely asynchronous with
    respect to this test -- this is a real wait, not a formality."""
    deadline = time.monotonic() + _BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(_POLL)
    raise AssertionError(
        f"task A reported RUNNING but nothing appeared at {path} within {_BOOT_TIMEOUT:g}s -- "
        f"the bind mount did not reach the host"
    )


async def _wait_for_logs(runtime: TaskRuntime, task_id: str, want: str) -> str:
    deadline = time.monotonic() + _BOOT_TIMEOUT
    logs = ""
    while time.monotonic() < deadline:
        logs = await runtime.logs(ENV, task_id, "app", tail=50)
        if want in logs:
            return logs
        await asyncio.sleep(_POLL)
    return logs


@_needs_containers
async def test_two_real_containers_share_one_real_directory():
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)
    stores = SynthStores(_STORE_ROOT)
    runtime = TaskRuntime(ColimaRuntime())
    started: list[str] = []
    file_system_id = ""
    try:
        # 1. A real file system -- which is a real, EMPTY directory. ---------
        created = json.loads((await _efs(
            stores, "POST", "/2015-02-01/file-systems",
            json.dumps({"CreationToken": LABEL, "Tags": [{"Key": "odin:node", "Value": LABEL}]}).encode(),
        )).body)
        file_system_id = created["FileSystemId"]
        directory = Path(stores.efsctl.get(ENV, f"fs:{file_system_id}")["host_dir"])
        assert directory.is_dir(), f"CreateFileSystem answered 201 but {directory} is not on disk"
        assert list(directory.iterdir()) == [], "the file system is not empty before the test writes to it"
        shared_file = directory / FILENAME
        # Asserted explicitly: step 3 must not be able to pass on a leftover.
        assert not shared_file.exists()

        await _ecs(stores, runtime, "CreateCluster", {"clusterName": CLUSTER})

        # 2. Task A writes into the mount. -----------------------------------
        await _ecs(stores, runtime, "RegisterTaskDefinition", _taskdef(
            "efs-e2e-writer", file_system_id,
            [f"echo '{CONTENT}' > {MOUNT}/{FILENAME};", "sleep 300"],
        ))
        await _ecs(stores, runtime, "CreateService", {
            "cluster": CLUSTER, "serviceName": "efs-e2e-writer",
            "taskDefinition": "efs-e2e-writer", "desiredCount": 1,
        })
        writer = await _wait_for_running(stores, "efs-e2e-writer")
        started.append(container_name(ENV, writer["task_id"], "app"))
        assert writer["last_status"] == "RUNNING", (
            f"task A never started: {writer.get('stopped_reason')}"
        )

        # 3. ...and the host really has it. container -> host. ---------------
        on_host = await _wait_for_file(shared_file)
        assert on_host == CONTENT, f"the host file holds {on_host!r}"

        # 4. Task B -- a SEPARATE container, from a SEPARATE service -- reads
        #    it back. host -> container, and the half a single container could
        #    not fake. -------------------------------------------------------
        await _ecs(stores, runtime, "RegisterTaskDefinition", _taskdef(
            "efs-e2e-reader", file_system_id,
            [f"cat {MOUNT}/{FILENAME};", "sleep 300"],
        ))
        await _ecs(stores, runtime, "CreateService", {
            "cluster": CLUSTER, "serviceName": "efs-e2e-reader",
            "taskDefinition": "efs-e2e-reader", "desiredCount": 1,
        })
        reader = await _wait_for_running(stores, "efs-e2e-reader")
        started.append(container_name(ENV, reader["task_id"], "app"))
        assert reader["last_status"] == "RUNNING", (
            f"task B never started: {reader.get('stopped_reason')}"
        )
        assert reader["task_id"] != writer["task_id"], "both halves ran in the same container"

        logs = await _wait_for_logs(runtime, reader["task_id"], CONTENT)

        # THE assertion this whole file exists for.
        assert CONTENT in logs, (
            f"task B could not read what task A wrote. Its container printed {logs!r}. "
            f"The host file at {shared_file} holds {shared_file.read_text()!r}, so the write "
            f"reached the host and the READ side of the mount is what failed."
        )
        print(
            f"\nMEASURED: two containers, one directory.\n"
            f"  host directory : {directory}\n"
            f"  task A ({writer['task_id'][:8]}) wrote  : {shared_file.read_text().strip()!r}\n"
            f"  task B ({reader['task_id'][:8]}) printed: {logs.strip()!r}"
        )

        # 5. Teardown really removes it, verified by LOOKING. ----------------
        deleted = await _efs(stores, "DELETE", f"/2015-02-01/file-systems/{file_system_id}")
        assert deleted.status_code == 204, deleted.body
        assert not directory.exists(), "DeleteFileSystem answered 204 over a directory still on disk"
    finally:
        # By EXACT name, and only what this test made. Never a machine-wide
        # filter -- another agent's containers are one `--filter label=odin=1`
        # away, and that has already deleted somebody's work mid-verification.
        for name in started:
            await ColimaRuntime().stop(name)
        shutil.rmtree(_STORE_ROOT, ignore_errors=True)


@_needs_containers
async def test_a_task_is_refused_rather_than_mounted_empty_when_the_directory_is_gone():
    """The guard, against the REAL runtime -- because this is the one place the
    difference is observable. `docker -v` of a missing source path does not
    fail: it creates a fresh empty directory, as root, and the container comes
    up reporting nothing wrong. So a unit test can prove odin RAISES, and only
    this can prove that what odin is refusing to do would really have looked
    like success.

    No container is expected to start, so this costs nothing but the refusal.
    """
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)
    stores = SynthStores(_STORE_ROOT)
    runtime = TaskRuntime(ColimaRuntime())
    started: list[str] = []
    try:
        created = json.loads((await _efs(
            stores, "POST", "/2015-02-01/file-systems",
            json.dumps({"CreationToken": LABEL}).encode(),
        )).body)
        file_system_id = created["FileSystemId"]
        directory = Path(stores.efsctl.get(ENV, f"fs:{file_system_id}")["host_dir"])
        directory.rmdir()  # the RECORD survives; the substrate does not
        assert not directory.exists(), "the injection did nothing -- this test proves nothing"

        await _ecs(stores, runtime, "CreateCluster", {"clusterName": CLUSTER})
        await _ecs(stores, runtime, "RegisterTaskDefinition", _taskdef(
            "efs-e2e-orphan", file_system_id, ["sleep 300"],
        ))
        await _ecs(stores, runtime, "CreateService", {
            "cluster": CLUSTER, "serviceName": "efs-e2e-orphan",
            "taskDefinition": "efs-e2e-orphan", "desiredCount": 1,
        })
        task = await _wait_for_running(stores, "efs-e2e-orphan")
        started.append(container_name(ENV, task["task_id"], "app"))

        assert task["last_status"] == "STOPPED", (
            "the task STARTED over a missing file system -- which means it mounted an empty "
            "directory and reported success, the exact failure this guard exists to prevent"
        )
        assert str(directory) in task["stopped_reason"]
        assert "empty directory" in task["stopped_reason"]
        # ...and odin did not create the directory as a side effect of refusing.
        assert not directory.exists()
    finally:
        for name in started:
            await ColimaRuntime().stop(name)
        shutil.rmtree(_STORE_ROOT, ignore_errors=True)


def test_the_store_root_this_file_uses_is_shareable_into_colima():
    """A cheap, container-free ratchet on the hazard in the module docstring.

    If someone later "tidies" `_STORE_ROOT` into `tmp_path`, both tests above
    keep passing against a fake runtime and mount NOTHING against a real one --
    silently, because Colima's virtiofs errors on nothing. This fails instead."""
    assert _STORE_ROOT.is_absolute()
    assert _STORE_ROOT.is_relative_to(Path.home()), (
        f"{_STORE_ROOT} is outside $HOME, the only tree Colima shares into its VM -- a bind mount "
        f"of a path under it would silently be an EMPTY directory inside the container"
    )
    assert "/var/folders/" not in str(_STORE_ROOT), (
        "macOS's per-user temp dir is NOT shared into Colima; a `-v` of a path under it mounts "
        "an empty directory and reports no error"
    )


def test_the_mount_join_this_file_depends_on_is_the_one_production_uses():
    """The two tests above drive `ecsctl._launch_task` -> `efsctl.task_mounts`
    -> `TaskRuntime.run`. This asserts that really is the production path, so
    the e2e cannot quietly become a test of a path only the e2e takes -- the
    "verify through the product's own path" rule, checked rather than assumed.
    """
    source = inspect.getsource(ecsctl._launch_task)
    assert "efsctl.task_mounts" in source, (
        "ecsctl no longer resolves EFS mounts through efsctl.task_mounts -- this e2e is measuring "
        "a path production does not take"
    )
    assert "volumes=" in source, "the resolved mounts no longer reach TaskRuntime.run"
