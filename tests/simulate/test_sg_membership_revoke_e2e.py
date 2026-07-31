"""Field test 3 HIGH-1, proven on the wire: REVOKING a security-group
membership really closes the path.

The reported failure, verbatim: the engineer moved `web1` OUT of `web-sg` and
into `admin-sg` -- the revoke direction -- and Apply returned `status:
applied`, tf ok, exit 0, `unsupported: []`, ZERO warnings. Meanwhile web1's
nebula certificate still carried `web-sg`, the VM was not recreated, and on
the wire web1 still reached the database (`pg_reply=b'N'`) even though the
canvas said it was no longer in the group `db-sg` admits.

This test is that exact sequence, and it is deliberately the HARDEST shape of
it: `web-sg` and `admin-sg` carry IDENTICAL rules, so moving web1 between them
changes NOTHING in its rendered nebula config. Only its certificate changes.
A fix that merely re-pushed config would pass every other test in this
directory and fail this one.

Three phases, each with a control so a reader can tell a firewall decision
from a dead tunnel:

  1. web1 in `web-sg`:    web1 -> db:5432 ALLOWED    (db-sg admits web-sg)
                          web1 -> admin1:22 ALLOWED  (the control path)
  2. web1 -> `admin-sg`:  web1 -> db:5432 REFUSED    <- THE FIX
                          web1 -> admin1:22 ALLOWED  (same instant, same VM:
                                                      the overlay is alive, so
                                                      the refusal is policy)
  3. web1 back in `web-sg`: web1 -> db:5432 ALLOWED again -- the grant
                          direction is no less reliable than the revoke.

Nothing is recreated across any of it: same instance ids, same overlay IPs,
same database container. What changes is the certificate, read back off disk
with `nebula-cert print` at every phase.

The control probe in phase 2 is doing double duty: web1's daemon RESTARTS to
adopt its new identity (a re-issued cert only reaches the wire when every
tunnel re-handshakes under it), so a control that answers immediately after
Apply returns is also the proof that MED-2's convergence window is closed --
the restarted member pokes its peers rather than waiting for them to notice
(`fabric/nebula.py::rehandshake_script`). The elapsed time is printed.

Store root: under the repo tree, NOT `tmp_path` -- Colima only shares $HOME
into its VM, and the DB's mesh sidecar reads its cert/config from a bind mount
(tests/simulate/test_sg_gates_backing_e2e.py's own note).
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.instances import instance_membership_path, vm_name
from odin.fabric.nebula import LighthouseManager
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.containers import reap_volumes

pytestmark = pytest.mark.integration

# Short: `odin-ec2-{env}-{i-17hex}` must keep Lima's ssh.sock path under
# UNIX_PATH_MAX=104 (compute/instances.py::max_env_name_len).
ENV = "sg-revoke-e2e"
DB_PORT = 5432
SSH_PORT = 22

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 620, "height": 480},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 320, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        # web-sg and admin-sg are RULE-IDENTICAL on purpose: moving web1
        # between them cannot change a single byte of its nebula config, so
        # only a re-issued CERTIFICATE can make the move real.
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 300}, "size": {"width": 280, "height": 60},
         "data": {"label": "web-sg", "ingressRules": f"tcp:{SSH_PORT}:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n4", "type": "sg", "position": {"x": 80, "y": 380}, "size": {"width": 280, "height": 60},
         "data": {"label": "admin-sg", "ingressRules": f"tcp:{SSH_PORT}:0.0.0.0/0", "vpc": "app-vpc"}},
        # THE rule under test: the database admits web-sg, and nothing else.
        {"id": "n5", "type": "sg", "position": {"x": 80, "y": 460}, "size": {"width": 280, "height": 60},
         "data": {"label": "db-sg", "ingressRules": f"tcp:{DB_PORT}:web-sg", "vpc": "app-vpc"}},
        {"id": "n6", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web1", "subnet": "public", "securityGroups": "web-sg"}},
        {"id": "n7", "type": "ec2", "position": {"x": 260, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "admin1", "subnet": "public", "securityGroups": "admin-sg"}},
        {"id": "n8", "type": "rds", "position": {"x": 420, "y": 300}, "size": {"width": 220, "height": 60},
         "data": {"label": "db", "engine": "postgres", "securityGroups": "db-sg"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _canvas_with_web1_in(group: str) -> dict:
    canvas = copy.deepcopy(CANVAS)
    web1 = next(n for n in canvas["nodes"] if n["data"]["label"] == "web1")
    web1["data"]["securityGroups"] = group
    return canvas


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
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=120)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=120)


@pytest.fixture
def containers():
    names: list[str] = []
    yield names
    for name in reversed(names):
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
    # The `-v` above does NOT take `odin-rds-{ENV}-db-data` with it: PGDATA is a
    # NAMED volume (`aws/rds.py`), and leaving named volumes alone is exactly
    # what `docker rm -f -v` promises. Nothing else here removes it -- the canvas
    # is never destroyed through a real DELETE -- so it leaked once per run.
    reap_volumes(ENV)


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


def _vm_shell(vm: str, *args: str, timeout: float = 25) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout)


def _tcp_reachable(vm: str, ip: str, port: int, timeout: float = 5.0) -> bool:
    probe = (
        f"import socket,sys; s=socket.socket(); s.settimeout({timeout}); "
        f"sys.exit(0 if s.connect_ex(('{ip}', {port})) == 0 else 1)"
    )
    return _vm_shell(vm, "python3", "-c", probe, timeout=timeout + 15).returncode == 0


def _wait_until_reachable(vm: str, ip: str, port: int, timeout: float = 150.0) -> float | None:
    """Seconds until the path answers, or None if it never did."""
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if _tcp_reachable(vm, ip, port):
            return time.monotonic() - started
        time.sleep(2.0)
    return None


def _cert_groups(root: Path, host_id: str) -> list[str]:
    """The groups on the certificate this member is REALLY holding, read with
    nebula's own tool -- structured JSON, never a regex over `print`'s text."""
    path = root / ENV / "nebula" / "hosts" / f"{host_id}.crt"
    out = subprocess.run(
        ["nebula-cert", "print", "-json", "-path", str(path)], capture_output=True, text=True, timeout=30,
    ).stdout
    printed = json.loads(out)
    records = printed if isinstance(printed, list) else [printed]
    return sorted(records[0]["details"]["groups"] or [])


def _nebula_unit(vm: str) -> str:
    return _vm_shell(
        vm, "sudo", "systemctl", "show", "nebula", "-p", "NRestarts", "-p", "ActiveEnterTimestamp",
    ).stdout.strip()


def _instances(root: Path) -> list[dict]:
    path = root / ENV / "gateway" / "ec2compute.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [v for k, v in state.items() if k.startswith("instance:")]


def _db_mesh_ip(client: TestClient, timeout: float = 180.0) -> str:
    """Poll /world until the database is healthy AND publishes its gated
    overlay address (the mesh join lands a tick or two after apply returns)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        world = client.get("/world", params={"env": ENV}).json()
        for resource in world.get("resources", []):
            if resource["id"] == "db" and resource["phase"] == "healthy":
                endpoint = (resource.get("facts") or {}).get("endpoint_mesh")
                if endpoint:
                    return endpoint.split(":")[0]
        time.sleep(2.0)
    raise AssertionError("the database never published a gated overlay address")


