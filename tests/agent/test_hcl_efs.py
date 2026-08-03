"""EFS file systems and their mounts, generate-side.

An `aws_efs_file_system` on its own is storage nothing can reach, so the feature
is really the two COMPANIONS -- a `volume` + `mountPoints` pair on an ecs task
definition, and a `file_system_config` on a lambda. That is where the expensive
mistakes live, and it is why this file exists rather than a few lines in
test_hcl.py:

  * a `volume` no container references mounts NOTHING, and a `mountPoints` entry
    naming a volume that does not exist is a RegisterTaskDefinition ECS rejects
    -- the two halves have to name each other or the drawn line does nothing;
  * `file_system_config.arn` is an ACCESS POINT arn (AWS's pattern for it ends
    `access-point/fsap-[a-f0-9]{17}`), so handing it a file-system arn produces a
    project real AWS rejects at CreateFunction;
  * `local_mount_path` takes exactly ONE segment under `/mnt`, which is not
    guessable from the field's name and is rejected at CreateFunction, not here
    -- and it binds ONLY a lambda, so applying it to every efs node refuses an
    ecs-only canvas that AWS and odin's own substrate both accept;
  * a mount naming a file system the generator DECLINED is an unresolvable
    reference, which fails `tofu plan` for the whole project -- not just for that
    node.

The real `tofu validate` over the generated three-node file lives in
`test_granted_workload_hcl_validates.py`, which is the only check here that reads
the provider schema.
"""
from __future__ import annotations

import json

import botocore.session

from odin.agent.hcl import (
    _LOCAL_MOUNT_PATH_PATTERN,
    _MAX_CREATION_TOKEN,
    _efs_fault,
    generate_tf,
    sanitize_name,
)
from odin.spec.translate import FILE_SYSTEM_MOUNT, canvas_to_stack

_SERVICE = {"id": "svc", "type": "ecs", "data": {"label": "api", "image": "nginx:alpine"}}
_FUNCTION = {"id": "fn", "type": "lambda",
             "data": {"label": "worker", "runtime": "python3.12",
                      "code": "def lambda_handler(event, context):\n    return event\n"}}


def _efs(node_id: str, label: str, **data) -> dict:
    return {"id": node_id, "type": "efs", "data": {"label": label, **data}}


def _edge(source: str, target: str, edge_type: str = FILE_SYSTEM_MOUNT) -> dict:
    return {"id": f"{source}-{target}", "source": source, "target": target,
            "data": {"edgeType": edge_type}}


def _project(nodes: list[dict], edges: list[dict]):
    return generate_tf(canvas_to_stack({"nodes": nodes, "edges": edges}))


def _access_points_named(main_tf: str) -> list[str]:
    """Every `aws_efs_access_point.<name>.arn` the file REFERENCES."""
    return [line.split("aws_efs_access_point.")[1].split(".arn")[0]
            for line in main_tf.splitlines() if "aws_efs_access_point." in line and ".arn" in line]


def _container_definitions(main_tf: str) -> list[dict]:
    """The taskdef's `container_definitions`, parsed. It is a JSON string
    LITERAL in the HCL, so it needs unquoting before it is JSON."""
    (line,) = [ln for ln in main_tf.splitlines() if "container_definitions" in ln]
    return json.loads(json.loads(line.split(" = ", 1)[1]))


def test_an_efs_node_emits_a_file_system_keyed_by_its_creation_token():
    """The whole primary resource, byte for byte. `creation_token` is the only
    argument: `name` is COMPUTED in the real provider schema (measured), so the
    canvas label reaches the file system through `odin:node` exactly as it
    reaches a kms key."""
    main_tf = _project([_efs("fs", "shared-data")], []).files["main.tf"]
    assert (
        'resource "aws_efs_file_system" "shared_data" {\n'
        '  creation_token = "shared-data"\n'
        "\n"
        "  tags = {\n"
        '    "odin:node" = "shared-data"\n'
        "  }\n"
        "}"
    ) in main_tf
    # No encrypted/kms_key_id (odin encrypts nothing here) and no
    # performance_mode/throughput_mode (the tile authors neither).
    for absent in ("encrypted", "kms_key_id", "performance_mode", "throughput_mode"):
        assert absent not in main_tf


