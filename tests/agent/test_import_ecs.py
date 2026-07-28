"""Terraform -> canvas for `aws_ecs_service`, and the one thing it cannot bring.

One canvas `ecs` node is THREE Terraform resources, so the interesting part is
which of them becomes a node: only the service. The task definition folds onto
it (image, port, memory and cpu all live THERE, not on the service, so without
the fold a service comes back as an nginx placeholder with no port), and the
cluster is a singleton odin always emits exactly one of.

## The honest gap, which is not the importer's fault

A workload's `${{db.DATABASE_URL}}` refs are deliberately NEVER written into the
generated HCL. `hcl.py`'s `_WIRED_KINDS` note has the reason: a resolved
DATABASE_URL carries the database password, so interpolating it would put a
credential into `terraform.tfstate` in plaintext and drift on every plan. The
values are delivered into the container at launch by `gateway/wiring.py` instead.

A round trip keeps NEITHER the values nor the ordering, and the second half of
that surprised me while writing these tests. odin does not carry `depends_on`
across as an argument — it RE-DERIVES it from the node's own refs
(`hcl.py::_ref_dependencies`), and those refs are exactly what cannot come back,
so a re-generated project has no ordering either. My first draft of the warning
said "only the ORDERING survives", which would have reassured someone that tofu
still sequenced their database ahead of the service consuming it.

An imported service would otherwise start with no configuration it had on the
canvas, silently — so the import names the producers, out of the source's own
`depends_on`, and says the refs must be re-added. That is the most an import can
honestly offer here, and stating it is the whole point.

## Placement

`placement_constraints { type = "memberOf" }` is the owner's "an ecs box inside
an ec2 box means ecs ON ec2" gesture as it survives Terraform. Losing it would
move a workload back onto the shared host -- a different machine with different
memory -- and report a clean import while doing it.
"""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

CANVAS = {
    "nodes": [
        {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0},
         "data": {"label": "api", "image": "ghcr.io/acme/api:1.4.2", "count": "3",
                  "port": "8080", "memory": "1024", "cpu": "512"}},
    ],
    "edges": [],
}


def _round_trip(canvas: dict):
    tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    return parse_hcl_text(tf), tf


def _node(result, label: str) -> dict:
    return next(n for n in result.nodes if n["id"] == label)


def test_only_the_service_becomes_a_node():
    """Three tf resources, one canvas node. A node per resource would multiply
    the canvas on every round trip."""
    result, _tf = _round_trip(CANVAS)
    assert [n["type"] for n in result.nodes] == ["ecs"]
    listed = {e.type for e in result.unsupported}
    assert not listed & {"aws_ecs_service", "aws_ecs_task_definition", "aws_ecs_cluster"}, listed


def test_the_image_and_port_come_off_the_TASK_DEFINITION():
    """Both live in `container_definitions`, a JSON string on the other resource.
    Without the fold the service is an nginx placeholder on no port."""
    result, _tf = _round_trip(CANVAS)
    data = _node(result, "api")["data"]
    assert data["image"] == "ghcr.io/acme/api:1.4.2"
    assert data["port"] == "8080"


def test_the_task_resources_round_trip():
    """`memory` is a HARD cap odin enforces (v0.8.2), so importing it as absent
    would silently drop a 1 GiB container to the 512 MiB default."""
    result, _tf = _round_trip(CANVAS)
    data = _node(result, "api")["data"]
    assert data["memory"] == "1024"
    assert data["cpu"] == "512"


def test_the_desired_count_round_trips():
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "api")["data"]["count"] == "3"


def test_the_whole_thing_regenerates_byte_for_byte():
    _result, first = _round_trip(CANVAS)
    imported, _ = _round_trip(CANVAS)
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == first


def test_odins_own_output_produces_no_warnings():
    result, _tf = _round_trip(CANVAS)
    assert result.warnings == [], result.warnings


# --- placement ----------------------------------------------------------------

PLACED = {
    "nodes": [
        {"id": "v1", "type": "vpc", "position": {"x": 0, "y": 0},
         "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "s1", "type": "subnet", "position": {"x": 0, "y": 0},
         "data": {"label": "app-subnet", "vpc": "prod-vpc", "cidr": "10.0.1.0/24"}},
        {"id": "e1", "type": "ec2", "position": {"x": 0, "y": 0},
         "data": {"label": "api-server", "subnet": "app-subnet", "instanceType": "t3.small"}},
        {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0},
         "data": {"label": "api", "image": "nginx:alpine", "count": "1", "port": "80",
                  "host": "api-server"}},
    ],
    "edges": [],
}


