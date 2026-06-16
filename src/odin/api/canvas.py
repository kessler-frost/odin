from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel


class CanvasGraph(BaseModel):
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []


def create_canvas_router(canvas_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/canvas")
    def get_canvas() -> CanvasGraph:
        if not canvas_path.exists():
            return CanvasGraph()
        data = json.loads(canvas_path.read_text())
        return CanvasGraph.model_validate(data)

    @router.post("/canvas")
    def save_canvas(graph: CanvasGraph) -> dict[str, str]:
        canvas_path.write_text(graph.model_dump_json(indent=2))
        return {"status": "saved"}

    return router


def create_suggest_defaults_router(agent, ws_manager=None) -> APIRouter:
    router = APIRouter()

    @router.post("/suggest-defaults")
    async def suggest_defaults(graph: CanvasGraph) -> dict[str, Any]:
        if not agent.is_running:
            return {"status": "skipped", "reason": "Agent not connected"}
        events: list[dict[str, Any]] = []
        async for event in agent.suggest_defaults(graph):
            event_data = event.model_dump()
            events.append(event_data)
            if ws_manager:
                await ws_manager.broadcast(event_data)
        return {"status": "completed", "events": events}

    return router


def _node_reg_name(node: dict[str, Any]) -> tuple[str, str]:
    """Return (label, registry_name) for a canvas node."""
    label = node.get("data", {}).get("label", node.get("id", ""))
    node_type = node.get("type", "")
    return label, f"{node_type}_{label}"


def create_validate_router(agent, ws_manager=None, registry=None) -> APIRouter:
    router = APIRouter()

    @router.post("/validate")
    async def validate(graph: CanvasGraph) -> dict[str, Any]:
        # Save previous status for each node, then mark as "validating"
        prev_status: dict[str, str] = {}
        node_reg_names: list[tuple[str, str]] = []
        for node in graph.nodes:
            label, reg_name = _node_reg_name(node)
            node_reg_names.append((label, reg_name))
            if label and registry:
                entry = registry.get(reg_name)
                prev_status[reg_name] = entry.status if entry else "draft"
                if entry:
                    registry.update_status(reg_name, "validating")
                else:
                    registry.register(reg_name, service=node.get("type", ""), file_path="")
                    registry.update_status(reg_name, "validating")
            if label and ws_manager:
                await ws_manager.broadcast({"type": "resource_validating", "name": reg_name})

        if not agent.is_running:
            for label, reg_name in node_reg_names:
                if label and registry:
                    registry.update_status(reg_name, "error", error="Agent not connected")
                if label and ws_manager:
                    await ws_manager.broadcast({"type": "resource_error", "name": reg_name, "error": "Agent not connected"})
            return {"status": "error", "error": "Agent not connected — start server outside Claude Code"}

        events: list[dict[str, Any]] = []
        async for event in agent.validate(graph):
            event_data = event.model_dump()
            events.append(event_data)
            if ws_manager:
                await ws_manager.broadcast(event_data)

        # Final pass: restore previous status for nodes the agent skipped
        for label, reg_name in node_reg_names:
            if not label:
                continue
            entry = registry.get(reg_name) if registry else None
            if entry and entry.status == "validating":
                restored = prev_status.get(reg_name, "draft")
                registry.update_status(reg_name, restored)
                entry = registry.get(reg_name)
            if entry and ws_manager:
                await ws_manager.broadcast({
                    "type": f"resource_{entry.status}",
                    "name": reg_name,
                    **({"error": entry.error} if entry.error else {}),
                })

        return {"status": "completed", "events": events}

    return router
