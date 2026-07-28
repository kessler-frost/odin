"""The memory you type on an ECS node is the cap the real container runs under.

## Why this is an e2e and not another unit test

`tests/agent/test_ecs_task_resources.py` proves the two ends: the canvas value
reaches the generated HCL, and `capacity.py`/`compute/tasks.py` agree about what
it means. Neither proves the MIDDLE, and the middle is the part nobody wrote --
it is OpenTofu's AWS provider deciding whether an `aws_ecs_task_definition`'s
`memory` argument goes on the RegisterTaskDefinition wire at all.

That is exactly the gap honesty rule 1 is about: every unit test here would keep
passing if the provider dropped the argument, because both ends were checked
against each other and never against the thing in between. So this drives a real
`tofu apply` through odin's own gateway and then asks DOCKER what limit the
container actually got.

## What it measures

Two services in one canvas, one apply:

  * `sized`   -- `memory: "256"` on the node -> `HostConfig.Memory` == 256 MiB
  * `default` -- no memory field at all      -> `HostConfig.Memory` == 512 MiB

The second is as load-bearing as the first. `agent/hcl.py` deliberately emits NO
`memory` attribute when the canvas does not set one (writing 512 into the HCL
would freeze today's default into every canvas ever applied), so the fallback has
to be supplied further down, by `compute/tasks.py::_memory_mib`. A regression
that lost the default would show up here as an unlimited container -- and
`HostConfig.Memory == 0` is docker's way of spelling "no limit", which reads as a
perfectly healthy container in every other test odin has.

Same substrate constraints as the other simulate e2es: the store root lives under
`$HOME` (Colima only mounts that tree), and container hygiene is by EXACT name.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.tasks import _DEFAULT_MEMORY_MIB, container_name
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ecs-task-memory-e2e"
SIZED = "sized"
DEFAULTED = "defaulted"
SIZED_MIB = 256

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "ecs", "data": {
            "label": SIZED, "image": "nginx:alpine", "count": "1", "port": "80",
            "memory": str(SIZED_MIB),
        }},
        # No `memory` key at all -- the fallback path, not "memory: ''".
        {"id": "n2", "type": "ecs", "data": {
            "label": DEFAULTED, "image": "nginx:alpine", "count": "1", "port": "80",
        }},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}
MIB = 1024 * 1024


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _running_tasks(root: Path) -> dict[str, str]:
    """`{service label: real container name}` for every RUNNING task, read from
    the persisted ecsctl state -- a task id is minted at launch, so the container
    name cannot be predicted (test_ecs_drift_e2e.py's technique)."""
    state = json.loads((root / ENV / "gateway" / "ecsctl.json").read_text())
    return {
        task["container_name"]: container_name(ENV, task["task_id"], task["container_name"])
        for key, task in state.items()
        if key.startswith("task:") and task["last_status"] == "RUNNING"
    }


def _memory_limit(name: str) -> int:
    """The container's REAL cap, in bytes, straight from docker."""
    result = _docker("inspect", "-f", "{{.HostConfig.Memory}}", name)
    assert result.returncode == 0, f"docker inspect {name} failed: {result.stderr}"
    return int(result.stdout.strip())


def _wait_for_tasks(root: Path, count: int, timeout: float = 120.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    running: dict[str, str] = {}
    while time.monotonic() < deadline:
        running = _running_tasks(root)
        if len(running) >= count:
            return running
        time.sleep(2)
    raise AssertionError(f"only {len(running)} task(s) reached RUNNING within {timeout}s: {running}")


@pytest.fixture
def cleanup():
    yield
    ps = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name={ENV}")
    for container_id in (line for line in ps.stdout.splitlines() if line):
        _docker("rm", "-f", "-v", container_id)


def test_an_ecs_nodes_memory_becomes_the_real_containers_cap(tmp_path, monkeypatch, cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    with TestClient(create_app(store=store)) as client:
        started = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        print(f"\n[ecs-memory] apply-full took {time.monotonic() - started:.1f}s")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["tf"]["status"] == "ok", body

        running = _wait_for_tasks(store.root, 2)
        assert set(running) == {SIZED, DEFAULTED}, running

        sized = _memory_limit(running[SIZED])
        defaulted = _memory_limit(running[DEFAULTED])
        print(f"[ecs-memory] sized={sized / MIB:.0f}MiB defaulted={defaulted / MIB:.0f}MiB")

        # THE assertion this file exists for: the number typed on the canvas
        # survived hcl.py, tofu's provider, RegisterTaskDefinition, the store
        # and `run_task`, and is enforced by the kernel.
        assert sized == SIZED_MIB * MIB, (
            f"the canvas asked for {SIZED_MIB} MiB and the container got {sized / MIB:.0f} MiB — "
            "the value was dropped somewhere between hcl.py and `docker run`"
        )
        # And an unset field still lands on odin's documented default rather
        # than on docker's "no limit at all", which is what 0 means here.
        assert defaulted == int(_DEFAULT_MEMORY_MIB) * MIB, (
            f"a service with no memory field got {defaulted} bytes; 0 means UNLIMITED, "
            f"not {_DEFAULT_MEMORY_MIB:.0f} MiB"
        )

        torn = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert torn.status_code == 200, torn.text
        assert torn.json()["tf"] == {"status": "ok", "exit_code": 0}, torn.json()

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name={ENV}")
    assert not leftover.stdout.strip(), f"containers survived teardown: {leftover.stdout!r}"
