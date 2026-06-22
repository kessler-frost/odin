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
        return CanvasGraph.model_validate(json.loads(canvas_path.read_text()))

    @router.post("/canvas")
    def save_canvas(graph: CanvasGraph) -> dict[str, str]:
        canvas_path.write_text(graph.model_dump_json(indent=2))
        return {"status": "saved"}

    return router
