"""Field test 4, proven on the wire: revoking a security-group membership now
closes a connection that is ALREADY OPEN, not only the next one.

v0.7.2 made the revoke real -- odin re-signs the member's nebula certificate
without the group and restarts its daemon, so NEW connections are refused
before Apply returns (tests/simulate/test_sg_membership_revoke_e2e.py proves
that, with rule-identical groups so only the certificate could have done it).
Field test 4 then asked the harder question: it held a real TCP flow open to
the database across such a revoke and pushed a genuine Postgres startup packet
down it afterwards. The database ANSWERED
(`R\\x00\\x00\\x00\\x17\\x00\\x00\\x00\\nSCRAM-SHA-256`). The session survived
the revoke.

That is nebula's documented design: its firewall keeps a conntrack entry per
flow and re-validates it only when its OWN ruleset version moves -- never when
a peer's certificate changes. So the admitting member (the database) had to be
made to move, and `fabric/nebula.py::FIREWALL_REVISION_KEY` is what moves it: a
key nebula ignores, rendered inside the `firewall` block, so a reload with only
that key changed installs a NEW ruleset with IDENTICAL rules (measured:
`New firewall has been installed ... rulesVersion=1` with firewallHashes equal
to oldFirewallHashes) and every held conntrack entry is re-checked against the
peer's CURRENT certificate.

THE EXPERIMENT, and its controls:

  hold      web1 opens TCP flows to db:5432 (permitted: db-sg admits web-sg)
            and keeps refreshing the newest one while it can, so the flow under
            test is provably one the database accepted while web1 still held
            the group -- and is only seconds old when probed, well inside
            Postgres' own 60s `authentication_timeout`, which would otherwise
            confound the result.
  revoke    web1 moves web-sg -> admin-sg. The two groups are RULE-IDENTICAL,
            so web1's own rendered config cannot change; only its certificate,
            and the database's membership revision, do.
  probe     a genuine Postgres StartupMessage down the HELD flow.
            BEFORE this fix: answered.  AFTER: dropped.

  control A db:9999 from web1, at the same instant. db-sg admits `admin-sg` on
            9999, so after the move that port is PERMITTED -- and nothing
            listens on it, so the database's own kernel answers RST. Same peer,
            same tunnel, same moment: `refused` proves packets still reach the
            database, so 5432's silence is a firewall decision and not a dead
            overlay. (Before the move it is `filtered`, which is the same rule
            working in the other direction.)
  control B the held flow's own age and the connect attempts it survived, both
            printed, so a reader can see the flow was established while the
            path was open rather than after it closed.

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
from odin.compute.instances import vm_name
from odin.fabric.nebula import LighthouseManager
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

# Short: `odin-ec2-{env}-{i-17hex}` must keep Lima's ssh.sock path under
# UNIX_PATH_MAX=104 (compute/instances.py::max_env_name_len).
ENV = "sg-flow-e2e"
DB_PORT = 5432
# Nothing listens here, and db-sg admits `admin-sg` on it. That combination is
# the control: after the revoke web1 IS in admin-sg, so the SYN passes the
# firewall, reaches the database's namespace, and comes back RST.
DEAD_PORT = 9999

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 620, "height": 480},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 320, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        # Rule-identical on purpose: moving web1 between them cannot change a
        # byte of its own nebula config.
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 300}, "size": {"width": 280, "height": 60},
         "data": {"label": "web-sg", "ingressRules": "tcp:22:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n4", "type": "sg", "position": {"x": 80, "y": 380}, "size": {"width": 280, "height": 60},
         "data": {"label": "admin-sg", "ingressRules": "tcp:22:0.0.0.0/0", "vpc": "app-vpc"}},
        # THE rule under test, plus the control rule that keeps the tunnel
        # provably alive for a member that has been moved out of web-sg.
        {"id": "n5", "type": "sg", "position": {"x": 80, "y": 460}, "size": {"width": 280, "height": 60},
         "data": {"label": "db-sg", "vpc": "app-vpc",
                  "ingressRules": f"tcp:{DB_PORT}:web-sg\ntcp:{DEAD_PORT}:admin-sg"}},
        {"id": "n6", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "web1", "subnet": "public", "securityGroups": "web-sg"}},
        {"id": "n7", "type": "rds", "position": {"x": 420, "y": 300}, "size": {"width": 220, "height": 60},
         "data": {"label": "db", "engine": "postgres", "securityGroups": "db-sg"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}

HOLDER_PATH = "/tmp/odin-holder.py"
HOLDER_RESULT = "/tmp/odin-holder.json"
HOLDER_GO = "/tmp/odin-holder.go"
HOLDER_READY = "/tmp/odin-holder.ready"

# Runs INSIDE web1 for the whole revoke. Keeps the newest still-open flow to
# the database and, on the go signal, pushes a genuine Postgres StartupMessage
# down it -- the exact packet a client sends after connecting, which a live
# server always answers (with an authentication request, or an error).
HOLDER = f'''
import json, pathlib, socket, struct, sys, time

host, port = sys.argv[1], int(sys.argv[2])
go = pathlib.Path("{HOLDER_GO}")
out = pathlib.Path("{HOLDER_RESULT}")

params = b"user\\x00postgres\\x00database\\x00postgres\\x00\\x00"
body = struct.pack("!i", 196608) + params            # protocol 3.0
STARTUP = struct.pack("!i", len(body) + 4) + body

held, held_at, opened, refused = None, None, 0, 0
deadline = time.monotonic() + 900
while not go.exists() and time.monotonic() < deadline:
    try:
        fresh = socket.create_connection((host, port), timeout=4)
    except OSError:
        refused += 1                                  # the revoke landing
    else:
        if held is not None:
            held.close()                              # keep only the newest
        held, held_at, opened = fresh, time.monotonic(), opened + 1
        pathlib.Path("{HOLDER_READY}").write_text("1")
    time.sleep(2)

result = {{"opened": opened, "refused": refused}}
if held is None:
    result["verdict"] = "NEVER_CONNECTED"
else:
    result["held_age_s"] = round(time.monotonic() - held_at, 1)
    try:
        held.settimeout(20)
        held.sendall(STARTUP)
        reply = held.recv(64)
        result["reply"] = reply.hex()
        result["verdict"] = "ANSWERED" if reply else "CLOSED"
    except Exception as exc:
        result["verdict"] = type(exc).__name__
        result["detail"] = str(exc)
out.write_text(json.dumps(result))
'''


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


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


def _vm_shell(vm: str, *args: str, timeout: float = 25, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout, **kwargs,
    )


def _connect_verdict(vm: str, ip: str, port: int, timeout: float = 5.0) -> str:
    """`open` / `refused` / `filtered` -- the three answers a TCP SYN can get,
    and the distinction the whole proof rests on. `refused` means an RST came
    back, so packets reached the peer; `filtered` means silence, which is what a
    nebula firewall DROP looks like."""
    probe = (
        "import errno,socket,sys\n"
        f"s=socket.socket(); s.settimeout({timeout})\n"
        f"rc=s.connect_ex(('{ip}', {port}))\n"
        "print('open' if rc==0 else 'refused' if rc==errno.ECONNREFUSED else 'filtered')"
    )
    proc = _vm_shell(vm, "python3", "-c", probe, timeout=timeout + 15)
    return proc.stdout.strip() or "error"


def _wait_until(predicate, timeout: float, interval: float = 2.0) -> float | None:
    """Seconds until `predicate()` is truthy, or None if it never was."""
    started = time.monotonic()
    while time.monotonic() < started + timeout:
        if predicate():
            return time.monotonic() - started
        time.sleep(interval)
    return None


def _instances(root: Path) -> list[dict]:
    path = root / ENV / "gateway" / "ec2compute.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [v for k, v in state.items() if k.startswith("instance:")]


def _db_overlay_ip(root: Path, timeout: float = 240.0) -> str:
    """The database's gated overlay address, straight off the RDS record that
    `rdsctl._join_mesh` writes when the sidecar joins.

    Deliberately not `/world`'s `endpoint_mesh`, which is the same value one
    projection removed: publishing it also waits on the World tick and on
    `reconcile/mesh_health.py`'s sweep cadence, and those have their own tests
    (test_sg_membership_revoke_e2e.py polls /world precisely to cover them).
    What THIS test is about is what happens on the wire to a flow that is
    already open, so it takes the address from the record and gets on with
    it -- on a machine busy enough to stretch a tick, the projection's latency
    is not evidence about a firewall."""
    deadline = time.monotonic() + timeout
    path = root / ENV / "gateway" / "rdsctl.json"
    last: dict = {}
    while time.monotonic() < deadline:
        last = json.loads(path.read_text()) if path.exists() else {}
        for record in last.values():
            if record.get("status") == "available" and record.get("overlay_ip"):
                return record["overlay_ip"]
        time.sleep(2.0)
    raise AssertionError(f"the database never joined the mesh: {json.dumps(last)}")


def _sidecar_unit(container: str) -> str:
    """The sidecar's identity + start time. A SIGHUP must leave both alone --
    that is what makes "the database adopted the revoke" different from "the
    database was restarted under it", which would close every flow trivially
    and prove nothing about the firewall."""
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.Id}} {{.State.StartedAt}} {{.RestartCount}}", container],
        capture_output=True, text=True, timeout=30,
    )
    return proc.stdout.strip()


def test_revoking_membership_drops_a_connection_that_is_already_open(
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
        db_container = f"odin-rds-{ENV}-db"
        containers.append(db_container)
        containers.append(f"{db_container}-mesh")

        # --- the canvas as drawn --------------------------------------------
        started = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        instances = _instances(store.root)
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        print(f"[FT4] first /apply-full (1 VM + a Postgres on one mesh) took {time.monotonic() - started:.1f}s")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "applied", resp.json()
        assert len(instances) == 1, instances

        web_id = instances[0]["instance_id"]
        web_vm = vm_name(ENV, web_id)
        db_ip = _db_overlay_ip(store.root)
        ec2net = json.loads((store.root / ENV / "gateway" / "ec2net.json").read_text())
        sg_ids = {v["group_name"]: v["group_id"] for k, v in ec2net.items() if k.startswith("sg:")}
        print(f"[FT4] web1={web_vm} db={db_ip}")

        opened = _wait_until(lambda: _connect_verdict(web_vm, db_ip, DB_PORT) == "open", timeout=150.0)
        assert opened is not None, "web1 is in web-sg, so db-sg must admit it on 5432 to start with"
        before_dead = _connect_verdict(web_vm, db_ip, DEAD_PORT)
        print(f"[FT4] BEFORE: db:{DB_PORT}=open (in {opened:.0f}s)   db:{DEAD_PORT}={before_dead}")
        assert before_dead == "filtered", (
            f"the control port is an admin-sg rule and web1 is in web-sg, so it must be dropped "
            f"(got {before_dead!r})"
        )

        # --- hold a real flow open, then revoke ------------------------------
        _vm_shell(web_vm, "rm", "-f", HOLDER_RESULT, HOLDER_GO, HOLDER_READY)
        _vm_shell(web_vm, "tee", HOLDER_PATH, input=HOLDER)
        _vm_shell(
            web_vm, "bash", "-c",
            f"setsid nohup python3 {HOLDER_PATH} {db_ip} {DB_PORT} >/tmp/odin-holder.log 2>&1 < /dev/null &",
        )
        held = _wait_until(
            lambda: _vm_shell(web_vm, "test", "-f", HOLDER_READY).returncode == 0, timeout=60.0, interval=1.0,
        )
        assert held is not None, "the holder never established a flow to the database"
        print(f"[FT4] a real TCP flow to db:{DB_PORT} is open and being held")
        sidecar_before = _sidecar_unit(f"{db_container}-mesh")

        edit_started = time.monotonic()
        revoked = client.post("/apply-full", params={"env": ENV}, json=_canvas_with_web1_in("admin-sg"))
        print(f"[FT4] revoke Apply took {time.monotonic() - edit_started:.1f}s -> "
              f"{revoked.json().get('status')}")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "applied", revoked.json()

        record = next(i for i in _instances(store.root) if i["instance_id"] == web_id)
        assert record["security_group_ids"] == [sg_ids["admin-sg"]], record

        # --- THE PROOF, down the flow that was already open ------------------
        _vm_shell(web_vm, "touch", HOLDER_GO)
        finished = _wait_until(
            lambda: _vm_shell(web_vm, "test", "-f", HOLDER_RESULT).returncode == 0, timeout=90.0, interval=1.0,
        )
        assert finished is not None, "the holder never reported a verdict"
        result = json.loads(_vm_shell(web_vm, "cat", HOLDER_RESULT).stdout)

        # The control, at the same moment: same peer, same tunnel, a port the
        # canvas still permits. `refused` is an RST from the database's own
        # kernel, so packets are demonstrably still arriving.
        after_dead = _connect_verdict(web_vm, db_ip, DEAD_PORT)
        after_db = _connect_verdict(web_vm, db_ip, DB_PORT)
        print(f"[FT4] AFTER:  held flow -> {result}")
        print(f"[FT4] AFTER:  new connect db:{DB_PORT}={after_db}   control db:{DEAD_PORT}={after_dead}")

        assert after_dead == "refused", (
            f"the control must prove the overlay is alive: db:{DEAD_PORT} is an admin-sg rule and web1 is now "
            f"in admin-sg, so the database's kernel must answer RST (got {after_dead!r})"
        )
        assert after_db == "filtered", "v0.7.2's guarantee: a NEW connection is refused after the revoke"
        assert result["opened"] >= 1, result
        assert result["verdict"] != "NEVER_CONNECTED", (
            f"the holder never got a flow open while the path was permitted, so it proves nothing: {result}"
        )
        assert result["verdict"] != "ANSWERED", (
            f"the flow web1 already had open survived the revoke -- field test 4's finding: {result}"
        )
        # A DROP is silence. A reset or a clean close would mean something else
        # ended the session (Postgres itself, or a restarted daemon), which is
        # not the firewall decision this test exists to prove.
        assert result["verdict"] in ("TimeoutError", "timeout"), (
            f"a revoked flow must be DROPPED (silence), not closed or reset: {result}"
        )

        # The mechanism itself, in the database's own log: a reload whose rules
        # are IDENTICAL and whose ruleset version still advanced. That is what
        # makes nebula re-check the conntrack entries it is already holding.
        sidecar_log = subprocess.run(
            ["docker", "logs", "--tail", "40", f"{db_container}-mesh"],
            capture_output=True, text=True, timeout=30,
        )
        installed = [ln for ln in (sidecar_log.stdout + sidecar_log.stderr).splitlines()
                     if "New firewall has been installed" in ln]
        print(f"[FT4] db nebula: {installed[-1][:240] if installed else '(no reload logged)'}")
        assert installed, (
            "the database's nebula never installed a new ruleset, so nothing could have re-validated the "
            "flow it was already holding"
        )

        # ...and the database adopted it by RELOADING, not by being restarted:
        # a restart would have closed the flow for a reason that has nothing to
        # do with the firewall, and would have dropped every other tunnel too.
        sidecar_after = _sidecar_unit(f"{db_container}-mesh")
        print(f"[FT4] db sidecar before: {sidecar_before!r}")
        print(f"[FT4] db sidecar after:  {sidecar_after!r}")
        assert sidecar_after == sidecar_before, (
            "the database's mesh daemon must have taken the revoke via SIGHUP -- same container, same start "
            "time, no restart count"
        )

        teardown = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert teardown.status_code == 200, teardown.text

    remaining = set(subprocess.run(
        ["limactl", "list", "-q"], capture_output=True, text=True, timeout=60).stdout.split())
    for name in vm_cleanup:
        assert name not in remaining, f"VM survived teardown: {name}"
