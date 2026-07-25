"""`POST /agent/debug` -- "what's wrong here?" for a selected region of the
canvas (W2.9 / M8).

The route is a thin, honest wrapper around `agent/debugger.py`'s two halves:
assemble the evidence for the selected node labels (pure, capped, redacted),
then run ONE agent pass whose only effect channel is the typed
`report_diagnosis` tool. Every outcome is a 200 -- an unknown node, an env
with no World yet, a missing SDK, an agent timeout -- because the answer to
"why is this broken" must never itself be a 500. When the agent can't run, the
answer is literally `"agent unavailable"` (see `debugger.UNAVAILABLE`).

Logs come from the wave-1 `/logs` resolver (`api/logs.py::fetch_logs`), not a
reimplementation: it already knows how to find an ec2 node's Lima VM journal,
every task container currently backing an ecs service, a lambda's RIE
container, and the rds/backing containers -- and it already answers honestly
(never raises) for a node with no running backing.

CSRF: this is a POST, so `server.py`'s `_csrf_guard` covers it like every
other unsafe-method route, and that is DELIBERATE even though the route
mutates nothing. It spends real money on the caller's behalf (an outbound
model call) and hands back the env's configuration, logs and crash reasons --
exactly the pair of things a cross-origin browser POST should not be able to
trigger against an unauthenticated local server. The CLI and agent callers
send no `Origin`, so the guard never sees them.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from pydantic import BaseModel

from odin.agent import debugger
from odin.api.logs import fetch_logs
from odin.api.ws import ConnectionManager
from odin.gateway.stores import SynthStores
from odin.spec.store import SpecStore

log = logging.getLogger("odin.debug")

DEFAULT_QUESTION = "what's wrong here?"


class DebugRequest(BaseModel):
    env: str = "default"
    # Canvas node LABELS (the canonical resource id -- what `spec/translate.py`
    # keys resources by and what the UI sends), never ReactFlow node ids.
    node_ids: list[str] = []
    question: str = DEFAULT_QUESTION


class Suspect(BaseModel):
    node_id: str
    reason: str


class DebugResponse(BaseModel):
    env: str
    answer: str
    suspects: list[Suspect] = []


def _logs_reader(store: SpecStore, stores: SynthStores, runtime, env: str):
    """The `logs` callable `assemble_context` takes. `fetch_logs` is honest
    rather than exceptional for every ABSENT case, so the guard here is only
    for the genuinely broken host (no `docker`, no `limactl`): a diagnosis with
    one node's logs missing is still worth having, and losing the whole answer
    to it is not."""

    def read(node: str) -> str:
        try:
            return fetch_logs(store, stores, runtime, env, node, tail=debugger.MAX_LOG_LINES).lines
        except Exception:
            log.exception("could not read logs for node %s in env %s (continuing without them)", node, env)
            return ""

    return read


def build_context(
    store: SpecStore, stores: SynthStores, runtime, ws_manager: ConnectionManager, env: str, node_ids: list[str],
) -> dict:
    """Everything the model gets, assembled from real state: the desired Stack,
    the observed World, the env's durable event log, and each node's real log
    tail. Blocking (the log reads shell out), so the route runs it off the
    event loop. Exposed as its own function because it's also the part an
    integration test can assert on WITHOUT needing the SDK at all."""
    stack = store.get_stack(env)
    world = store.current_world(env)
    events = ws_manager.get_events(env)
    return debugger.assemble_context(stack, world, events, _logs_reader(store, stores, runtime, env), node_ids)


def create_debug_router(
    store: SpecStore, stores: SynthStores, runtime, ws_manager: ConnectionManager,
    diagnose: Callable[..., Awaitable[dict]] | None = None,
) -> APIRouter:
    """`diagnose` is injectable purely as a test seam (the wiring tests drive
    the route without an SDK); every real caller leaves it None, which resolves
    `debugger.diagnose` per request (late, so it stays monkeypatchable)."""
    router = APIRouter()

    @router.post("/agent/debug")
    async def debug_route(body: DebugRequest) -> DebugResponse:
        context = await asyncio.to_thread(
            build_context, store, stores, runtime, ws_manager, body.env, body.node_ids,
        )
        result = await (diagnose or debugger.diagnose)(context, body.question or DEFAULT_QUESTION)
        return DebugResponse(
            env=body.env,
            answer=str(result.get("answer", "")),
            suspects=[Suspect(**s) for s in debugger.normalize_suspects(result.get("suspects"))],
        )

    return router
