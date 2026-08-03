"""v0.8.18 -- a drawn security group's EGRESS stops outbound traffic from a
REAL EC2 VM. The container-level proof does not transfer, and that is the point.

v0.8.17 compiled `egressRules` into the nebula `outbound` list and measured a
real BLOCK -- on CONTAINERS (`test_sg_egress_gates_e2e.py`, mesh members via
`fabric/sidecar.py`). Its own release note said the honest thing:

    "No real Lima VM ever wore a restricted egress. My proof is container-level.
     The VM path (`ec2compute::_instance_firewall` -> `InstanceVm`) uses the
     identical `compiled_firewall` bytes, so I expect it to hold -- but that is
     an inference, not a measurement."

An inference about whether a firewall BLOCKS is exactly what this repo does not
let stand, and "same bytes" is not "same effect": the two members reach nebula
by different routes entirely -- a VM gets `/etc/nebula/config.yml` written by
`limactl shell ... sudo tee` and runs the daemon under systemd as root with its
own tun; a container gets a bind-mounted config and runs nebula as pid 1 inside
the backing's network namespace. The repo has already been bitten by exactly
this class of gap (a re-handshake poke that waited on `/sys/class/net/nebula1`
when the device is really `tun0`).

THE MEASUREMENT is a 2x2 over ONE canvas, TWO real Lima VMs, and one Nebula
overlay. Both security groups carry the IDENTICAL ingress (`_INGRESS`, both
ports open from anywhere), so the ONLY difference between the two VMs is the
`egressRules` field:

                        -> peer:6000        -> peer:5432
  locked  (egress 6000)  ALLOWED  (1)        REFUSED  (2)   <- the claim
  peer    (egress all)   ALLOWED  (4)        ALLOWED  (3)   <- the control

(2) is the assertion that carries the weight; it is the only line that fails
when the compiler change is reverted. (1) is the positive control on the SAME
VM to the SAME peer in the same moment -- so "the mesh is down" cannot explain
(2). (3) is the control (2) needs that a single VM cannot give: a VM with
unrestricted egress reaching :5432 on the same overlay in the same run rules
out "no VM can reach that port", which is the VM-specific confound a
container-level proof would have hidden. (3) also proves nebula's conntrack
lets `locked` ANSWER on a port its egress does not name -- egress restricts
what a member SENDS, exactly as in AWS.

WHAT A VM'S EGRESS DOES NOT GATE is measured here too, because a security
promise needs its boundary stated: nebula's firewall governs the overlay tun
and nothing else, and a Lima VM has a SECOND path off the box -- vzNAT. The
same `locked` VM that cannot reach :5432 over the overlay opens a TCP
connection to the host over vzNAT in the same breath. `docs/limits.md` carries
it.

VM hygiene absolute: `vm_cleanup` force-deletes the EXACT VM names,
`lighthouse_cleanup` stops the env's lighthouse by its own pidfile, and the
final `limactl list -q` asserts the end state rather than trusting a remove.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.agent import translate as translate_mod
from odin.compute.instances import vm_name
from odin.fabric.nebula import LighthouseManager
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "egvm-vm-egress-e2e"
ALLOWED_PORT = 6000
BLOCKED_PORT = 5432

# The SAME ingress on BOTH groups. Without this the two VMs would differ in two
# ways at once and a refusal could be read off either -- the whole design is
# that `egressRules` is the only field that is not identical.
# ICMP is in here for a reason beyond symmetry: it is what
# `fabric/nebula.py::rehandshake_script` pokes peers with after a restart, so
# probes (6)/(7) below measure that poke's own primitive against a restricted
# egress. Compiles to `proto: icmp, port: any` (AWS expresses ICMP type/code in
# the port fields; `_PORTLESS_PROTOCOLS` drops them).
_INGRESS = f"tcp:{ALLOWED_PORT}:0.0.0.0/0\ntcp:{BLOCKED_PORT}:0.0.0.0/0\nicmp:0:0.0.0.0/0"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "vpc", "position": {"x": 40, "y": 40}, "size": {"width": 600, "height": 460},
         "data": {"label": "app-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "n2", "type": "subnet", "position": {"x": 80, "y": 120}, "size": {"width": 300, "height": 100},
         "data": {"label": "public", "cidr": "10.0.1.0/24", "vpc": "app-vpc"}},
        # No `egressRules` at all: AWS's own default, and what every canvas
        # drawn before the field existed gets -- allow-all outbound.
        {"id": "n3", "type": "sg", "position": {"x": 80, "y": 300}, "size": {"width": 260, "height": 60},
         "data": {"label": "open-sg", "ingressRules": _INGRESS, "vpc": "app-vpc"}},
        # THE rule under test. The TF provider revokes the seeded allow-all
        # egress because this config names an `egress` block of its own.
        {"id": "n4", "type": "sg", "position": {"x": 80, "y": 380}, "size": {"width": 260, "height": 60},
         "data": {"label": "locked-sg", "ingressRules": _INGRESS,
                  "egressRules": f"tcp:{ALLOWED_PORT}:0.0.0.0/0", "vpc": "app-vpc"}},
        {"id": "n5", "type": "ec2", "position": {"x": 100, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "peer", "subnet": "public", "securityGroups": "open-sg"}},
        {"id": "n6", "type": "ec2", "position": {"x": 240, "y": 140}, "size": {"width": 140, "height": 40},
         "data": {"label": "locked", "subnet": "public", "securityGroups": "locked-sg"}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}

# A REAL request/response, run inside the VM: a bare `connect_ex` proves less.
# One try/except and no branches -- the caller matches on the returned line.
_PROBE = """
import socket, sys
s = socket.socket(); s.settimeout(6.0)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    s.sendall(b"GET / HTTP/1.0\\r\\n\\r\\n")
    sys.stdout.write(s.recv(64).decode("utf-8", "replace").split("\\r\\n")[0])
