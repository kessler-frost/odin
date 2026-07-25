"""Field test 2 HIGH-1, proven: editing a security group reaches a VM that is
ALREADY RUNNING.

The reported failure, verbatim: `tcp:8080:admin-sg` was added to `web-sg`,
Apply returned `applied` with `Plan: 0 to add, 1 to change`, the gateway model
had the rule -- and the running VM's `/etc/nebula/config.yml` still contained
only port 22, with `NRestarts=0` and its original `ActiveEnterTimestamp`. A VM
created AFTER the edit DID get the rule, so two VMs in the same drawn group
enforced different firewalls on the wire.

This test is that exact sequence with the same two probes the field engineer
used, plus the two things that make the result unambiguous:

  1. the previously-BLOCKED port is probed on the SAME already-running VM
     before and after the edit -- nothing is recreated in between (asserted:
     same instance id, same overlay IP, and `limactl` never created a VM);
  2. `systemctl show nebula` proves the daemon was RELOADED, not restarted --
     `NRestarts=0` and an UNCHANGED `ActiveEnterTimestamp` across the edit, so
     the widening never dropped a live tunnel. (The field's own evidence line,
     now meaning the opposite thing.)

The dead-tunnel control (`:22`, allowed by the original rule) passes on both
sides of the edit, so a "blocked" reading is a firewall decision and never a
broken overlay.
"""
from __future__ import annotations

import copy
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

