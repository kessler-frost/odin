from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, model_validator

from odin.spec.translate import canvas_problems
from odin.util import atomic_write_text

_EMPTY: dict[str, list] = {"nodes": [], "edges": []}


class CanvasGraph(BaseModel):
    """The canvas as it arrives on the wire — for `/canvas`, and for the
    `/apply`, `/apply-full` and `/translate` bodies too, which is what makes
    the check below cover every route that takes a canvas.

    Field test 4, P4-5: a canvas whose `data.label` was a list was accepted
    here, written to disk, and only blew up two layers down inside
    `canvas_to_stack`, as an unhandled `ValidationError` — a 500 and a bare
    "Internal Server Error" for what is entirely a CLIENT mistake. A
    structurally impossible canvas is now refused at the boundary, naming the
    node and the field, so the sender can fix it.

    The node/edge fields stay `dict[str, Any]`, deliberately: the canvas is an
    open document (the UI, the config panel and the TF importer all add keys
    odin doesn't model) and `model_dump()` has to hand `canvas_to_stack` back
    exactly what arrived — declaring the few fields we DO read would inject
    `null` defaults for the ones a node omits, including the `position` the UI
    needs. `canvas_problems` inspects instead of re-typing."""

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    @model_validator(mode="after")
    def _must_be_translatable(self) -> CanvasGraph:
        problems = canvas_problems(self.model_dump())
        if problems:
            raise ValueError("this canvas cannot be applied — " + "; ".join(problems))
        return self


def create_canvas_router(canvas_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/canvas")
    def get_canvas() -> dict[str, Any]:
        """The stored canvas VERBATIM, never re-validated: whatever is on disk
        is what `odin canvas get` must hand back, or a canvas hand-edited into
        an unusable shape would be unreachable by the one command that could
        be used to repair it. (POST is where the shape is enforced.)"""
        return json.loads(canvas_path.read_text()) if canvas_path.exists() else dict(_EMPTY)

    @router.post("/canvas")
    def save_canvas(graph: CanvasGraph) -> dict[str, str]:
        # Security finding #3: a node's fields (e.g. an rds `password`) land
        # in this file in cleartext by design (the reconciler reads it back
        # verbatim) -- 0600 is the only thing stopping another local account
        # from reading it.
        atomic_write_text(canvas_path, graph.model_dump_json(indent=2), mode=0o600)
        return {"status": "saved"}

    return router