except OSError as exc:
    sys.stdout.write("REFUSED %s" % exc)
"""
_OK = "HTTP/1.0 200 OK"

# The vzNAT probe is CONNECT-ONLY, and deliberately so: its far end is a bare
# `listen()`ing host socket that nobody accepts on, so the kernel completes the
# handshake into the backlog and then nothing ever writes. A `recv` there would
# time out and be reported as REFUSED -- a false negative that would have
# "measured" the opposite of the truth. A completed 3-way handshake is the whole
# claim anyway: a SYN left the VM off-overlay and a SYN-ACK came back.
_CONNECT_PROBE = """
import socket, sys
s = socket.socket(); s.settimeout(6.0)
err = s.connect_ex((sys.argv[1], int(sys.argv[2])))
sys.stdout.write("CONNECTED" * (err == 0) + ("REFUSED errno=%d" % err) * (err != 0))
"""


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


@pytest.fixture
def host_listener():
    """A plain TCP listener on the macOS HOST, for the vzNAT half of the
    measurement (what a VM's egress does NOT gate). Ephemeral port: nothing can
    collide with it, which is stronger isolation than any number this test
    could pick."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.listen(8)
    yield sock.getsockname()[1]
    sock.close()


def _vm_shell(vm: str, *args: str, timeout: float = 25) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout)


def _start_listener(vm: str, port: int) -> None:
    _vm_shell(
        vm, "bash", "-c",
        f"nohup python3 -m http.server {port} --bind 0.0.0.0 > /tmp/http{port}.log 2>&1 & disown",
    )


def _talk(vm: str, ip: str, port: int) -> str:
    """What `vm` gets back from `ip:port`, verbatim -- `HTTP/1.0 200 OK` when a
    packet got out AND one came back, `REFUSED ...` otherwise."""
    proc = _vm_shell(vm, "python3", "-c", _PROBE, ip, str(port), timeout=40)
    return (proc.stdout + proc.stderr).strip()


def _talk_until(vm: str, ip: str, port: int, attempts: int = 25) -> str:
    """The ALLOWED directions poll: both VMs' nebula daemons still have to
    handshake through the lighthouse relay after apply returns. The BLOCKED
    direction NEVER polls -- waiting for a thing to keep not happening only
    makes a slow mesh look like a working firewall, which is the exact false
    green this test exists to avoid."""
    out = ""
    for _ in range(attempts):
        out = _talk(vm, ip, port)
        if _OK in out:
            return out
        time.sleep(3.0)
    return out


