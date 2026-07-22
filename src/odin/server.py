"""allfather FastAPI app factory.

The canvas authors a desired-state Stack; a continuous Reconciler drives reality
(real Postgres for rds nodes, and per-env backing containers for the
AWS-shaped resources, both via Colima); the World projects back to the canvas
over WebSocket.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from odin.agent import import_tf as import_tf_mod
from odin.agent import translate as translate_mod
from odin.agent.hcl import generate_tf
from odin.api.canvas import CanvasGraph, create_canvas_router
from odin.api.ws import ConnectionManager
from odin.aws.backings import BackingAws
from odin.aws.rds import PostgresRds
from odin.fabric.localhost import LocalhostFabric
from odin.fabric.nebula import mesh_state
from odin.gateway import DEFAULT_GATEWAY_PORT, GATEWAY_PORT_ENV
from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore, Principal
from odin.gateway.stores import SynthStores
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import ColimaRuntime
from odin.simulate.runner import SimulateBusy, TfRunner, TofuNotInstalled
from odin.spec.models import Stack
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack, skipped_node_types

ODIN_DIR = Path(".odin")
CANVAS_PATH = ODIN_DIR / "canvas.json"
ENV = "default"

log = logging.getLogger("odin")


def create_apply_router(store: SpecStore, reconciler_for, keystore: KeyStore) -> APIRouter:
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
        keystore.revoke_env(env)  # gateway-issued keys die with the env they belong to
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


_TOFU_NOT_INSTALLED = {"error": "tofu not installed", "fix": "brew install opentofu"}


class ImportTfRequest(BaseModel):
    source: Literal["hcl", "live"]
    hcl: str = ""
    resources: list[dict] = []  # [{"type": "s3", "id": "uploads"}, ...] -- see import_tf.LiveResource


def create_tf_router(store: SpecStore, runner: TfRunner, keystore: KeyStore, gateway_port) -> APIRouter:
    """`/tf/*` -- Simulate's own apply/destroy/status, independent of the
    canvas `/apply`/`/destroy` above (S2 CONTRACT ADDENDUM: routes named
    `/tf/*`, not `/simulate/*` -- "the owner renamed the user surface to
    Apply"). `gateway_port` is a zero-arg callable rather than a plain int:
    the real port is only known once the gateway's uvicorn listener starts
    in `create_app`'s `lifespan`, resolved AFTER this router is built."""
    router = APIRouter()

    def _issue_operator(env: str) -> tuple[str, str]:
        return keystore.issue(env, OPERATOR_NODE_ID)

    @router.post("/tf/apply")
    async def tf_apply(env: str = ENV) -> JSONResponse:
        stack = store.get_stack(env)
        project = generate_tf(stack)
        access_key, secret_key = _issue_operator(env)
        try:
            result = await runner.apply(env, project, gateway_port(), access_key, secret_key)
        except TofuNotInstalled:
            return JSONResponse(status_code=409, content=_TOFU_NOT_INSTALLED)
        except SimulateBusy as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        body = {
            "status": "applied" if result.ok else "failed", "env": env,
            "exit_code": result.exit_code, "unsupported": project.unsupported,
        }
        if not result.ok:
            body["tail"] = list(result.tail)
        return JSONResponse(status_code=200 if result.ok else 500, content=body)

    @router.post("/tf/destroy")
    async def tf_destroy(env: str = ENV) -> JSONResponse:
        access_key, secret_key = _issue_operator(env)
        try:
            result = await runner.destroy(env, gateway_port(), access_key, secret_key)
        except TofuNotInstalled:
            return JSONResponse(status_code=409, content=_TOFU_NOT_INSTALLED)
        except SimulateBusy as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        body = {"status": "destroyed" if result.ok else "failed", "env": env, "exit_code": result.exit_code}
        if not result.ok:
            body["tail"] = list(result.tail)
        return JSONResponse(status_code=200 if result.ok else 500, content=body)

    @router.get("/tf/status")
    def tf_status(env: str = ENV) -> dict:
        return runner.status(env)

    @router.post("/translate")
    async def translate_route(env: str = ENV) -> dict:
        """S3b: the canvas -> TF review pass, for the UI to show before
        Apply runs it (`translate` is always best-effort; see its own
        docstring for the fallback chain -- this route never fails)."""
        stack = store.get_stack(env)
        result = await translate_mod.translate(stack)
        return result.model_dump()

    @router.post("/import-tf")
    async def import_tf_route(body: ImportTfRequest, env: str = ENV) -> dict:
        """S4: TF -> canvas, the reverse direction. `source="hcl"` parses the
        given text deterministically; `source="live"` resolves `resources`
        against the env's real backings through the gateway (operator creds,
        same as /tf/apply)."""
        if body.source == "hcl":
            result = import_tf_mod.parse_hcl_text(body.hcl)
        else:
            resources = [import_tf_mod.LiveResource(type=r["type"], id=r["id"]) for r in body.resources]
            access_key, secret_key = _issue_operator(env)
            result = await import_tf_mod.import_live(resources, gateway_port(), access_key, secret_key)
        return result.model_dump()

    return router


def create_app(
    runtime=None,
    store: SpecStore | None = None,
    rds=None,
    aws=None,
    backings: bool = True,
    gateway_port: int | None = None,
) -> FastAPI:
    _runtime = runtime or ColimaRuntime()
    _store = store or SpecStore(ODIN_DIR)
    ws_manager = ConnectionManager(_store.root)
    _resolved_gateway_port = gateway_port if gateway_port is not None else int(os.environ.get(GATEWAY_PORT_ENV, DEFAULT_GATEWAY_PORT))

    # The gateway: workload SDK calls carry per-node creds and land here
    # (checked reverse proxy -> real backing), never the backing directly.
    # Stateless routing table + key registry, rebuilt every tick from
    # (Stack, issued keys) -- never a cache that outlives an Apply.
    gateway_state = GatewayState()
    gateway_keystore = KeyStore(_store.root)
    # The synthesized control-plane's tag/attribute/delete-marker stores
    # (gateway/synth.py) -- unlike gateway_state, this must OUTLIVE a tick.
    gateway_stores = SynthStores(_store.root)
    # port=0 (the test default) resolves to an ephemeral port; lifespan fills
    # this in with the ACTUAL bound port before any reconciler is made, so
    # BackingAws/`/health` never advertise the possibly-0 request instead.
    gateway_port_actual: int | None = None
    # Simulate (S2): materializes .odin/{env}/tf/ and drives tofu through the
    # gateway above under the OPERATOR principal. No lifespan hook of its own
    # (unlike reconcilers) -- routes only ever run once lifespan has resolved
    # gateway_port_actual, so a plain closure over it is enough.
    tf_runner = TfRunner(_store.root, ws_manager)

    # One reconciler per environment, created lazily. Each gets its own
    # env-scoped rds runner + backing containers, so AWS state stays isolated.
    reconcilers: dict[str, Reconciler] = {}

    def _make_reconciler(env: str) -> Reconciler:
        env_rds = rds or PostgresRds(_runtime, env)
        env_aws = aws or (BackingAws(_runtime, env, gateway_port=gateway_port_actual) if backings else None)
        return Reconciler(
            _store, _runtime, env_rds, aws=env_aws, gateway=gateway_state, fabric=LocalhostFabric(),
            ws=ws_manager, env=env, poll_interval=1.0,
        )

    async def reconciler_for(env: str) -> Reconciler:
        if env not in reconcilers:
            reconcilers[env] = _make_reconciler(env)
            await reconcilers[env].start()
        return reconcilers[env]

    async def on_deny(principal: Principal | None, action: str | None, resource: str | None, reason: str) -> None:
        await ws_manager.broadcast({
            "type": "access_denied",
            "env": principal.env if principal else "default",
            "resource_id": principal.node_id if principal else None,
            "action": action,
            "target": resource,
            "reason": reason,
        })

    gateway_app = create_gateway_app(gateway_state, gateway_keystore, gateway_stores, on_deny)
    gateway_server = None
    gateway_thread = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal gateway_server, gateway_thread, gateway_port_actual
        # The gateway listener starts FIRST: reconcilers (built below, for
        # envs resumed on restart) need the ACTUAL resolved port to point
        # BackingAws's goaws.yaml at.
        gateway_server, gateway_thread, gateway_port_actual = serve_in_thread(gateway_app, port=_resolved_gateway_port)
        for env in _store.list_envs():  # resume reconciling existing environments
            await reconciler_for(env)
        try:
            yield
        finally:
            for reconciler in reconcilers.values():
                await reconciler.stop()
            stop_in_thread(gateway_server, gateway_thread)

    app = FastAPI(title="allfather", version="0.1.0", lifespan=lifespan)
    app.include_router(create_canvas_router(CANVAS_PATH))
    app.include_router(create_apply_router(_store, reconciler_for, gateway_keystore))
    app.include_router(create_tf_router(_store, tf_runner, gateway_keystore, lambda: gateway_port_actual))

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
        return {"ok": True, "gateway": {"port": gateway_port_actual}}

    app.state.store = _store
    app.state.runtime = _runtime
    app.state.ws_manager = ws_manager
    app.state.reconcilers = reconcilers
    app.state.gateway = gateway_state
    app.state.gateway_keys = gateway_keystore
    app.state.tf_runner = tf_runner

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
