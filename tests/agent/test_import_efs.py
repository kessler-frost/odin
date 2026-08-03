"""Terraform -> canvas for EFS file systems and the mounts that reach them.

The file system itself is the easy half. The MOUNTS are the half worth testing,
and they differ from every other companion in `import_tf`: an
`aws_volume_attachment` is a RESOURCE naming both ends, so its inverse is a
lookup, while an EFS mount is a nested block on the CONSUMER naming only the
file system. So the edge has to be REASSEMBLED -- from two places on an ecs task
definition (`volume {}` for the file system, `mountPoints[]` for the path) and
through an access point in the middle on a lambda.

A dropped mount is not a cosmetic loss: the regenerated project starts the
container with an empty directory where its shared data was.
"""
from __future__ import annotations

import json

from odin.iac import hcl
from odin.iac.hcl import generate_tf
from odin.iac.import_tf import parse_hcl_text
from odin.spec.translate import FILE_SYSTEM_MOUNT, canvas_to_stack

_FS = (
    'resource "aws_efs_file_system" "shared" {\n'
    '  creation_token = "shared"\n'
    "\n"
    "  tags = {\n"
    '    "odin:node" = "shared"\n'
    "  }\n"
    "}\n"
)
_ACCESS_POINT = (
    'resource "aws_efs_access_point" "shared_ap" {\n'
    "  file_system_id = aws_efs_file_system.shared.id\n"
    "\n"
    "  root_directory {\n"
    '    path = "/"\n'
    "  }\n"
    "}\n"
)
_CLUSTER = 'resource "aws_ecs_cluster" "odin" {\n  name = "odin"\n}\n'


def _container(**extra) -> str:
    definition = [{
        "name": "api",
        "image": "nginx:alpine",
        "essential": True,
        "portMappings": [{"containerPort": 80, "hostPort": 0, "protocol": "tcp"}],
        **extra,
    }]
    return hcl.quote(json.dumps(definition))


def _taskdef(source_volume: str = "shared", path: str = "/mnt/efs", volume: bool = True) -> str:
    mount_points = [{"sourceVolume": source_volume, "containerPath": path, "readOnly": False}]
    block = (
        "\n"
        "  volume {\n"
        '    name = "shared"\n'
        "\n"
        "    efs_volume_configuration {\n"
        "      file_system_id = aws_efs_file_system.shared.id\n"
        '      root_directory = "/"\n'
        "    }\n"
        "  }\n"
    ) if volume else ""
    return (
        'resource "aws_ecs_task_definition" "api_taskdef" {\n'
        '  family                   = "api"\n'
        '  requires_compatibilities = ["EC2"]\n'
        '  network_mode             = "bridge"\n'
        "\n"
        f"  container_definitions = {_container(mountPoints=mount_points)}\n"
        f"{block}"
        "}\n"
    )


_SERVICE = (
    'resource "aws_ecs_service" "api" {\n'
    '  name            = "api"\n'
    "  cluster         = aws_ecs_cluster.odin.id\n"
    "  task_definition = aws_ecs_task_definition.api_taskdef.arn\n"
    "  desired_count   = 1\n"
    '  launch_type     = "EC2"\n'
    "  wait_for_steady_state              = true\n"
    "  deployment_minimum_healthy_percent = 100\n"
    "  deployment_maximum_percent         = 200\n"
    "\n"
    "  tags = {\n"
    '    "odin:node" = "api"\n'
    "  }\n"
    "}\n"
)


def _lambda(path: str = "/mnt/efs", arn: str = "aws_efs_access_point.shared_ap.arn") -> str:
    return (
        'resource "aws_iam_role" "worker_role" {\n'
        '  name               = "worker-role"\n'
        '  assume_role_policy = "{}"\n'
        "}\n"
        "\n"
        'resource "aws_lambda_function" "worker" {\n'
        '  function_name    = "worker"\n'
        "  role             = aws_iam_role.worker_role.arn\n"
        '  handler          = "lambda_function.lambda_handler"\n'
        '  runtime          = "python3.12"\n'
        '  filename         = "worker.zip"\n'
        '  source_code_hash = filebase64sha256("worker.zip")\n'
        "\n"
        "  file_system_config {\n"
        f"    arn              = {arn}\n"
        f"    local_mount_path = {hcl.quote(path)}\n"
        "  }\n"
        "\n"
        "  tags = {\n"
        '    "odin:node" = "worker"\n'
        "  }\n"
        "}\n"
    )