def test_an_efs_node_nobody_mounts_is_a_free_standing_file_system():
    """Not an error, and not a dangling access point either -- the `_ECS_CLUSTER_KEY`
    rule: never a companion resource on a canvas that does not need one."""
    project = _project([_SERVICE, _efs("fs", "shared-data")], [])
    assert 'resource "aws_efs_file_system" "shared_data"' in project.files["main.tf"]
    assert "aws_efs_access_point" not in project.files["main.tf"]
    assert "efs_volume_configuration" not in project.files["main.tf"]
    assert project.unsupported == []


def test_an_efs_edged_to_an_ecs_service_mounts_it_on_that_task_definition():
    project = _project([_SERVICE, _efs("fs", "shared-data")], [_edge("fs", "svc")])
    main_tf = project.files["main.tf"]
    assert project.unsupported == []
    assert (
        "  volume {\n"
        '    name = "shared-data"\n'
        "\n"
        "    efs_volume_configuration {\n"
        "      file_system_id = aws_efs_file_system.shared_data.id\n"
        '      root_directory = "/"\n'
        "    }\n"
        "  }"
    ) in main_tf
    (definition,) = _container_definitions(main_tf)
    assert definition["mountPoints"] == [
        {"sourceVolume": "shared-data", "containerPath": "/mnt/efs", "readOnly": False},
    ]


def test_the_mount_point_and_the_volume_name_each_other():
    """The half a `tofu validate` cannot see. A `volume` no container references
    mounts nothing, and a `mountPoints` entry naming a volume that does not exist
    is rejected by RegisterTaskDefinition -- either mistake is a drawn line that
    does nothing, and both files parse perfectly."""
    main_tf = _project(
        [_SERVICE, _efs("fs", "shared-data", path="/mnt/data")], [_edge("fs", "svc")],
    ).files["main.tf"]
    volume_names = {line.split("=")[1].strip().strip('"')
                    for line in main_tf.splitlines() if line.startswith("    name = ")}
    (definition,) = _container_definitions(main_tf)
    sources = {mount["sourceVolume"] for mount in definition["mountPoints"]}
    assert sources == volume_names == {"shared-data"}
    assert [mount["containerPath"] for mount in definition["mountPoints"]] == ["/mnt/data"]


def test_an_efs_edged_to_a_lambda_mounts_it_through_an_access_point():
    """The arn a lambda mounts is an ACCESS POINT's, never the file system's:
    AWS's own pattern for the argument ends `access-point/fsap-[a-f0-9]{17}`, so
    a file-system arn is a project real AWS rejects at CreateFunction."""
    project = _project([_FUNCTION, _efs("fs", "shared-data")], [_edge("fs", "fn")])
    main_tf = project.files["main.tf"]
    assert project.unsupported == []
    assert (
        'resource "aws_efs_access_point" "shared_data_ap" {\n'
        "  file_system_id = aws_efs_file_system.shared_data.id\n"
        "\n"
        "  root_directory {\n"
        '    path = "/"\n'
        "  }\n"
        "}"
    ) in main_tf
    assert (
        "  file_system_config {\n"
        "    arn              = aws_efs_access_point.shared_data_ap.arn\n"
        '    local_mount_path = "/mnt/efs"\n'
        "  }"
    ) in main_tf
    assert "aws_efs_file_system.shared_data.arn" not in main_tf


