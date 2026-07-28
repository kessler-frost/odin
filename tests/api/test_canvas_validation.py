"""Field test 4, P4-5 — a malformed canvas is a CLIENT error, on every route
that takes a canvas.

`/apply`, `/apply-full` and `/translate` each died on one with a bare 500 and
"Internal Server Error", from an unhandled `ValidationError` two layers down
inside `canvas_to_stack`. They all take the same `CanvasGraph` body, so they
are all fixed by the same check -- and all four are pinned here.

The boundary is narrow on purpose: a node whose KIND odin cannot build is NOT
malformed (it applies, and is reported as `skipped`/`not_covered`), and a node
missing its `position` is repaired rather than refused. Only a structurally
impossible canvas is turned away.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime

# The exact shape field test 4 posted: `data.label` is a list, and an IAM edge
# points at that node (which is how the list reached `Edge(dst=...)`).
MALFORMED = {
    "nodes": [{"id": "s3", "type": "s3", "position": {"x": 0, "y": 0},
               "data": {"label": ["s3", "p2-assets"]}}],
    "edges": [{"id": "e1", "source": "s3", "target": "s3",
               "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}}],
}

# Well-formed, but `kinesis` is a kind odin has no coverage for at all.
UNSUPPORTED = {
    "nodes": [{"id": "k1", "type": "kinesis", "position": {"x": 0, "y": 0}, "data": {"label": "stream"}},
              {"id": "d1", "type": "rds", "position": {"x": 260, "y": 0}, "data": {"label": "db"}}],
    "edges": [],
}


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)


def test_every_canvas_route_refuses_a_malformed_canvas_with_4xx_and_a_reason(tmp_path):
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        for path in ("/canvas", "/apply", "/apply-full", "/translate"):
            response = client.post(path, json=MALFORMED)
            assert response.status_code == 422, (path, response.status_code)
            # No internals in the response body, ever: no Python traceback, no
            # module path, no exception class name.
            body = response.text
            assert "Traceback" not in body and "odin/spec" not in body and "ValidationError" not in body
            message = json.dumps(response.json())
            assert "node[0]" in message, message               # WHICH node
            assert "data.label" in message, message            # WHICH field
            assert "must be a string" in message, message      # and what's wrong with it


def test_an_unsupported_but_well_formed_kind_is_still_applied_and_still_reported(tmp_path):
    """THE BOUNDARY THAT MUST NOT MOVE (field test 4 verified this working):
    odin accepts nodes it cannot build and says so, rather than refusing the
    whole canvas."""
    app = _app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.post("/apply", json=UNSUPPORTED).json()
        assert body["status"] == "applied"
        assert body["skipped"] == ["kinesis"] and body["not_covered"] == ["kinesis"]
        assert [r.id for r in app.state.store.get_stack("default").resources] == ["db"]


def test_a_node_with_no_position_is_repaired_not_refused(tmp_path):
    """A missing `position` is a thing odin fixes for you (`odin canvas set`
    places it on the grid, the UI does the same) -- never a rejection."""
    graph = {"nodes": [{"id": "x1", "type": "rds", "data": {"label": "db"}}], "edges": []}
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        assert client.post("/apply", json=graph).json()["status"] == "applied"


# --- an ABSENT `nodes` key is malformed; an EXPLICIT empty one is an order ---
#
# These two must never collapse into each other, because the difference is
# destructive. Measured against a live server before `nodes` became required:
#
#     POST /apply-full?env=X  {"detail": "Internal Server Error"}
#     -> HTTP 200 {"status": "applied"}, a real revision committed
#
# FastAPI's own error shape validated as a canvas of zero nodes, so a 500 from
# anywhere upstream became "tear down every resource in that environment".

def test_a_body_with_no_nodes_key_is_refused_on_every_canvas_route(tmp_path):
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        for path in ("/canvas", "/apply", "/apply-full", "/translate"):
            for body in ({"detail": "Internal Server Error"}, {"error": "nope"}, {"edges": []}, {}):
                response = client.post(path, json=body)
                assert response.status_code == 422, (path, body, response.status_code, response.text)


def test_an_explicitly_empty_canvas_is_still_accepted_and_still_applies(tmp_path):
    # "Remove everything" is a real instruction and must keep working -- this
    # is the case the guard above must NOT catch.
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        assert client.post("/canvas", json={"nodes": [], "edges": []}).status_code == 200
        assert client.post("/apply", json={"nodes": [], "edges": []}).json()["status"] == "applied"


def test_edges_stay_optional(tmp_path):
    # A canvas with nodes and no edges is ordinary, and nothing destructive
    # follows from assuming none -- so `edges` keeps its default.
    graph = {"nodes": [{"id": "s1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "b"}}]}
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        assert client.post("/canvas", json=graph).status_code == 200