def _project(*blocks: str) -> str:
    return "\n".join(blocks)


def _changed(result) -> list[str]:
    return [w for w in result.warnings if "CHANGED" in w]


def _lost(result) -> list[str]:
    return [w for w in result.warnings if "imported without" in w]


def _mount_edges(result) -> list[dict]:
    return [e for e in result.edges if (e.get("data") or {}).get("edgeType") == FILE_SYSTEM_MOUNT]


# --------------------------------------------------------------------------
# The node
# --------------------------------------------------------------------------

def test_a_file_system_becomes_an_efs_node():
    (node,) = parse_hcl_text(_FS).nodes
    assert node["type"] == "efs"
    # No `path` at all: the path is a property of the MOUNT, and nothing mounts
    # this one, so inventing one would put a field on the canvas the source
    # never stated.
    assert node["data"] == {"label": "shared"}


def test_the_label_comes_from_the_creation_token_not_the_hcl_resource_name():
    """`creation_token` is a `_NAME_ATTR` entry (`aws_efs_file_system` has no
    `name` argument -- the provider schema makes `name` COMPUTED), which is what
    lets a hand-authored project keep the name it chose.

    NO `odin:node` TAG, and that is the whole test. The first draft carried one
    and moved it in step with the token, so `_label`'s tag FALLBACK produced the
    same answer and deleting the `_NAME_ATTR` entry changed nothing -- a test
    whose name promised the entry was load-bearing while asserting something
    that held without it. Caught by mutation, not by review.
    """
    tf = (
        'resource "aws_efs_file_system" "shared" {\n'
        '  creation_token = "team-scratch"\n'
        "}\n"
    )
    (node,) = parse_hcl_text(tf).nodes
    assert node["id"] == "team-scratch" and node["data"]["label"] == "team-scratch"


def test_a_creation_token_odin_cannot_read_is_reported_as_a_RENAME():
    """The reason `creation_token` is in `_NAME_ATTR` rather than left to the
    `odin:node` fallback: `_renamed_by_import` only fires for a type that has an
    entry, so without it this file system would come back named `shared` (the
    bare HCL resource name) and be CREATED under that token, in silence."""
    tf = (
        'resource "aws_efs_file_system" "shared" {\n'
        '  creation_token = "${var.env}-shared"\n'
        "}\n"
    )
    result = parse_hcl_text(tf)
    (changed,) = _changed(result)
    assert "creation_token=${var.env}-shared" in changed
    assert "odin always emits shared" in changed


def test_an_argument_odin_does_not_model_is_reported_never_dropped():
    """`encrypted` is the one that matters: odin's substrate is a plain host
    directory and encrypts NOTHING, so carrying the argument onto the canvas
    would claim a property the substrate has not got."""
    tf = _FS.replace('  creation_token = "shared"',
                     '  creation_token = "shared"\n  encrypted      = true\n'
                     '  performance_mode = "maxIO"')
    (lost,) = _lost(parse_hcl_text(tf))
    assert lost.startswith("shared (efs): imported without unmodeled attribute(s):")
    assert "encrypted" in lost and "performance_mode" in lost


# --------------------------------------------------------------------------
# The ECS mount -- a JOIN across two places on the task definition
# --------------------------------------------------------------------------

def test_an_ecs_mount_becomes_a_mount_edge_carrying_the_container_path():
    result = parse_hcl_text(_project(_FS, _CLUSTER, _taskdef(), _SERVICE))
    assert result.unsupported == []
    (edge,) = _mount_edges(result)
    assert edge == {"source": "shared", "target": "api", "data": {"edgeType": "mount"}}
    efs = next(n for n in result.nodes if n["type"] == "efs")
    assert efs["data"]["path"] == "/mnt/efs"
    # ...and the mount stays an edge: neither half becomes a node of its own.
    assert {n["type"] for n in result.nodes} == {"efs", "ecs"}


