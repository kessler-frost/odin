"""`POST /agent/debug` -- "what's wrong here?" for a selected region of the
canvas (W2.9 / M8).

The route is a thin, honest wrapper around `agent/debugger.py`'s two halves:
assemble the evidence for the selected node labels (pure, capped, redacted),
then run ONE agent pass whose only effect channel is the typed
`report_diagnosis` tool.

EVERY FAILURE OF THE DIAGNOSIS IS A 200 -- an unknown node, an env with no
World yet, a missing SDK, an agent timeout, a node whose logs cannot be read
(`_logs_reader`) -- because the answer to "why is this broken" must never
itself be a 500. When the agent can't run, the answer is literally
`"agent unavailable"` (see `debugger.UNAVAILABLE`).

A FILE ODIN WROTE AND CAN NO LONGER PARSE IS NOT ONE OF THOSE, and the
docstring used to imply it was. Two reads here are load-bearing rather than
best-effort, and both raise a NAMED failure that `server.py::_unhandled_failure`
renders as JSON quoting the file:
  - the Stack/World themselves (`spec/store.py::StoreUnreadable`) -- with no
    evidence there is no diagnosis to give, only an invented one;
  - the issued-credential scrub set (`ScrubSetUnreadable`, below) -- degrading
    that one to "no secrets" is how odin's own keys reach a model prompt.
Both mean the env is broken beneath this feature, so the honest answer names
the file rather than shrugging inside a 200 that reads like a diagnosis.

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

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, TypeAdapter, ValidationError

from odin.agent import debugger
from odin.api.logs import fetch_logs
from odin.api.ws import ConnectionManager
from odin.gateway.stores import SynthStores
from odin.spec.store import SpecStore

log = logging.getLogger("odin.debug")

DEFAULT_QUESTION = "what's wrong here?"

# `gateway/keys.py::KeyStore` persists exactly `{node_id: [access_key, secret_key]}`.
# Validating the SHAPE, not just the JSON, is load-bearing: a file of
# `{"db": "AKIA..."}` parses fine and then makes the scrub set below iterate a
# STRING, producing the set of its single characters -- probed, and it really
# returns `{'A','K','I','4','o','d','i','n','1','2','3'}`. That is worse than
# any exception: every one of those letters would then be redacted out of the
# whole prompt, mangling the evidence while looking like it worked.
_ISSUED_KEYS = TypeAdapter(dict[str, list[str]])


class ScrubSetUnreadable(RuntimeError):
    """`keys.json` exists but odin cannot turn it into a scrub set.

    Deliberately NOT swallowed (see `issued_credentials`): this set is the only
    thing keeping odin's OWN issued credentials out of a model prompt, and an
    empty set means "nothing was ever issued", never "I could not tell". A
    corrupt file that degraded to `frozenset()` would silently re-open the leak
    field test 2 finding #6 closed, and would do it at exactly the moment
    something is already wrong with the env.

    The same file is `KeyStore`'s own state (`gateway/keys.py::_ensure_loaded`
    parses it unguarded too), so an env in this state has broken gateway auth
    regardless -- naming the file loudly is the actionable answer, not a
    graceful shrug."""

    def __init__(self, path: Path, cause: Exception) -> None:
        self.path = path.absolute()
        super().__init__(
            f"{self.path} holds odin's issued credentials for this env and could not be read as "
            f"{{node: [access_key, secret_key]}} -- {type(cause).__name__}: {cause}. Refusing to "
            f"build a diagnosis prompt without it, because those credentials could not then be "
            # No trailing period: `server.py::_failure_body` appends its own
            # sentence right after `str(exc)`, and one used to render as ".."
            f"scrubbed out of it. Restore or delete that file (deleting it makes odin reissue)"
        )


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
    rather than exceptional for every ABSENT case, so this guard exists for the
    rest: a diagnosis with one node's logs missing is still worth having, and
    losing the whole answer to it is not.

    It used to say it was for "the genuinely broken host (no `docker`, no
    `limactl`)", and that is the one thing it is NOT for -- probed, on a PATH
    with no `docker` at all: `ColimaRuntime.status` returns `'absent'` and
    `.logs` returns `''`, because every driver read is `check=False` and
    `util.run_command` turns a missing binary into rc 127 rather than an
    exception. Nothing raises. What DOES reach here is a corrupt
    `.odin/<env>/gateway/<name>.json` (`JSONDecodeError` out of
    `gateway/stores.py::_data`) -- and swallowing it is right HERE, unlike in
    `GET /logs`: the diagnosis has the Stack, the World and the event log to
    reason from, so one node's log tail going missing is a degraded answer
    rather than no answer. It is logged with a traceback either way."""

    async def read(node: str) -> str:
        try:
            return await fetch_logs(store, stores, runtime, env, node, tail=debugger.MAX_LOG_LINES).lines
        except Exception:
            log.exception("could not read logs for node %s in env %s (continuing without them)", node, env)
            return ""

    return read


