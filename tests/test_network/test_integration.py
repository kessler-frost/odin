from __future__ import annotations

import asyncio

import pytest

from odin.compute.cloud_init import generate_cloud_init
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import get_instance_type
from odin.compute.vm_manager import VmManager
from odin.network.models import FirewallRule, FirewallRules, VpcOverlay
from odin.network.nebula_manager import NebulaManager
from odin.network.vpc_mapper import LIGHTHOUSE_FIREWALL

pytestmark = pytest.mark.integration


async def test_two_vms_can_ping_over_nebula():
    """End-to-end: create VPC with lighthouse, deploy two VMs, verify overlay connectivity."""
    vm = VmManager()
    nebula = NebulaManager()
    vpc_name = "integration-vpc"

    # Create CA
    ca_info = await nebula.create_ca(vpc_name)
    overlay = VpcOverlay(vpc_name=vpc_name)

    # Sign and deploy lighthouse
    lighthouse_name = f"lighthouse-{vpc_name}"
    lh_cert = await nebula.sign_cert(vpc_name, lighthouse_name, f"{overlay.lighthouse_ip}/16")
    lh_config = nebula.generate_config(
        lighthouse_ip=overlay.lighthouse_ip,
        lighthouse_underlay="",
        cert_paths=lh_cert,
        firewall_rules=LIGHTHOUSE_FIREWALL,
        is_lighthouse=True,
    )
    lh_cloud_init = generate_cloud_init(
        hostname=lighthouse_name,
        nebula_ca_crt=ca_info.ca_crt.read_text(),
        nebula_host_crt=lh_cert.crt.read_text(),
        nebula_host_key=lh_cert.key.read_text(),
        nebula_config=lh_config,
    )
    lh_yaml = generate_lima_yaml(
        get_instance_type("t2.micro"),
        cloud_init_script=lh_cloud_init,
        install_nebula=True,
        shared_network=True,
    )
    await vm.create_vm_from_yaml(lighthouse_name, lh_yaml)
    await vm.start_vm(lighthouse_name)
    await asyncio.sleep(10)

    lh_underlay = await vm.get_vm_network_ip(lighthouse_name)
    overlay.lighthouse_underlay_ip = lh_underlay

    # Allocate subnet
    subnet = overlay.allocate_subnet("subnet-test")

    # Deploy two VMs
    for name in ["vm-a", "vm-b"]:
        ip = subnet.allocate(name)
        cert = await nebula.sign_cert(vpc_name, name, f"{ip}/24")
        fw = FirewallRules(
            inbound=[FirewallRule(port="any", proto="any")],
            outbound=[FirewallRule(port="any", proto="any")],
        )
        config = nebula.generate_config(
            lighthouse_ip=overlay.lighthouse_ip,
            lighthouse_underlay=lh_underlay,
            cert_paths=cert,
            firewall_rules=fw,
        )
        ci = generate_cloud_init(
            hostname=name,
            nebula_ca_crt=ca_info.ca_crt.read_text(),
            nebula_host_crt=cert.crt.read_text(),
            nebula_host_key=cert.key.read_text(),
            nebula_config=config,
        )
        yaml_str = generate_lima_yaml(
            get_instance_type("t2.micro"),
            cloud_init_script=ci,
            install_nebula=True,
            shared_network=True,
        )
        await vm.create_vm_from_yaml(name, yaml_str)
        await vm.start_vm(name)

    await asyncio.sleep(15)

    # Verify VM-A can ping VM-B over Nebula overlay
    vm_b_ip = subnet.assignments["vm-b"]
    output = await vm.exec_in_vm("vm-a", f"ping -c 2 -W 5 {vm_b_ip}")
    assert "2 received" in output or "2 packets received" in output

    # Cleanup
    for name in ["vm-a", "vm-b", lighthouse_name]:
        await vm.stop_vm(name)
        await vm.delete_vm(name)