def test_a_drawn_egress_rule_stops_a_real_vms_outbound_traffic(
    tmp_path, vm_cleanup, lighthouse_cleanup, host_listener, monkeypatch,
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
    app = create_app(store=store)
    with TestClient(app) as client:
        lighthouse_cleanup.append((store.root, ENV))
        started = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        state_path = store.root / ENV / "gateway" / "ec2compute.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        instances = [v for k, v in state.items() if k.startswith("instance:")]
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        print(f"[egress-vm] /apply-full (two real VM boots + two nebula joins) took {time.monotonic() - started:.1f}s")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["unsupported"] == [], body
        assert len(instances) == 2, instances

        tags = json.loads((store.root / ENV / "gateway" / "tags.json").read_text())
        overlay = json.loads((store.root / ENV / "nebula" / "overlay.json").read_text())
        by_label = {}
        for instance in instances:
            instance_id = instance["instance_id"]
            label = tags[f"ec2:{instance_id}"]["odin:node"]
            by_label[label] = (vm_name(ENV, instance_id), overlay["subnets"]["hosts"]["assignments"][instance_id],
                               instance_id)
            assert instance["state_name"] == "running", instance
            assert instance["security_group_ids"], f"{label} carries no assigned SG"
        peer_vm, peer_ip, _ = by_label["peer"]
        locked_vm, locked_ip, locked_id = by_label["locked"]

        # --- the COMPILE, before any packet moves. This is what used to be a
        # hardcoded allow-all for both groups.
        ec2net = json.loads((store.root / ENV / "gateway" / "ec2net.json").read_text())
        groups = {v["group_name"]: v for v in ec2net.values() if "group_name" in v}
        open_out = [(r["port"], r["proto"]) for r in groups["open-sg"]["firewall"]["outbound"]]
        locked_out = [(r["port"], r["proto"]) for r in groups["locked-sg"]["firewall"]["outbound"]]
        print(f"[egress-vm] open-sg   outbound={open_out}")
        print(f"[egress-vm] locked-sg outbound={locked_out}")
        assert locked_out == [(str(ALLOWED_PORT), "tcp")], locked_out
        assert open_out == [("any", "any")], open_out
        # ...and the INGRESS really is identical, which is what makes (2) a
        # statement about EGRESS. Without this, "locked could not reach
        # peer:5432" could be read off peer's inbound instead -- and (3) only
        # measures LOCKED's inbound on that port, never peer's.
        sides = {name: sorted((r["port"], r["proto"], r.get("cidr"), r.get("group"))
                              for r in groups[name]["firewall"]["inbound"])
                 for name in ("open-sg", "locked-sg")}
        assert sides["open-sg"] == sides["locked-sg"], sides
        print(f"[egress-vm] both groups' inbound (identical, by construction): {sides['open-sg']}")

        # --- and the bytes that REACHED THE VM, read back off the VM's own
        # disk rather than off odin's record of what it meant to write. "Same
        # bytes" is the claim the container-level proof rested on; this is the
        # only place it is checked on the thing that enforces them.
        on_vm = _vm_shell(locked_vm, "sudo", "cat", "/etc/nebula/config.yml")
        assert on_vm.returncode == 0, on_vm.stderr
        recorded = (store.root / ENV / "nebula" / "instances" / locked_id / "config.yml").read_text()
        assert on_vm.stdout.strip() == recorded.strip(), "the VM is not running the config odin recorded"
        on_vm_firewall = yaml.safe_load(on_vm.stdout)["firewall"]
        print(f"[egress-vm] /etc/nebula/config.yml on {locked_vm}: outbound={on_vm_firewall['outbound']}")
        assert [(r["port"], r["proto"]) for r in on_vm_firewall["outbound"]] == locked_out, on_vm_firewall

        for vm in (peer_vm, locked_vm):
            _start_listener(vm, ALLOWED_PORT)
            _start_listener(vm, BLOCKED_PORT)
        time.sleep(1.0)

        # (1) the positive control ON THE RESTRICTED VM: the port its own
        # egress rule names. This is what makes (2) mean something -- it proves
        # this VM is on the mesh and can reach this peer right now.
        one = _talk_until(locked_vm, peer_ip, ALLOWED_PORT)
        print(f"[egress-vm] (1) locked -> peer:{ALLOWED_PORT} (its egress rule)      {one!r}")
        assert _OK in one, f"locked-sg's egress names tcp:{ALLOWED_PORT}; the VM must reach it"

        # (3) the control (2) needs and a single VM cannot give: a VM with
        # unrestricted egress DOES reach :5432 on this overlay, right now. It
        # also proves nebula's conntrack lets `locked` answer on a port its own
        # egress does not name -- egress gates sending, never answering.
        three = _talk_until(peer_vm, locked_ip, BLOCKED_PORT)
        print(f"[egress-vm] (3) peer   -> locked:{BLOCKED_PORT} (egress allow-all)     {three!r}")
        assert _OK in three, "a VM whose egress is allow-all must reach tcp/5432 over this same overlay"

        # (4) symmetry, so the 2x2 is complete rather than three-quarters of one.
        four = _talk_until(peer_vm, locked_ip, ALLOWED_PORT, attempts=5)
        print(f"[egress-vm] (4) peer   -> locked:{ALLOWED_PORT} (egress allow-all)     {four!r}")
        assert _OK in four, "an allow-all egress must reach tcp/6000 too"

        # (2) THE assertion. Same VM as (1), same peer, same instant, a port its
        # egress does not name -- against a listener (3) just proved is live and
        # overlay-reachable. Reverting `sg_rules_to_firewall`'s outbound
        # compilation makes this line, and only this line, fail.
        two = _talk(locked_vm, peer_ip, BLOCKED_PORT)
        print(f"[egress-vm] (2) locked -> peer:{BLOCKED_PORT} (NOT in its egress)    {two!r}")
        assert _OK not in two, (
            f"a security group whose egress omits tcp:{BLOCKED_PORT} must not reach it over the overlay -- "
            "the egress rule is decorative if this passes"
        )

        # --- (5)/(6) ICMP, and this pair is not decoration. `_converge` closes
        # a measured 10-60s dead window after a nebula restart by having the
        # restarted member poke every peer -- and `rehandshake_script` pokes
        # with ICMP. Egress compilation is brand new, so nothing has ever asked
        # what that poke does when the member's OWN egress does not name icmp.
        # Both groups admit icmp INBOUND identically (asserted above), so (5)'s
        # outcome is attributable to `locked`'s outbound exactly the way (2)'s
        # is. See docs/limits.md -- this is a real consequence of v0.8.17.
        # ORDER IS LOAD-BEARING, and the first version of this pair got it
        # wrong: run peer->locked first and locked->peer PASSES, because
        # nebula's conntrack opened an ICMP entry for that pair when the
        # INBOUND ping was admitted, and locked's own ping then rides it as an
        # established flow. Measured both ways -- the reversed order is a
        # working outbound firewall reported as a broken one. So the RESTRICTED
        # direction goes first, on a pair with no ICMP state at all.
        five = _vm_shell(locked_vm, "sudo", "ping", "-c", "2", "-W", "2", peer_ip, timeout=30)
        print(f"[egress-vm] (5) locked -> ping peer   (icmp NOT in egress) rc={five.returncode}")
        assert five.returncode != 0, (
            "locked-sg's egress names only tcp:6000, so its own ICMP must not leave the overlay"
        )
        six = _vm_shell(peer_vm, "sudo", "ping", "-c", "2", "-W", "2", locked_ip, timeout=30)
        print(f"[egress-vm] (6) peer   -> ping locked (egress allow-all)   rc={six.returncode}")
        assert six.returncode == 0, f"both groups admit icmp inbound, so this ping must land: {six.stdout}"

        # --- WHAT IT DOES NOT GATE, measured in the same breath rather than
        # asserted from theory: nebula's firewall governs the overlay tun, and a
        # Lima VM's vzNAT path to the host is not on it. Same VM, same restricted
        # egress, a plain TCP connection straight off the box.
        host_underlay = overlay["lighthouse_underlay_ip"]
        vznat = _vm_shell(
            locked_vm, "python3", "-c", _CONNECT_PROBE, host_underlay, str(host_listener), timeout=40,
        ).stdout.strip()
        print(f"[egress-vm] (7) locked -> host {host_underlay}:{host_listener} over vzNAT, NOT the overlay: {vznat!r}")
        assert vznat == "CONNECTED", (
            "a VM's SG egress governs overlay traffic only; the vzNAT path to the host must still work "
            f"(if this ever starts failing the limit recorded in docs/limits.md has changed): {vznat}"
        )

        # The same limit one hop further out, and the one a user actually cares
        # about. MEASURED, never asserted: this is the only probe in the file
        # that depends on the machine having working DNS and egress to the
        # public internet, and a red test on the CI box's network would say
        # nothing about odin. `docs/limits.md` quotes what this printed.
        internet = _vm_shell(
            locked_vm, "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
            "--max-time", "8", "https://example.com", timeout=30,
        )
        print(f"[egress-vm] (8) locked -> https://example.com over slirp, NOT the overlay: "
              f"rc={internet.returncode} out={internet.stdout.strip()!r} err={internet.stderr.strip()!r}")

        teardown = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert teardown.status_code == 200, teardown.text
        assert teardown.json()["tf"] == {"status": "ok", "exit_code": 0}, teardown.json()

    listing = subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=30)
    remaining = set(listing.stdout.split())
    for name in vm_cleanup:
        assert name not in remaining, f"VM survived teardown: {name}"
