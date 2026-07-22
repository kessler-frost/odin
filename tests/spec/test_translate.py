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


def test_iam_edges_survive_when_source_node_is_an_unknown_kind():
    # Post-ripout contract (NORTHSTAR.md): edges-as-grants outlive workload
    # kinds. A workload identity on the canvas (e.g. a phantom node standing
    # in for a principal that will be issued keys directly) isn't a runnable
    # resource kind, so `_resource()` drops it from Stack.resources -- but
    # its iam edge must still translate, since `labels` + `edges` are built
    # from ALL canvas nodes/edges, not filtered to known kinds.
    canvas = {
        "nodes": [
            {"id": "s3-node", "type": "s3", "data": {"label": "uploads"}},
            {"id": "worker-node", "type": "phantomWorkload", "data": {"label": "worker"}},
        ],
        "edges": [{"source": "worker-node", "target": "s3-node",
                   "data": {"edgeType": "iam", "permissions": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]}}],
    }
    stack = canvas_to_stack(canvas)
    assert [r.id for r in stack.resources] == ["uploads"]  # the unknown-kind node itself is dropped
    edge = stack.edges[0]
    assert (edge.src, edge.dst, edge.kind) == ("worker", "uploads", "iam")
    assert edge.perms == ("s3:PutObject", "s3:GetObject", "s3:ListBucket")


def test_canvas_to_stack_maps_kinds_fields_refs():
    canvas = {
        "nodes": [
            {"type": "rds", "data": {"label": "db", "engine": "postgres"}},
            {"type": "s3", "data": {
                "label": "uploads", "arn": "",
                "env": {"DATABASE_URL": "${{db.DATABASE_URL}}", "STATIC": "v"},
            }},
            {"type": "vpc", "data": {"label": "ignored"}},  # unknown kind dropped
        ],
        "edges": [],
    }
    stack = canvas_to_stack(canvas)
    ids = {r.id for r in stack.resources}
    assert ids == {"db", "uploads"}  # vpc dropped

    db = next(r for r in stack.resources if r.id == "db")
    assert db.kind == "rds" and db.fields["engine"].value == "postgres"

    bucket = next(r for r in stack.resources if r.id == "uploads")
    assert bucket.kind == "s3"
    assert bucket.refs[0].target_id == "db" and bucket.refs[0].var == "DATABASE_URL"
    assert bucket.fields["env"].value == {"STATIC": "v"}  # ref lifted out of static env
