from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Response
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

    # `nodes` is REQUIRED; `edges` is not.
    #
    # An explicit `"nodes": []` is a legitimate instruction -- "this
    # environment should hold nothing" -- and Apply must keep honouring it.
    # An ABSENT `nodes` key is a malformed request, and defaulting it to `[]`
    # made those two indistinguishable at the point where the difference is
    # destructive. Measured against a live server before this change:
    #
    #     POST /apply-full?env=X  {"detail": "Internal Server Error"}
    #     -> HTTP 200 {"status": "applied"}, a real revision committed
    #
    # FastAPI's own error shape validated as a canvas of zero nodes, so a 500
    # from anywhere upstream -- a proxy, a flaky read, a client bug -- became
    # "tear down every resource in that environment". The v0.7.7 fix was
    # client-side (`ui/src/lib/canvasLoad.ts` refuses to hand such a body to
    # Apply); this closes the route itself, for every client.
    #
    # `edges` keeps its default: a canvas with nodes and no edges is ordinary,
    # and nothing destructive follows from assuming none.
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]] = []

    @model_validator(mode="after")
    def _must_be_translatable(self) -> CanvasGraph:
        problems = canvas_problems(self.model_dump())
        if problems:
            raise ValueError("this canvas cannot be applied — " + "; ".join(problems))
        return self


def canvas_revision(canvas_path: Path) -> str:
    """A content hash of the stored canvas -- the value a client echoes back in
    `If-Match` to say WHICH canvas its edit was based on.

    Content-addressed rather than a counter so it needs no extra state and
    survives a hand-edited file: two clients holding identical bytes agree, and
    any change at all produces a different revision. An absent file hashes as
    the empty canvas, so a fresh odin and one whose canvas was cleared behave
    the same.
    """
    raw = canvas_path.read_bytes() if canvas_path.exists() else json.dumps(_EMPTY).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def create_canvas_router(canvas_path: Path, ws=None) -> APIRouter:
    """`ws` is the `ConnectionManager`, optional so every existing caller (and
    every test) keeps working. When present, a successful save is broadcast so
    OTHER TABS CONVERGE instead of silently overwriting each other."""
    router = APIRouter()

    @router.get("/canvas")
    def get_canvas(response: Response) -> dict[str, Any]:
        """The stored canvas VERBATIM, never re-validated: whatever is on disk
        is what `odin canvas get` must hand back, or a canvas hand-edited into
        an unusable shape would be unreachable by the one command that could
        be used to repair it. (POST is where the shape is enforced.)

        The revision rides in the `ETag` HEADER, deliberately not in the body:
        the body must stay byte-for-byte what is on disk.
        """
        response.headers["ETag"] = canvas_revision(canvas_path)
        return json.loads(canvas_path.read_text()) if canvas_path.exists() else dict(_EMPTY)

    @router.post("/canvas")
    async def save_canvas(graph: CanvasGraph, response: Response, if_match: str | None = Header(None)) -> dict[str, str]:
        """Save, and tell everyone else.

        The canvas is GLOBAL by design -- one architecture, many environments --
        and every tab SHOULD show the same thing. What it did instead was
        diverge: each page held its own copy plus a debounced save, so whichever
        re-rendered last silently overwrote the others. Measured while recording
        the v0.7.7 GIFs, repeatedly, once replacing three applied resources with
        a single node from a tab left open in another window. Nothing warned,
        and the overwritten work was unrecoverable.

        Two mechanisms, because they cover different failures:

        * `If-Match` -- an OPTIONAL precondition. A client that sends the
          revision its edit was based on gets a 409 instead of clobbering a
          newer canvas. Optional so `odin canvas set`, curl and every existing
          test keep working unchanged; the UI sends it, so the case that
          actually bites a user is covered.
        * the `canvas_updated` broadcast -- so other tabs RELOAD rather than
          sit on a stale copy until their next render overwrites this one. This
          is what makes "the same canvas everywhere" true rather than aspirational.
        """
        if if_match is not None and if_match != canvas_revision(canvas_path):
            raise HTTPException(
                status_code=409,
                detail=(
                    "the canvas changed since you loaded it — another tab or command saved "
                    "first. Reload to see the current canvas, then re-apply your change."
                ),
            )
        # Security finding #3: a node's fields (e.g. an rds `password`) land
        # in this file in cleartext by design (the reconciler reads it back
        # verbatim) -- 0600 is the only thing stopping another local account
        # from reading it.
        atomic_write_text(canvas_path, graph.model_dump_json(indent=2), mode=0o600)
        revision = canvas_revision(canvas_path)
        response.headers["ETag"] = revision
        if ws is not None:
            # Carries the new revision so a tab that just saved can recognise
            # its OWN echo and skip reloading -- otherwise every save round-trips
            # into a reload, and a reload mid-edit is its own kind of data loss.
            await ws.broadcast({"type": "canvas_updated", "rev": revision})
        return {"status": "saved", "rev": revision}

    return router