def test_odins_own_ecs_mount_imports_without_a_single_warning():
    """Warning noise is not harmless in a module whose whole value is that its
    warnings are worth reading -- and odin generates this exact file."""
    assert parse_hcl_text(_project(_FS, _CLUSTER, _taskdef(), _SERVICE)).warnings == []


def test_a_volume_naming_no_imported_file_system_is_reported_as_a_LOST_mount():
    """The container comes back with an empty directory where its shared data
    was, so this is the loudest warning in the file."""
    tf = _project(_CLUSTER, _taskdef(), _SERVICE)  # no aws_efs_file_system at all
    result = parse_hcl_text(tf)
    assert _mount_edges(result) == []
    (warning,) = [w for w in result.warnings if "MOUNT IS LOST" in w]
    assert warning.startswith("api (ecs): its task definition mounts a volume named 'shared'")
    assert "empty directory where its shared data was" in warning


def test_a_volume_no_container_mounts_is_reported_and_makes_no_edge():
    """A declared-but-unmounted volume does nothing in ECS, and an odin edge
    MEANS mounted -- so drawing one would author a mount the source never had.
    Reported instead."""
    result = parse_hcl_text(_project(_FS, _CLUSTER, _taskdef(source_volume="other"), _SERVICE))
    assert _mount_edges(result) == []
    (warning,) = [w for w in result.warnings if "DECLARES this volume" in w]
    assert "sourceVolume = 'shared'" in warning


def test_a_host_volume_is_not_reported_as_a_lost_efs_mount():
    """A `host {}` volume is an ordinary bind mount, not an EFS one. Calling it
    a lost efs mount would be a lie about its type, and every task definition
    that uses one would carry a warning about a file system it never had."""
    taskdef = _taskdef(volume=False).replace(
        "}\n",
        "\n  volume {\n    name = \"scratch\"\n\n    host {\n"
        '      source_path = "/tmp/scratch"\n    }\n  }\n}\n',
    )
    result = parse_hcl_text(_project(_FS, _CLUSTER, taskdef, _SERVICE))
    assert result.warnings == [] and _mount_edges(result) == []


def test_a_transit_encrypted_mount_is_reported_because_odin_re_emits_nothing():
    """odin bind-mounts a host directory, so there is no TLS session to encrypt
    and no access point to authorize against. A mount the source encrypted
    coming back plain with nothing said is this module's worst failure."""
    taskdef = _taskdef().replace(
        '      root_directory = "/"\n',
        '      root_directory = "/"\n      transit_encryption = "ENABLED"\n',
    )
    (lost,) = _lost(parse_hcl_text(_project(_FS, _CLUSTER, taskdef, _SERVICE)))
    assert lost.startswith("shared -> api (efs mount): imported without unmodeled attribute(s)")
    assert "efs_volume_configuration.transit_encryption" in lost


def test_a_volume_rooted_at_a_subdirectory_is_reported_as_CHANGED():
    """odin re-roots the mount at `/`, so the container comes back seeing the
    WHOLE file system where the source confined it to one subtree. A widening,
    which is the direction worth a warning."""
    taskdef = _taskdef().replace('root_directory = "/"', 'root_directory = "/app-data"')
    (changed,) = _changed(parse_hcl_text(_project(_FS, _CLUSTER, taskdef, _SERVICE)))
    assert "efs_volume_configuration.root_directory=/app-data" in changed
    assert "sees the WHOLE file system" in changed


# --------------------------------------------------------------------------
# The lambda mount -- two hops, through the access point
# --------------------------------------------------------------------------

def test_a_lambda_mount_becomes_a_mount_edge_through_the_access_point():
    result = parse_hcl_text(_project(_FS, _ACCESS_POINT, _lambda()))
    assert result.unsupported == []
    (edge,) = _mount_edges(result)
    assert edge == {"source": "shared", "target": "worker", "data": {"edgeType": "mount"}}
    efs = next(n for n in result.nodes if n["type"] == "efs")
    assert efs["data"]["path"] == "/mnt/efs"