def test_an_ecs_only_canvas_gets_no_access_point():
    """An `efs_volume_configuration` names the file system directly, so a service
    needs none -- and an access point nothing references is a resource the user
    pays for (in real AWS) and odin has to reconcile, for nothing."""
    main_tf = _project([_SERVICE, _efs("fs", "shared-data")], [_edge("fs", "svc")]).files["main.tf"]
    assert "aws_efs_access_point" not in main_tf


def test_every_access_point_a_lambda_names_is_really_declared():
    """The unresolvable-reference class, pinned directly rather than through the
    one canvas that happens to trigger it. `tofu plan` fails for the WHOLE
    project on one dangling reference, so this stops every other resource on the
    canvas from applying too."""
    canvases = [
        ([_FUNCTION, _efs("fs", "shared-data")], [_edge("fs", "fn")]),
        ([_FUNCTION, _efs("fs", "shared-data", path="/mnt/efs/data")], [_edge("fs", "fn")]),
        ([_FUNCTION, _efs("fs", "shared-data", path="/data/efs")], [_edge("fs", "fn")]),
        ([_FUNCTION, _SERVICE, _efs("fs", "x" * (_MAX_CREATION_TOKEN + 1))],
         [_edge("fs", "fn"), _edge("fs", "svc")]),
        ([_FUNCTION, _efs("f1", "alpha"), _efs("f2", "beta")], [_edge("f1", "fn"), _edge("f2", "fn")]),
    ]
    for nodes, edges in canvases:
        main_tf = _project(nodes, edges).files["main.tf"]
        named = set(_access_points_named(main_tf))
        declared = {line.split('"')[3] for line in main_tf.splitlines()
                    if line.startswith('resource "aws_efs_access_point"')}
        assert named <= declared, f"dangling access point reference in {main_tf}"
    # Guards the guard: the first canvas really does reference one, so the
    # subset check above is not passing over four empty sets.
    assert _access_points_named(_project(*canvases[0]).files["main.tf"]) == ["shared_data_ap"]


def test_the_reverse_direction_edge_produces_the_identical_file():
    """Which end the user started the drag from carries no meaning, so both
    orders read the same rather than one silently doing nothing."""
    nodes = [_SERVICE, _FUNCTION, _efs("fs", "shared-data")]
    drawn_out = _project(nodes, [_edge("fs", "svc"), _edge("fs", "fn")]).files
    drawn_in = _project(nodes, [_edge("svc", "fs"), _edge("fn", "fs")]).files
    assert drawn_out == drawn_in
    assert "efs_volume_configuration" in drawn_in["main.tf"]


def test_an_edge_carrying_the_legacy_type_name_still_mounts():
    """The mount pass keys on the two NODE kinds and never on `edge.kind`, and
    this is NOT merely convention here -- an earlier draft of this test said it
    was, and the claim was false.

    Measured from git, three commits:
      * `ac796d6` (2026-06-20) shipped the tile draggable, sublabel
        'Elastic file system' -- with NO `(placeholder)` marker at all;
      * `1b158fe` (2026-07-26) added the marker;
      * `41d214b` (2026-07-27) hid placeholders, PALETTE-ONLY by its own commit
        message -- a canvas already holding one still renders.
    So for five weeks an efs tile looked like an ordinary Storage tile rather
    than a warned-off one, which is what makes such saved canvases likely rather
    than merely possible. Every edge from that node is typed `network`, the
    unregistered-pair catch-all -- exactly what this test replays.

    Milder than ebs's version of this, and worth being exact rather than
    dramatic: `efs` was never in `translate.py::_KIND`, so Apply skipped the node
    for that whole window and nothing was ever provisioned -- there is no live
    file system for tofu to tear down. What an `edge.kind == "mount"` gate WOULD
    do is silently ignore the old edge, so the first Apply after this change
    creates a file system and mounts it NOWHERE, with no error at all.
    """
    nodes = [_SERVICE, _FUNCTION, _efs("fs", "shared-data")]
    expected = _project(nodes, [_edge("fs", "svc"), _edge("fs", "fn")]).files["main.tf"]
    for stored in ("network", "unmodelled", "ref", "connection"):
        legacy = _project(nodes, [_edge("fs", "svc", stored), _edge("fs", "fn", stored)])
        assert legacy.files["main.tf"] == expected, stored
    # Guards the guard: the expected file really does carry both mounts, so the
    # equality above is not comparing two files that mount nothing.
    assert "efs_volume_configuration" in expected and "file_system_config" in expected


