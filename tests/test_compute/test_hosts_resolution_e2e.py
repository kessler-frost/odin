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
 4. **Removing the record empties the block immediately, and stops resolution
    WITHIN A BOUND.** An append-only writer passes every positive assertion
    above and fails this one, which is exactly why it is here.

    The bound is not a hedge, it is the contract odin publishes. Every record
    carries `ttl = 60` (`hcl.py::_DNS_RECORD_TTL`), so a resolver is entitled to
    keep answering for up to a minute -- real Route 53 defaults an A record to
    300s. This assertion originally demanded INSTANT withdrawal and failed for a
    measured ~2.2s while `systemd-resolved` served what /etc/hosts used to say:
    it was asserting a property odin deliberately did not build, ~27x stricter
    than its own TTL. The file half stays immediate and unbounded, because that
    part is odin's own work and nothing is entitled to delay it.

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
import time

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


# How long a withdrawn name may still resolve inside the VM before odin calls it
# broken. Chosen against the contract odin PUBLISHES, not against the number that
# happens to pass: every record carries `ttl = 60` (`hcl.py::_DNS_RECORD_TTL`),
# so a resolver may legitimately answer for up to a minute, and real Route 53
# defaults an A record to 300s. Measured on a real Lima VM the window is ~2.2s.
# 15s sits an order of magnitude above what was measured and well under the TTL:
# wide enough that a loaded machine cannot make it flap, tight enough that a
# writer which never withdraws anything (an append-only one) fails it outright.
_REMOVAL_BUDGET = 15.0


def _resolves_within(name: str, budget: float) -> bool:
    """Does `name` STILL resolve at the end of `budget` seconds? Polls so a
    quick convergence costs a fraction of a second, and returns the state at the
    deadline rather than the first sample -- the first sample is exactly what
    made the old assertion wrong."""
    deadline = time.monotonic() + budget
    while True:
        if "NO-RESOLVE" in _in_vm(f"getent hosts {name} || echo NO-RESOLVE").stdout:
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.5)


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

    # HALF ONE, and it is the half that proves ODIN did its job: the block is
    # empty the moment the push returns. Unbounded and immediate, because this
    # is a file odin writes and nothing else is entitled to delay it.
    block = _in_vm(f"sed -n '/^{HOSTS_BEGIN}$/,/^# ODIN-ROUTE53-END$/p' /etc/hosts").stdout
    assert DRAWN not in block, block
    assert _in_vm(f"grep -c {DRAWN} /etc/hosts || true").stdout.strip() == "0"

    # HALF TWO, BOUNDED -- and the bound is the point. This assertion used to
    # demand that resolution stop INSTANTLY, and it failed for a measured ~2.2s
    # while `systemd-resolved` served what /etc/hosts used to say.
    #
    # That was the wrong thing to assert. odin emits `ttl = 60` on every record
    # (`hcl.py::_DNS_RECORD_TTL`, pinned byte-for-byte by
    # `test_hcl_route53.py`), so a resolver is ENTITLED to keep answering for a
    # minute; real Route 53's own default for an A record is 300s. Demanding
    # instant withdrawal was asserting a property odin deliberately did not
    # build, roughly 27x stricter than the TTL it publishes.
    #
    # `_REMOVAL_BUDGET` is well inside that 60s and still far above the 2.2s
    # measured, so this fails loudly if removal genuinely breaks -- an
    # append-only writer never converges at all -- while no longer failing for a
    # cache behaving exactly as a cache should.
    assert _resolves_within(DRAWN, _REMOVAL_BUDGET) is False, (
        f"{DRAWN} still resolved {_REMOVAL_BUDGET}s after removal, which is beyond "
        f"anything the published 60s TTL can excuse"
    )
    # ...and the guest's own file is still intact after the removal.
    assert _in_vm("getent hosts localhost").returncode == 0