def test_the_access_point_never_becomes_a_node_and_is_never_unsupported():
    """It folds onto its file system the way an ecs cluster and an alb's target
    group do. Reporting it unsupported would put a warning on every single
    lambda-mounted canvas odin generates."""
    result = parse_hcl_text(_project(_FS, _ACCESS_POINT, _lambda()))
    assert {n["type"] for n in result.nodes} == {"efs", "lambda"}
    assert [e.type for e in result.unsupported] == []


def test_an_access_point_nothing_mounts_through_is_reported():
    """The alb target group's rule: a companion that folds onto nothing is
    REPORTED, because the regenerated project does not contain it."""
    result = parse_hcl_text(_project(_FS, _ACCESS_POINT))
    (entry,) = result.unsupported
    assert entry.type == "aws_efs_access_point" and entry.name == "shared_ap"
    assert "would NOT contain it" in entry.reason


def test_a_file_system_config_naming_no_access_point_is_a_LOST_mount():
    tf = _project(_FS, _lambda(arn="aws_efs_access_point.missing.arn"))
    result = parse_hcl_text(tf)
    assert _mount_edges(result) == []
    (warning,) = [w for w in result.warnings if "MOUNT IS LOST" in w]
    assert "names no imported aws_efs_access_point" in warning


def test_an_access_point_naming_no_file_system_is_a_LOST_mount():
    """The SECOND hop. Both are reported separately because they fail for
    different reasons and a single line would hide which one broke."""
    ap = _ACCESS_POINT.replace("aws_efs_file_system.shared.id", "aws_efs_file_system.gone.id")
    result = parse_hcl_text(_project(_FS, ap, _lambda()))
    assert _mount_edges(result) == []
    (warning,) = [w for w in result.warnings if "MOUNT IS LOST" in w]
    assert "has a `file_system_id` that does not either" in warning


def test_an_access_point_posix_user_is_reported_never_dropped():
    """`posix_user` forces every file the mount creates to one uid/gid, and odin
    re-emits nothing for it."""
    ap = _ACCESS_POINT.replace(
        "  root_directory {",
        "  posix_user {\n    uid = 1000\n    gid = 1000\n  }\n\n  root_directory {",
    )
    (lost,) = _lost(parse_hcl_text(_project(_FS, ap, _lambda())))
    assert lost.startswith("shared (efs): imported without unmodeled aws_efs_access_point ")
    assert "posix_user" in lost


def test_an_access_point_rooted_at_a_subdirectory_is_reported_as_CHANGED():
    ap = _ACCESS_POINT.replace('path = "/"', 'path = "/tenant-a"')
    (changed,) = _changed(parse_hcl_text(_project(_FS, ap, _lambda())))
    assert "root_directory=/tenant-a" in changed
    assert "sees the WHOLE file system" in changed


# --------------------------------------------------------------------------
# The per-consumer path -- one tile field, many possible mounts
# --------------------------------------------------------------------------

def test_consumers_that_disagree_about_the_path_are_reported_never_substituted():
    """AWS lets every consumer mount a file system wherever it likes; odin's
    tile has ONE `path` field. Substituting in silence is the elasticache bug in
    another costume -- a function told to read `/mnt/config` comes back reading
    `/mnt/efs`, finds an empty directory, and nothing said so."""
    result = parse_hcl_text(_project(
        _FS, _ACCESS_POINT, _CLUSTER, _taskdef(path="/mnt/tasks"), _SERVICE,
        _lambda(path="/mnt/config"),
    ))
    (changed,) = _changed(result)
    assert "api mounts it at /mnt/tasks" in changed
    assert "odin always emits /mnt/config to EVERY consumer" in changed
    assert "canvas tile carries ONE path field" in changed


