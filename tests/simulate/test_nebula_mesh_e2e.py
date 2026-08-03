"""R4 -- the ROOTLESS single-host mesh activation FLAGSHIP: a real `tofu
apply` boots TWO real Lima VMs (V3's own flagship path) into the SAME V1
vpc/subnet, with ingress rules authorized on the VPC's default security
group for tcp/8080 + ICMP (and NOT tcp/9090) BEFORE either instance ever
boots -- through the real gateway -- and proves the whole mesh is real, not
just compiled artifacts sitting on disk:

  1. The HOST runs a real, UNPRIVILEGED `nebula` lighthouse process for this
     env (`fabric.nebula.LighthouseManager`) -- no root, no sudo, no ctl
     script (R4: `tun: disabled: true`, empirically verified in that
     module's docstring). It coordinates only; it is NOT a data-plane member
     of the mesh (no tun device, hence no overlay IP of its own), so every
     connectivity proof below runs FROM one VM TO another, never from the
     host -- unlike R3, where the host itself held a real tun device and
     could ping a VM directly (exactly the privileged design R4 replaces).
  2. BOTH VMs run a real `nebula` daemon, joined to that lighthouse, with
     the VPC's default SG's compiled firewall baked in at boot (RunInstances
     has no SecurityGroupIds param -- v1's own recorded limit -- so every
     instance inherits its VPC's default SG's rules, exactly like real AWS
     with none specified; the SG is authorized in a FIRST `tofu apply`
     covering only VPC/Subnet/KeyPair, then BOTH instances are added in a
     SECOND apply against the SAME workspace -- one pair of VM boots total,
     the SG rules already live when RunInstances snapshots them).
  3. `ping <VM-B overlay IP>` FROM INSIDE VM-A (`limactl shell vm-a --
     ping ...`) succeeds -- a real encrypted VM-to-VM tunnel, coordinated by
     (but never routed through) the lighthouse.
  4. The compiled SG rule actually FILTERS, proven the same VM-to-VM way: a
     real listener on VM-B's ALLOWED port (8080) is reachable from VM-A over
     the overlay; an identical real listener on a port with NO SG rule
     (9090) is NOT -- proving nebula's firewall, not merely "nothing was
     listening", is what blocks it.

Along the way this test's own construction found and fixed a real bug (see
`fabric/nebula.py::sg_rules_to_firewall`'s `_PORTLESS_PROTOCOLS`): an ICMP SG
rule's AWS FromPort=-1 ("all types") was being passed straight through as a
nebula `port` value, which made nebula refuse to start outright -- silently
breaking proof 3 above. Phase 1 below authorizes ICMP explicitly (real AWS
default security groups don't auto-allow ping between members without an
explicit rule or a same-SG self-reference, and this gateway's default SG
doesn't model that self-reference either -- see `ec2net.py::_new_sg`).

VM + lighthouse hygiene ABSOLUTE (carried forward): `vm_cleanup` (exact VM
names) and `lighthouse_cleanup` (`LighthouseManager.ensure_stopped` by exact
pidfile pid -- never a pattern/blanket kill) force-clean in finalizers, so a
test FAILURE never leaves a stray VM or a stray process. There is no
root-owned process anywhere in this design to worry about leaking.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import time

import boto3
import pytest
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.iac.hcl import TfProject
from odin.compute.instances import vm_name
from odin.fabric.nebula import LighthouseManager
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "nebula-mesh-e2e"
ALLOWED_PORT = 8080
BLOCKED_PORT = 9090

_KEY_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF3v9y1Q8k2v0e0h3m9F6y3JcRZfN0v8k6r7q6q6q6q6 odin-test@example.com"

_VPC = f"""resource "aws_vpc" "main" {{
  cidr_block = {hcl.quote("10.0.0.0/16")}
}}"""

_SUBNET = f"""resource "aws_subnet" "a" {{
  vpc_id     = aws_vpc.main.id
  cidr_block = {hcl.quote("10.0.1.0/24")}
}}"""

_KEY_PAIR = f"""resource "aws_key_pair" "deploy" {{
  key_name   = {hcl.quote("deploy")}
  public_key = {hcl.quote(_KEY_PUBLIC)}
}}"""

_INSTANCE_A = f"""resource "aws_instance" "server_a" {{
  ami           = {hcl.quote("ami-0c101f26f147fa7fd")}
  instance_type = {hcl.quote("t3.micro")}
  subnet_id     = aws_subnet.a.id
  key_name      = aws_key_pair.deploy.key_name
}}"""

_INSTANCE_B = f"""resource "aws_instance" "server_b" {{
  ami           = {hcl.quote("ami-0c101f26f147fa7fd")}
  instance_type = {hcl.quote("t3.micro")}
  subnet_id     = aws_subnet.a.id
  key_name      = aws_key_pair.deploy.key_name
}}"""

NETWORK_TF = "\n\n".join([hcl.HEADER, hcl.provider_block(), _VPC, _SUBNET, _KEY_PAIR]) + "\n"
FULL_TF = "\n\n".join(
    [hcl.HEADER, hcl.provider_block(), _VPC, _SUBNET, _KEY_PAIR, _INSTANCE_A, _INSTANCE_B],
) + "\n"


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


def _tofu(args: list[str], workspace, env_vars: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def _ec2compute_state(root, env: str) -> dict:
    path = root / env / "gateway" / "ec2compute.json"
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def vm_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=30)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=30)


@pytest.fixture
def lighthouse_cleanup():
    """`(root, env)` pairs to stop on teardown -- `LighthouseManager.
    ensure_stopped` reads THIS env's own pidfile and kills by exact pid,
    never a pattern/blanket kill (mirrors `vm_cleanup`'s exact-name
    discipline for VMs)."""
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


def _vm_shell(vm: str, *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "shell", vm, "--", *args], capture_output=True, text=True, timeout=timeout)


def _wait_for_ping_from_vm(vm: str, ip: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    """A real overlay tunnel needs both VMs' nebula daemons to install,
    start, and handshake with the lighthouse (and each other) AFTER `tofu
    apply` already returned (`systemctl enable --now` returning proves the
    unit STARTED, not that the tunnel is UP yet) -- so this polls rather
    than asserting once. Runs FROM VM-A (never the host: the R4 host
    lighthouse has no tun device, hence no overlay presence of its own)."""
    deadline = time.monotonic() + timeout
    last = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    while time.monotonic() < deadline:
        last = _vm_shell(vm, "ping", "-c", "1", "-W", "2", ip, timeout=10)
        if last.returncode == 0:
            return last
        time.sleep(2.0)
    return last


def _start_http_listener(vm: str, port: int) -> None:
    _vm_shell(
        vm, "bash", "-c",
        f"nohup python3 -m http.server {port} --bind 0.0.0.0 > /tmp/http{port}.log 2>&1 & disown",
    )


def _tcp_reachable_from_vm(vm: str, ip: str, port: int, timeout: float = 5.0) -> bool:
    """No `nc` assumed inside the Ubuntu cloud image -- a plain `python3`
    socket connect (python3 is already guaranteed present: `_start_http_
    listener` runs `python3 -m http.server`), probed FROM VM-A, mirroring
    the ping proof's "never from the host" rule."""
    probe = (
        f"import socket,sys; s=socket.socket(); s.settimeout({timeout}); "
        f"sys.exit(0 if s.connect_ex(('{ip}', {port})) == 0 else 1)"
    )
    result = _vm_shell(vm, "python3", "-c", probe, timeout=timeout + 5)
    return result.returncode == 0


