"""W2.6 piece 3 -- THE flagship: a drawn security group gates a real backing
container, proven with a real Lima VM on one side and a real Postgres on the
other.

One canvas, one Apply. `db-sg` authorizes 5432 FROM `web-sg` (an SG-to-SG
ingress rule -- `iac/hcl.py::_ingress_source`), the `web` instance is in
`web-sg`, the `worker` instance is in no SG at all, and the `db` node names
`db-sg`. Everything downstream is real: tofu creates the VPC/subnets/SGs AND
the database through the gateway, two real Lima VMs boot and join the env's
Nebula mesh with their ASSIGNED SG's firewall + their sg ids as cert groups
(piece 1), and the Postgres container joins the SAME mesh behind `db-sg`'s
compiled firewall via a nebula sidecar in its network namespace (piece 2).

W2.7 moved the database itself onto Terraform, so `db-sg` now reaches it the
way it reaches an EC2 instance: as the `aws_db_instance`'s
`vpc_security_group_ids`, compiled into a firewall by the gateway's own RDS
model (`gateway/models/rdsctl.py::_db_firewall`) rather than by a reconciler
tick. That also makes the ordering a REAL terraform dependency -- the SG
cannot be created after the database that references it.

Then, over the overlay, from INSIDE the VMs:

  web    -> db:5432   ALLOWED  -- its cert carries `web-sg`'s group id, which
                                 is exactly what `db-sg`'s compiled rule names.
  worker -> db:5432   REFUSED  -- same database, same port, same moment; it
                                 simply isn't in `web-sg`.

"only the web tier may reach the DB" -- the promise the canvas has been making
since SGs were drawable, now true on the wire.

Store root: under the repo tree, NOT `tmp_path` -- Colima only shares $HOME
into its VM, and the DB's mesh sidecar reads its cert/config from a bind mount
(see tests/aws/test_backing_mesh_e2e.py's own note).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.agent import translate as translate_mod
from odin.compute.instances import vm_name
from odin.fabric.nebula import LighthouseManager
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "sg-gates-backing-e2e"
DB_PORT = 5432

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 620, "height": 480},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 320, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 300}, "size": {"width": 280, "height": 60},
         "data": {"label": "web-sg", "ingressRules": "tcp:22:0.0.0.0/0", "vpc": "app-vpc"}},
        # THE rule: 5432, from the web tier ONLY -- an SG-to-SG (AWS
        # UserIdGroupPairs) rule, which compiles to a nebula `group:` rule.
        {"id": "n4", "type": "sg", "position": {"x": 80, "y": 380}, "size": {"width": 280, "height": 60},
         "data": {"label": "db-sg", "ingressRules": f"tcp:{DB_PORT}:web-sg", "vpc": "app-vpc"}},
        {"id": "n5", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web", "subnet": "public", "securityGroups": "web-sg"}},
        # No securityGroups at all: inherits the VPC default SG, and its cert
        # carries no sg group -- the "without the edge" case.
        {"id": "n6", "type": "ec2", "position": {"x": 260, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "worker", "subnet": "public"}},
        {"id": "n7", "type": "rds", "position": {"x": 420, "y": 300}, "size": {"width": 220, "height": 60},
         "data": {"label": "db", "engine": "postgres", "securityGroups": "db-sg"}},
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
def vm_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=60)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=60)


@pytest.fixture
def containers():
    names: list[str] = []
    yield names
    for name in reversed(names):
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
        # ...and, for an rds container, its NAMED data volume: `rm -f -v`
        # deliberately leaves those standing (that is what makes odin's repair
        # non-destructive), so removing only the container leaks a Postgres
        # volume on every run that fails before its real teardown. A no-op --
        # exit 0 -- for every other kind, which has no such volume.
        subprocess.run(["docker", "volume", "rm", "-f", f"{name}-data"], capture_output=True, timeout=60)


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


def _vm_shell(vm: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout)


def _tcp_reachable(vm: str, ip: str, port: int, timeout: float = 6.0) -> bool:
    probe = (
        f"import socket,sys; s=socket.socket(); s.settimeout({timeout}); "
        f"sys.exit(0 if s.connect_ex(('{ip}', {port})) == 0 else 1)"
    )
    return _vm_shell(vm, "python3", "-c", probe, timeout=timeout + 10).returncode == 0


def _wait_until_reachable(vm: str, ip: str, port: int, timeout: float = 150.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_reachable(vm, ip, port):
            return True
        time.sleep(3.0)
    return False


def _db_facts(client: TestClient, timeout: float = 180.0) -> dict:
    """Poll /world until the database is healthy AND publishes its gated
    overlay address. The mesh join runs AFTER the instance reports `available`
    (`rdsctl._finish_create`, so mesh wiring can never hold up tofu's create
    waiter), and the World projection is a reconciler tick behind that -- so
    the mesh fact appears a tick or two after apply returns."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        world = client.get("/world", params={"env": ENV}).json()
        for resource in world.get("resources", []):
            if resource["id"] == "db":
                last = resource.get("facts") or {}
                if resource["phase"] == "healthy" and "endpoint_mesh" in last:
                    return last
        time.sleep(2.0)
    return last


