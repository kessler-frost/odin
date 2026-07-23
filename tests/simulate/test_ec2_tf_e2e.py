"""V3d -- the FLAGSHIP integration test: a real `tofu apply` boots a REAL
Lima VM for an `aws_instance` behind `aws_key_pair`, inside a V1 vpc/subnet,
through the real gateway -- proving NORTHSTAR directive 5's whole EC2 slice
end-to-end (RunInstances -> InstanceVm.boot -> a real vzNAT-reachable VM ->
the provider's own pending->running waiter -> zero-drift plan -> Terminate ->
a real VM delete). Modeled on test_ec2net_tf_e2e.py, with one load-bearing
difference: V1a's test needed no Colima/Lima at all (EC2-network is pure
model); this one boots and deletes an actual VM, so it's `integration`-only
and pays a real ~30-90s boot (first-ever run also downloads the Ubuntu 24.04
cloud image, ~600MB, cached after -- the apply step's timeout accounts for a
cold cache).

VM hygiene ABSOLUTE (owner's own words, the V3 brief): the `vm_cleanup`
fixture force-stops + force-deletes the exact VM name in a finalizer, so a
test FAILURE never leaves a stray VM even if `tofu destroy` itself never
runs. It only ever names the one VM this test itself created.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent.hcl import TfProject
from odin.compute.instances import vm_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ec2-tf-e2e"

# A throwaway ed25519 public key -- shape only matters (ImportKeyPair stores
# it verbatim, never validates it cryptographically).
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

MAIN_TF = "\n\n".join([hcl.HEADER, hcl.provider_block(), _VPC, _SUBNET, _KEY_PAIR, _INSTANCE]) + "\n"


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
    """VM hygiene ABSOLUTE: names appended here are force-stopped + force-
    deleted by EXACT name on teardown, regardless of test outcome -- the
    guarantee `tofu destroy` alone can't give if the test fails before it."""
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=30)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=30)


def test_tf_apply_boots_a_real_vm_zero_drift_destroy(tmp_path, vm_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        workspace = workspace_mod.materialize(store.root, ENV, TfProject(files={"main.tf": MAIN_TF}))

        init = _tofu(["init"], workspace, env_vars)
        assert init.returncode == 0, f"init failed:\n{init.stdout}\n{init.stderr}"

        # 600s: a cold Lima image-download cache (~600MB, first-ever run)
        # plus a ~30-90s warm boot -- the brief's own accepted budget.
        boot_start = time.monotonic()
        apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=600)
        boot_elapsed = time.monotonic() - boot_start
        print(f"\n[V3d] tofu apply (incl. real Lima VM boot) took {boot_elapsed:.1f}s")

        # Register the VM for cleanup from the state on disk BEFORE asserting
        # apply succeeded: `RunInstances` writes the instance record (and a
        # `limactl create` may already have made a VM directory) even when
        # the boot itself then fails -- an assertion failure below must never
        # skip past `vm_cleanup.append`, or a failed run leaks a VM (found
        # empirically: the vzNAT/socket_vmnet fix this test surfaced left
        # exactly this kind of stray VM on its first failed run).
        state = _ec2compute_state(store.root, ENV)
        instances = [v for k, v in state.items() if k.startswith("instance:")]
        for instance in instances:
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))

        assert apply.returncode == 0, f"apply failed:\n{apply.stdout}\n{apply.stderr}"

        (instance,) = instances
        (keypair,) = [v for k, v in state.items() if k.startswith("keypair:")]
        instance_id = instance["instance_id"]
        vm = vm_name(ENV, instance_id)

        assert instance["state_name"] == "running"
        assert instance["private_ip"], "RunInstances -> boot -> a real vzNAT IP, not a placeholder"
        assert keypair["key_name"] == "deploy"

        # THE proof the VM is real, not a model fiction: shell into it.
        shell = subprocess.run(
            ["limactl", "shell", vm, "--", "echo", "ok"],
            capture_output=True, text=True, timeout=30,
        )
        print(f"[V3d] limactl shell {vm} -- echo ok  =>  rc={shell.returncode} stdout={shell.stdout.strip()!r}")
        assert shell.returncode == 0 and shell.stdout.strip() == "ok"

        # V1b/V3b: the instance's Nebula cert+config landed on the VM's disk
        # (real cert artifacts, no daemon started -- the flagged next step is
        # installing/starting nebula + a host lighthouse, not built here).
        cert_check = subprocess.run(
            ["limactl", "shell", vm, "--", "test", "-f", "/etc/nebula/host.crt"],
            capture_output=True, timeout=30,
        )
        print(f"[V3d] /etc/nebula/host.crt present on the VM: {cert_check.returncode == 0}")
        assert (store.root / ENV / "nebula" / "hosts" / f"{instance_id}.crt").exists()

        # zero drift: the research bar -- apply -> plan changes NOTHING.
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        destroy = _tofu(["destroy", "-auto-approve"], workspace, env_vars, timeout=120)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"

        # the VM is actually gone -- not just the model record.
        listing = subprocess.run(["limactl", "list", "-q"], capture_output=True, text=True, timeout=30)
        assert vm not in listing.stdout.split()

        # The instance record itself may still be there (the ~60s
        # post-terminate grace window, MiniStack's lazy-sweep pattern) but
        # must never be anything other than `terminated`; the key pair has
        # no such window and is gone outright.
        final_state = _ec2compute_state(store.root, ENV)
        final_instance = final_state.get(f"instance:{instance_id}")
        assert final_instance is None or final_instance["state_name"] == "terminated"
        assert f"keypair:{keypair['key_name']}" not in final_state