def test_revoking_a_vms_security_group_membership_closes_the_path_on_the_wire(
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
    with TestClient(create_app(store=store)) as client:
        lighthouse_cleanup.append((store.root, ENV))
        containers.append(f"odin-rds-{ENV}-db")
        containers.append(f"odin-rds-{ENV}-db-mesh")

        # --- phase 1: the canvas as drawn -----------------------------------
        started = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        instances = _instances(store.root)
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        print(f"[HIGH-1] first /apply-full (2 VMs + a Postgres on one mesh) took {time.monotonic() - started:.1f}s")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "applied", resp.json()
        assert len(instances) == 2, instances

        tags = json.loads((store.root / ENV / "gateway" / "tags.json").read_text())
        overlay = json.loads((store.root / ENV / "nebula" / "overlay.json").read_text())
        by_label = {
            tags[f"ec2:{i['instance_id']}"]["odin:node"]: i["instance_id"]
            for i in instances
        }
        web_id, admin_id = by_label["web1"], by_label["admin1"]
        web_vm, admin_vm = vm_name(ENV, web_id), vm_name(ENV, admin_id)
        admin_ip = overlay["subnets"]["hosts"]["assignments"][admin_id]
        db_ip = _db_mesh_ip(client)

        ec2net = json.loads((store.root / ENV / "gateway" / "ec2net.json").read_text())
        sg_ids = {v["group_name"]: v["group_id"] for k, v in ec2net.items() if k.startswith("sg:")}
        print(f"[HIGH-1] web1={web_vm} admin1={admin_vm} ({admin_ip}) db={db_ip}")
        print(f"[HIGH-1] web1's cert groups: {_cert_groups(store.root, web_id)}")
        assert _cert_groups(store.root, web_id) == sorted(["ec2", sg_ids["web-sg"]])

        control_1 = _wait_until_reachable(web_vm, admin_ip, SSH_PORT)
        db_1 = _wait_until_reachable(web_vm, db_ip, DB_PORT)
        print(f"[HIGH-1] BEFORE: web1->admin1:{SSH_PORT} ok in {control_1}s   "
              f"web1->db:{DB_PORT} ok in {db_1}s")
        assert control_1 is not None, "the control path must work before anything is revoked"
        assert db_1 is not None, "web1 is in web-sg, so db-sg must admit it to start with"
        unit_before, admin_unit_before = _nebula_unit(web_vm), _nebula_unit(admin_vm)

        # --- phase 2: THE REVOKE --------------------------------------------
        # web1 leaves web-sg for admin-sg. Rule-identical groups, so its
        # rendered config is byte-for-byte what it already has: the ONLY thing
        # that can close the database path is a re-issued certificate.
        edit_started = time.monotonic()
        revoked = client.post("/apply-full", params={"env": ENV}, json=_canvas_with_web1_in("admin-sg"))
        apply_seconds = time.monotonic() - edit_started
        print(f"[HIGH-1] revoke Apply took {apply_seconds:.1f}s -> {revoked.json().get('status')}")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "applied", revoked.json()

        # Nothing recreated: same instances, same overlay IPs, same database.
        after = _instances(store.root)
        assert {i["instance_id"] for i in after} == {web_id, admin_id}, after
        assert json.loads((store.root / ENV / "nebula" / "overlay.json").read_text()) == overlay
        web_record = next(i for i in after if i["instance_id"] == web_id)
        assert web_record["security_group_ids"] == [sg_ids["admin-sg"]], web_record

        # The certificate -- the thing that used to never change.
        groups_after = _cert_groups(store.root, web_id)
        print(f"[HIGH-1] web1's cert groups after the revoke: {groups_after}")
        assert groups_after == sorted(["ec2", sg_ids["admin-sg"]])
        assert sg_ids["web-sg"] not in groups_after, "the revoked group must be GONE from the cert"
        recorded = json.loads(instance_membership_path(store.root, ENV, web_id).read_text())
        assert recorded == sorted(["ec2", sg_ids["admin-sg"]])

        # THE PROOF, on the wire. Control first, and it is doing double duty:
        # web1's daemon restarted to adopt the new identity, so a control that
        # answers straight away is also MED-2's convergence window closing.
        control_2 = _wait_until_reachable(web_vm, admin_ip, SSH_PORT, timeout=90.0)
        db_2 = _tcp_reachable(web_vm, db_ip, DB_PORT)
        print(f"[HIGH-1] AFTER:  web1->admin1:{SSH_PORT} ok in {control_2}s (control)   "
              f"web1->db:{DB_PORT}={db_2}")
        assert control_2 is not None, (
            "the overlay itself must still work, or 'refused' below proves nothing"
        )
        assert not db_2, (
            "web1 is no longer in web-sg, so db-sg must refuse it -- this is the field-test bug"
        )

        unit_after, admin_unit_after = _nebula_unit(web_vm), _nebula_unit(admin_vm)
        print(f"[HIGH-1] web1 nebula before: {unit_before!r}")
        print(f"[HIGH-1] web1 nebula after:  {unit_after!r}")
        assert unit_after != unit_before, "a re-issued cert only reaches the wire on a restart"
        assert admin_unit_after == admin_unit_before, (
            "admin1's membership did not change, so nothing may touch its daemon"
        )

        # --- phase 3: the GRANT direction, on the same running VM -----------
        grant_started = time.monotonic()
        granted = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        print(f"[HIGH-1] grant Apply took {time.monotonic() - grant_started:.1f}s "
              f"-> {granted.json().get('status')}")
        assert granted.status_code == 200, granted.text
        assert granted.json()["status"] == "applied", granted.json()
        assert _cert_groups(store.root, web_id) == sorted(["ec2", sg_ids["web-sg"]])

        db_3 = _wait_until_reachable(web_vm, db_ip, DB_PORT, timeout=90.0)
        control_3 = _tcp_reachable(web_vm, admin_ip, SSH_PORT)
        print(f"[HIGH-1] REGRANTED: web1->db:{DB_PORT} ok in {db_3}s   "
              f"web1->admin1:{SSH_PORT}={control_3}")
        assert db_3 is not None, "putting web1 back in web-sg must re-open the database path"
        assert control_3, "and the control path is still up"
        assert {i["instance_id"] for i in _instances(store.root)} == {web_id, admin_id}

        teardown = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert teardown.status_code == 200, teardown.text

    remaining = set(subprocess.run(
        ["limactl", "list", "-q"], capture_output=True, text=True, timeout=60).stdout.split())
    for name in vm_cleanup:
        assert name not in remaining, f"VM survived teardown: {name}"