def test_a_drawn_sg_lets_the_web_vm_reach_postgres_and_refuses_the_other(
    mesh_root, vm_cleanup, containers, lighthouse_cleanup, monkeypatch,
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

    store = SpecStore(mesh_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        lighthouse_cleanup.append((store.root, ENV))
        containers.append(f"odin-rds-{ENV}-db")
        containers.append(f"odin-rds-{ENV}-db-mesh")

        started = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        state_path = store.root / ENV / "gateway" / "ec2compute.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        instances = [v for k, v in state.items() if k.startswith("instance:")]
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        print(f"[W2.6-p3] /apply-full (2 VMs + a Postgres, all onto one mesh) took {time.monotonic() - started:.1f}s")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        # EVERY node on this canvas is a tofu resource now -- the database was
        # the last exception and W2.7 closed it.
        assert body["unsupported"] == [], body
        assert len(instances) == 2, instances

        tags = json.loads((store.root / ENV / "gateway" / "tags.json").read_text())
        overlay = json.loads((store.root / ENV / "nebula" / "overlay.json").read_text())
        vms = {}
        for instance in instances:
            label = tags[f"ec2:{instance['instance_id']}"]["odin:node"]
            assert instance["state_name"] == "running", instance
            vms[label] = vm_name(ENV, instance["instance_id"])
        web_vm, worker_vm = vms["web"], vms["worker"]

        facts = _db_facts(client)
        assert "endpoint_mesh" in facts, f"the database never published a gated overlay address: {facts}"
        db_ip = facts["endpoint_mesh"].split(":")[0]
        assert db_ip in overlay["subnets"]["hosts"]["assignments"].values() or db_ip.startswith("10.42.")
        print(f"[W2.6-p3] db on the mesh at {facts['endpoint_mesh']}; web={web_vm} worker={worker_vm}")

        # The compiled firewall really is `db-sg`'s: the instance record carries
        # the group id tofu assigned it, and the sidecar's config names web-sg's
        # group id -- which is what nebula matches against a peer's certificate
        # groups.
        ec2net = json.loads((store.root / ENV / "gateway" / "ec2net.json").read_text())
        web_sg_id = next(v["group_id"] for k, v in ec2net.items() if k.startswith("sg:") and v["group_name"] == "web-sg")
        db_sg_id = next(v["group_id"] for k, v in ec2net.items() if k.startswith("sg:") and v["group_name"] == "db-sg")
        rds_state = json.loads((store.root / ENV / "gateway" / "rdsctl.json").read_text())
        assert rds_state["db:db"]["vpc_security_group_ids"] == [db_sg_id], rds_state["db:db"]
        member_config = (store.root / ENV / "nebula" / "members" / f"odin-rds-{ENV}-db" / "config.yml").read_text()
        assert f"group: {web_sg_id}" in member_config, member_config
        assert str(DB_PORT) in member_config

        allowed = _wait_until_reachable(web_vm, db_ip, DB_PORT)
        print(f"[W2.6-p3] web -> db:{DB_PORT} over the overlay (db-sg allows web-sg): {allowed}")
        if not allowed:  # mesh diagnostics, so a failure names its own cause
            print("[W2.6-p3] host UDP 4242 holders:", subprocess.run(
                ["lsof", "-nP", "-iUDP:4242"], capture_output=True, text=True).stdout)
            print("[W2.6-p3] VM lima.yaml portForwards:", subprocess.run(
                ["grep", "-A4", "portForwards", str(Path.home() / ".lima" / web_vm / "lima.yaml")],
                capture_output=True, text=True).stdout)
            print("[W2.6-p3] lighthouse.log tail:", (store.root / ENV / "nebula" / "lighthouse.log").read_text()[-1500:])
            print("[W2.6-p3] db sidecar log:", subprocess.run(
                ["docker", "logs", "--tail", "12", f"odin-rds-{ENV}-db-mesh"], capture_output=True, text=True).stdout[-1500:])
            print("[W2.6-p3] web VM nebula:", _vm_shell(web_vm, "sudo", "journalctl", "-u", "nebula", "-n", "12", "--no-pager").stdout[-1500:])
        assert allowed, "the web VM is in web-sg, so db-sg must let it reach Postgres over the overlay"

        refused = _tcp_reachable(worker_vm, db_ip, DB_PORT)
        print(f"[W2.6-p3] worker -> db:{DB_PORT} over the overlay (not in web-sg): {refused}")
        assert not refused, "a VM outside web-sg must NOT reach the database over the overlay"

        teardown = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert teardown.status_code == 200, teardown.text

    listing = subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=30)
    remaining = set(listing.stdout.split())
    for name in vm_cleanup:
        assert name not in remaining, f"VM survived teardown: {name}"