def _process_owner(pid: int) -> str:
    probe = subprocess.run(["ps", "-o", "user=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    return probe.stdout.strip()


def test_real_overlay_ping_and_sg_rule_filters_a_real_connection(tmp_path, vm_cleanup, lighthouse_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"
    # R4: no sudo / root-setup gate here -- the lighthouse always runs
    # unprivileged, so there is no one-time host setup left to check for.

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        lighthouse_cleanup.append((store.root, ENV))

        # Phase 1: VPC/Subnet/KeyPair only -- no VM yet. This is where the
        # VPC's default SG comes into existence so the ingress rules below
        # can be authorized BEFORE either instance ever boots.
        workspace = workspace_mod.materialize(store.root, ENV, TfProject(files={"main.tf": NETWORK_TF}))
        init = _tofu(["init"], workspace, env_vars)
        assert init.returncode == 0, f"init failed:\n{init.stdout}\n{init.stderr}"
        net_apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=120)
        assert net_apply.returncode == 0, f"network apply failed:\n{net_apply.stdout}\n{net_apply.stderr}"

        ec2_client = boto3.client(
            "ec2", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        )
        (vpc,) = ec2_client.describe_vpcs()["Vpcs"]
        (default_sg,) = ec2_client.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}, {"Name": "group-name", "Values": ["default"]}],
        )["SecurityGroups"]
        ec2_client.authorize_security_group_ingress(
            GroupId=default_sg["GroupId"],
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": ALLOWED_PORT, "ToPort": ALLOWED_PORT,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                # ICMP, so VM-A can ping VM-B -- real AWS default SGs don't
                # auto-allow this between members without an explicit rule
                # or a same-SG self-reference (this gateway's default SG
                # doesn't model that self-reference; see ec2net.py::_new_sg).
                {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            ],
        )
        print(f"[R4] authorized tcp/{ALLOWED_PORT} + icmp on default SG {default_sg['GroupId']} (tcp/{BLOCKED_PORT} left unauthorized)")

        # Phase 2: add BOTH instances -- the SG rules are already live, so
        # RunInstances' snapshot of the VPC default SG's compiled firewall
        # carries them from the start. Re-materializing never disturbs the
        # existing state/plugin cache (workspace.py's own contract) -- VPC/
        # Subnet/KeyPair are unchanged (zero drift on those), only the two
        # instances get created.
        workspace = workspace_mod.materialize(store.root, ENV, TfProject(files={"main.tf": FULL_TF}))
        boot_start = time.monotonic()
        apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=600)
        boot_elapsed = time.monotonic() - boot_start
        print(f"[R4] tofu apply (two real VM boots, two real nebula joins) took {boot_elapsed:.1f}s")

        state = _ec2compute_state(store.root, ENV)
        instances = [v for k, v in state.items() if k.startswith("instance:")]
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        assert apply.returncode == 0, f"apply failed:\n{apply.stdout}\n{apply.stderr}"
        assert len(instances) == 2, f"expected 2 instances, got {len(instances)}: {instances}"

        overlay_path = store.root / ENV / "nebula" / "overlay.json"
        vms = []
        for instance in instances:
            instance_id = instance["instance_id"]
            vm = vm_name(ENV, instance_id)
            assert instance["state_name"] == "running", f"{instance_id} never reached running: {instance}"
            assert (store.root / ENV / "nebula" / "hosts" / f"{instance_id}.crt").exists(), f"{instance_id} cert never landed"
            overlay_ip = json.loads(overlay_path.read_text())["subnets"]["hosts"]["assignments"][instance_id]
            print(f"[R4] instance {instance_id} ({vm}) overlay IP: {overlay_ip}")
            nebula_status = _vm_shell(vm, "systemctl", "is-active", "nebula")
            print(f"[R4] {vm} nebula.service is-active: {nebula_status.stdout.strip()!r}")
            assert nebula_status.stdout.strip() == "active", f"nebula daemon never started inside {vm}"
            vms.append((vm, overlay_ip))
        (vm_a, ip_a), (vm_b, ip_b) = vms

        # 1: a real, UNPRIVILEGED lighthouse on the host -- never root.
        assert LighthouseManager().is_running(store.root, ENV), "host lighthouse process never started"
        pidfile = store.root / ENV / "nebula" / "lighthouse.pid"
        lighthouse_pid = int(pidfile.read_text().strip())
        owner = _process_owner(lighthouse_pid)
        invoking_user = getpass.getuser()
        print(f"[R4] lighthouse pid {lighthouse_pid} owned by {owner!r} (invoking user: {invoking_user!r})")
        assert owner != "root", "lighthouse must NEVER run as root (R4: tun disabled, fully unprivileged)"
        assert owner == invoking_user, f"lighthouse should run as the invoking user, not {owner!r}"

        # 2 + 3: two real nebula daemons in the VMs, a real VM-to-VM ping --
        # never host-to-VM (the host has no overlay presence in R4).
        ping = _wait_for_ping_from_vm(vm_a, ip_b)
        print(f"[R4] ping {ip_b} FROM {vm_a} (overlay):\n{ping.stdout}")
        assert ping.returncode == 0, f"VM-to-VM overlay ping never succeeded:\n{ping.stdout}\n{ping.stderr}"

        # 4: the real SG-filter proof, also VM-to-VM -- both ports have a
        # REAL listener on VM-B; only the SG-authorized one is reachable
        # from VM-A over the overlay.
        _start_http_listener(vm_b, ALLOWED_PORT)
        _start_http_listener(vm_b, BLOCKED_PORT)
        time.sleep(1.0)
        allowed = _tcp_reachable_from_vm(vm_a, ip_b, ALLOWED_PORT)
        blocked = _tcp_reachable_from_vm(vm_a, ip_b, BLOCKED_PORT)
        print(f"[R4] tcp/{ALLOWED_PORT} (SG-allowed) reachable from {vm_a}: {allowed}; tcp/{BLOCKED_PORT} (no SG rule): {blocked}")
        assert allowed, f"tcp/{ALLOWED_PORT} should be reachable (SG-authorized) but was not"
        assert not blocked, f"tcp/{BLOCKED_PORT} should be BLOCKED (no SG rule) but was reachable"

        destroy = _tofu(["destroy", "-auto-approve"], workspace, env_vars, timeout=180)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"

        listing = subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=30)
        remaining = set(listing.stdout.split())
        assert vm_a not in remaining and vm_b not in remaining

        # "last VM leaves" -- the lighthouse must stop AUTOMATICALLY
        # (gateway/models/ec2compute.py::_maybe_stop_lighthouse), not just
        # via the test's own teardown fixture.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and LighthouseManager().is_running(store.root, ENV):
            time.sleep(0.5)
        assert not LighthouseManager().is_running(store.root, ENV), "lighthouse still running after both VMs terminated"
