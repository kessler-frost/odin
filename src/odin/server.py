"""allfather FastAPI app factory.

The canvas authors a desired-state Stack; a continuous Reconciler drives reality
(real Postgres for rds nodes, and per-env backing containers for the
AWS-shaped resources, both via Colima); the World projects back to the canvas
over WebSocket.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from odin.api.canvas import CanvasGraph, create_canvas_router
from odin.api.ws import ConnectionManager
from odin.aws.backings import BackingAws
from odin.aws.rds import PostgresRds
from odin.fabric.localhost import LocalhostFabric
from odin.fabric.nebula import mesh_state
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import ColimaRuntime
from odin.spec.models import Stack
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack, skipped_node_types

ODIN_DIR = Path(".odin")
CANVAS_PATH = ODIN_DIR / "canvas.json"
ENV = "default"

log = logging.getLogger("odin")


def create_apply_router(store: SpecStore, reconciler_for) -> APIRouter:
    router = APIRouter()

    @router.post("/apply")
    async def apply(graph: CanvasGraph, env: str = ENV) -> dict:
        reconciler = await reconciler_for(env)
        canvas = graph.model_dump()
        stack = canvas_to_stack(canvas, env=env)
        rev = store.apply(stack)
        await reconciler.tick()  # kick an immediate pass; the loop continues it
        return {"status": "applied", "rev": rev, "env": env, "skipped": skipped_node_types(canvas)}

    @router.post("/destroy")
    async def destroy(env: str = ENV) -> dict:
        reconciler = await reconciler_for(env)
        store.apply(Stack(env=env))  # empty desired state -> reconciler prunes all
        await reconciler.tick()
        return {"status": "destroyed", "env": env}

    @router.get("/world")
    def world(env: str = ENV) -> dict:
        return store.current_world(env).model_dump()

    @router.get("/mesh")
    def mesh(env: str = ENV) -> dict:
        """The env's Nebula overlay membership — the read model a mesh UI builds
        on. Empty until hosts join (single-host today)."""
        return mesh_state(store.root, env, store.current_world(env)).model_dump()

    @router.get("/envs")
    def envs() -> dict:
        return {"envs": store.list_envs()}

    return router


def create_app(
    runtime=None,
    store: SpecStore | None = None,
    rds=None,
    aws=None,
    backings: bool = True,
) -> FastAPI:
    _runtime = runtime or ColimaRuntime()
    _store = store or SpecStore(ODIN_DIR)
    ws_manager = ConnectionManager(_store.root)

    # One reconciler per environment, created lazily. Each gets its own
    # env-scoped rds runner + backing containers, so AWS state stays isolated.
    reconcilers: dict[str, Reconciler] = {}

    def _make_reconciler(env: str) -> Reconciler:
        env_rds = rds or PostgresRds(_runtime, env)
        env_aws = aws or (BackingAws(_runtime, env) if backings else None)
        return Reconciler(
            _store, _runtime, env_rds, aws=env_aws, fabric=LocalhostFabric(),
            ws=ws_manager, env=env, poll_interval=1.0,
        )

    async def reconciler_for(env: str) -> Reconciler:
        if env not in reconcilers:
            reconcilers[env] = _make_reconciler(env)
            await reconcilers[env].start()
        return reconcilers[env]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for env in _store.list_envs():  # resume reconciling existing environments
            await reconciler_for(env)
        try:
            yield
        finally:
            for reconciler in reconcilers.values():
                await reconciler.stop()

    app = FastAPI(title="allfather", version="0.1.0", lifespan=lifespan)
    app.include_router(create_canvas_router(CANVAS_PATH))
    app.include_router(create_apply_router(_store, reconciler_for))

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    @app.get("/events")
    def get_events(env: str = ENV):
        return ws_manager.get_events(env)

    @app.get("/health")
    def health():
        return {"ok": True}

    app.state.store = _store
    app.state.runtime = _runtime
    app.state.ws_manager = ws_manager
    app.state.reconcilers = reconcilers

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