def test_the_LAMBDA_path_wins_because_only_lambdas_have_a_pattern_to_break():
    """Not a tie-break for its own sake, and the asymmetry is AWS's own: Lambda's
    `LocalMountPath` carries a pattern (ONE segment under /mnt, enforced
    client-side by the provider) and ECS's `containerPath` carries none. So a
    lambda's path is always legal for an ecs task and `/data` is NOT legal for a
    lambda -- preferring the lambda's can never produce a project odin then
    declines by name, and preferring the other one can.

    Mutation-test: flip the `entry[0] != "lambda"` sort key in `_stamp_efs_paths`
    and this fails with `path == "/data"` and a generator that refuses the node.
    """
    result = parse_hcl_text(_project(
        _FS, _ACCESS_POINT, _CLUSTER, _taskdef(path="/data"), _SERVICE,
        _lambda(path="/mnt/config"),
    ))
    efs = next(n for n in result.nodes if n["type"] == "efs")
    assert efs["data"]["path"] == "/mnt/config"
    # ...and the chosen path is one the generator accepts, which is the point.
    assert hcl._MOUNT_PATH.fullmatch(efs["data"]["path"])
    assert not [w for w in result.warnings if "generator REFUSES" in w]


def test_an_ecs_only_path_under_no_pattern_is_imported_WITHOUT_a_warning():
    """`/data` is a perfectly legal ecs-only mount: ECS's `containerPath` carries
    no pattern (only Lambda's `LocalMountPath` does), and odin's bind-mount
    substrate serves it happily. So the import must say NOTHING about it.

    This test previously asserted the opposite, and the history is the lesson.
    `hcl.py::_efs_fault` once applied Lambda's pattern to every efs node
    regardless of who mounted it, so this canvas really did regenerate with the
    file system dropped -- and the import warned about it, correctly, with the
    generator's own regex as the source of truth. When `_efs_fault` was narrowed
    to decline only the offending FUNCTION, the regex stayed true and the
    CONCLUSION drawn from it went false: a warning telling the user their file
    system would disappear, about a canvas that regenerates intact.

    A guard reading a real signal is not the same as a guard drawing a true
    conclusion from it, and only the round trip below could tell the two apart.
    """
    result = parse_hcl_text(_project(_FS, _CLUSTER, _taskdef(path="/data"), _SERVICE))
    efs = next(n for n in result.nodes if n["type"] == "efs")
    assert efs["data"]["path"] == "/data", "the source's own path, not a substitution"
    assert result.warnings == [], result.warnings

    # ...and that silence is TRUE, asked of the generator rather than asserted.
    again = generate_tf(canvas_to_stack({"nodes": result.nodes, "edges": result.edges}))
    assert 'resource "aws_efs_file_system"' in again.files["main.tf"]
    assert not any(gap.startswith("shared (efs):") for gap in again.unsupported), again.unsupported


def test_a_path_no_LAMBDA_can_mount_is_reported_at_IMPORT_time():
    """The case that survives the narrowing, and it is reachable rather than
    theoretical: `_stamp_efs_paths` prefers a lambda's path precisely so this
    cannot happen -- but a lambda whose own `local_mount_path` is COMPUTED
    contributes no readable path, so an ecs consumer's `/data` wins and lands on
    a node that function mounts.

    Reported at import rather than left to surface as an Apply that silently
    drops a function (`_stamp_containment`'s rule, field test U2).
    """
    result = parse_hcl_text(_project(
        _FS, _ACCESS_POINT, _CLUSTER, _taskdef(path="/data"), _SERVICE,
        _lambda(path="${var.mount}"),
    ))
    efs = next(n for n in result.nodes if n["type"] == "efs")
    assert efs["data"]["path"] == "/data"
    (warning,) = [w for w in result.warnings if "cannot mount" in w]
    assert "one segment under /mnt" in warning
    assert "declines the FUNCTION by name" in warning

    # The warning is TRUE, and specifically about the FUNCTION: the file system
    # and its ecs mount survive, which is exactly what the earlier version of
    # this guard got wrong.
    again = generate_tf(canvas_to_stack({"nodes": result.nodes, "edges": result.edges}))
    assert 'resource "aws_efs_file_system"' in again.files["main.tf"]
    assert any("worker" in gap for gap in again.unsupported), again.unsupported