def test_one_file_system_mounted_by_two_workloads_is_shared_not_copied():
    """Sharing is the whole feature, and it is the one thing that makes `mount`
    a different edge kind from `volume`: an attachment is refused a second host,
    a mount must accept every consumer drawn to it."""
    project = _project([_SERVICE, _FUNCTION, _efs("fs", "shared-data")],
                       [_edge("fs", "svc"), _edge("fs", "fn")])
    main_tf = project.files["main.tf"]
    assert project.unsupported == []
    assert main_tf.count('resource "aws_efs_file_system"') == 1
    assert main_tf.count('resource "aws_efs_access_point"') == 1
    assert "file_system_id = aws_efs_file_system.shared_data.id" in main_tf  # the service's volume
    assert "arn              = aws_efs_access_point.shared_data_ap.arn" in main_tf  # the lambda's


def test_a_lambda_drawn_to_two_file_systems_is_declined_by_name():
    """A Lambda function mounts at most ONE file system: `file_system_config` is
    `max_items: 1` in the real provider schema and `FileSystemConfigs` is `max: 1`
    in botocore's model. MEASURED, not assumed -- a hand-built file with two
    blocks fails `tofu validate` with 'Too many file_system_config blocks', which
    would take the WHOLE project down, so picking one silently is not available
    as an answer either."""
    project = _project([_FUNCTION, _efs("f1", "alpha"), _efs("f2", "beta")],
                       [_edge("f1", "fn"), _edge("f2", "fn")])
    main_tf = project.files["main.tf"]
    (declined,) = project.unsupported
    assert declined.startswith("worker (lambda): drawn to more than one EFS file system")
    assert "'alpha'" in declined and "'beta'" in declined
    assert "file_system_config" not in main_tf
    assert "aws_lambda_function" not in main_tf  # the function itself is declined, not half-built
    # ...and both file systems are still real, free-standing resources.
    assert main_tf.count('resource "aws_efs_file_system"') == 2


def test_two_file_systems_at_the_same_path_on_one_service_decline_the_second():
    """The DEFAULT collision, not an exotic one: the tile defaults `path` to
    /mnt/efs, so a second efs node edged to the same service collides at once.
    odin renders one `-v <host dir>:<path>` per mounted file system
    (`runtime/colima.py`), so two at one path would be two mounts fighting over
    one destination."""
    project = _project([_SERVICE, _efs("f1", "alpha"), _efs("f2", "beta")],
                       [_edge("f1", "svc"), _edge("f2", "svc")])
    main_tf = project.files["main.tf"]
    (declined,) = project.unsupported
    assert declined.startswith("beta (efs): api already mounts alpha at /mnt/efs")
    assert "only alpha is mounted there" in declined
    (definition,) = _container_definitions(main_tf)
    assert [mount["sourceVolume"] for mount in definition["mountPoints"]] == ["alpha"]
    assert main_tf.count("efs_volume_configuration") == 1
    # The declined file system is still a real resource; it is the MOUNT that is
    # missing, and `unsupported` says which one and why.
    assert 'resource "aws_efs_file_system" "beta"' in main_tf


