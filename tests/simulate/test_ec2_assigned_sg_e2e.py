"""W2.6 piece 1 -- an instance's ASSIGNED security group really gates its VM
traffic, proven with two REAL Lima VMs drawn on one canvas.

The gap this closes: v0.5.4 made an instance's `SecurityGroupIds` reflect in
DescribeInstances (zero-drift re-apply), but the VM's nebula firewall was
still compiled from the VPC's DEFAULT security group -- so two instances in
two different drawn SGs got identical firewalls, and the canvas' promise
("this box is in web-sg, that one is in closed-sg") was decorative on the
wire. `ec2compute.py::_instance_firewall` now compiles the UNION of an
instance's own groups (falling back to the VPC default only when it has
none, as real AWS does).

The canvas draws it all in ONE apply: an inline `ingress` block is part of
the `aws_security_group` resource, and each instance references its group via
`vpc_security_group_ids`, so tofu's own graph creates the fully-ruled SG
BEFORE RunInstances ever snapshots it (no two-phase apply needed, unlike
test_nebula_mesh_e2e.py, which had to authorize rules on the implicit default
SG out of band).

The proof is a THREE-way probe, all over the real overlay, all VM-to-VM (the
R4 host lighthouse has no tun device, so the host has no overlay presence of
its own):

  1. worker -> web:8080 is ALLOWED   -- `open-sg` authorizes tcp/8080, and
     the VPC default SG (no ingress rules at all) does NOT: under the old
     default-SG-only compilation this connection was refused.
  2. web -> worker:8080 is REFUSED   -- `closed-sg` has no 8080 rule, though
     a REAL listener is running there. Per-instance gating, not "nothing
     listening".
  3. web -> worker:22 is ALLOWED     -- same source, same destination, a port
     `closed-sg` DOES authorize (the VM's own sshd). This is what rules out
     "the tunnel simply doesn't work in that direction" as an explanation for
     #2, making #2 a real firewall refusal.

VM + lighthouse hygiene ABSOLUTE: `vm_cleanup` force-deletes the EXACT VM
names and `lighthouse_cleanup` stops the env's lighthouse by its own pidfile
pid in finalizers, so even a failing test leaves nothing behind.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.instances import vm_name
from odin.fabric.nebula import LighthouseManager
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ec2-assigned-sg-e2e"
ALLOWED_PORT = 8080
SSH_PORT = 22

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 600, "height": 460},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 300, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 300}, "size": {"width": 260, "height": 60},
         "data": {"label": "open-sg", "ingressRules": f"tcp:{ALLOWED_PORT}:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n4", "type": "sg", "position": {"x": 80, "y": 380}, "size": {"width": 260, "height": 60},
         "data": {"label": "closed-sg", "ingressRules": f"tcp:{SSH_PORT}:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n5", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web", "subnet": "public", "securityGroups": "open-sg"}},
        {"id": "n6", "type": "ec2", "position": {"x": 240, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "worker", "subnet": "public", "securityGroups": "closed-sg"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _ec2compute_state(root, env: str) -> dict:
    path = root / env / "gateway" / "ec2compute.json"
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def vm_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=60)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=60)


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


def _vm_shell(vm: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout)


def _start_http_listener(vm: str, port: int) -> None:
    _vm_shell(
        vm, "bash", "-c",
        f"nohup python3 -m http.server {port} --bind 0.0.0.0 > /tmp/http{port}.log 2>&1 & disown",
    )


def _tcp_reachable(vm: str, ip: str, port: int, timeout: float = 5.0) -> bool:
    """A plain `python3` socket connect FROM INSIDE `vm` (python3 is
    guaranteed present -- `_start_http_listener` runs it), mirroring
    test_nebula_mesh_e2e.py's "never probe from the host" rule."""
    probe = (
        f"import socket,sys; s=socket.socket(); s.settimeout({timeout}); "
        f"sys.exit(0 if s.connect_ex(('{ip}', {port})) == 0 else 1)"
    )
    return _vm_shell(vm, "python3", "-c", probe, timeout=timeout + 10).returncode == 0


def _wait_until_reachable(vm: str, ip: str, port: int, timeout: float = 120.0) -> bool:
    """Both VMs' nebula daemons still have to start and handshake with the
    lighthouse AFTER apply returned, so the ALLOWED direction polls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_reachable(vm, ip, port):
            return True
        time.sleep(3.0)
    return False


def test_an_instances_assigned_sg_gates_its_real_vm_traffic(tmp_path, vm_cleanup, lighthouse_cleanup, monkeypatch):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        lighthouse_cleanup.append((store.root, ENV))
        boot_start = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        instances = [v for k, v in _ec2compute_state(store.root, ENV).items() if k.startswith("instance:")]
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        print(f"[W2.6-p1] /apply-full (two real VM boots + two nebula joins) took {time.monotonic() - boot_start:.1f}s")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["unsupported"] == [], body
        assert len(instances) == 2, instances

        # Map each instance to the node it came from (its `odin:node` tag) so
        # the probes below are unambiguous about which VM is which.
        tags = json.loads((store.root / ENV / "gateway" / "tags.json").read_text())
        by_label = {}
        for instance in instances:
            instance_id = instance["instance_id"]
            label = tags[f"ec2:{instance_id}"]["odin:node"]
            overlay = json.loads((store.root / ENV / "nebula" / "overlay.json").read_text())
            by_label[label] = (vm_name(ENV, instance_id), overlay["subnets"]["hosts"]["assignments"][instance_id])
            assert instance["state_name"] == "running", instance
            assert instance["security_group_ids"], f"{label} carries no assigned SG"
        web_vm, web_ip = by_label["web"]
        worker_vm, worker_ip = by_label["worker"]
        print(f"[W2.6-p1] web={web_vm} ({web_ip}, open-sg)  worker={worker_vm} ({worker_ip}, closed-sg)")

        _start_http_listener(web_vm, ALLOWED_PORT)
        _start_http_listener(worker_vm, ALLOWED_PORT)
        time.sleep(1.0)

        # 1: worker -> web:8080 -- allowed by web's OWN sg (`open-sg`). The
        # VPC default SG has no ingress rules at all, so under the previous
        # default-SG-only compilation this was refused.
        allowed = _wait_until_reachable(worker_vm, web_ip, ALLOWED_PORT)
        print(f"[W2.6-p1] worker -> web:{ALLOWED_PORT} (open-sg allows): {allowed}")
        assert allowed, "the ASSIGNED open-sg must authorize tcp/8080 into web over the overlay"

        # 2 + 3: same source, same destination -- 8080 refused (not in
        # closed-sg, though a real listener answers there), 22 allowed (in
        # closed-sg). #3 is what makes #2 a firewall refusal rather than a
        # dead tunnel.
        blocked = _tcp_reachable(web_vm, worker_ip, ALLOWED_PORT)
        ssh_open = _wait_until_reachable(web_vm, worker_ip, SSH_PORT, timeout=60.0)
        print(f"[W2.6-p1] web -> worker:{ALLOWED_PORT} (closed-sg omits): {blocked}; web -> worker:{SSH_PORT} (closed-sg allows): {ssh_open}")
        assert ssh_open, "closed-sg authorizes tcp/22, so the overlay path web->worker must work at all"
        assert not blocked, f"tcp/{ALLOWED_PORT} into worker must be REFUSED -- closed-sg has no such rule"

        resp2 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["tf"] == {"status": "ok", "exit_code": 0}, resp2.json()

    listing = subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=30)
    remaining = set(listing.stdout.split())
    for name in vm_cleanup:
        assert name not in remaining, f"VM survived teardown: {name}"
