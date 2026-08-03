"""route53 becomes REAL name resolution -- asked of the resolver itself.

Everything else about a route53 record can be green while no name resolves:
the builder emits HCL, the gateway stores a record, `DescribeRecordSets`
returns what odin itself wrote. None of that is resolution. So this file asks
the CONSUMER -- `getent hosts` inside a real container and inside a real Lima
VM -- and believes only what it answers.

FOUR claims, and the last two are the ones no unit test can reach:

 1. **A container resolves the name.** `--add-host` really lands, and a name
    that was NOT drawn does NOT resolve -- a resolver that answers everything
    proves nothing.
 2. **A VM resolves the name at boot**, from cloud-init, and its ORIGINAL
    /etc/hosts entries survive. `/etc/hosts` is not odin's file: the image's
    loopback lines and Lima's hostname line are already in it, and a writer
    that truncated would break resolution for the whole guest while appearing
    to work for the one name under test.
 3. **A record edited AFTER boot reaches a running VM, with no reboot.**
    `generate_cloud_init` runs once inside `limactl create` and its bytes are
    frozen into the instance's lima.yaml; `limactl edit` refuses a running
    instance outright. So without `push_hosts` an edited record could never
    arrive, and the canvas would report `applied` over a guest that never saw
    it. The no-reboot half is proven by `boot_id`, not assumed: a push that
    silently rebooted the VM would satisfy "it resolves now" just as well, and
    would be a very different product.
 4. **Removing the record stops resolution.** An append-only writer passes
    every positive assertion above and fails this one, which is exactly why it
    is here.

NOT COVERED HERE, deliberately: the canvas -> record -> per-consumer address
resolution. That layer needs the route53 store shape and is owned elsewhere;
this file proves the substrate underneath it, so a failure here is odin's
plumbing and a failure there is odin's mapping, and the two cannot be confused.

Hygiene: every name is `r53h-` prefixed and this file removes only what it
created. Never a machine-wide sweep -- other agents' containers and VMs share
this machine.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from odin.compute.cloud_init import HOSTS_BEGIN
from odin.compute.instances import InstanceVm
from odin.compute.models import get_instance_type
from odin.runtime.colima import ColimaRuntime, ContainerSpec

pytestmark = pytest.mark.integration

PREFIX = "r53h"
CONTAINER = f"{PREFIX}-consumer"
VM = f"{PREFIX}-vm"

DRAWN = "api.internal"
UNDRAWN = "nothing.internal"
FIRST_IP = "10.42.0.5"
SECOND_IP = "10.42.0.6"


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def _limactl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", *args], capture_output=True, text=True, timeout=600)


def _in_vm(script: str) -> subprocess.CompletedProcess:
    return _limactl("shell", VM, "--", "sh", "-c", script)


@pytest.fixture
def container_cleanup():
    yield
    _docker("rm", "-f", "-v", CONTAINER)


@pytest.fixture
def vm_cleanup():
    yield
    _limactl("stop", "--force", VM)
    _limactl("delete", "--force", VM)


async def test_a_container_resolves_a_drawn_name_and_only_a_drawn_name(container_cleanup):
    assert shutil.which("docker"), "docker must be on PATH"
    runtime = ColimaRuntime()
    await runtime.stop(CONTAINER)
    await runtime.run_container(ContainerSpec(
        name=CONTAINER, image="alpine:3.20", command=("sleep", "300"),
        hosts={DRAWN: FIRST_IP},
    ))

    resolved = await runtime.exec_sh(CONTAINER, f"getent hosts {DRAWN} || echo NO-RESOLVE")
    assert FIRST_IP in resolved, resolved

    # THE NEGATIVE. A resolver that answers for everything -- a wildcard DNS,
    # a search domain, a test asserting on the wrong thing -- would pass the
    # assertion above while proving nothing at all.
    missing = await runtime.exec_sh(CONTAINER, f"getent hosts {UNDRAWN} || echo NO-RESOLVE")
    assert "NO-RESOLVE" in missing, missing


async def test_a_vm_resolves_at_boot_without_losing_its_own_hosts_file(vm_cleanup):
    assert shutil.which("limactl"), "limactl must be on PATH"
    vm = InstanceVm()
    await vm.boot(
        VM, get_instance_type("t3.micro"), hostname=VM, hosts={DRAWN: FIRST_IP},
    )

    resolved = _in_vm(f"getent hosts {DRAWN} || echo NO-RESOLVE")
    assert FIRST_IP in resolved.stdout, resolved.stdout

    # The guest's OWN entries survived. A `cat >` would have taken these with
    # it and broken resolution for everything except the name under test.
    hosts_file = _in_vm("cat /etc/hosts")
    assert "127.0.0.1" in hosts_file.stdout, hosts_file.stdout
    assert HOSTS_BEGIN in hosts_file.stdout, hosts_file.stdout
    assert _in_vm("getent hosts localhost").returncode == 0


async def test_an_edited_record_reaches_a_running_vm_without_rebooting_it(tmp_path, vm_cleanup):
    """The claim the whole in-place path exists for."""
    assert shutil.which("limactl"), "limactl must be on PATH"
    vm = InstanceVm()
    await vm.boot(VM, get_instance_type("t3.micro"), hostname=VM, hosts={DRAWN: FIRST_IP})
    before = _in_vm("cat /proc/sys/kernel/random/boot_id").stdout.strip()
    assert before, "could not read the guest's boot id"

    action = await vm.push_hosts(VM, tmp_path, "r53h-env", "i-1", {DRAWN: SECOND_IP})
    assert action == "pushed"

    resolved = _in_vm(f"getent hosts {DRAWN}")
    assert SECOND_IP in resolved.stdout, resolved.stdout
    assert FIRST_IP not in resolved.stdout, (
        f"the OLD address is still resolving -- the block was appended to, not rewritten:\n{resolved.stdout}"
    )

    # NOT A REBOOT. `boot_id` is regenerated on every boot, so an unchanged one
    # is real evidence the running kernel never went away. A push that quietly
    # restarted the VM would satisfy every other assertion here.
    after = _in_vm("cat /proc/sys/kernel/random/boot_id").stdout.strip()
    assert after == before, f"the VM rebooted: boot_id {before} -> {after}"

    # And a second push of the SAME set is free -- no churn on every Apply.
    assert await vm.push_hosts(VM, tmp_path, "r53h-env", "i-1", {DRAWN: SECOND_IP}) == "unchanged"


async def test_removing_the_record_stops_the_name_resolving(tmp_path, vm_cleanup):
    """The assertion an append-only writer fails. Every positive test above
    passes for a writer that only ever adds lines; this is the one that
    catches it, and a record you cannot withdraw is not a record."""
    assert shutil.which("limactl"), "limactl must be on PATH"
    vm = InstanceVm()
    await vm.boot(VM, get_instance_type("t3.micro"), hostname=VM, hosts={DRAWN: FIRST_IP})
    assert FIRST_IP in _in_vm(f"getent hosts {DRAWN}").stdout

    assert await vm.push_hosts(VM, tmp_path, "r53h-env", "i-1", {}) == "pushed"

    gone = _in_vm(f"getent hosts {DRAWN} || echo NO-RESOLVE")
    assert "NO-RESOLVE" in gone.stdout, gone.stdout
    # ...and the guest's own file is still intact after the removal.
    assert _in_vm("getent hosts localhost").returncode == 0