def test_placement_survives_the_round_trip():
    """The gesture, as Terraform carries it. Losing `host` moves the workload
    back to the shared host and reports a clean import."""
    result, _tf = _round_trip(PLACED)
    assert _node(result, "api")["data"]["host"] == "api-server"


def test_a_placed_canvas_regenerates_byte_for_byte():
    _result, first = _round_trip(PLACED)
    imported, _ = _round_trip(PLACED)
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == first


# --- the wiring gap -----------------------------------------------------------

WIRED = {
    "nodes": [
        {"id": "d1", "type": "rds", "position": {"x": 0, "y": 0}, "data": {"label": "app-db"}},
        {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0},
         "data": {"label": "api", "image": "nginx:alpine", "count": "1", "port": "80",
                  "env": {"DATABASE_URL": "${{app-db.DATABASE_URL}}"}}},
    ],
    "edges": [],
}


# A project odin did NOT write: it has ordering and no `odin:ref:` tags, which
# is the only shape whose wiring genuinely cannot be rebuilt. The two tests below
# used to generate their fixture, which meant that the day the emitter started
# tagging refs they would have been measuring the recoverable case while claiming
# the unrecoverable one -- passing, and testing something else.
WIRED_HCL = '''
resource "aws_db_instance" "app_db" {
  identifier = "app-db"
  engine     = "postgres"
}

resource "aws_ecs_cluster" "odin" {
  name = "odin"
}

resource "aws_ecs_task_definition" "api_taskdef" {
  family                = "api"
  container_definitions = "[{\\"name\\": \\"api\\", \\"image\\": \\"nginx:alpine\\"}]"
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.odin.id
  task_definition = aws_ecs_task_definition.api_taskdef.arn

  depends_on = [aws_db_instance.app_db]
}
'''


def test_no_resolved_credential_reaches_the_generated_hcl():
    """This test used to assert `"DATABASE_URL" not in tf`, and it was renamed
    from `..._is_reported_as_unrecoverable_...` because BOTH halves of the old
    name became untrue: the wiring is recoverable now, and the check it made was
    not the check it meant.

    Greping for the variable NAME was always a proxy. Once a ref travels as
    `"odin:ref:DATABASE_URL" = "app-db.DATABASE_URL"`, the proxy fires on a tag
    KEY that carries no secret at all, while the property actually worth
    defending -- no resolved VALUE in the file -- would still hold. Measured: the
    only occurrence of the password is `aws_db_instance.password`, the master
    password Terraform must send in order to create the database, and there is no
    `postgresql://` string anywhere.

    So it asserts the real thing in three parts: the credential is absent
    everywhere but the one block that legitimately needs it, the resolved URL
    SCHEME is absent entirely, and -- positively, so the test cannot pass by the
    file simply not having wiring in it -- the reference form is present.

    Mutation-tested by making `_ref_tags` emit a resolved `postgresql://...`
    value instead of `<producer>.<attr>`: this test fails, the old
    name-substring assertion did not.
    """
    canvas = {
        "nodes": [
            {"id": "d1", "type": "rds", "position": {"x": 0, "y": 0},
             "data": {"label": "app-db", "password": "canvas-password-fixture"}},
            {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0},
             "data": {"label": "api", "image": "nginx:alpine", "count": "1", "port": "80",
                      "env": {"DATABASE_URL": "${{app-db.DATABASE_URL}}",
                              "API_TOKEN": "canvas-token-fixture"}}},
        ],
        "edges": [],
    }
    tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]

    # 1. The credential appears ONLY in the aws_db_instance block.
    blocks = [b for b in tf.split("\nresource ") if "canvas-password-fixture" in b]
    assert len(blocks) == 1, blocks
    assert blocks[0].startswith('"aws_db_instance"'), blocks[0][:60]

    # 2. No RESOLVED endpoint, in any form. This is the string a ref becomes at
    #    launch, and the one that carries the password into tfstate if emitted.
    assert "postgresql://" not in tf
    # 3. ...nor a static env value, which a user may well have typed a secret into.
    assert "canvas-token-fixture" not in tf

    # 4. Positively: the REFERENCE is carried, so this cannot pass vacuously on a
    #    file that simply has no wiring.
    assert '"odin:ref:DATABASE_URL" = "app-db.DATABASE_URL"' in tf


