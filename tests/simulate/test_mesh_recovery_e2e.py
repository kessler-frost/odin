"""The field-test 2 mesh proofs, end to end: real Lima VMs, real containers,
real nebula, real Apply.

Two tests, three proofs.

`test_a_killed_database_gets_its_mesh_endpoint_back_after_one_apply`
  (a) THE broken case. `docker kill` the Postgres, re-Apply (the documented
      recovery), and the SG-gated overlay endpoint WORKS AGAIN from inside a
      VM. Before the fix the sidecar stayed in the dead container's network
      namespace forever: two further Applies and ~3 minutes of ticks did
      nothing, in two independent envs, while odin reported `healthy` and kept
      publishing the address.
  (b) ...and the v0.7.0 SG enforcement still holds across that change, since
      it rides the same path: the `web-sg` VM reaches the database over the
      overlay, the un-grouped VM does NOT, and the refused VM proves its
      tunnel is alive by reaching the allowed VM's :22 over the same overlay
      (the dead-tunnel control).
  Plus: an ec2 node now publishes `PRIVATE_IP` / `MESH_IP` (it published
  nothing at all before), and the mesh addresses used here come from those
  facts rather than from hand-reading `overlay.json`.

`test_an_env_whose_lighthouse_cannot_start_never_advertises_a_mesh_endpoint`
  (c) An env whose lighthouse cannot bind its port (exactly B8's collision,
      forced with `ODIN_LIGHTHOUSE_PORT` pointed at a port this test holds)
      must not report mesh-dependent resources `healthy` and must not publish
      a `*_MESH` fact -- while the host-reachable facts carry on working.

Store root: under the repo tree, NOT `tmp_path` -- Colima shares only $HOME
into its VM and the sidecar reads its cert/config from a bind mount (see
tests/aws/test_backing_mesh_e2e.py's note).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.instances import vm_name
from odin.fabric.nebula import LighthouseManager
from odin.reconcile import mesh_health
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "mesh-recovery-e2e"
DARK_ENV = "mesh-dark-e2e"
DB_PORT = 5432
SSH_PORT = 22

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 620, "height": 480},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 320, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 300}, "size": {"width": 280, "height": 60},
         "data": {"label": "web-sg", "ingressRules": "tcp:22:0.0.0.0/0", "vpc": "app-vpc"}},
        # 5432 from the web tier ONLY -- an SG-to-SG rule, which compiles to a
        # nebula `group:` rule matched against the peer's certificate.
        {"id": "n4", "type": "sg", "position": {"x": 80, "y": 380}, "size": {"width": 280, "height": 60},
         "data": {"label": "db-sg", "ingressRules": f"tcp:{DB_PORT}:web-sg", "vpc": "app-vpc"}},
        {"id": "n5", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web", "subnet": "public", "securityGroups": "web-sg"}},
        {"id": "n6", "type": "ec2", "position": {"x": 260, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "worker", "subnet": "public"}},
        {"id": "n7", "type": "rds", "position": {"x": 420, "y": 300}, "size": {"width": 220, "height": 60},
         "data": {"label": "db", "engine": "postgres", "securityGroups": "db-sg"}},
    ],
    "edges": [],
}
# No EC2 at all: (c) is about what odin ADVERTISES, which needs no VM.
DARK_CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 620, "height": 300},
         "data": {"label": "dark-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 320, "height": 100},
         "data": {"label": "dark-public", "cidr": "10.0.1.0/24", "vpc": "dark-vpc"}},
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 240}, "size": {"width": 280, "height": 60},
         "data": {"label": "dark-sg", "ingressRules": f"tcp:{DB_PORT}:0.0.0.0/0", "vpc": "dark-vpc"}},
        {"id": "n4", "type": "rds", "position": {"x": 420, "y": 200}, "size": {"width": 220, "height": 60},
         "data": {"label": "darkdb", "engine": "postgres", "securityGroups": "dark-sg"}},
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
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=120)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=120)


@pytest.fixture
def containers():
    names: list[str] = []
    yield names
    for name in reversed(names):
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


@pytest.fixture(autouse=True)
def _clean_mesh_cache():
    """The health sweep's cache is process-wide by design (the projection
    builds its callers fresh every tick); a test that kills and heals things
    inside seconds must not read a previous test's answer."""
    mesh_health.reset_cache()
    yield
    mesh_health.reset_cache()


@pytest.fixture
def deterministic_translate(monkeypatch):
    """The agent pass is not what these tests are about -- the deterministic
    skeleton is (the same seam every other simulate/ e2e uses)."""
    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)


def _vm_shell(vm: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout)