def test_one_consumer_mounting_the_same_file_system_twice_makes_ONE_edge():
    """Two edges between the same two nodes is a canvas the UI cannot draw and a
    round trip that is not stable, so the pair is de-duplicated. The second path
    is still REPORTED -- odin can express one mount per (file system, consumer)
    pair and the source asked for two."""
    # Built through `_container`, NOT by string-replacing the rendered file:
    # `container_definitions` is a QUOTED json literal, so a `.replace()` of the
    # plain JSON matches nothing and silently leaves the second mountPoint out.
    # The first draft did exactly that and "passed" the edge count while proving
    # nothing -- the harness, not the code, was what failed.
    mount_points = [
        {"sourceVolume": "shared", "containerPath": "/mnt/efs", "readOnly": False},
        {"sourceVolume": "shared2", "containerPath": "/mnt/second", "readOnly": False},
    ]
    volume = (
        "  volume {{\n    name = \"{name}\"\n\n    efs_volume_configuration {{\n"
        "      file_system_id = aws_efs_file_system.shared.id\n"
        "      root_directory = \"/\"\n    }}\n  }}\n"
    )
    taskdef = (
        'resource "aws_ecs_task_definition" "api_taskdef" {\n'
        '  family = "api"\n'
        f"  container_definitions = {_container(mountPoints=mount_points)}\n\n"
        f"{volume.format(name='shared')}\n{volume.format(name='shared2')}"
        "}\n"
    )
    result = parse_hcl_text(_project(_FS, _CLUSTER, taskdef, _SERVICE))
    assert len(_mount_edges(result)) == 1
    (changed,) = _changed(result)
    assert "api mounts it at /mnt/second" in changed


def test_a_path_odin_cannot_read_is_reported_rather_than_defaulted_in_silence():
    result = parse_hcl_text(_project(_FS, _ACCESS_POINT, _lambda(path="${var.mount}")))
    efs = next(n for n in result.nodes if n["type"] == "efs")
    assert "path" not in efs["data"], "odin's default must not pass for the source's own"
    (warning,) = [w for w in result.warnings if "cannot read as a literal" in w]
    assert warning.startswith("shared (efs): worker mount(s) it at a `local_mount_path`")
    # The MOUNT itself is real and its edge still comes back -- only the path is
    # unknown. Dropping the edge would detach a live file system on the next apply.
    assert len(_mount_edges(result)) == 1


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------

_CANVAS = {
    "nodes": [
        {"id": "n1", "type": "efs", "data": {"label": "shared", "path": "/mnt/efs"}},
        {"id": "n2", "type": "ecs", "data": {"label": "api", "image": "nginx:alpine",
                                             "count": "1", "port": "80"}},
        {"id": "n3", "type": "lambda", "data": {
            "label": "worker",
            "code": "def lambda_handler(event, context):\n    return event\n",
        }},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "data": {"edgeType": "mount"}},
        # Drawn the OTHER way, and with the pre-registry `"network"` type name.
        # `hcl.py`'s mount pass keys on the two node KINDS, never on the type
        # name, so both directions and both spellings must produce the identical
        # file.
        #
        # THIS IS NOT A HYPOTHETICAL CANVAS, and `efs-CONTRACT.md` says it is.
        # The contract argues the `edge.kind`-gate hazard "does not bite here"
        # because placeholders are filtered from the palette so "no saved canvas
        # can contain an efs node" -- then adds "Do not claim to have dodged a
        # live bullet." Both clauses are false, checked against git:
        #   ac796d6 (2026-06-20) shipped the EFS tile with sublabel
        #     'Elastic file system' -- no `(placeholder)` marker at all;
        #   1b158fe (2026-07-26) added the marker, five weeks later;
        #   41d214b (2026-07-27) hid placeholders and says in its own message
        #     "Hiding is PALETTE-ONLY: CATALOG keeps every entry, so a canvas
        #     already containing a placeholder node still renders properly."
        # So real saved canvases hold efs nodes with `network`-typed edges, and
        # gating on the type name would drop their mounts on the next apply.
        {"id": "e2", "source": "n3", "target": "n1", "data": {"edgeType": "network"}},
    ],
}


