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
from odin.aws.embed import (
    account_for_env,
    aws_container_env,
    install_rds_spawn_rewire,
    start_ministack,
    stop_ministack,
)
from odin.aws.provision import MiniStackAws
from odin.aws.rds import MiniStackRds
from odin.fabric.localhost import LocalhostFabric
from odin.reconcile.reconciler import Reconciler
from odin.reconcile.scheduler import Scheduler
from odin.runtime.colima import ColimaRuntime
from odin.spec.models import Stack
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack

ODIN_DIR = Path(".odin")
CANVAS_PATH = ODIN_DIR / "canvas.json"
ENV = "default"

log = logging.getLogger("odin")


def create_apply_router(store: SpecStore, reconciler_for, complete_fn=None) -> APIRouter:
    router = APIRouter()

    @router.post("/apply")
    async def apply(graph: CanvasGraph, env: str = ENV) -> dict:
        reconciler = await reconciler_for(env)
        stack = canvas_to_stack(graph.model_dump(), env=env)
        if complete_fn is not None:  # best-effort AI completion; defaults cover failure
            try:
                stack = await complete_fn(stack)
            except Exception:
                log.exception("brain completion failed; applying as-is")
        rev = store.apply(stack)
        await reconciler.tick()  # kick an immediate pass; the loop continues it
        return {"status": "applied", "rev": rev, "env": env}

    @router.post("/destroy")
    async def destroy(env: str = ENV) -> dict:
        reconciler = await reconciler_for(env)
        store.apply(Stack(env=env))  # empty desired state -> reconciler prunes all
        await reconciler.tick()
        return {"status": "destroyed", "env": env}

    @router.post("/preview")
    async def preview(graph: CanvasGraph, env: str = ENV) -> dict:
        """Staged changeset: what the AI would fill, for review before Apply."""
        from odin.agent.completion import ai_diff

        stack = canvas_to_stack(graph.model_dump(), env=env)
        if complete_fn is not None:
            try:
                stack = await complete_fn(stack)
            except Exception:
                log.exception("preview completion failed")
        return {"diff": ai_diff(stack), "env": env}

    @router.post("/review-iam")
    async def review_iam_route(graph: CanvasGraph, env: str = ENV) -> dict:
        from odin.agent.brain import review_iam

        stack = canvas_to_stack(graph.model_dump(), env=env)
        try:
            findings = await review_iam(stack)
        except Exception:
            log.exception("iam review failed")
            findings = []
        return {"findings": findings, "env": env}

    @router.get("/world")
    def world(env: str = ENV) -> dict:
        return store.current_world(env).model_dump()

    @router.get("/envs")
    def envs() -> dict:
        return {"envs": store.list_envs()}

    return router


def create_app(
    runtime=None,
    store: SpecStore | None = None,
    rds=None,
    embed: bool = True,
    complete: bool = True,
) -> FastAPI:
    _runtime = runtime or ColimaRuntime()
    _store = store or SpecStore(ODIN_DIR)
    ws_manager = ConnectionManager()
    scheduler = Scheduler(_runtime.ensure_host().total_mem_mib or 4096.0)
    complete_fn = None
    if complete:
        from odin.agent.brain import claude_complete
        complete_fn = claude_complete

    # One reconciler per environment, created lazily. Each is scoped to its own
    # MiniStack account so environments get isolated AWS state in one embed.
    reconcilers: dict[str, Reconciler] = {}

    def _make_reconciler(env: str) -> Reconciler:
        account = account_for_env(env)
        env_rds = rds or MiniStackRds(account)
        env_aws_env = (lambda a=account: aws_container_env(a)) if embed else None
        return Reconciler(
            _store, _runtime, env_rds, aws=MiniStackAws(account), fabric=LocalhostFabric(),
            ws=ws_manager, env=env, scheduler=scheduler, aws_env=env_aws_env, poll_interval=1.0,
        )

    async def reconciler_for(env: str) -> Reconciler:
        if env not in reconcilers:
            reconcilers[env] = _make_reconciler(env)
            await reconcilers[env].start()
        return reconcilers[env]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if embed:
            await asyncio.to_thread(start_ministack)
            install_rds_spawn_rewire(_runtime)
        for env in _store.list_envs():  # resume reconciling existing environments
            await reconciler_for(env)
        try:
            yield
        finally:
            for reconciler in reconcilers.values():
                await reconciler.stop()
            if embed:
                stop_ministack()

    app = FastAPI(title="allfather", version="0.1.0", lifespan=lifespan)
    app.include_router(create_canvas_router(CANVAS_PATH))
    app.include_router(create_apply_router(_store, reconciler_for, complete_fn=complete_fn))

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

    @app.get("/health")
    def health():
        return {"ok": True, "agent": False}

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
