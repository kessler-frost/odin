from __future__ import annotations

import asyncio

import pytest

from odin.compute.vm_manager import VmManager

pytestmark = pytest.mark.integration


async def test_create_and_delete_real_vm():
    """End-to-end: create a t2.micro VM, verify it exists, delete it."""
    vm = VmManager()

    await vm.create_vm("integration-test", instance_type="t2.micro")
    await vm.start_vm("integration-test")

    # Give VM time to boot
    await asyncio.sleep(5)

    info = await vm.get_vm("integration-test")
    assert info is not None
    assert info.status == "Running"
    assert info.ssh_port is not None

    # Verify we can exec inside
    output = await vm.exec_in_vm("integration-test", "hostname")
    assert "integration-test" in output

    await vm.stop_vm("integration-test")
    await vm.delete_vm("integration-test")

    info = await vm.get_vm("integration-test")
    assert info is None
