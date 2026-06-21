from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from odin.mcp.tools import OdinTools
from odin.orchestrator import Orchestrator

ODIN_DIR = Path(".odin")
TF_DIR = ODIN_DIR / "tf"


def create_resource_router(
    tools: OdinTools,
    orchestrator: Orchestrator | None = None,
    agent=None,
    ws_manager=None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health():
        return {
            "status": "ok",
            "agent": agent.is_running if agent else False,
            "ws_connections": len(ws_manager._connections) if ws_manager else 0,
        }

    @router.get("/state")
    def get_state(service: str | None = Query(default=None)):
        return tools.get_infrastructure_state(service=service)

    @router.post("/reset")
    async def reset():
        tools._registry.clear()
        for name in ("main.tf", "terraform.tfstate", "terraform.tfstate.backup"):
            (TF_DIR / name).unlink(missing_ok=True)
        (ODIN_DIR / "canvas.json").unlink(missing_ok=True)
        if ws_manager:
            ws_manager.clear_events()
        if orchestrator:
            orchestrator.engine.reset()
        if agent and agent.is_running:
            await agent.reset()
        return {"status": "reset"}

    return router


def create_deploy_router(orchestrator: Orchestrator) -> APIRouter:
    router = APIRouter()

    @router.post("/deploy/{name}")
    async def deploy_resource(name: str):
        entry = orchestrator.registry.get(name)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Resource '{name}' not found")
        await orchestrator.deploy(name)
        entry = orchestrator.registry.get(name)
        return {"name": name, "status": entry.status}

    @router.post("/deploy")
    async def deploy_all():
        return {"deployed": await orchestrator.deploy_all()}

    @router.post("/destroy/{name}")
    async def destroy_resource(name: str):
        entry = orchestrator.registry.get(name)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Resource '{name}' not found")
        await orchestrator.destroy(name)
        entry = orchestrator.registry.get(name)
        return {"name": name, "status": entry.status}

    @router.post("/destroy-all")
    async def destroy_all():
        return {"destroyed": await orchestrator.destroy_all()}

    return router


def create_simulate_router(orchestrator: Orchestrator) -> APIRouter:
    """Real local execution (Lima VMs + Nebula), separate from the Moto deploy."""
    from odin.api.canvas import CanvasGraph

    router = APIRouter()

    @router.post("/simulate")
    async def simulate(graph: CanvasGraph):
        return await orchestrator.simulate(graph)

    @router.post("/simulate-destroy")
    async def simulate_destroy():
        return await orchestrator.simulate_destroy()

    return router