def test_two_file_systems_at_different_paths_on_one_service_both_mount():
    project = _project(
        [_SERVICE, _efs("f1", "alpha", path="/mnt/alpha"), _efs("f2", "beta", path="/mnt/beta")],
        [_edge("f1", "svc"), _edge("f2", "svc")],
    )
    main_tf = project.files["main.tf"]
    assert project.unsupported == []
    assert main_tf.count("efs_volume_configuration") == 2
    (definition,) = _container_definitions(main_tf)
    assert definition["mountPoints"] == [
        {"sourceVolume": "alpha", "containerPath": "/mnt/alpha", "readOnly": False},
        {"sourceVolume": "beta", "containerPath": "/mnt/beta", "readOnly": False},
    ]


def test_the_mount_order_comes_from_the_sorted_node_ids_not_the_edge_order():
    """Deterministic, or an unrelated re-draw churns `container_definitions` --
    which `ecsctl` stores verbatim, so a changed string is a new task-definition
    revision and a redeploy of the service."""
    nodes = [_SERVICE, _efs("f1", "alpha", path="/mnt/alpha"), _efs("f2", "beta", path="/mnt/beta")]
    forward = _project(nodes, [_edge("f1", "svc"), _edge("f2", "svc")]).files["main.tf"]
    backward = _project(nodes, [_edge("f2", "svc"), _edge("f1", "svc")]).files["main.tf"]
    assert forward == backward


def test_a_lambda_mounting_a_path_with_a_second_segment_is_declined_by_name():
    """`/mnt/efs/data` looks obviously fine and is not: AWS's own pattern has no
    `/` in its character class, so a lambda mount is one segment under /mnt.
    Rejected at CreateFunction, a long way from the field that caused it.

    The FUNCTION is what gets declined, not the file system: the file system is
    perfectly buildable and every other consumer's mount of it still works, so
    declining the efs node would take a pile of working resources down over one
    function's constraint."""
    project = _project([_FUNCTION, _efs("fs", "shared-data", path="/mnt/efs/data")],
                       [_edge("fs", "fn")])
    (declined,) = project.unsupported
    assert declined.startswith(
        "worker (lambda): mounts shared-data at '/mnt/efs/data', which is not a path a Lambda "
        "function can mount",
    )
    assert _LOCAL_MOUNT_PATH_PATTERN in declined
    assert 'resource "aws_efs_file_system" "shared_data"' in project.files["main.tf"]
    assert "aws_lambda_function" not in project.files["main.tf"]
    assert "aws_efs_access_point" not in project.files["main.tf"]


def test_an_ecs_only_canvas_may_mount_where_a_lambda_could_not():
    """The over-broad-guard bug, pinned so it cannot come back.

    ECS's `MountPoint.containerPath` carries NO pattern and no length limit at
    all (botocore, printed: `metadata={}`) and odin's substrate is a bind mount
    that serves any path, so `/data` on an ecs-only canvas is legal in AWS, legal
    here, and must be built. The first version of this file applied lambda's
    pattern to every efs node and refused it -- odin declining work it can
    actually do, which is the same class of dishonesty as claiming work it
    cannot."""
    for path in ("/data", "/srv/shared/deep/tree", "/var/lib/x"):
        project = _project([_SERVICE, _efs("fs", "shared-data", path=path)], [_edge("fs", "svc")])
        assert project.unsupported == [], path
        (definition,) = _container_definitions(project.files["main.tf"])
        assert definition["mountPoints"] == [
            {"sourceVolume": "shared-data", "containerPath": path, "readOnly": False},
        ], path


def test_an_ecs_mount_survives_a_path_only_the_lambda_cannot_use():
    """The blast radius, measured. One canvas, two consumers, one impossible
    mount: the service keeps its mount, the file system is real, and only the
    function is named."""
    project = _project([_SERVICE, _FUNCTION, _efs("fs", "shared-data", path="/data")],
                       [_edge("fs", "svc"), _edge("fs", "fn")])
    main_tf = project.files["main.tf"]
    (declined,) = project.unsupported
    assert declined.startswith("worker (lambda):")
    assert 'resource "aws_efs_file_system" "shared_data"' in main_tf
    assert 'resource "aws_ecs_service" "api"' in main_tf
    assert main_tf.count("efs_volume_configuration") == 1
    assert "aws_lambda_function" not in main_tf


