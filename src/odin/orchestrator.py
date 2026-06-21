"""Drives validate / deploy / destroy through Terraform against the Moto server.

Validate runs `tofu plan`, deploy runs `tofu apply`, destroy runs `tofu destroy`
— all against the local Moto server. The Lima/Nebula/container modules are
parked (a future "Simulate" feature), not wired in here.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from odin.agent.client import AgentEvent, OdinAgent
from odin.api.canvas import (
    NODE_AWS_TYPE,
    CanvasGraph,
    hcl_name,
    node_reg_name,
    node_tf_address,
)
from odin.api.ws import ConnectionManager
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry
from odin.simulator.runner import SimulationRunner
from odin.terraform.runner import PlanResult, TofuRunner


def _error_text(result: PlanResult) -> str | None:
    summaries = [
        d.get("summary", "") for d in result.diagnostics if d.get("severity") == "error"
    ]
    return "; ".join(s for s in summaries if s) or None


def _addr_matches(diag_addr: str, node_addr: str) -> bool:
    """Whether a tofu diagnostic address names a node's resource.

    Matches the exact resource address or an indexed instance of it
    (count/for_each), e.g. `aws_instance.web[0]`. A plain prefix is NOT a
    match, so a failing `aws_instance.web2` never taints `aws_instance.web`.
    """
    return diag_addr == node_addr or diag_addr.startswith(node_addr + "[")


def _entry_tf_address(entry) -> str | None:
    """The Terraform address for a registry entry, e.g. `aws_s3_bucket.data`.

    A registry name is `{type}_{label}` and `service` is the node type, so the
    label is the name with the `{service}_` prefix stripped.
    """
    aws_type = NODE_AWS_TYPE.get(entry.service)
    if not aws_type:
        return None
    label = entry.name[len(entry.service) + 1:]
    return f"{aws_type}.{hcl_name(label)}"


class Orchestrator:
    """Wires the agent, registry, Moto engine, and Terraform runner together."""

    def __init__(
        self,
        engine: MotoEngine,
        registry: ResourceRegistry,
        runner: TofuRunner,
        ws_manager: ConnectionManager | None = None,
        agent: OdinAgent | None = None,
        simulation: SimulationRunner | None = None,
    ) -> None:
        self.engine = engine
        self.registry = registry
        self._runner = runner
        self._ws = ws_manager
        self._agent = agent
        self._simulation = simulation

    def start(self) -> None:
        """Start the Moto server and reset tofu state to match its empty world."""
        self.engine.start()
        for name in ("terraform.tfstate", "terraform.tfstate.backup"):
            (self._runner.work_dir / name).unlink(missing_ok=True)

    def stop(self) -> None:
        self.engine.stop()

    async def validate(self, graph: CanvasGraph) -> AsyncIterator[AgentEvent]:
        """Mark nodes validating, run the agent, then plan and map status back."""
        reg_names = self._register_validating(graph)
        await self._prune_stale(reg_names)
        for reg_name in reg_names:
            await self._broadcast({"type": "resource_validating", "name": reg_name})

        if self._agent is None or not self._agent.is_running:
            for reg_name in reg_names:
                self.registry.update_status(reg_name, "error", error="Agent not connected")
                await self._broadcast(
                    {"type": "resource_error", "name": reg_name, "error": "Agent not connected"}
                )
            return

        async for event in self._agent.validate(graph):
            await self._broadcast(event.model_dump())
            yield event

        await self._finalize(graph, reg_names)

    async def _prune_stale(self, current: list[str]) -> None:
        """Drop registry entries for canvas nodes that no longer exist.

        The canvas is the source of truth; without this, deleting a node leaves
        a phantom resource in the registry (and `/state`) until a full reset.
        """
        keep = set(current)
        for entry in self.registry.list_all():
            if entry.name not in keep:
                self.registry.remove(entry.name)
                await self._broadcast({"type": "resource_removed", "name": entry.name})

    def _register_validating(self, graph: CanvasGraph) -> list[str]:
        reg_names: list[str] = []
        for node in graph.nodes:
            label, reg_name = node_reg_name(node)
            if not label:
                continue
            reg_names.append(reg_name)
            if self.registry.get(reg_name) is None:
                self.registry.register(reg_name, service=node.get("type", ""), file_path="")
            self.registry.update_status(reg_name, "validating")
        return reg_names

    async def _finalize(self, graph: CanvasGraph, reg_names: list[str]) -> None:
        if not reg_names:
            return

        validated = await self._runner.validate()
        planned = await self._runner.plan() if validated.ok else None
        diagnostics = validated.diagnostics + (planned.diagnostics if planned else [])
        errors = [d for d in diagnostics if d.get("severity") == "error"]

        addr_to_reg = {
            node_tf_address(node): reg
            for node, reg in ((n, node_reg_name(n)[1]) for n in graph.nodes)
            if node_tf_address(node)
        }

        # Attribute each error to the node(s) it names, collecting per-node
        # messages. Errors we can't attribute to any node are "unmatched".
        per_node: dict[str, list[str]] = {}
        unmatched: list[str] = []
        for d in errors:
            summary = d.get("summary", "") or "validation error"
            matched = [
                reg for addr, reg in addr_to_reg.items()
                if _addr_matches(d.get("address", ""), addr)
            ]
            for reg in matched:
                per_node.setdefault(reg, []).append(summary)
            if not matched:
                unmatched.append(summary)

        # If errors exist but none could be attributed, the config is broken as
        # a whole — blame every node with the unattributed messages.
        global_msgs = unmatched if (errors and not per_node) else []

        for reg_name in reg_names:
            messages = per_node.get(reg_name, []) + global_msgs
            if messages:
                text = "; ".join(dict.fromkeys(messages))
                self.registry.update_status(reg_name, "error", error=text)
                await self._broadcast({"type": "resource_error", "name": reg_name, "error": text})
            else:
                self.registry.update_status(reg_name, "validated", error=None)
                await self._broadcast({"type": "resource_validated", "name": reg_name})

    async def deploy(self, resource_name: str) -> None:
        entry = self.registry.get(resource_name)
        if not entry or entry.status not in ("validated", "live"):
            return
        result = await self._runner.apply()
        await self._apply_status(result, only=resource_name)

    async def deploy_all(self) -> list[str]:
        result = await self._runner.apply()
        return await self._apply_status(result)

    async def _apply_status(self, result: PlanResult, only: str | None = None) -> list[str]:
        """Map a `tofu apply` result to per-resource status.

        A resource is `live` if it landed in tofu state and no error names it;
        `error` (with only its own message) if a diagnostic names it; otherwise
        it wasn't applied (e.g. skipped after an upstream failure) and is left
        untouched. This keeps one resource's failure from tainting the rest.
        """
        errors = [d for d in result.diagnostics if d.get("severity") == "error"]
        state_addrs = set(await self._runner.state_list())
        deployed: list[str] = []
        for entry in self.registry.list_all():
            if entry.status not in ("validated", "live"):
                continue
            if only is not None and entry.name != only:
                continue
            addr = _entry_tf_address(entry)
            messages = [
                d.get("summary", "") or "deploy error"
                for d in errors
                if addr and _addr_matches(d.get("address", ""), addr)
            ]
            if messages:
                text = "; ".join(dict.fromkeys(messages))
                self.registry.update_status(entry.name, "error", error=text)
                await self._broadcast({"type": "resource_error", "name": entry.name, "error": text})
            elif addr in state_addrs:
                self.registry.update_status(entry.name, "live", error=None)
                await self._broadcast({"type": "resource_live", "name": entry.name})
                deployed.append(entry.name)
        return deployed

    async def destroy(self, resource_name: str) -> None:
        entry = self.registry.get(resource_name)
        if not entry or entry.status not in ("live", "validated", "error"):
            return
        self.registry.update_status(resource_name, "draft", error=None)
        await self._broadcast({"type": "resource_draft", "name": resource_name})

    async def destroy_all(self) -> list[str]:
        await self._runner.destroy()
        destroyed: list[str] = []
        for entry in self.registry.list_all():
            if entry.status in ("live", "validated", "error"):
                self.registry.update_status(entry.name, "draft", error=None)
                await self._broadcast({"type": "resource_draft", "name": entry.name})
                destroyed.append(entry.name)
        return destroyed

    async def simulate(self, graph: CanvasGraph) -> dict:
        """Run the canvas for real (Lima VMs + Nebula), not against Moto."""
        if self._simulation is None:
            return {"simulated": [], "error": "Simulation runner not available"}
        return await self._simulation.simulate(graph)

    async def simulate_destroy(self) -> dict:
        """Tear down everything the last simulate created."""
        if self._simulation is None:
            return {"destroyed": []}
        return await self._simulation.cleanup()

    async def _broadcast(self, message: dict) -> None:
        if self._ws:
            await self._ws.broadcast(message)
