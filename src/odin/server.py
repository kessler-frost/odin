from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from odin.agent.client import OdinAgent
from odin.api.canvas import create_canvas_router, create_validate_router
from odin.api.resources import create_deploy_router, create_resource_router
from odin.api.ws import ConnectionManager
from odin.compute.container_manager import ContainerManager
from odin.compute.vm_manager import VmManager
from odin.mcp.tools import OdinTools
from odin.network.nebula_manager import NebulaManager
from odin.orchestrator import Orchestrator
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry

ODIN_DIR = Path(".odin")
REGISTRY_PATH = ODIN_DIR / "registry.json"
CANVAS_PATH = ODIN_DIR / "canvas.json"


def _ensure_registry() -> ResourceRegistry:
    ODIN_DIR.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_PATH.exists():
        REGISTRY_PATH.write_text(json.dumps({"resources": {}}))
    return ResourceRegistry(REGISTRY_PATH)


def create_app(
    engine: MotoEngine | None = None,
    registry: ResourceRegistry | None = None,
    vm_manager: VmManager | None = None,
    nebula_manager: NebulaManager | None = None,
    container_manager: ContainerManager | None = None,
) -> FastAPI:
    _engine = engine or MotoEngine()
    _registry = registry or _ensure_registry()
    ws_manager = ConnectionManager()
    tools = OdinTools(_engine, _registry, ws_manager=ws_manager)

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.engine = _engine
    orchestrator.registry = _registry
    orchestrator._executor = None
    orchestrator._ws = ws_manager
    orchestrator._vm = vm_manager or VmManager()
    orchestrator._nebula = nebula_manager or NebulaManager()
    orchestrator._container = container_manager or ContainerManager()
    orchestrator._container_hosts = {}

    agent = OdinAgent(
        infra_dir=str(ODIN_DIR / "infra"),
        tools=tools,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import os
        os.environ.pop("CLAUDECODE", None)
        _engine.start()
        try:
            await agent.start()
        except Exception as e:
            import logging
            logging.getLogger("odin").warning("Agent failed to start: %s — server running without agent", e)
            agent._client = None
        yield
        await agent.stop()
        _engine.stop()

    app = FastAPI(title="Odin", version="0.1.0", lifespan=lifespan)

    app.include_router(create_resource_router(tools, agent=agent, ws_manager=ws_manager))
    app.include_router(create_deploy_router(orchestrator))
    app.include_router(create_canvas_router(CANVAS_PATH))
    app.include_router(create_validate_router(agent, ws_manager=ws_manager, registry=_registry))
    # EXPERIMENTAL: smart defaults disabled until agent reliability improves (Phase 5+)
    # app.include_router(create_suggest_defaults_router(agent, ws_manager=ws_manager))

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

    app.state.engine = _engine
    app.state.registry = _registry
    app.state.tools = tools
    app.state.ws_manager = ws_manager
    app.state.orchestrator = orchestrator
    app.state.agent = agent

    # Serve built React frontend on the same port
    ui_dist = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"

    if ui_dist.exists():
        @app.get("/")
        def serve_index():
            return FileResponse(ui_dist / "index.html")

        app.mount("/assets", StaticFiles(directory=str(ui_dist / "assets")), name="assets")

        # SPA catch-all: serve static file if it exists, otherwise index.html
        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            static_file = ui_dist / full_path
            if static_file.is_file():
                return FileResponse(static_file)
            return FileResponse(ui_dist / "index.html")

    return app
