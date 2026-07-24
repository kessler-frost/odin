from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from odin.util import atomic_write_text


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
        # Security finding #3: a node's fields (e.g. an rds `password`) land
        # in this file in cleartext by design (the reconciler reads it back
        # verbatim) -- 0600 is the only thing stopping another local account
        # from reading it.
        atomic_write_text(canvas_path, graph.model_dump_json(indent=2), mode=0o600)
        return {"status": "saved"}

    return router
