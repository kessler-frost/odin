"""W2.3 — `odin export`/`odin import` against REAL backings: the whole disaster.

Apply a small canvas in env `bak`, put a real object in the real bucket,
export, tear it all down and delete `.odin/bak` outright — the exact failure
this feature exists for — then import and start a FRESH server. Nothing
re-supplies the canvas: the restored Stack lineage is the only thing left that
knows what `bak` should look like, and the new server's lifespan resumes
reconciling it off the archive alone, provisioning real containers again.

Convergence is asserted against the PHYSICAL backings (`aws.exists`), never
the World phases — the archive restores a `world.json` that already said
"healthy", so a phase-only assertion would pass before the fresh reconciler
had observed a single thing.

The bucket comes back EMPTY. That's the documented boundary, asserted here on
purpose: odin exports control-plane state (what should exist), never
data-plane container volumes (what's inside).

The store CANNOT live under pytest's `tmp_path`: that's macOS's per-user
TMPDIR (`/private/var/folders/…`), which Colima does not share into its VM, so
goaws's config mount comes up empty and it binds its default 4100 instead of
the gateway port odin publishes (proven by this test's own first run —
`Failure to find config file: /conf/goaws.yaml`, then a 120s readiness
timeout). Same constraint `aws/backings.py` and `test_lambda_tf_e2e.py`
already document. `workdir` below puts the whole thing under the repo
checkout, which these commands need anyway: `odin export`/`odin import` read
`.odin` relative to the CURRENT DIRECTORY, so the test has to run from one.

Marked integration: needs Colima/Docker with the backing images pulled.
"""
from __future__ import annotations

import asyncio
import shutil
import tarfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.aws.backings import BackingAws
from odin.backup import ENV_PREFIX, MANIFEST_NAME
from odin.cli.app import app as cli
from odin.runtime.colima import ColimaRuntime
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.containers import own_containers

pytestmark = pytest.mark.integration

ENV = "bak"
CANVAS = {
    "nodes": [
        {"id": "n1", "type": "s3", "data": {"label": "uploads"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
    ],
    "edges": [],
}
_BOTH = ("uploads", "jobs")
OBJECT_KEY = "receipt.txt"


async def _own_containers(rt: ColimaRuntime) -> list[str]:
    """Only the containers THIS test can have made -- it creates them in one
    env, and `aws.gc` already tears them down by env-scoped exact name, so
    the fixture needs no wider reach either. The naming rules (and why the
    unscoped `await rt.list_odin()` this replaced was dangerous) live in
    `tests/containers.py`, shared with every other integration file."""
    return await own_containers(rt, ENV)


@pytest.fixture
async def runtime():
    rt = ColimaRuntime()
    yield rt
    for name in await _own_containers(rt):
        await rt.stop(name)


@pytest.fixture
def workdir(monkeypatch):
    """A working directory under the repo checkout (so `$HOME`, so Colima
    shares it) that the CLI's relative `.odin` resolves inside. Wiped on both
    setup and teardown, so a prior failed run can't leave state a fresh one
    would read."""
    root = Path(__file__).resolve().parents[2] / ".odin-w23-e2e"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _phases(client) -> dict:
    return {r["id"]: r["phase"] for r in client.get("/world", params={"env": ENV}).json()["resources"]}


def _wait(predicate, describe, timeout=180.0, step=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(step)
    raise AssertionError(f"never true within {timeout}s: {describe}")


async def _wait_async(predicate, describe, timeout=180.0, step=1.0):
    """`_wait` for a predicate that has to await (`aws.exists` is a coroutine
    since v0.7.7). A separate helper because `await` is a syntax error inside a
    `lambda`, so these call sites pass a coroutine FUNCTION instead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"never true within {timeout}s: {describe}")


def _aws(client, runtime) -> BackingAws:
    return BackingAws(runtime, ENV, gateway_port=client.get("/health").json()["gateway"]["port"])


async def test_export_then_lose_odin_then_import_converges_again(runner, workdir, runtime):
    root = workdir / ".odin"
    store = SpecStore(root)
    archive = workdir / f"odin-{ENV}-export.tar.gz"

    # --- 1. a real env, real resources, real DATA in one of them
    with TestClient(create_app(runtime=runtime, store=store)) as client:
        assert client.post("/apply", params={"env": ENV}, json=CANVAS).status_code == 200
        aws = _aws(client, runtime)
        _wait(lambda: all(_phases(client).get(n) == "healthy" for n in _BOTH), "both healthy")
        assert await aws.exists("s3", "uploads") and await aws.exists("sqs", "jobs")
        s3 = await aws.client("s3")
        s3.put_object(Bucket="uploads", Key=OBJECT_KEY, Body=b"data-plane bytes")
        assert s3.get_object(Bucket="uploads", Key=OBJECT_KEY)["Body"].read() == b"data-plane bytes"

        # --- 2. export: offline, straight off the filesystem, server up or down
        export = runner.invoke(cli, ["export", "--env", ENV])
        assert export.exit_code == 0, export.output
        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
        assert MANIFEST_NAME in names
        assert {f"{ENV_PREFIX}/HEAD", f"{ENV_PREFIX}/world.json"} <= names
        assert not [n for n in names if f"{ENV_PREFIX}/tf/.terraform/" in n]  # excluded, always

        # --- 3. tear the env down for real
        assert client.post("/destroy", params={"env": ENV}).status_code == 200
        _wait(lambda: not _phases(client), "world empty")
        await aws.gc(set())
        assert not await _own_containers(runtime), "backings gone before the store is wiped"

    # --- ...and lose .odin/bak entirely. odin now has no idea this env existed.
    shutil.rmtree(root / ENV)
    assert ENV not in store.list_envs()

    # --- 4. restore, server down, from the archive alone
    restore = runner.invoke(cli, ["import", str(archive)])
    assert restore.exit_code == 0, restore.output
    assert store.list_envs() == [ENV]
    assert {r.id for r in store.get_stack(ENV).resources} == set(_BOTH)

    # --- 5. a FRESH server, given nothing but the restored store, converges.
    # No canvas is re-supplied and no Apply is issued: the lifespan resumes
    # every env it finds and the reconciler drives the restored Stack back into
    # real containers. Asserted on the physical backings, not on phases.
    with TestClient(create_app(runtime=runtime, store=store)) as client:
        aws = _aws(client, runtime)

        async def _both_exist() -> bool:
            return await aws.exists("s3", "uploads") and await aws.exists("sqs", "jobs")

        await _wait_async(_both_exist, "both backing resources really re-created")
        # ...and the World projection catches up to what's physically there.
        _wait(
            lambda: all(_phases(client).get(n) == "healthy" for n in _BOTH),
            "both resources healthy again",
        )

        # The documented boundary: the bucket is back, its CONTENTS are not.
        # `client` is the coroutine; `list_objects_v2` on the boto3 client it
        # returns is SYNC -- hence the parenthesised await.
        contents = (await aws.client("s3")).list_objects_v2(Bucket="uploads")
        assert contents.get("KeyCount", 0) == 0, contents

        assert client.post("/destroy", params={"env": ENV}).status_code == 200
        _wait(lambda: not _phases(client), "world empty again")
        await aws.gc(set())

    assert await _own_containers(runtime) == [], "every container this test made is gone"