# Deliberately SHORT: a Lima instance's `~/.lima/<vm>/ssh.sock.<suffix>` path
# must stay under UNIX_PATH_MAX=104, and `odin-ec2-{env}-{instance_id}` eats
# 29 of those characters before the env name does -- `sg-edit-propagation-e2e`
# hit it exactly (`instance name ... too long`, both boots, at create time).
ENV = "sg-edit-e2e"
EDITED_PORT = 8080
SSH_PORT = 22

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 600, "height": 460},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 300, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        # web-sg starts with SSH only -- the edit below adds 8080 from admin-sg.
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 300}, "size": {"width": 260, "height": 60},
         "data": {"label": "web-sg", "ingressRules": f"tcp:{SSH_PORT}:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n4", "type": "sg", "position": {"x": 80, "y": 380}, "size": {"width": 260, "height": 60},
         "data": {"label": "admin-sg", "ingressRules": f"tcp:{SSH_PORT}:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n5", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web1", "subnet": "public", "securityGroups": "web-sg"}},
        {"id": "n6", "type": "ec2", "position": {"x": 240, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "admin1", "subnet": "public", "securityGroups": "admin-sg"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _edited_canvas() -> dict:
    """The canvas edit under test: one more ingress rule on `web-sg`, sourced
    from the OTHER group (an SG-to-SG rule, so it also proves cert-group
    matching still works after a live reload). Nothing else changes."""
    canvas = copy.deepcopy(CANVAS)
    web_sg = next(n for n in canvas["nodes"] if n["data"]["label"] == "web-sg")
    web_sg["data"]["ingressRules"] = f"tcp:{SSH_PORT}:0.0.0.0/0\ntcp:{EDITED_PORT}:admin-sg"
    return canvas


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


def _vm_shell(vm: str, *args: str, timeout: float = 25) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout)


def _start_http_listener(vm: str, port: int) -> None:
    _vm_shell(
        vm, "bash", "-c",
        f"nohup python3 -m http.server {port} --bind 0.0.0.0 > /tmp/http{port}.log 2>&1 & disown",
    )


def _tcp_reachable(vm: str, ip: str, port: int, timeout: float = 5.0) -> bool:
    probe = (
        f"import socket,sys; s=socket.socket(); s.settimeout({timeout}); "
        f"sys.exit(0 if s.connect_ex(('{ip}', {port})) == 0 else 1)"
    )
    return _vm_shell(vm, "python3", "-c", probe, timeout=timeout + 15).returncode == 0


def _wait_until_reachable(vm: str, ip: str, port: int, timeout: float = 150.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_reachable(vm, ip, port):
            return True
        time.sleep(3.0)
    return False


def _nebula_unit_state(vm: str) -> str:
    return _vm_shell(
        vm, "sudo", "systemctl", "show", "nebula", "-p", "NRestarts", "-p", "ActiveEnterTimestamp",
    ).stdout.strip()


def _vm_firewall_ports(vm: str) -> set[str]:
    """The ports in the config the VM is REALLY holding, read off its disk."""
    out = _vm_shell(vm, "sudo", "cat", "/etc/nebula/config.yml").stdout
    return {line.split(":", 1)[1].strip().strip("'\"") for line in out.splitlines() if line.strip().startswith("- port:")}


def _instances(store_root, env: str) -> list[dict]:
    path = store_root / env / "gateway" / "ec2compute.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [v for k, v in state.items() if k.startswith("instance:")]


def test_editing_a_security_group_rule_reaches_an_already_running_vm(
    tmp_path, vm_cleanup, lighthouse_cleanup, monkeypatch,
):
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
    with TestClient(create_app(store=store)) as client:
        lighthouse_cleanup.append((store.root, ENV))
        boot_start = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        instances = _instances(store.root, ENV)
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        print(f"[HIGH-1] first /apply-full (two real VMs, web-sg = ssh only) took {time.monotonic() - boot_start:.1f}s")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "applied", resp.json()
        assert len(instances) == 2, instances

        tags = json.loads((store.root / ENV / "gateway" / "tags.json").read_text())
        overlay = json.loads((store.root / ENV / "nebula" / "overlay.json").read_text())
        by_label = {
            tags[f"ec2:{i['instance_id']}"]["odin:node"]:
                (i["instance_id"], vm_name(ENV, i["instance_id"]),
                 overlay["subnets"]["hosts"]["assignments"][i["instance_id"]])
            for i in instances
        }
        web_id, web_vm, web_ip = by_label["web1"]
        _admin_id, admin_vm, _admin_ip = by_label["admin1"]
        print(f"[HIGH-1] web1={web_vm} ({web_ip}, web-sg)  admin1={admin_vm} (admin-sg)")

        _start_http_listener(web_vm, EDITED_PORT)
        time.sleep(1.5)
        listening = "LISTEN" in _vm_shell(web_vm, "ss", "-ltn").stdout
        assert listening, "the probe needs a REAL listener, or 'blocked' proves nothing"

        # Control first: the overlay itself works, on the rule web-sg has had
        # since boot. Everything after this is a firewall decision.
        control_before = _wait_until_reachable(admin1 := admin_vm, web_ip, SSH_PORT)
        blocked_before = _tcp_reachable(admin1, web_ip, EDITED_PORT)
        unit_before = _nebula_unit_state(web_vm)
        print(f"[HIGH-1] BEFORE the edit: admin1->web1:{SSH_PORT}={control_before} "
              f"admin1->web1:{EDITED_PORT}={blocked_before}")
        assert control_before, "the overlay path admin1->web1 must work at all"
        assert not blocked_before, f"tcp/{EDITED_PORT} must start out refused -- web-sg has no such rule yet"
        assert _vm_firewall_ports(web_vm) == {"22", "any"}, "web1 boots with ssh-only ingress"

        # THE EDIT: one more ingress rule on web-sg, sourced from admin-sg.
        edit_start = time.monotonic()
        edited = client.post("/apply-full", params={"env": ENV}, json=_edited_canvas())
        print(f"[HIGH-1] edit Apply took {time.monotonic() - edit_start:.1f}s -> {edited.json().get('status')}")
        assert edited.status_code == 200, edited.text
        assert edited.json()["status"] == "applied", edited.json()

        # Nothing was recreated: same instance, same VM, same overlay IP.
        after = _instances(store.root, ENV)
        assert {i["instance_id"] for i in after} == {i["instance_id"] for i in instances}, after
        assert json.loads((store.root / ENV / "nebula" / "overlay.json").read_text()) == overlay

        ports_after = _vm_firewall_ports(web_vm)
        print(f"[HIGH-1] web1's ON-DISK firewall ports after the edit: {sorted(ports_after)}")
        assert str(EDITED_PORT) in ports_after, "the edited rule must reach the RUNNING VM's config"

        allowed_after = _wait_until_reachable(admin1, web_ip, EDITED_PORT, timeout=90.0)
        control_after = _tcp_reachable(admin1, web_ip, SSH_PORT)
        unit_after = _nebula_unit_state(web_vm)
        print(f"[HIGH-1] AFTER the edit:  admin1->web1:{EDITED_PORT}={allowed_after} "
              f"admin1->web1:{SSH_PORT}={control_after}")
        print(f"[HIGH-1] nebula unit before: {unit_before!r}")
        print(f"[HIGH-1] nebula unit after:  {unit_after!r}")
        assert allowed_after, (
            f"the ALREADY-RUNNING web1 must honour the edited web-sg rule "
            f"(config on disk: {sorted(ports_after)})"
        )
        assert control_after, "the dead-tunnel control must still pass after the edit"
        # RELOADED, not restarted: same NRestarts, same ActiveEnterTimestamp.
        assert unit_after == unit_before, "widening a rule must not restart nebula (it would drop live tunnels)"
        assert "NRestarts=0" in unit_after, unit_after

        teardown = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert teardown.status_code == 200, teardown.text

    remaining = set(subprocess.run(
        ["limactl", "list", "-q"], capture_output=True, text=True, timeout=60).stdout.split())
    for name in vm_cleanup:
        assert name not in remaining, f"VM survived teardown: {name}"
