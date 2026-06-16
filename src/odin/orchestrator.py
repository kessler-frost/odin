from __future__ import annotations

from pathlib import Path

from odin.api.ws import ConnectionManager
from odin.compute.cloud_init import generate_cloud_init
from odin.compute.container_manager import ContainerManager
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import get_instance_type
from odin.compute.vm_manager import VmManager
from odin.network.models import FirewallRule, FirewallRules, VpcOverlay
from odin.network.nebula_manager import NebulaManager
from odin.network.vpc_mapper import LIGHTHOUSE_FIREWALL, sg_rules_to_firewall
from odin.simulator.engine import MotoEngine
from odin.simulator.executor import Executor
from odin.simulator.registry import ResourceRegistry

COMPUTE_SERVICES = frozenset({"ec2", "lambda"})
METADATA_SERVICES = frozenset({"ec2", "vpc", "subnet", "sg", "lambda"})
NETWORK_SERVICES = frozenset({"vpc", "subnet", "sg"})


class Orchestrator:
    """Wires together the executor, registry, moto engine, and VM manager."""

    def __init__(
        self,
        infra_dir: Path,
        ws_manager: ConnectionManager | None = None,
        vm_manager: VmManager | None = None,
        nebula_manager: NebulaManager | None = None,
        container_manager: ContainerManager | None = None,
    ) -> None:
        self._infra_dir = infra_dir
        registry_path = infra_dir.parent / "registry.json"

        self.engine = MotoEngine()
        self.registry = ResourceRegistry(registry_path)
        self._executor = Executor(self.engine)
        self._ws = ws_manager
        self._vm = vm_manager or VmManager()
        self._nebula = nebula_manager or NebulaManager()
        self._container = container_manager or ContainerManager()
        self._container_hosts: dict[str, str] = {}  # vpc_resource -> container-host VM name

    def start_engine(self) -> None:
        self.engine.start()

    def stop_engine(self) -> None:
        self.engine.stop()

    def _snapshot_resource_ids(self, service: str) -> set[str]:
        """Snapshot existing moto resource IDs before execution."""
        if service not in METADATA_SERVICES:
            return set()
        ec2 = self.engine.get_client("ec2")
        if service == "vpc":
            return {v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"]}
        if service == "subnet":
            return {s["SubnetId"] for s in ec2.describe_subnets()["Subnets"]}
        if service == "sg":
            return {sg["GroupId"] for sg in ec2.describe_security_groups()["SecurityGroups"]}
        if service == "ec2":
            ids: set[str] = set()
            for r in ec2.describe_instances()["Reservations"]:
                for i in r["Instances"]:
                    ids.add(i["InstanceId"])
            return ids
        if service == "lambda":
            lam = self.engine.get_client("lambda")
            return {f["FunctionName"] for f in lam.list_functions()["Functions"]}
        return set()

    def _capture_new_metadata(self, service: str, pre_ids: set[str]) -> dict:
        """Diff moto state to find newly created resources and return metadata."""
        if service not in METADATA_SERVICES:
            return {}
        ec2 = self.engine.get_client("ec2")
        if service == "vpc":
            for vpc in ec2.describe_vpcs()["Vpcs"]:
                if vpc["VpcId"] not in pre_ids:
                    return {"vpc_id": vpc["VpcId"], "cidr_block": vpc["CidrBlock"]}
        elif service == "subnet":
            for subnet in ec2.describe_subnets()["Subnets"]:
                if subnet["SubnetId"] not in pre_ids:
                    return {"subnet_id": subnet["SubnetId"], "vpc_id": subnet["VpcId"], "cidr_block": subnet["CidrBlock"]}
        elif service == "sg":
            for sg in ec2.describe_security_groups()["SecurityGroups"]:
                if sg["GroupId"] not in pre_ids:
                    return {"group_id": sg["GroupId"], "vpc_id": sg.get("VpcId", ""), "group_name": sg["GroupName"]}
        elif service == "ec2":
            for r in ec2.describe_instances()["Reservations"]:
                for inst in r["Instances"]:
                    if inst["InstanceId"] not in pre_ids:
                        return {
                            "instance_id": inst["InstanceId"],
                            "instance_type": inst.get("InstanceType", "t2.micro"),
                            "vpc_id": inst.get("VpcId"),
                            "subnet_id": inst.get("SubnetId"),
                            "security_groups": [sg["GroupId"] for sg in inst.get("SecurityGroups", [])],
                        }
        elif service == "lambda":
            lam = self.engine.get_client("lambda")
            for fn in lam.list_functions()["Functions"]:
                if fn["FunctionName"] not in pre_ids:
                    return {
                        "function_name": fn["FunctionName"],
                        "function_arn": fn["FunctionArn"],
                        "runtime": fn.get("Runtime", ""),
                        "handler": fn.get("Handler", ""),
                        "role": fn.get("Role", ""),
                        "timeout": fn.get("Timeout", 3),
                        "memory_size": fn.get("MemorySize", 128),
                    }
        return {}

    def _find_vpc_resource(self, moto_vpc_id: str) -> str | None:
        """Find the Odin resource name for a moto VPC ID."""
        for entry in self.registry.list_by_service("vpc"):
            if entry.metadata.get("vpc_id") == moto_vpc_id:
                return entry.name
        return None

    def _find_subnet_resource(self, moto_subnet_id: str) -> str | None:
        """Find the Odin resource name for a moto subnet ID."""
        for entry in self.registry.list_by_service("subnet"):
            if entry.metadata.get("subnet_id") == moto_subnet_id:
                return entry.name
        return None

    async def _deploy_vpc(self, resource_name: str) -> None:
        """Create Nebula CA and lighthouse VM for a VPC."""
        ca_info = await self._nebula.create_ca(resource_name)

        overlay = VpcOverlay(vpc_name=resource_name)

        lighthouse_name = f"lighthouse-{resource_name}"
        cert_paths = await self._nebula.sign_cert(
            resource_name, lighthouse_name, f"{overlay.lighthouse_ip}/16",
        )

        nebula_config = self._nebula.generate_config(
            lighthouse_ip=overlay.lighthouse_ip,
            lighthouse_underlay="",
            cert_paths=cert_paths,
            firewall_rules=LIGHTHOUSE_FIREWALL,
            is_lighthouse=True,
        )

        cloud_init = generate_cloud_init(
            hostname=lighthouse_name,
            nebula_ca_crt=ca_info.ca_crt.read_text(),
            nebula_host_crt=cert_paths.crt.read_text(),
            nebula_host_key=cert_paths.key.read_text(),
            nebula_config=nebula_config,
        )

        config = get_instance_type("t2.micro")
        lima_yaml = generate_lima_yaml(config, cloud_init_script=cloud_init, install_nebula=True, shared_network=True)

        await self._vm.create_vm_from_yaml(lighthouse_name, lima_yaml)
        await self._vm.start_vm(lighthouse_name)

        underlay_ip = await self._vm.get_vm_network_ip(lighthouse_name)
        overlay.lighthouse_underlay_ip = underlay_ip

        self._nebula.save_overlay(overlay)

    async def _deploy_subnet(self, resource_name: str) -> None:
        """Allocate a /24 CIDR range in the VPC's overlay."""
        entry = self.registry.get(resource_name)
        moto_vpc_id = entry.metadata.get("vpc_id", "")
        vpc_resource = self._find_vpc_resource(moto_vpc_id)
        if not vpc_resource:
            return

        overlay = self._nebula.load_overlay(vpc_resource)
        if not overlay:
            return

        overlay.allocate_subnet(resource_name)
        self._nebula.save_overlay(overlay)

    async def _ensure_container_host(self, vpc_resource: str) -> str:
        """Create or return existing container-host VM for a VPC."""
        if vpc_resource in self._container_hosts:
            return self._container_hosts[vpc_resource]

        host_name = f"container-host-{vpc_resource}"
        overlay = self._nebula.load_overlay(vpc_resource)

        # Sign Nebula cert for container host (use .254 address in base range)
        cert_paths = await self._nebula.sign_cert(
            vpc_resource, host_name, f"{overlay.lighthouse_ip.rsplit('.', 1)[0]}.254/16",
        )

        firewall_rules = FirewallRules(
            inbound=[FirewallRule(port="any", proto="any")],
            outbound=[FirewallRule(port="any", proto="any")],
        )
        nebula_config = self._nebula.generate_config(
            lighthouse_ip=overlay.lighthouse_ip,
            lighthouse_underlay=overlay.lighthouse_underlay_ip or "",
            cert_paths=cert_paths,
            firewall_rules=firewall_rules,
        )

        vm_name = self._vm._vm_name(host_name)
        _, public_key = self._vm.generate_ssh_keypair(vm_name)

        cloud_init = generate_cloud_init(
            hostname=host_name,
            ssh_pubkey=public_key.read_text().strip(),
            nebula_ca_crt=cert_paths.ca_crt.read_text(),
            nebula_host_crt=cert_paths.crt.read_text(),
            nebula_host_key=cert_paths.key.read_text(),
            nebula_config=nebula_config,
            install_nerdctl=True,
        )

        config = get_instance_type("t2.medium")
        lima_yaml = generate_lima_yaml(
            config, cloud_init_script=cloud_init,
            install_nebula=True, shared_network=True,
        )

        await self._vm.create_vm_from_yaml(host_name, lima_yaml)
        await self._vm.start_vm(host_name)

        self._container_hosts[vpc_resource] = vm_name
        return vm_name

    async def _deploy_lambda(self, resource_name: str) -> None:
        """Deploy a Lambda function as a container in the VPC's container-host VM."""
        entry = self.registry.get(resource_name)
        moto_vpc_id = entry.metadata.get("vpc_id")

        if not moto_vpc_id:
            # Plain Lambda — no VPC, no container host needed (API-level only)
            return

        vpc_resource = self._find_vpc_resource(moto_vpc_id)
        overlay = self._nebula.load_overlay(vpc_resource)

        # Ensure container-host VM exists
        container_host_vm = await self._ensure_container_host(vpc_resource)

        # Allocate overlay IP for this Lambda
        moto_subnet_id = entry.metadata.get("subnet_id")
        subnet_resource = self._find_subnet_resource(moto_subnet_id)
        subnet_alloc = overlay.get_subnet(subnet_resource)
        overlay_ip = subnet_alloc.allocate(resource_name)
        self._nebula.save_overlay(overlay)

        # Sign Nebula cert
        mask = subnet_alloc.cidr.split("/")[1]
        await self._nebula.sign_cert(
            vpc_resource, resource_name, f"{overlay_ip}/{mask}",
        )

        # Copy Dockerfile to container-host VM
        dockerfile_dir = self._infra_dir / resource_name
        function_name = entry.metadata.get("function_name", resource_name)
        image_tag = f"lambda-{function_name}:latest"
        remote_build_dir = f"/tmp/{resource_name}"

        await self._container.copy_to_vm(
            container_host_vm, str(dockerfile_dir), remote_build_dir,
        )

        # Build image
        await self._container.build_image(container_host_vm, remote_build_dir, image_tag)

        # Run container with env vars
        env = {
            "AWS_LAMBDA_FUNCTION_NAME": function_name,
            "AWS_LAMBDA_FUNCTION_HANDLER": entry.metadata.get("handler", ""),
            "AWS_LAMBDA_FUNCTION_TIMEOUT": str(entry.metadata.get("timeout", 30)),
            "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": str(entry.metadata.get("memory_size", 128)),
        }

        container_name = f"lambda-{function_name}"
        container_id = await self._container.run_container(
            container_host_vm, container_name, image_tag, env=env,
        )

        # Update metadata with deployment info
        entry.metadata.update({
            "container_id": container_id,
            "overlay_ip": overlay_ip,
            "container_host_vm": container_host_vm,
            "container_name": container_name,
        })
        self.registry.update_status(resource_name, "deploying", metadata=entry.metadata)

    def _get_firewall_rules(self, sg_ids: list[str]) -> FirewallRules:
        """Look up security group rules from moto and translate to Nebula firewall."""
        if not sg_ids:
            return FirewallRules(
                inbound=[FirewallRule(port="any", proto="any")],
                outbound=[FirewallRule(port="any", proto="any")],
            )
        ec2 = self.engine.get_client("ec2")
        response = ec2.describe_security_groups(GroupIds=sg_ids)
        all_permissions: list[dict] = []
        for sg in response["SecurityGroups"]:
            all_permissions.extend(sg.get("IpPermissions", []))
        return sg_rules_to_firewall(all_permissions)

    async def _deploy_ec2(self, resource_name: str) -> None:
        """Deploy an EC2 resource — with Nebula if in a VPC, plain otherwise."""
        entry = self.registry.get(resource_name)
        moto_vpc_id = entry.metadata.get("vpc_id")

        if not moto_vpc_id:
            # Plain EC2 — no VPC, no Nebula
            instance_type = entry.metadata.get("instance_type", "t2.micro")
            await self._vm.create_vm(resource_name, instance_type=instance_type)
            await self._vm.start_vm(resource_name)
            return

        # EC2 in VPC — provision with Nebula
        vpc_resource = self._find_vpc_resource(moto_vpc_id)
        overlay = self._nebula.load_overlay(vpc_resource)

        # Allocate overlay IP from subnet
        moto_subnet_id = entry.metadata.get("subnet_id")
        subnet_resource = self._find_subnet_resource(moto_subnet_id)
        subnet_alloc = overlay.get_subnet(subnet_resource)
        overlay_ip = subnet_alloc.allocate(resource_name)
        self._nebula.save_overlay(overlay)

        # Sign Nebula host cert
        mask = subnet_alloc.cidr.split("/")[1]
        cert_paths = await self._nebula.sign_cert(
            vpc_resource, resource_name, f"{overlay_ip}/{mask}",
        )

        # Get firewall rules from moto security groups
        sg_ids = entry.metadata.get("security_groups", [])
        firewall_rules = self._get_firewall_rules(sg_ids)

        # Generate Nebula config
        nebula_config = self._nebula.generate_config(
            lighthouse_ip=overlay.lighthouse_ip,
            lighthouse_underlay=overlay.lighthouse_underlay_ip or "",
            cert_paths=cert_paths,
            firewall_rules=firewall_rules,
        )

        # Generate SSH keypair
        vm_name = self._vm._vm_name(resource_name)
        _, public_key = self._vm.generate_ssh_keypair(vm_name)
        ssh_pubkey = public_key.read_text().strip()

        # Generate cloud-init with Nebula
        cloud_init = generate_cloud_init(
            hostname=resource_name,
            ssh_pubkey=ssh_pubkey,
            nebula_ca_crt=cert_paths.ca_crt.read_text(),
            nebula_host_crt=cert_paths.crt.read_text(),
            nebula_host_key=cert_paths.key.read_text(),
            nebula_config=nebula_config,
        )

        # Generate Lima YAML with Nebula and shared networking
        instance_type = entry.metadata.get("instance_type", "t2.micro")
        config = get_instance_type(instance_type)
        lima_yaml = generate_lima_yaml(
            config, cloud_init_script=cloud_init,
            install_nebula=True, shared_network=True,
        )

        await self._vm.create_vm_from_yaml(resource_name, lima_yaml)
        await self._vm.start_vm(resource_name)

    async def deploy(self, resource_name: str) -> None:
        entry = self.registry.get(resource_name)
        if not entry or entry.status not in ("validated",):
            return

        self.registry.update_status(resource_name, "deploying")
        await self._broadcast({"type": "resource_deploying", "name": resource_name})

        if entry.service == "vpc":
            await self._deploy_vpc(resource_name)
        elif entry.service == "subnet":
            await self._deploy_subnet(resource_name)
        elif entry.service == "lambda":
            await self._deploy_lambda(resource_name)
        elif entry.service in COMPUTE_SERVICES:
            await self._deploy_ec2(resource_name)

        self.registry.update_status(resource_name, "live")
        await self._broadcast({"type": "resource_live", "name": resource_name})

    DEPLOY_ORDER = ["vpc", "subnet", "sg", "ec2", "lambda"]

    async def deploy_all(self) -> list[str]:
        deployed: list[str] = []
        # Deploy in dependency order
        for service in self.DEPLOY_ORDER:
            for entry in self.registry.list_by_service(service):
                if entry.status == "validated":
                    await self.deploy(entry.name)
                    deployed.append(entry.name)
        # Deploy remaining services (s3, iam, etc.)
        for entry in self.registry.list_all():
            if entry.status == "validated" and entry.name not in deployed:
                await self.deploy(entry.name)
                deployed.append(entry.name)
        return deployed

    async def destroy(self, resource_name: str) -> None:
        entry = self.registry.get(resource_name)
        if not entry or entry.status not in ("live", "validated", "error"):
            return

        await self._broadcast({"type": "resource_destroying", "name": resource_name})

        # For live resources, tear down real infra (VMs, containers, Nebula)
        if entry.status == "live":
            if entry.service == "vpc":
                await self._destroy_vpc(resource_name)
            elif entry.service == "lambda":
                await self._destroy_lambda(resource_name)
            elif entry.service in COMPUTE_SERVICES:
                await self._destroy_ec2(resource_name)

        # Always reset to draft
        self.registry.update_status(resource_name, "draft", error=None)
        await self._broadcast({"type": "resource_draft", "name": resource_name})

    async def _destroy_vpc(self, resource_name: str) -> None:
        """Delete lighthouse VM, container-host VM, and clean up Nebula CA."""
        lighthouse_name = f"lighthouse-{resource_name}"
        await self._vm.delete_vm(lighthouse_name)

        if resource_name in self._container_hosts:
            await self._vm.delete_vm(f"container-host-{resource_name}")
            del self._container_hosts[resource_name]

    async def _destroy_ec2(self, resource_name: str) -> None:
        """Delete Lima VM and revoke Nebula cert if in VPC."""
        entry = self.registry.get(resource_name)
        moto_vpc_id = entry.metadata.get("vpc_id")

        await self._vm.delete_vm(resource_name)

        if moto_vpc_id:
            vpc_resource = self._find_vpc_resource(moto_vpc_id)
            if vpc_resource:
                await self._nebula.revoke_cert(vpc_resource, resource_name)
                overlay = self._nebula.load_overlay(vpc_resource)
                if overlay:
                    moto_subnet_id = entry.metadata.get("subnet_id")
                    subnet_resource = self._find_subnet_resource(moto_subnet_id)
                    subnet_alloc = overlay.get_subnet(subnet_resource)
                    if subnet_alloc:
                        subnet_alloc.release(resource_name)
                        self._nebula.save_overlay(overlay)

    async def _destroy_lambda(self, resource_name: str) -> None:
        """Stop and remove a Lambda container, revoke cert, release IP."""
        entry = self.registry.get(resource_name)
        container_host = entry.metadata.get("container_host_vm", "")
        container_name = entry.metadata.get("container_name", "")

        await self._container.stop_container(container_host, container_name)
        await self._container.remove_container(container_host, container_name)

        # Revoke Nebula cert and release overlay IP
        moto_vpc_id = entry.metadata.get("vpc_id")
        if moto_vpc_id:
            vpc_resource = self._find_vpc_resource(moto_vpc_id)
            if vpc_resource:
                await self._nebula.revoke_cert(vpc_resource, resource_name)
                overlay = self._nebula.load_overlay(vpc_resource)
                if overlay:
                    moto_subnet_id = entry.metadata.get("subnet_id")
                    subnet_resource = self._find_subnet_resource(moto_subnet_id)
                    subnet_alloc = overlay.get_subnet(subnet_resource)
                    if subnet_alloc:
                        subnet_alloc.release(resource_name)
                        self._nebula.save_overlay(overlay)

    async def invoke_lambda(self, resource_name: str, payload: str = "") -> str:
        """Invoke a deployed Lambda by executing its handler in the container."""
        entry = self.registry.get(resource_name)
        container_host = entry.metadata.get("container_host_vm", "")
        container_name = entry.metadata.get("container_name", "")
        handler = entry.metadata.get("handler", "")

        # Execute handler in the container, passing payload via env var
        command = f"python -c \"import json, os; payload={payload!r}; exec(open('{handler.split('.')[0]}.py').read()); print(json.dumps({handler.split('.')[-1]}(json.loads(payload), {{}})))\""

        return await self._container.exec_in_container(
            container_host, container_name, command,
        )

    async def _broadcast(self, message: dict) -> None:
        if self._ws:
            await self._ws.broadcast(message)
