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
            {"type": "phantomWorkload", "data": {"label": "ignored"}},  # unknown kind dropped
        ],
        "edges": [],
    }
    stack = canvas_to_stack(canvas)
    ids = {r.id for r in stack.resources}
    assert ids == {"db", "uploads"}  # the unknown-kind node is dropped, not the others

    db = next(r for r in stack.resources if r.id == "db")
    assert db.kind == "rds" and db.fields["engine"].value == "postgres"

    bucket = next(r for r in stack.resources if r.id == "uploads")
    assert bucket.kind == "s3"
    assert bucket.refs[0].target_id == "db" and bucket.refs[0].var == "DATABASE_URL"
    assert bucket.fields["env"].value == {"STATIC": "v"}  # ref lifted out of static env


def test_vpc_subnet_sg_round_trip_with_containment_fields():
    # V1c: the UI stamps data.vpc/data.subnet from spatial containment; the
    # translator must carry them (and cidr/ingressRules) into ResourceDesired
    # fields untouched — `_resource` copies every non-UI data key generically.
    canvas = {
        "nodes": [
            {"id": "n1", "type": "vpc", "data": {"label": "net", "cidr": "10.9.0.0/16", "status": "draft"}},
            {"id": "n2", "type": "subnet", "data": {"label": "web", "cidr": "10.9.1.0/24", "vpc": "net"}},
            {"id": "n3", "type": "sg", "data": {"label": "web-sg", "vpc": "net", "subnet": "web",
                                                 "ingressRules": "tcp:443:0.0.0.0/0"}},
        ],
        "edges": [],
    }
    stack = canvas_to_stack(canvas)
    by_id = {r.id: r for r in stack.resources}
    assert set(by_id) == {"net", "web", "web-sg"}

    assert by_id["net"].kind == "vpc" and by_id["net"].fields["cidr"].value == "10.9.0.0/16"
    assert "status" not in by_id["net"].fields  # UI-only field stays out

    assert by_id["web"].kind == "subnet"
    assert by_id["web"].fields["vpc"].value == "net"
    assert by_id["web"].fields["cidr"].value == "10.9.1.0/24"

    assert by_id["web-sg"].kind == "sg"
    assert by_id["web-sg"].fields["vpc"].value == "net"
    assert by_id["web-sg"].fields["subnet"].value == "web"
    assert by_id["web-sg"].fields["ingressRules"].value == "tcp:443:0.0.0.0/0"


def test_password_field_is_marked_sensitive():
    canvas = {"nodes": [{"type": "rds", "data": {"label": "db", "password": "hunter2"}}], "edges": []}
    db = canvas_to_stack(canvas).resources[0]
    assert db.fields["password"].sensitive is True
    assert db.fields["password"].value == "hunter2"  # the real value is still there -- only flagged


def test_ordinary_field_is_not_marked_sensitive():
    canvas = {"nodes": [{"type": "rds", "data": {"label": "db", "engine": "postgres"}}], "edges": []}
    db = canvas_to_stack(canvas).resources[0]
    assert db.fields["engine"].sensitive is False


def test_env_field_is_sensitive_if_any_entry_looks_like_a_secret():
    canvas = {"nodes": [{"type": "ecs", "data": {
        "label": "api", "env": {"DB_PASSWORD": "hunter2", "PORT": "8080"},
    }}], "edges": []}
    api = canvas_to_stack(canvas).resources[0]
    assert api.fields["env"].sensitive is True


def test_env_field_is_not_sensitive_when_nothing_looks_like_a_secret():
    canvas = {"nodes": [{"type": "ecs", "data": {
        "label": "api", "env": {"PORT": "8080", "LOG_LEVEL": "info"},
    }}], "edges": []}
    api = canvas_to_stack(canvas).resources[0]
    assert api.fields["env"].sensitive is False


def test_iam_role_and_ecr_translate_with_fields_passed_generically():
    # V2c: iam_role/ecr are pure gateway-model kinds like vpc/subnet/sg --
    # `_resource` needs no special-casing for them, just the _KIND mapping.
    canvas = {
        "nodes": [
            {"id": "n1", "type": "iam_role", "data": {"label": "lambda-exec", "inlinePolicy": '{"Version": "2012-10-17"}'}},
            {"id": "n2", "type": "ecr", "data": {"label": "app-image"}},
        ],
        "edges": [],
    }
    stack = canvas_to_stack(canvas)
    by_id = {r.id: r for r in stack.resources}
    assert set(by_id) == {"lambda-exec", "app-image"}

    assert by_id["lambda-exec"].kind == "iam_role"
    assert by_id["lambda-exec"].fields["inlinePolicy"].value == '{"Version": "2012-10-17"}'

    assert by_id["app-image"].kind == "ecr"