def _tcp_reachable(vm: str, ip: str, port: int, timeout: float = 6.0) -> bool:
    probe = (
        f"import socket,sys; s=socket.socket(); s.settimeout({timeout}); "
        f"sys.exit(0 if s.connect_ex(('{ip}', {port})) == 0 else 1)"
    )
    return _vm_shell(vm, "python3", "-c", probe, timeout=timeout + 10).returncode == 0


def _wait_until_reachable(vm: str, ip: str, port: int, timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_reachable(vm, ip, port):
            return True
        time.sleep(3.0)
    return False


def _resource(client: TestClient, env: str, node: str) -> dict:
    world = client.get("/world", params={"env": env}).json()
    return next((r for r in world.get("resources", []) if r["id"] == node), {})


def _await_resource(client: TestClient, env: str, node: str, predicate, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _resource(client, env, node)
        if last and predicate(last):
            return last
        time.sleep(2.0)
    return last


def _healthy_with_mesh(resource: dict) -> bool:
    return resource["phase"] == "healthy" and "endpoint_mesh" in (resource.get("facts") or {})


def _netns_target(container: str) -> str:
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", f"{container}-mesh"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def _container_id(container: str) -> str:
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.Id}}", container], capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def _mesh_diagnostics(store_root: Path, env: str, vm: str, container: str) -> None:
    print("[mesh-e2e] lighthouse.log tail:", (store_root / env / "nebula" / "lighthouse.log").read_text()[-1200:])
    print("[mesh-e2e] sidecar log:", subprocess.run(
        ["docker", "logs", "--tail", "12", f"{container}-mesh"], capture_output=True, text=True).stdout[-1200:])
    print("[mesh-e2e] VM nebula:", _vm_shell(
        vm, "sudo", "journalctl", "-u", "nebula", "-n", "12", "--no-pager").stdout[-1200:])


