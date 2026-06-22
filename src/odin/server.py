"""allfather FastAPI app factory.

The canvas authors a desired-state Stack; a continuous Reconciler drives reality
(real containers via Colima, the AWS control plane via embedded MiniStack); the
World projects back to the canvas over WebSocket. This replaces odin's old
one-shot Moto/OpenTofu validate path (those modules remain on disk, unused).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from odin.api.canvas import CanvasGraph, create_canvas_router
from odin.api.ws import ConnectionManager
from odin.aws.embed import install_rds_spawn_rewire, start_ministack, stop_ministack
from odin.aws.rds import MiniStackRds
from odin.fabric.localhost import LocalhostFabric
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import ColimaRuntime
from odin.spec.models import Stack
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack

ODIN_DIR = Path(".odin")
CANVAS_PATH = ODIN_DIR / "canvas.json"
ENV = "default"

log = logging.getLogger("odin")


def create_apply_router(store: SpecStore, reconciler: Reconciler) -> APIRouter:
    router = APIRouter()

    @router.post("/apply")
    async def apply(graph: CanvasGraph) -> dict:
        rev = store.apply(canvas_to_stack(graph.model_dump(), env=ENV))
        await reconciler.tick()  # kick an immediate pass; the loop continues it
        return {"status": "applied", "rev": rev}

    @router.post("/destroy")
    async def destroy() -> dict:
        store.apply(Stack(env=ENV))  # empty desired state -> reconciler prunes all
        await reconciler.tick()
        return {"status": "destroyed"}

    @router.get("/world")
    def world() -> dict:
        return store.current_world(ENV).model_dump()

    return router


def create_app(
    runtime=None,
    store: SpecStore | None = None,
    rds=None,
    embed: bool = True,
) -> FastAPI:
    _runtime = runtime or ColimaRuntime()
    _store = store or SpecStore(ODIN_DIR)
    _rds = rds or MiniStackRds()
    ws_manager = ConnectionManager()
    reconciler = Reconciler(
        _store, _runtime, _rds, fabric=LocalhostFabric(),
        ws=ws_manager, env=ENV, poll_interval=1.0,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if embed:
            await asyncio.to_thread(start_ministack)
            install_rds_spawn_rewire(_runtime)
        await reconciler.start()
        try:
            yield
        finally:
            await reconciler.stop()
            if embed:
                stop_ministack()

    app = FastAPI(title="allfather", version="0.1.0", lifespan=lifespan)
    app.include_router(create_canvas_router(CANVAS_PATH))
    app.include_router(create_apply_router(_store, reconciler))

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    @app.get("/events")
    def get_events():
        return ws_manager.get_events()

    app.state.store = _store
    app.state.runtime = _runtime
    app.state.ws_manager = ws_manager
    app.state.reconciler = reconciler

    bundled_ui = Path(__file__).resolve().parent / "_ui"
    source_ui = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"
    ui_dist = bundled_ui if bundled_ui.exists() else source_ui
    if ui_dist.exists():
        @app.get("/")
        def serve_index():
            return FileResponse(ui_dist / "index.html")

        app.mount("/assets", StaticFiles(directory=str(ui_dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            static_file = ui_dist / full_path
            return FileResponse(static_file if static_file.is_file() else ui_dist / "index.html")

    return app