def test_a_file_without_ref_tags_reports_the_wiring_as_unrecoverable():
    """The case the old test's name described, on a fixture that genuinely has
    it: a hand-authored project with ordering and no `odin:ref:` tags."""
    result = parse_hcl_text(WIRED_HCL)
    (warning,) = [w for w in result.warnings if "wiring" in w]
    assert "app-db" in warning, warning
    assert "re-add the env references" in warning, warning


def test_the_env_wiring_round_trips_from_odins_own_output():
    """The READ half, against the REAL emitter now that it is on develop: the
    refs come back and the ordering comes back with them."""
    canvas = {
        "nodes": [
            {"id": "d1", "type": "rds", "position": {"x": 0, "y": 0}, "data": {"label": "app-db"}},
            {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0},
             "data": {"label": "api", "image": "nginx:alpine", "count": "1", "port": "80",
                      "env": {"DATABASE_URL": "${{app-db.DATABASE_URL}}"}}},
        ],
        "edges": [],
    }
    first = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    imported = parse_hcl_text(first)
    api = next(n for n in imported.nodes if n["type"] == "ecs")
    assert api["data"]["env"] == {"DATABASE_URL": "${{app-db.DATABASE_URL}}"}
    assert [w for w in imported.warnings if "wiring" in w] == []

    second = generate_tf(
        canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges})
    ).files["main.tf"]
    assert second == first, "generate -> import -> generate must be byte-stable"


def test_not_even_the_ordering_survives_and_the_warning_says_so():
    """I wrote this test expecting `depends_on` to survive, and it does not.

    odin does not carry `depends_on` across as an argument -- it RE-DERIVES it
    from the node's own `${{producer.ATTR}}` refs (`hcl.py::_ref_dependencies`).
    Those refs are exactly what an import cannot recover, so a re-generated
    project has no ordering either, and the first draft of this warning claiming
    "only the ORDERING survives" was wrong in the direction that matters: it
    would have reassured someone that tofu still sequenced their database ahead
    of the service consuming it.

    v0.8.14 narrows WHEN this holds without changing that it holds: the refs are
    recoverable now, from the `odin:ref:<VAR>` tags `hcl.py` emits, so a file
    that HAS them round-trips its ordering (see
    `tests/agent/test_import_wiring.py`). `WIRED_HCL` deliberately has none -- a
    hand-authored project, or one from an older odin -- and for it the reasoning
    above is unchanged and the warning must still say so.
    """
    result = parse_hcl_text(WIRED_HCL)
    regenerated = generate_tf(canvas_to_stack(
        {"nodes": result.nodes, "edges": result.edges})).files["main.tf"]
    assert "depends_on" not in regenerated
    (warning,) = [w for w in result.warnings if "wiring" in w]
    assert "odin re-derives that FROM the references" in warning, warning
    assert "loses the ordering too" in warning, warning


def test_a_service_whose_task_definition_is_missing_says_so():
    tf = '''
resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = "arn:aws:ecs:us-east-1:000000000000:cluster/odin"
  task_definition = aws_ecs_task_definition.gone.arn
  desired_count   = 1
}
'''
    result = parse_hcl_text(tf)
    (warning,) = [w for w in result.warnings if "task_definition" in w]
    assert "DEFAULT image" in warning, warning


def test_a_service_placed_in_an_instance_with_no_env_refs_does_NOT_warn():
    """Found end to end, through the real CLI, after the unit tests were green.

    `depends_on` has TWO sources in hcl.py -- the node's env refs and
    `_placement_dependency` (the instance a placed service must not start before)
    -- and the first draft of the wiring warning could not tell them apart. A
    service drawn inside an ec2 box with no env refs at all was told to "re-add
    the env references it consumed", about references it had never had. A warning
    that fires on a correct import is worse than none: it is exactly how people
    learn to skip them.
    """
    result, _tf = _round_trip(PLACED)
    assert _node(result, "api")["data"]["host"] == "api-server"
    assert result.warnings == [], result.warnings
