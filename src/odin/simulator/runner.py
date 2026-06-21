"""Real local execution of a canvas: Lima VMs + Nebula, parallel to the Moto path.

Each EC2 node becomes a real Lima VM; each VPC gets a Nebula CA + overlay for
isolation; each Lambda runs as a container inside its VPC's host VM. This is
heavy (one Ubuntu VM per EC2), so it's a user-triggered "Simulate" action, not
the default deploy. Topology (which node is in which VPC) is read from the
canvas geometry, since the agent isn't in this path.
"""
from __future__ import annotations

import json
from pathlib import Path

from odin.api.canvas import CanvasGraph, node_reg_name
from odin.api.ws import ConnectionManager
from odin.compute.cloud_init import generate_cloud_init
from odin.compute.container_manager import ContainerManager
from odin.compute.lima_yaml import generate_lima_yaml
from odin.compute.models import get_instance_type
from odin.compute.vm_manager import VmManager
from odin.network.models import VpcOverlay
from odin.network.nebula_manager import NebulaManager
from odin.network.vpc_mapper import LIGHTHOUSE_FIREWALL
from odin.simulator.registry import ResourceRegistry

# Default node sizes (mirror the UI) for geometric VPC-containment checks.
_DEFAULT_SIZES = {
    "vpc": (560, 380), "subnet": (520, 280), "ec2": (220, 80),
    "lambda": (220, 80), "s3": (200, 60), "sg": (220, 80),
}


# Stateful AWS services → real container images for Simulate mode. Moto serves
# the AWS API for the validate/deploy path; Simulate runs these for real — e.g.
# S3 → RustFS (Apache-2.0), ElastiCache → Valkey. Images run via nerdctl in the
# host VM (native on Linux; via the Lima host VM on macOS). No new host deps.
SERVICE_CONTAINERS: dict[str, dict] = {
    "s3": {"image": "rustfs/rustfs:latest", "env": {"RUSTFS_ACCESS_KEY": "odin", "RUSTFS_SECRET_KEY": "odinpass"}},
    "rds": {"image": "postgres:16", "env": {"POSTGRES_PASSWORD": "odin"}},
    "dynamodb": {"image": "amazon/dynamodb-local:latest", "env": {}},
    "sqs": {"image": "softwaremill/elasticmq-native:latest", "env": {}},
    "elasticache": {"image": "valkey/valkey:8", "env": {}},
}


def _bbox(node: dict) -> tuple[float, float, float, float]:
    pos = node.get("position", {})
    size = node.get("size") or {}
    dw, dh = _DEFAULT_SIZES.get(node.get("type", ""), (200, 80))
    x, y = float(pos.get("x", 0)), float(pos.get("y", 0))
    return x, y, float(size.get("width", dw)), float(size.get("height", dh))


def _contains(outer: dict, inner: dict) -> bool:
    ox, oy, ow, oh = _bbox(outer)
    ix, iy, iw, ih = _bbox(inner)
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


