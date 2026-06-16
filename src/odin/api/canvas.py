from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel


class CanvasGraph(BaseModel):
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []


# Canvas node type → AWS / Terraform resource type.
NODE_AWS_TYPE = {
    "vpc": "aws_vpc",
    "subnet": "aws_subnet",
    "sg": "aws_security_group",
    "ec2": "aws_instance",
    "lambda": "aws_lambda_function",
    "s3": "aws_s3_bucket",
}


def hcl_name(label: str) -> str:
    """Sanitize a node label into a valid HCL resource name."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", label).lower()
    return name if name[:1].isalpha() else f"r_{name}"


def node_reg_name(node: dict[str, Any]) -> tuple[str, str]:
    """Return (label, registry_name) for a canvas node."""
    label = node.get("data", {}).get("label", node.get("id", ""))
    node_type = node.get("type", "")
    return label, f"{node_type}_{label}"


def node_tf_address(node: dict[str, Any]) -> str | None:
    """The Terraform address for a node, e.g. `aws_vpc.prod_vpc`."""
    label = node.get("data", {}).get("label", node.get("id", ""))
    aws_type = NODE_AWS_TYPE.get(node.get("type", ""))
    if not aws_type or not label:
        return None
    return f"{aws_type}.{hcl_name(label)}"


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


def create_validate_router(orchestrator) -> APIRouter:
    """Validate the canvas: the orchestrator drives the agent + tofu and
    broadcasts per-node status; this router just collects the agent events."""
    router = APIRouter()

    @router.post("/validate")
    async def validate(graph: CanvasGraph) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        async for event in orchestrator.validate(graph):
            events.append(event.model_dump())
        return {"status": "completed", "events": events}

    return router
