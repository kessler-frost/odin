"""Drives validate / deploy / destroy through Terraform against the Moto server.

Validate runs `tofu plan`, deploy runs `tofu apply`, destroy runs `tofu destroy`
— all against the local Moto server. The Lima/Nebula/container modules are
parked (a future "Simulate" feature), not wired in here.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from odin.agent.client import AgentEvent, OdinAgent
from odin.api.canvas import CanvasGraph, node_reg_name, node_tf_address
from odin.api.ws import ConnectionManager
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry
from odin.terraform.runner import PlanResult, TofuRunner


def _error_text(result: PlanResult) -> str | None:
    summaries = [
        d.get("summary", "") for d in result.diagnostics if d.get("severity") == "error"
    ]
    return "; ".join(s for s in summaries if s) or None


class Orchestrator:
    """Wires the agent, registry, Moto engine, and Terraform runner together."""

    def __init__(
        self,
        engine: MotoEngine,
        registry: ResourceRegistry,
        runner: TofuRunner,
        ws_manager: ConnectionManager | None = None,
        agent: OdinAgent | None = None,
    ) -> None:
        self.engine = engine
        self.registry = registry
        self._runner = runner
        self._ws = ws_manager
        self._agent = agent

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
        text = "; ".join(d.get("summary", "") for d in errors) or None

        addr_to_reg = {
            node_tf_address(node): reg
            for node, reg in ((n, node_reg_name(n)[1]) for n in graph.nodes)
            if node_tf_address(node)
        }
        errored = {
            reg
            for d in errors
            for addr, reg in addr_to_reg.items()
            if d.get("address", "").startswith(addr)
        }
        if errors and not errored:
            errored = set(reg_names)

        for reg_name in reg_names:
            if reg_name in errored:
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
        status = "live" if result.ok else "error"
        self.registry.update_status(resource_name, status, error=_error_text(result))
        await self._broadcast({"type": f"resource_{status}", "name": resource_name})

    async def deploy_all(self) -> list[str]:
        result = await self._runner.apply()
        deployed: list[str] = []
        for entry in self.registry.list_all():
            if entry.status not in ("validated", "live"):
                continue
            status = "live" if result.ok else "error"
            self.registry.update_status(entry.name, status, error=_error_text(result))
            await self._broadcast({"type": f"resource_{status}", "name": entry.name})
            if result.ok:
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

    async def _broadcast(self, message: dict) -> None:
        if self._ws:
            await self._ws.broadcast(message)
