"""S2.5 — canvas graph -> desired Stack (kinds, fields, refs)."""
from __future__ import annotations

from odin.spec.translate import canvas_to_stack, parse_ref


def test_parse_ref():
    assert parse_ref("DATABASE_URL", "${{db.DATABASE_URL}}") == \
        __import__("odin.spec.models", fromlist=["Ref"]).Ref(
            var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL")
    assert parse_ref("X", "literal") is None


def test_edges_thread_perms_and_kind_from_ui_data():
    # The UI stores edge access metadata under data.permissions + data.edgeType.
    canvas = {"nodes": [], "edges": [
        {"source": "api", "target": "db",
         "data": {"edgeType": "iam", "permissions": ["rds:GetItem", "rds:PutItem"]}},
        {"source": "api", "target": "cache", "data": {"edgeType": "network"}},
    ]}
    edges = {(e.src, e.dst): e for e in canvas_to_stack(canvas).edges}
    assert edges[("api", "db")].kind == "iam"
    assert edges[("api", "db")].perms == ("rds:GetItem", "rds:PutItem")
    assert edges[("api", "cache")].kind == "network" and edges[("api", "cache")].perms == ()


def test_edges_translate_reactflow_node_ids_to_labels():
    # Canvas edges carry ReactFlow node IDs, but Stack resources are keyed by
    # label — the reconciler's sns-subscription matching needs label edges.
    canvas = {"nodes": [
        {"id": "n1", "type": "sns", "data": {"label": "alerts"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
    ], "edges": [{"source": "n1", "target": "n2"}]}
    edge = canvas_to_stack(canvas).edges[0]
    assert (edge.src, edge.dst) == ("alerts", "jobs")


def test_edges_already_naming_labels_pass_through_unchanged():
    # Stack-level tests build edges with labels directly — no id to map.
    canvas = {"nodes": [
        {"id": "n1", "type": "sns", "data": {"label": "alerts"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
    ], "edges": [{"source": "alerts", "target": "jobs"}]}
    edge = canvas_to_stack(canvas).edges[0]
    assert (edge.src, edge.dst) == ("alerts", "jobs")


def test_canvas_to_stack_maps_kinds_fields_refs():
    canvas = {
        "nodes": [
            {"type": "rds", "data": {"label": "db", "engine": "postgres"}},
            {"type": "service", "data": {
                "label": "api", "image": "app:latest", "port": 8000,
                "env": {"DATABASE_URL": "${{db.DATABASE_URL}}", "STATIC": "v"},
            }},
            {"type": "vpc", "data": {"label": "ignored"}},  # unknown kind dropped
        ],
        "edges": [],
    }
    stack = canvas_to_stack(canvas)
    ids = {r.id for r in stack.resources}
    assert ids == {"db", "api"}  # vpc dropped

    db = next(r for r in stack.resources if r.id == "db")
    assert db.kind == "rds" and db.fields["engine"].value == "postgres"

    api = next(r for r in stack.resources if r.id == "api")
    assert api.kind == "service" and api.fields["image"].value == "app:latest"
    assert api.refs[0].target_id == "db" and api.refs[0].var == "DATABASE_URL"
    assert api.fields["env"].value == {"STATIC": "v"}  # ref lifted out of static env
