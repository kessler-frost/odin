"""Field test 3 HIGH-B, proven: an interrupted apply no longer strands real
Lima VMs with no supported way to reclaim them.

The reported failure: `kill -9` on tofu mid-apply -- the everyday equivalents
being Ctrl-C, an OOM, or a laptop going to sleep -- left tofu's state empty
while three VMs kept Running. `odin destroy` then returned
`destroyed / tf ok` in 1.7 seconds with all three still up and `/world` still
listing seven resources. A second destroy, the README's empty-canvas-Apply
teardown, and a server restart ALL failed to reclaim them, because destroy
cleared tofu's state but left the instances in `gateway/ec2compute.json` --
so `reap_orphaned_vms`, whose "expected" set comes from that same store, saw
them as expected and spared them. Only `limactl delete` by hand worked. Real
VMs eating the user's RAM and disk with no supported way to find or remove
them.

The crux is that reality and the store disagreed and the STORE was trusted.
This test reproduces the disagreement for real -- a genuine `SIGKILL` on the
genuine `tofu` process, identified by its working directory so nothing else
on the machine can be touched -- and then requires that

  1. the VMs really are stranded and Running (else the test proves nothing),
  2. `odin destroy` -- a supported command, the first one a user would
     reach for -- reclaims them, and
  3. afterwards `/world`, the gateway store, and `limactl` all agree with
     each other and with reality.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.fabric.nebula import LighthouseManager
from odin.server import create_app
from odin.simulate.workspace import tf_dir
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "kill-apply-e2e"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 600, "height": 300},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 300, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        {"id": "n3", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web1", "subnet": "public"}},
        {"id": "n4", "type": "ec2", "position": {"x": 260, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web2", "subnet": "public"}},
    ],
    "edges": [],
}


@pytest.fixture
def vm_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=120)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=120)


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


def _tofu_pids_in(workspace: Path) -> list[int]:
    """Every live `tofu` whose WORKING DIRECTORY is this test's own workspace.

    Exactness is not a nicety here: other odin work can be running tofu on
    this same machine, and a `pkill tofu` would take it out. `lsof -d cwd` is
    the only thing that positively identifies the process this test started."""
    listing = subprocess.run(["ps", "-Ao", "pid=,comm="], capture_output=True, text=True, timeout=30).stdout
    pids = [
        int(line.split()[0]) for line in listing.splitlines()
        if line.strip() and Path(line.strip().split(None, 1)[1]).name == "tofu"
    ]
    target = str(workspace.resolve())
    mine = []
    for pid in pids:
        cwd = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)], capture_output=True, text=True, timeout=30,
        ).stdout
        if any(line[1:] == target for line in cwd.splitlines() if line.startswith("n")):
            mine.append(pid)
    return mine


def _vm_names() -> set[str]:
    return set(subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=60).stdout.split())


def _instances(root: Path) -> list[dict]:
    path = root / ENV / "gateway" / "ec2compute.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [v for k, v in state.items() if k.startswith("instance:")]


def _tf_instance_ids(root: Path) -> list[str]:
    state = tf_dir(root, ENV) / "terraform.tfstate"
    text = state.read_text().strip() if state.exists() else ""
    parsed = json.loads(text) if text else {}
    return [
        instance.get("attributes", {}).get("id")
        for resource in parsed.get("resources", []) if resource.get("type") == "aws_instance"
        for instance in resource.get("instances", [])
    ]


def test_killing_tofu_mid_apply_leaves_vms_that_destroy_really_reclaims(
    tmp_path, vm_cleanup, lighthouse_cleanup, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"
    assert shutil.which("lsof"), "lsof is how this test identifies its OWN tofu process"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    workspace = tf_dir(store.root, ENV)
    before = _vm_names()

    with TestClient(create_app(store=store)) as client:
        lighthouse_cleanup.append((store.root, ENV))
        result: dict = {}

        def apply_in_background() -> None:
            response = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
            result["status_code"] = response.status_code
            result["body"] = response.json()

        applier = threading.Thread(target=apply_in_background, daemon=True)
        applier.start()

        # Wait until tofu is really mid-apply AND has really created VMs --
        # killing before either would prove nothing about stranded machines.
        deadline = time.monotonic() + 240.0
        killed: list[int] = []
        while time.monotonic() < deadline:
            fresh = _vm_names() - before
            pids = _tofu_pids_in(workspace)
            if fresh and pids:
                vm_cleanup.extend(sorted(fresh))
                for pid in pids:
                    os.kill(pid, signal.SIGKILL)  # THE interruption: Ctrl-C / OOM / a closed laptop
                killed = pids
                break
            time.sleep(2.0)
        print(f"[HIGH-B] SIGKILLed tofu pid(s) {killed} mid-apply; VMs created so far: {sorted(_vm_names() - before)}")
        assert killed, "the test never caught tofu mid-apply with a VM up -- it proves nothing"

        applier.join(timeout=300)
        assert not applier.is_alive(), "the interrupted apply never returned"
        print(f"[HIGH-B] the interrupted apply returned {result['status_code']} {result['body'].get('status')!r}")

        # The disagreement the field engineer was left holding: real VMs are
        # Running, the gateway store claims them, and tofu's state does not.
        stranded = sorted(_vm_names() - before)
        records = _instances(store.root)
        print(f"[HIGH-B] stranded VMs: {stranded}")
        print(f"[HIGH-B] gateway store claims {len(records)} instance(s); tofu state holds {_tf_instance_ids(store.root)}")
        assert stranded, "no VM survived the kill -- nothing to reclaim, so nothing is proven"
        assert records, "the gateway store must still claim them (that IS the bug's crux)"

        # ...and now the supported command. This used to answer
        # `destroyed / tf ok` in 1.7s with every VM still Running.
        started = time.monotonic()
        destroyed = client.post("/destroy", params={"env": ENV})
        print(f"[HIGH-B] /destroy took {time.monotonic() - started:.1f}s -> "
              f"{destroyed.status_code} {destroyed.json().get('status')!r} "
              f"reclaimed={destroyed.json().get('reclaimed_vms')}")
        assert destroyed.status_code == 200, destroyed.text
        assert destroyed.json()["status"] == "destroyed", destroyed.json()

        # Reality, the store and /world now agree -- the three-way check the
        # field test found broken in every direction.
        left = sorted(_vm_names() - before)
        world = client.get("/world", params={"env": ENV}).json()
        print(f"[HIGH-B] after destroy: VMs {left}; store instances {len(_instances(store.root))}; "
              f"/world resources {[r['id'] for r in world.get('resources', [])]}")
        assert left == [], f"real Lima VMs survived a successful destroy: {left}"
        assert _instances(store.root) == [], "the store must stop claiming what it just reclaimed"
        assert world.get("resources", []) == [], "/world must not list resources that no longer exist"
        assert set(destroyed.json().get("reclaimed_vms") or []) == set(stranded)
