from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from odin.mcp.tools import OdinTools
from odin.orchestrator import Orchestrator

ODIN_DIR = Path(".odin")


def create_resource_router(tools: OdinTools, agent=None, ws_manager=None) -> APIRouter:
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
        # Clear registry
        tools._registry.clear()
        # Clear agent-generated infra files
        infra_dir = ODIN_DIR / "infra"
        if infra_dir.exists():
            shutil.rmtree(infra_dir)
        infra_dir.mkdir(parents=True, exist_ok=True)
        # Clear canvas
        canvas_path = ODIN_DIR / "canvas.json"
        canvas_path.unlink(missing_ok=True)
        # Clear WS events
        if ws_manager:
            ws_manager.clear_events()
        # Reset agent session (fresh conversation)
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
        deployed = await orchestrator.deploy_all()
        return {"deployed": deployed}

    @router.post("/destroy/{name}")
    async def destroy_resource(name: str):
        entry = orchestrator.registry.get(name)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Resource '{name}' not found")
        await orchestrator.destroy(name)
        entry = orchestrator.registry.get(name)
        return {"name": name, "status": entry.status}

    @router.get("/vm/{name}/ssh")
    async def get_vm_ssh(name: str):
        vm = await orchestrator._vm.get_vm(name)
        if vm is None:
            raise HTTPException(status_code=404, detail=f"VM '{name}' not found")
        return {
            "name": name,
            "ssh_address": vm.ssh_address,
            "ssh_port": vm.ssh_port,
            "ssh_command": vm.ssh_local_port_string,
        }

    @router.post("/destroy-all")
    async def destroy_all():
        destroyed = []
        for entry in orchestrator.registry.list_all():
            if entry.status in ("live", "validated", "error"):
                await orchestrator.destroy(entry.name)
                destroyed.append(entry.name)
        return {"destroyed": destroyed}

    @router.post("/invoke/{name}")
    async def invoke_lambda(name: str, body: dict | None = None):
        entry = orchestrator.registry.get(name)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Resource '{name}' not found")
        if entry.service != "lambda":
            raise HTTPException(status_code=400, detail=f"Resource '{name}' is not a Lambda")
        if entry.status != "live":
            raise HTTPException(status_code=400, detail=f"Lambda '{name}' is not deployed")
        payload = (body or {}).get("payload", "")
        result = await orchestrator.invoke_lambda(name, payload)
        return {"name": name, "result": result}

    return router