def test_a_killed_database_gets_its_mesh_endpoint_back_after_one_apply(
    mesh_root, vm_cleanup, containers, lighthouse_cleanup, deterministic_translate,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"

    store = SpecStore(mesh_root)
    db_container = f"odin-rds-{ENV}-db"
    with TestClient(create_app(store=store)) as client:
        lighthouse_cleanup.append((store.root, ENV))
        containers += [db_container, f"{db_container}-mesh"]

        started = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        state_path = store.root / ENV / "gateway" / "ec2compute.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        instances = [v for k, v in state.items() if k.startswith("instance:")]
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        print(f"[mesh-e2e] first /apply-full (2 VMs + Postgres, one mesh) took {time.monotonic() - started:.1f}s")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "applied", resp.json()
        assert resp.json()["unsupported"] == [], resp.json()
        assert len(instances) == 2, instances

        tags = json.loads((store.root / ENV / "gateway" / "tags.json").read_text())
        vms = {tags[f"ec2:{i['instance_id']}"]["odin:node"]: vm_name(ENV, i["instance_id"]) for i in instances}
        web_vm, worker_vm = vms["web"], vms["worker"]

        # LOW-13: an ec2 node publishes its own addresses now, so this test
        # reads the VMs' overlay IPs off /world instead of hand-reading
        # `.odin/<env>/nebula/overlay.json` the way the field engineer had to.
        web_facts = _await_resource(client, ENV, "web", lambda r: "MESH_IP" in (r.get("facts") or {}))["facts"]
        print(f"[mesh-e2e] web facts from /world: {web_facts}")
        assert web_facts["MESH_IP"].startswith("10.42."), web_facts
        assert web_facts["PRIVATE_IP"], web_facts

        db = _await_resource(client, ENV, "db", _healthy_with_mesh)
        assert _healthy_with_mesh(db), f"the database never published a verified overlay address: {db}"
        db_ip = db["facts"]["endpoint_mesh"].split(":")[0]
        print(f"[mesh-e2e] db on the mesh at {db['facts']['endpoint_mesh']}")

        # (b) the v0.7.0 enforcement, re-proven on the changed path.
        allowed = _wait_until_reachable(web_vm, db_ip, DB_PORT)
        if not allowed:
            _mesh_diagnostics(store.root, ENV, web_vm, db_container)
        assert allowed, "the web VM is in web-sg, so db-sg must let it reach Postgres over the overlay"
        refused = _tcp_reachable(worker_vm, db_ip, DB_PORT)
        assert not refused, "a VM outside web-sg must NOT reach the database over the overlay"
        control = _wait_until_reachable(worker_vm, web_facts["MESH_IP"], SSH_PORT, timeout=90.0)
        print(f"[mesh-e2e] SG gating: web->db {allowed}, worker->db {refused}, worker->web:22 {control}")
        assert control, "the refused VM's own tunnel must be alive (else the refusal proves nothing)"

        # (a) THE broken case: kill the database the way a real one dies.
        old_id = _container_id(db_container)
        subprocess.run(["docker", "kill", db_container], capture_output=True, timeout=60)
        crashed = _await_resource(client, ENV, "db", lambda r: r["phase"] != "healthy", timeout=120.0)
        print(f"[mesh-e2e] after docker kill: phase={crashed['phase']} verdict={crashed.get('verdict')!r}")
        assert crashed["phase"] != "healthy", "a killed database must not read healthy"
        assert "endpoint_mesh" not in (crashed.get("facts") or {}), "a dead DB must not advertise a mesh endpoint"

        recover = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert resp.status_code == 200, resp.text
        healed = _await_resource(client, ENV, "db", _healthy_with_mesh, timeout=300.0)
        print(f"[mesh-e2e] recovery Apply + healthy-with-mesh took {time.monotonic() - recover:.1f}s")
        assert _healthy_with_mesh(healed), f"one Apply must restore the verified mesh endpoint: {healed}"
        assert healed["facts"]["endpoint_mesh"].split(":")[0] == db_ip, "the overlay IP is sticky"

        # The mechanism, directly: a NEW container, and the sidecar followed it.
        new_id = _container_id(db_container)
        assert new_id and new_id != old_id, "the recovery really replaced the container"
        assert _netns_target(db_container) == f"container:{new_id}", (
            f"the sidecar must be in the LIVE container's namespace (was {_netns_target(db_container)})"
        )

        back = _wait_until_reachable(web_vm, db_ip, DB_PORT)
        if not back:
            _mesh_diagnostics(store.root, ENV, web_vm, db_container)
        print(f"[mesh-e2e] web -> db:{DB_PORT} over the overlay AFTER the crash+recover: {back}")
        assert back, "the SG-gated mesh endpoint must work again after a crash and one Apply"
        assert not _tcp_reachable(worker_vm, db_ip, DB_PORT), "the SG must still refuse the un-grouped VM"

        teardown = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert teardown.status_code == 200, teardown.text

    remaining = set(subprocess.run(
        ["limactl", "list", "-q"], capture_output=True, text=True, timeout=60).stdout.split())
    for name in vm_cleanup:
        assert name not in remaining, f"VM survived teardown: {name}"


def test_an_env_whose_lighthouse_cannot_start_never_advertises_a_mesh_endpoint(
    mesh_root, containers, lighthouse_cleanup, deterministic_translate, monkeypatch,
):
    """(c) B8, forced: something else on the machine holds this env's
    lighthouse port, so `nebula` exits immediately. That used to be one log
    line while /world kept publishing the env's SG-gated addresses and every
    node stayed `healthy`."""
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"

    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as squatter:
        squatter.bind(("", 0))
        held = squatter.getsockname()[1]
        # An explicit pin is honoured verbatim (no reallocation), which is what
        # lets this test reproduce a collision deterministically.
        monkeypatch.setenv("ODIN_LIGHTHOUSE_PORT", str(held))
        print(f"[mesh-e2e] holding UDP {held} and pinning the {DARK_ENV!r} lighthouse to it")

        store = SpecStore(mesh_root)
        db_container = f"odin-rds-{DARK_ENV}-darkdb"
        with TestClient(create_app(store=store)) as client:
            lighthouse_cleanup.append((store.root, DARK_ENV))
            containers += [db_container, f"{db_container}-mesh"]

            resp = client.post("/apply-full", params={"env": DARK_ENV}, json=DARK_CANVAS)
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "applied", resp.json()

            # The database itself is genuinely up on its published host port --
            # so the ONLY honest report is "up, but its mesh address is not".
            db = _await_resource(
                client, DARK_ENV, "darkdb",
                lambda r: bool((r.get("facts") or {}).get("DATABASE_URL")) or r["phase"] == "crashed",
            )
            print(f"[mesh-e2e] dark env db: phase={db['phase']} verdict={db.get('verdict')!r}")
            print(f"[mesh-e2e] dark env facts: {sorted((db.get('facts') or {}))}")

            assert db["phase"] != "healthy", "a resource whose mesh path is dead must not read healthy"
            facts = db.get("facts") or {}
            assert "DATABASE_URL_MESH" not in facts and "endpoint_mesh" not in facts, facts
            assert "lighthouse is not running" in (db.get("verdict") or ""), db

            state = json.loads((store.root / DARK_ENV / "gateway" / "rdsctl.json").read_text())
            assert state["db:darkdb"]["overlay_ip"], "the DB did join the overlay -- it is the LIGHTHOUSE that is down"

            mesh = client.get("/mesh", params={"env": DARK_ENV}).json()
            assert mesh["lighthouse_running"] is False
            assert mesh["lighthouse_port"] == held, mesh
            assert (store.root / DARK_ENV / "nebula" / "lighthouse.log").exists(), "the real failure is on disk"

            teardown = client.post("/apply-full", params={"env": DARK_ENV}, json=EMPTY_CANVAS)
            assert teardown.status_code == 200, teardown.text
