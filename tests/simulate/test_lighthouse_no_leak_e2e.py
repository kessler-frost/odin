"""Field test 3 HIGH-A, proven: an apply/destroy cycle leaks no lighthouse
and holds no port.

The reported failure: the minimal canvas -- a VPC plus a single S3 bucket, NO
EC2 at all -- leaked one live `nebula` lighthouse process and one UDP port on
every cycle. `odin destroy` reported "destroyed" and deleted
`.odin/<env>/nebula/`, taking with it the pidfile that was the only way to
name the process still running against it. Three orphans were measured
holding `*:4343`, `*:4344`, `*:4345`, one of them 8m20s old. About a hundred
cycles exhausts the 4342-4441 pool -- re-creating by accumulation exactly the
failure class per-env ports were introduced to eliminate.

It did NOT leak on the env that had EC2 VMs, and that is the tell: the only
stop was `ec2compute._finish_terminate`'s "last VM leaves", which a
VPC-without-VMs env never reaches.

This test runs the minimal canvas through three REAL apply/destroy cycles and
after each one asserts, against the live process table:

  1. no `nebula` process anywhere is running against a config under this
     store's root -- the strict form, so a lighthouse that is merely
     unreferenced still counts as a leak;
  2. the UDP port that env's lighthouse actually held is bindable again;
  3. `orphaned_lighthouses` (the startup backstop) agrees there is nothing
     left to reap.

No VM boots here at all, which is the point: this is the cheap canvas the
field engineer used, and it leaked hardest.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.fabric.nebula import LIGHTHOUSE_CONFIG, LighthouseManager, orphaned_lighthouses
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "lighthouse-leak-e2e"
CYCLES = 3

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 400, "height": 200},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "s3", "position": {"x": 500, "y": 60}, "size": {"width": 200, "height": 60},
         "data": {"label": "uploads"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


@pytest.fixture
def mesh_root():
    root = Path(".odin-mesh-it") / secrets.token_hex(4)
    root.mkdir(parents=True)
    yield root.resolve()
    if not os.environ.get("ODIN_KEEP_IT_ARTIFACTS"):
        shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture
def lighthouse_cleanup():
    """A last-resort net so a FAILING assertion below cannot itself leak the
    very process it is complaining about."""
    roots: list[Path] = []
    yield roots
    for root in roots:
        for pid, _config in _nebula_processes_under(root):
            subprocess.run(["kill", str(pid)], capture_output=True, timeout=10)
        LighthouseManager().ensure_stopped(root, ENV)


@pytest.fixture
def containers():
    names: list[str] = []
    yield names
    for name in reversed(names):
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)


def _nebula_processes_under(root: Path) -> list[tuple[int, str]]:
    """Every live `nebula` whose `-config` is inside `root` -- whether or not
    that file still exists. STRICTER than `orphaned_lighthouses` on purpose: a
    destroyed env must leave no daemon at all, not merely no unreferenced one."""
    out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True, timeout=30).stdout
    marker = f"{root.resolve()}/"
    found = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        tokens = args.split()
        if not pid.isdigit() or "-config" not in tokens[:-1] or Path(tokens[0]).name != "nebula":
            continue
        config = tokens[tokens.index("-config") + 1]
        if config.startswith(marker):
            found.append((int(pid), config))
    return found


def _port_free(port: int) -> bool:
    families = (socket.AF_INET, socket.AF_INET6) if socket.has_ipv6 else (socket.AF_INET,)
    for family in families:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.bind(("", port))
        except OSError:
            return False
    return True


async def test_apply_destroy_cycles_leak_no_lighthouse_and_hold_no_port(
    mesh_root, lighthouse_cleanup, containers, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(mesh_root)
    lighthouse_cleanup.append(store.root)
    containers.append(f"odin-aws-rustfs-{ENV}")
    containers.append(f"odin-aws-rustfs-{ENV}-mesh")
    ports_used: list[int] = []

    with TestClient(create_app(store=store)) as client:
        for cycle in range(1, CYCLES + 1):
            applied = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
            assert applied.status_code == 200, applied.text
            assert applied.json()["status"] == "applied", applied.json()

            overlay = json.loads((store.root / ENV / "nebula" / "overlay.json").read_text())
            port = overlay["lighthouse_port"]
            ports_used.append(port)
            running = _nebula_processes_under(store.root)
            print(f"[HIGH-A] cycle {cycle}: applied; lighthouse on UDP {port}, {len(running)} nebula process(es)")
            assert running, "the canvas drew a VPC, so this env really does run a lighthouse to leak"

            destroyed = client.post("/destroy", params={"env": ENV})
            assert destroyed.status_code == 200, destroyed.text
            assert destroyed.json()["status"] == "destroyed", destroyed.json()

            survivors = _nebula_processes_under(store.root)
            print(f"[HIGH-A] cycle {cycle}: destroyed; surviving nebula process(es): {survivors}; "
                  f"UDP {port} free: {_port_free(port)}")
            assert survivors == [], f"cycle {cycle} leaked a lighthouse: {survivors}"
            assert _port_free(port), f"cycle {cycle} left UDP {port} held"
            assert await orphaned_lighthouses(store.root) == []
            assert not (store.root / ENV / "nebula" / LIGHTHOUSE_CONFIG).exists()

            teardown = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
            assert teardown.status_code == 200, teardown.text

    print(f"[HIGH-A] {CYCLES} cycles, ports used: {ports_used}; zero survivors, zero held ports")
    assert _nebula_processes_under(store.root) == []