class SimulationRunner:
    """Wires VmManager + ContainerManager + NebulaManager to run a canvas for real."""

    def __init__(
        self,
        vm_manager: VmManager,
        container_manager: ContainerManager,
        nebula_manager: NebulaManager,
        registry: ResourceRegistry,
        ws_manager: ConnectionManager | None = None,
        state_path: Path | str = ".odin/simulation.json",
    ) -> None:
        self._vm = vm_manager
        self._container = container_manager
        self._nebula = nebula_manager
        self._registry = registry
        self._ws = ws_manager
        self._state_path = Path(state_path)

    async def _broadcast(self, message: dict) -> None:
        if self._ws:
            await self._ws.broadcast(message)

    async def _mark(self, reg: str, status: str, event: str, error: str | None = None) -> None:
        if self._registry.get(reg) is None:
            self._registry.register(reg, service=reg.split("_", 1)[0], file_path="")
        self._registry.update_status(reg, status, error=error)
        message = {"type": event, "name": reg}
        if error:
            message["error"] = error
        await self._broadcast(message)

    async def simulate(self, graph: CanvasGraph) -> dict:
        """Create Nebula overlays, Lima VMs (EC2), and containers (Lambda)."""
        vpcs = [n for n in graph.nodes if n.get("type") == "vpc"]
        ec2s = [n for n in graph.nodes if n.get("type") == "ec2"]
        lambdas = [n for n in graph.nodes if n.get("type") == "lambda"]
        state: dict = {"vms": [], "containers": [], "vpcs": [], "hosts": {}}
        overlays: dict[str, VpcOverlay] = {}

        for v in vpcs:
            vpc_name = node_reg_name(v)[1]
            await self._nebula.create_ca(vpc_name)
            overlay = VpcOverlay(vpc_name=vpc_name)
            overlay.allocate_subnet("default")
            self._nebula.save_overlay(overlay)
            overlays[vpc_name] = overlay
            state["vpcs"].append(vpc_name)

        simulated: list[str] = []
        for node in ec2s:
            reg = node_reg_name(node)[1]
            await self._mark(reg, "simulating", "resource_simulating")
            vm_name = await self._create_ec2_vm(node, vpcs, overlays)
            state["vms"].append(vm_name)
            await self._mark(reg, "simulated", "resource_simulated")
            simulated.append(reg)

        for node in lambdas:
            reg = node_reg_name(node)[1]
            await self._mark(reg, "simulating", "resource_simulating")
            host, container = await self._run_lambda_container(node, vpcs, overlays, state)
            state["containers"].append({"vm": host, "name": container})
            await self._mark(reg, "simulated", "resource_simulated")
            simulated.append(reg)

        # Stateful services (S3→RustFS, RDS→Postgres, …) run as real containers.
        for node in [n for n in graph.nodes if n.get("type") in SERVICE_CONTAINERS]:
            reg = node_reg_name(node)[1]
            await self._mark(reg, "simulating", "resource_simulating")
            host, container = await self._run_service_container(node, vpcs, overlays, state)
            state["containers"].append({"vm": host, "name": container})
            await self._mark(reg, "simulated", "resource_simulated")
            simulated.append(reg)

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, indent=2))
        return {"simulated": simulated}

    def _vpc_of(self, node: dict, vpcs: list[dict]) -> str | None:
        for v in vpcs:
            if _contains(v, node):
                return node_reg_name(v)[1]
        return None

    async def _create_ec2_vm(self, node: dict, vpcs: list[dict], overlays: dict[str, VpcOverlay]) -> str:
        label, reg = node_reg_name(node)
        _, pub = self._vm.generate_ssh_keypair(reg)
        nebula_kwargs = await self._nebula_cloud_init(node, reg, label, vpcs, overlays, lighthouse=False)
        cloud_init = generate_cloud_init(hostname=label, ssh_pubkey=Path(pub).read_text(), **nebula_kwargs)
        instance_type = node.get("data", {}).get("instanceType", "t2.micro")
        yaml = generate_lima_yaml(
            get_instance_type(instance_type),
            cloud_init_script=cloud_init,
            install_nebula=bool(nebula_kwargs),
            # Shared (vmnet) networking is only needed for the Nebula mesh, and
            # it requires socket_vmnet. A standalone VM uses user-mode networking.
            shared_network=bool(nebula_kwargs),
        )
        await self._vm.create_vm_from_yaml(reg, yaml)
        await self._vm.start_vm(reg)
        return reg

    async def _ensure_host_vm(self, vpc_name: str, overlays: dict[str, VpcOverlay], state: dict) -> str:
        """A per-VPC host VM (with nerdctl) that Lambda containers run inside."""
        host = state["hosts"].get(vpc_name)
        if host:
            return host
        host = f"host_{vpc_name}" if vpc_name else "host_default"
        cloud_init = generate_cloud_init(hostname=host, install_nerdctl=True)
        yaml = generate_lima_yaml(get_instance_type("t2.small"), cloud_init_script=cloud_init, shared_network=True)
        await self._vm.create_vm_from_yaml(host, yaml)
        await self._vm.start_vm(host)
        state["hosts"][vpc_name] = host
        state["vms"].append(host)
        return host

    async def _run_lambda_container(
        self, node: dict, vpcs: list[dict], overlays: dict[str, VpcOverlay], state: dict
    ) -> tuple[str, str]:
        label, reg = node_reg_name(node)
        vpc_name = self._vpc_of(node, vpcs) or ""
        host = await self._ensure_host_vm(vpc_name, overlays, state)
        runtime = node.get("data", {}).get("runtime", "python3.12").replace("python", "python:")
        await self._container.run_container(
            vm_name=host,
            name=reg,
            image=f"{runtime}-slim",
            env={"HANDLER": node.get("data", {}).get("handler", "index.handler")},
            volumes=[],
        )
        return host, reg

    async def _run_service_container(
        self, node: dict, vpcs: list[dict], overlays: dict[str, VpcOverlay], state: dict
    ) -> tuple[str, str]:
        """Run a stateful service (S3→RustFS, RDS→Postgres, …) as a real container."""
        reg = node_reg_name(node)[1]
        spec = SERVICE_CONTAINERS[node.get("type", "")]
        vpc_name = self._vpc_of(node, vpcs) or ""
        host = await self._ensure_host_vm(vpc_name, overlays, state)
        await self._container.run_container(
            vm_name=host, name=reg, image=spec["image"], env=spec.get("env", {}), volumes=[],
        )
        return host, reg

    async def _nebula_cloud_init(
        self, node: dict, reg: str, label: str, vpcs: list[dict], overlays: dict[str, VpcOverlay], lighthouse: bool
    ) -> dict:
        """Build the Nebula cloud-init kwargs for a node in a VPC (empty if standalone)."""
        vpc_name = self._vpc_of(node, vpcs)
        if not vpc_name or vpc_name not in overlays:
            return {}
        overlay = overlays[vpc_name]
        subnet = overlay.get_subnet("default") or overlay.allocate_subnet("default")
        ip = subnet.allocate(reg)
        cert = await self._nebula.sign_cert(vpc_name, reg, f"{ip}/16", groups=[node.get("type", "node")])
        config = self._nebula.generate_config(
            lighthouse_ip=overlay.lighthouse_ip,
            lighthouse_underlay=overlay.lighthouse_underlay_ip or "",
            cert_paths=cert,
            firewall_rules=LIGHTHOUSE_FIREWALL,
            is_lighthouse=lighthouse,
        )
        return {
            "nebula_ca_crt": cert.ca_crt.read_text(),
            "nebula_host_crt": cert.crt.read_text(),
            "nebula_host_key": cert.key.read_text(),
            "nebula_config": config,
        }

    async def cleanup(self) -> dict:
        """Stop + delete every VM/container and revoke certs from the last simulate."""
        if not self._state_path.exists():
            return {"destroyed": []}
        state = json.loads(self._state_path.read_text())
        for c in state.get("containers", []):
            await self._container.stop_container(c["vm"], c["name"])
            await self._container.remove_container(c["vm"], c["name"])
        destroyed: list[str] = []
        for vm_name in state.get("vms", []):
            await self._vm.delete_vm(vm_name)  # --force stops + deletes in one step
            destroyed.append(vm_name)
        for entry in self._registry.list_all():
            if entry.status in ("simulating", "simulated"):
                self._registry.update_status(entry.name, "draft", error=None)
                await self._broadcast({"type": "resource_draft", "name": entry.name})
        self._state_path.unlink(missing_ok=True)
        return {"destroyed": destroyed}