def test_every_path_a_lambda_cannot_mount_is_declined_and_every_one_it_can_is_built():
    """One canvas per shape, so the guard is pinned on both sides -- a check that
    only ever declines is as wrong as one that never does."""
    rejected = ["/mnt/efs/data", "/data/efs", "/mnt/", "mnt/efs", "/mnt/e s", "/mnt/efs:x",
                "/mnt/efs/", "   "]
    # `""` is in the ACCEPTED list on purpose, and it is measured rather than
    # assumed: `canvas_to_stack` drops an empty-string field entirely (printed --
    # the resource comes back with `fields == {}`), so an untouched `path` box is
    # "not set" and takes the default. Declining for a field the user never typed
    # in would be the guard firing on the wrong signal.
    accepted = ["/mnt/efs", "/mnt/a.b-c_d", "/mnt/X9", ""]
    for path in rejected:
        project = _project([_FUNCTION, _efs("fs", "shared-data", path=path)], [_edge("fs", "fn")])
        assert project.unsupported and "can mount" in project.unsupported[0], path
        assert "file_system_config" not in project.files["main.tf"], path
    for path in accepted:
        project = _project([_FUNCTION, _efs("fs", "shared-data", path=path)], [_edge("fs", "fn")])
        assert project.unsupported == [], path
        assert "file_system_config" in project.files["main.tf"], path


def test_the_mount_path_pattern_is_the_one_aws_publishes():
    """The guard reads AWS's OWN constraint, not a remembered one. botocore ships
    the pattern in its Lambda model, so this compares odin's copy against the
    real thing and fails the build if AWS ever changes it -- the same
    cross-source ratchet `test_hcl_iam_arns.py` holds over the ARN constants."""
    shape = botocore.session.get_session().get_service_model("lambda").shape_for("FileSystemConfig")
    assert _LOCAL_MOUNT_PATH_PATTERN == shape.members["LocalMountPath"].metadata["pattern"]


def test_the_creation_token_limit_is_the_one_aws_publishes():
    request = botocore.session.get_session().get_service_model("efs").shape_for(
        "CreateFileSystemRequest",
    )
    assert _MAX_CREATION_TOKEN == request.members["CreationToken"].metadata["max"]


def test_a_label_past_the_creation_token_limit_is_declined_by_name():
    """Truncating it would let two long labels name one file system, which is a
    canvas showing two resources over one."""
    label = "a" * (_MAX_CREATION_TOKEN + 1)
    project = _project([_efs("fs", label)], [])
    (declined,) = project.unsupported
    assert declined.startswith(f"{label} (efs): the label is {_MAX_CREATION_TOKEN + 1} characters")
    assert str(_MAX_CREATION_TOKEN) in declined
    assert "aws_efs_file_system" not in project.files["main.tf"]
    # ...and one character shorter is built.
    assert 'resource "aws_efs_file_system"' in _project(
        [_efs("fs", "a" * _MAX_CREATION_TOKEN)], []).files["main.tf"]


def test_a_declined_file_system_leaves_no_mount_pointing_at_nothing():
    """The `built_ids`/`_efs_fault` gate, from both consumer sides at once. A
    `volume` or a `file_system_config` naming a file system pass 2 declined is an
    unresolvable reference, and `tofu plan` fails for the WHOLE project on one of
    those -- so every other resource on the canvas stops applying too."""
    project = _project([_SERVICE, _FUNCTION, _efs("fs", "s" * (_MAX_CREATION_TOKEN + 1))],
                       [_edge("fs", "svc"), _edge("fs", "fn")])
    main_tf = project.files["main.tf"]
    assert "aws_efs_file_system" not in main_tf
    assert "aws_efs_access_point" not in main_tf
    assert "efs_volume_configuration" not in main_tf
    assert "file_system_config" not in main_tf
    # ...and the two workloads themselves still build.
    assert 'resource "aws_ecs_service" "api"' in main_tf
    assert 'resource "aws_lambda_function" "worker"' in main_tf
    assert [u for u in project.unsupported if "(efs): the label is" in u]