def test_a_canvas_with_both_mounts_survives_generate_import_generate():
    """THE claim: byte-identical, over efs + ecs + lambda + both mount edges.

    Without the edges coming back, the second generate emits no `volume` block
    and no `file_system_config`, so the next apply unmounts a live file system
    from both consumers -- and the import would have looked clean.
    """
    project = generate_tf(canvas_to_stack(_CANVAS))
    main_tf = project.files["main.tf"]
    assert 'resource "aws_efs_file_system"' in main_tf
    assert 'resource "aws_efs_access_point"' in main_tf
    assert "file_system_config" in main_tf
    assert "efs_volume_configuration" in main_tf
    assert "mountPoints" in main_tf

    imported = parse_hcl_text(main_tf)
    assert imported.unsupported == [], [e.type for e in imported.unsupported]
    # The ONE expected warning is `_stamp_lambda`'s standing one -- a function's
    # body lives in a zip beside main.tf, so TEXT mode can never recover it. It
    # is subtracted by name rather than by loosening the assertion to
    # "no efs warnings", which would stop this test noticing a new one.
    assert [w for w in imported.warnings if "its CODE could not be imported" not in w] == [], (
        imported.warnings
    )
    assert {(e["source"], e["target"]) for e in _mount_edges(imported)} == {
        ("shared", "api"), ("shared", "worker"),
    }

    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf, "generate -> import -> generate must be stable"


def test_the_round_trip_keeps_a_non_default_mount_path():
    """DO NOT "SIMPLIFY" `/mnt/scratch` BACK TO `/mnt/efs`. The value being
    non-default is the entire test, and the reason lives here because this is
    where the temptation is.

    A round-trip assertion over DEFAULT values proves the defaults agree, not
    that the data survives. `/mnt/efs` round-trips byte-identically **even if
    the path is dropped entirely** -- `hcl.py::_DEFAULT_EFS_PATH` refills it,
    full-file equality passes, and the field this test exists to protect is
    silently unprotected. Choosing a value the default cannot reproduce is what
    turns the assertion into a measurement.

    Measured rather than reasoned, over an efs+ecs canvas generated three ways:

        path = "/mnt/efs"      vs path ABSENT  -> IDENTICAL
        path = "/mnt/scratch"  vs path ABSENT  -> DIFFER

    The first line is the trap: with `/mnt/efs` the assertion below cannot tell
    a carried path from a lost one. The second is why this test can.
    """
    canvas = {**_CANVAS, "nodes": [
        {**_CANVAS["nodes"][0], "data": {"label": "shared", "path": "/mnt/scratch"}},
        *_CANVAS["nodes"][1:],
    ]}
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert "/mnt/scratch" in main_tf

    imported = parse_hcl_text(main_tf)
    efs = next(n for n in imported.nodes if n["type"] == "efs")
    assert efs["data"]["path"] == "/mnt/scratch"
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf


def test_two_services_sharing_one_file_system_come_back_as_two_edges():
    """SHARING is the entire feature, and it is what makes `mount` a different
    edge kind from `volume`: a gp3 volume attaches to exactly one instance, an
    EFS file system is mounted by many consumers at once."""
    canvas = {
        "nodes": [
            {"id": "n1", "type": "efs", "data": {"label": "shared", "path": "/mnt/efs"}},
            {"id": "n2", "type": "ecs", "data": {"label": "api", "image": "nginx:alpine"}},
            {"id": "n3", "type": "ecs", "data": {"label": "jobs", "image": "nginx:alpine"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "data": {"edgeType": "mount"}},
            {"id": "e2", "source": "n1", "target": "n3", "data": {"edgeType": "mount"}},
        ],
    }
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    imported = parse_hcl_text(main_tf)
    assert imported.warnings == [] and imported.unsupported == []
    assert {(e["source"], e["target"]) for e in _mount_edges(imported)} == {
        ("shared", "api"), ("shared", "jobs"),
    }
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf


def test_an_efs_node_nothing_mounts_still_round_trips():
    """A file system with no consumers is legal -- storage waiting for a mount --
    and must not acquire a phantom access point or a phantom path."""
    canvas = {"nodes": [{"id": "n1", "type": "efs", "data": {"label": "shared"}}], "edges": []}
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert "aws_efs_access_point" not in main_tf
    imported = parse_hcl_text(main_tf)
    assert imported.warnings == [] and imported.unsupported == []
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf
