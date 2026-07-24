"""R3 -- the single-host mesh activation FLAGSHIP: a real `tofu apply` boots a
REAL Lima VM (V3's own flagship path) into a V1 vpc/subnet, with an ingress
rule authorized on the VPC's default security group for tcp/8080 (and NOT
tcp/9090) BEFORE the instance ever boots -- through the real gateway -- and
proves the whole mesh is real, not just compiled artifacts sitting on disk:

  1. The HOST runs a real `nebula` lighthouse process for this env
     (`fabric.nebula.LighthouseManager`, root via `sudo -n`).
  2. The VM runs a real `nebula` daemon, joined to that lighthouse, with the
     VPC's default SG's compiled firewall baked in at boot (RunInstances has
     no SecurityGroupIds param -- v1's own recorded limit -- so every
     instance inherits its VPC's default SG's rules, exactly like real AWS
     with none specified; the SG is authorized in a FIRST `tofu apply`
     covering only VPC/Subnet/KeyPair, then the instance is added in a
     SECOND apply against the SAME workspace -- one VM boot total, the SG
     rule already live when RunInstances snapshots it).
  3. `ping <VM overlay IP>` from the HOST succeeds -- a real encrypted
     tunnel, not a vzNAT artifact (vzNAT itself is outbound-only from the
     VM -- a raw host->VM ping over the bare vzNAT address fails; only the
     nebula overlay tunnel makes the host->VM direction work).
  4. The compiled SG rule actually FILTERS: a real listener on the ALLOWED
     port (8080) is reachable over the overlay; an identical real listener
     on a port with NO SG rule (9090) is NOT -- proving nebula's firewall,
     not merely "nothing was listening", is what blocks it.

Root requirement (macOS, verified empirically -- see `fabric/nebula.py`'s
`LighthouseManager` docstring): creating a utun device needs root. This test
is SKIPPED with a clear, actionable message if the one-time hardened setup
(a root-owned `allfather-nebula-ctl` control script + a root-owned nebula
copy, both only root can ever replace, per `scripts/allfather-nebula-ctl`)
hasn't been done on this Mac -- never a silent pass, and never an attempt to
self-provision root (a real security boundary, not a test-harness
inconvenience to route around). The sudoers grant is scoped to that one
fixed script path -- NEVER the user-writable brew `nebula` binary directly,
which would be a root-escalation hole.

VM + lighthouse hygiene ABSOLUTE (V3d's own words, carried forward): both
`vm_cleanup` (exact VM name) and `lighthouse_cleanup`
(`LighthouseManager.ensure_stopped` by exact pidfile pid -- never a
pattern/blanket kill) force-clean in finalizers, so a test FAILURE never
leaves a stray VM or a stray root-owned `nebula` process.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import boto3
import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent.hcl import TfProject
from odin.compute.instances import vm_name
from odin.fabric.nebula import NEBULA_CTL_PATH, LighthouseManager
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

_INSTANCE = f"""resource "aws_instance" "server" {{
  ami           = {hcl.quote("ami-0c101f26f147fa7fd")}
  instance_type = {hcl.quote("t3.micro")}
  subnet_id     = aws_subnet.a.id
  key_name      = aws_key_pair.deploy.key_name
}}"""

NETWORK_TF = "\n\n".join([hcl.HEADER, hcl.provider_block(), _VPC, _SUBNET, _KEY_PAIR]) + "\n"
FULL_TF = "\n\n".join([hcl.HEADER, hcl.provider_block(), _VPC, _SUBNET, _KEY_PAIR, _INSTANCE]) + "\n"


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


_SETUP_COMMAND = (
    "sudo install -o root -g wheel -m 755 scripts/allfather-nebula-ctl /usr/local/libexec/allfather-nebula-ctl && "
    'sudo install -o root -g wheel -m 755 "$(which nebula)" /usr/local/libexec/allfather-nebula && '
    f'echo "$(whoami) ALL=(root) NOPASSWD: {NEBULA_CTL_PATH}" | sudo tee /etc/sudoers.d/allfather-nebula'
)


def _sudo_nebula_authorized() -> bool:
    """A SCOPED probe -- runs `allfather-nebula-ctl check` under `sudo -n`,
    the EXACT command `LighthouseManager` uses, never a raw `nebula`
    invocation (that would need a NOPASSWD grant on brew's user-writable
    path, a root-escalation hole `scripts/allfather-nebula-ctl`'s whole
    design exists to avoid). Proves the specific hardened grant exists, not
    merely that some passwordless sudo ticket happens to be cached."""
    probe = subprocess.run(["sudo", "-n", NEBULA_CTL_PATH, "check"], capture_output=True, timeout=10)
    return probe.returncode == 0


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


def _wait_for_ping(ip: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    """A real overlay tunnel needs the VM's nebula daemon to install, start,
    and handshake with the lighthouse AFTER `tofu apply` already returned
    (`systemctl enable --now` returning proves the unit STARTED, not that
    the tunnel is UP yet) -- so this polls rather than asserting once."""
    deadline = time.monotonic() + timeout
    last = subprocess.CompletedProcess(args=[], returncode=1)
    while time.monotonic() < deadline:
        last = subprocess.run(["ping", "-c", "1", "-t", "2", ip], capture_output=True, text=True, timeout=5)
        if last.returncode == 0:
            return last
        time.sleep(2.0)
    return last


def _start_http_listener(vm: str, port: int) -> None:
    subprocess.run(
        ["limactl", "shell", vm, "--", "bash", "-c",
         f"nohup python3 -m http.server {port} --bind 0.0.0.0 > /tmp/http{port}.log 2>&1 & disown"],
        capture_output=True, timeout=15,
    )


def _tcp_reachable(ip: str, port: int, timeout: float = 5.0) -> bool:
    probe = subprocess.run(["nc", "-z", "-w", str(int(timeout)), ip, str(port)], capture_output=True, timeout=timeout + 3)
    return probe.returncode == 0


def test_real_overlay_ping_and_sg_rule_filters_a_real_connection(tmp_path, vm_cleanup, lighthouse_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"
    if not _sudo_nebula_authorized():
        pytest.skip(
            "the hardened one-time host setup for the nebula lighthouse is not done on "
            "this Mac -- a root-owned control script + a root-owned nebula copy, both "
            "only root can replace (never a NOPASSWD grant on the user-writable brew "
            f"path -- see scripts/allfather-nebula-ctl):\n  {_SETUP_COMMAND}"
        )

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        lighthouse_cleanup.append((store.root, ENV))

        # Phase 1: VPC/Subnet/KeyPair only -- no VM yet. This is where the
        # VPC's default SG comes into existence so the ingress rule below
        # can be authorized BEFORE any instance ever boots.
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
            IpPermissions=[{"IpProtocol": "tcp", "FromPort": ALLOWED_PORT, "ToPort": ALLOWED_PORT,
                             "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        )
        print(f"[R3] authorized tcp/{ALLOWED_PORT} on default SG {default_sg['GroupId']} (tcp/{BLOCKED_PORT} left unauthorized)")

        # Phase 2: add the instance -- the SG rule is already live, so
        # RunInstances' snapshot of the VPC default SG's compiled firewall
        # carries it from the start. Re-materializing never disturbs the
        # existing state/plugin cache (workspace.py's own contract) -- VPC/
        # Subnet/KeyPair are unchanged (zero drift on those), only the
        # instance gets created. One VM boot, total.
        workspace = workspace_mod.materialize(store.root, ENV, TfProject(files={"main.tf": FULL_TF}))
        boot_start = time.monotonic()
        apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=600)
        boot_elapsed = time.monotonic() - boot_start
        print(f"[R3] tofu apply (real VM boot, real nebula join) took {boot_elapsed:.1f}s")

        state = _ec2compute_state(store.root, ENV)
        instances = [v for k, v in state.items() if k.startswith("instance:")]
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))
        assert apply.returncode == 0, f"apply failed:\n{apply.stdout}\n{apply.stderr}"

        (instance,) = instances
        instance_id = instance["instance_id"]
        vm = vm_name(ENV, instance_id)
        assert instance["state_name"] == "running"
        assert (store.root / ENV / "nebula" / "hosts" / f"{instance_id}.crt").exists(), "instance cert never landed"

        overlay = store.root / ENV / "nebula" / "overlay.json"
        overlay_ip = json.loads(overlay.read_text())["subnets"]["hosts"]["assignments"][instance_id]
        print(f"[R3] instance {instance_id} overlay IP: {overlay_ip}")

        # 1 + 2: a real lighthouse on the host, a real daemon in the VM.
        assert LighthouseManager().is_running(store.root, ENV), "host lighthouse process never started"
        nebula_status = subprocess.run(
            ["limactl", "shell", vm, "--", "systemctl", "is-active", "nebula"],
            capture_output=True, text=True, timeout=15,
        )
        print(f"[R3] VM nebula.service is-active: {nebula_status.stdout.strip()!r}")
        assert nebula_status.stdout.strip() == "active", "nebula daemon never started inside the VM"

        # 3: the real ping proof.
        ping = _wait_for_ping(overlay_ip)
        print(f"[R3] ping {overlay_ip} (overlay):\n{ping.stdout}")
        assert ping.returncode == 0, f"overlay ping never succeeded:\n{ping.stdout}\n{ping.stderr}"

        # 4: the real SG-filter proof -- both ports have a REAL listener;
        # only the SG-authorized one is reachable over the overlay.
        _start_http_listener(vm, ALLOWED_PORT)
        _start_http_listener(vm, BLOCKED_PORT)
        time.sleep(1.0)
        allowed = _tcp_reachable(overlay_ip, ALLOWED_PORT)
        blocked = _tcp_reachable(overlay_ip, BLOCKED_PORT)
        print(f"[R3] tcp/{ALLOWED_PORT} (SG-allowed) reachable: {allowed}; tcp/{BLOCKED_PORT} (no SG rule) reachable: {blocked}")
        assert allowed, f"tcp/{ALLOWED_PORT} should be reachable (SG-authorized) but was not"
        assert not blocked, f"tcp/{BLOCKED_PORT} should be BLOCKED (no SG rule) but was reachable"

        destroy = _tofu(["destroy", "-auto-approve"], workspace, env_vars, timeout=120)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"

        listing = subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=30)
        assert vm not in listing.stdout.split()

        # "last VM leaves" -- the lighthouse must stop AUTOMATICALLY
        # (gateway/models/ec2compute.py::_maybe_stop_lighthouse), not just
        # via the test's own teardown fixture.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and LighthouseManager().is_running(store.root, ENV):
            time.sleep(0.5)
        assert not LighthouseManager().is_running(store.root, ENV), "lighthouse still running after the last VM terminated"