def test_the_mount_pass_and_the_builder_agree_on_which_file_systems_build():
    """The invariant the whole design leans on, pinned instead of reasoned about
    once.

    The mount pass runs BEFORE pass 2 -- `_lambda` reads its answer out of `refs`
    -- so it gates on `_efs_fault`, while the two companion passes gate on
    `built_ids`. Those are the same set only because `_efs` declines for
    `_efs_fault` and for nothing else. The day a second decline path is added to
    the builder, or `edge_declined` starts covering efs nodes, the mount pass
    would author a reference to a resource pass 2 withheld, and one unresolvable
    reference fails `tofu plan` for the WHOLE project. This fails first instead,
    and it is also what makes the belt-and-braces `built_ids` checks in the
    companion passes redundant rather than load-bearing.
    """
    canvases = [
        [_efs("f1", "alpha"), _efs("f2", "b" * (_MAX_CREATION_TOKEN + 1))],
        [_efs("f1", "a" * (_MAX_CREATION_TOKEN + 5)), _efs("f2", "b" * (_MAX_CREATION_TOKEN + 1))],
        [_efs("f1", "alpha"), _efs("f2", "beta", path="/mnt/beta")],
    ]
    seen: list[tuple[int, int]] = []
    for nodes in canvases:
        stack = canvas_to_stack({"nodes": [_SERVICE, _FUNCTION, *nodes], "edges": []})
        buildable = {res.id for res in stack.resources if res.kind == "efs" and not _efs_fault(res)}
        main_tf = generate_tf(stack).files["main.tf"]
        emitted = {line.split('"')[3] for line in main_tf.splitlines()
                   if line.startswith('resource "aws_efs_file_system"')}
        assert {sanitize_name(node_id) for node_id in buildable} == emitted, nodes
        seen.append((len(emitted), len(nodes) - len(emitted)))
    # Guards the guard: the canvases above really do build some and decline some,
    # so the equality is never comparing two empty sets.
    assert seen == [(1, 1), (0, 2), (2, 0)]


def test_an_efs_edged_to_a_kind_that_cannot_mount_mounts_nothing():
    """`_EFS_MOUNT_KINDS` is ecs+lambda -- the two kinds odin runs as containers.
    An ec2 node is a Lima VM created with `"mounts": []`, so a host directory is
    not visible inside it at all and the edge stays the decorative line the
    canvas labels 'Not modelled' rather than an empty mount nobody notices."""
    others = [
        {"id": "i", "type": "ec2", "data": {"label": "box", "vpc": "net", "subnet": "web"}},
        {"id": "b", "type": "s3", "data": {"label": "uploads"}},
    ]
    project = _project([*others, _efs("fs", "shared-data")],
                       [_edge("fs", "i", "unmodelled"), _edge("fs", "b", "unmodelled")])
    main_tf = project.files["main.tf"]
    assert "efs_volume_configuration" not in main_tf
    assert "file_system_config" not in main_tf
    assert "aws_efs_access_point" not in main_tf
    assert 'resource "aws_efs_file_system" "shared_data"' in main_tf


def test_a_canvas_with_no_efs_node_generates_what_it_always_did():
    """`container_definitions` is stored VERBATIM by `ecsctl`, so a changed
    string is a new task-definition revision and a redeploy -- `mountPoints` is
    absent rather than `[]` for exactly that reason."""
    (definition,) = _container_definitions(_project([_SERVICE], []).files["main.tf"])
    assert "mountPoints" not in definition
    assert list(definition) == ["name", "image", "essential", "portMappings"]