def issued_credentials(root: Path, env: str) -> frozenset[str]:
    """Every credential odin ISSUED for this env, as a scrub set for the model
    prompt (field test 2 finding #6).

    `gateway/keys.py::KeyStore` persists `{node_id: [access_key, secret_key]}`
    to `<root>/<env>/keys.json` on every mutation, and `server.py` builds that
    KeyStore on the SpecStore's own root -- so the file is the complete,
    always-current record without threading a second object through this
    route. Both halves go in: the access key identifies the principal and the
    secret authenticates it, and neither belongs in a prompt.

    Read-only, and best-effort about EXACTLY ONE thing: an env that has issued
    nothing has no file at all, which is an ordinary state and answers with an
    empty set. Everything else is a hard `ScrubSetUnreadable`, because "no
    credentials exist" and "I could not read which credentials exist" must
    never collapse into the same empty answer -- the second one silently
    un-scrubs a prompt.

    That distinction is the whole guard, so it is drawn on a real signal rather
    than an assumed one. PROBED against this file, every case:
        absent                          -> frozenset()          (the ONE best-effort case)
        well-formed                     -> the pairs
        truncated write                 -> was JSONDecodeError, now ScrubSetUnreadable
        empty file                      -> was JSONDecodeError, now ScrubSetUnreadable
        a list instead of an object     -> was AttributeError,  now ScrubSetUnreadable
        a null value                    -> was TypeError,       now ScrubSetUnreadable
        values are strings, not pairs   -> was a SET OF SINGLE CHARACTERS, now ScrubSetUnreadable
        mode 000                        -> was PermissionError, now ScrubSetUnreadable
    The second-to-last was the dangerous one: it raised nothing at all and
    quietly produced a garbage scrub set (see `_ISSUED_KEYS`)."""
    path = root / env / "keys.json"
    if not path.exists():
        return frozenset()
    try:
        pairs = _ISSUED_KEYS.validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ScrubSetUnreadable(path, exc) from exc
    return frozenset(value for pair in pairs.values() for value in pair if value)


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
    return debugger.assemble_context(
        stack, world, events, _logs_reader(store, stores, runtime, env), node_ids,
        extra_secrets=issued_credentials(store.root, env),
    )


def create_debug_router(
    store: SpecStore, stores: SynthStores, runtime, ws_manager: ConnectionManager,
    diagnose: Callable[..., Awaitable[dict]] | None = None,
) -> APIRouter:
    """`diagnose` is injectable purely as a test seam (the wiring tests drive
    the route without an SDK); every real caller leaves it None, which resolves
    `debugger.diagnose` per request (late, so it stays monkeypatchable)."""
    router = APIRouter()

    @router.post("/agent/debug")
    async def debug_route(body: DebugRequest, request: Request) -> DebugResponse:
        # This route takes its env in the BODY, and `server.py::_failure_body`
        # can only see query params -- so a failure here used to advise
        # `odin world --env default` for an env the caller never named. The
        # handler of last resort must stay pure string building (it cannot
        # await a body that may already be consumed), so the env is recorded
        # here instead, the earliest point it is known.
        request.state.env = body.env
        context = await build_context(store, stores, runtime, ws_manager, body.env, body.node_ids)
        # Every selected id unknown to odin => an honest answer naming them,
        # and NO model call: there is no evidence, so the only thing a run
        # could produce is confident noise (field test 2 finding #8).
        result = debugger.no_evidence_answer(context, body.node_ids) or await (
            diagnose or debugger.diagnose
        )(context, body.question or DEFAULT_QUESTION)
        return DebugResponse(
            env=body.env,
            answer=str(result.get("answer", "")),
            suspects=[Suspect(**s) for s in debugger.normalize_suspects(result.get("suspects"))],
        )

    return router
