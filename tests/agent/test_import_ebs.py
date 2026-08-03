"""Terraform -> canvas for EBS volumes and their attachments.

The volume itself is the easy half. The ATTACHMENT is the half worth testing:
it becomes an EDGE, and an edge carries no arguments -- so anything the source
put on the `aws_volume_attachment` has to be accounted for here or it vanishes,
and a dropped attachment is a disk the next apply DETACHES.
"""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

_VOLUME = (
    'resource "aws_ebs_volume" "data" {\n'
    '  availability_zone = "us-east-1b"\n'
    "  size              = 100\n"
    '  type              = "gp3"\n'
    "\n"
    "  tags = {\n"
    '    "odin:node" = "data"\n'
    "  }\n"
    "}\n"
)
# The instance is drawn inside a real VPC + Subnet: an `aws_instance` whose
# `subnet_id` names nothing imported warns about its containment, which would
# swamp the "odin's own file imports silently" assertions below with a note that
# has nothing to do with volumes.
_NETWORK = (
    'resource "aws_vpc" "net" {\n  cidr_block = "10.0.0.0/16"\n\n'
    '  tags = {\n    "odin:node" = "net"\n  }\n}\n'
    "\n"
    'resource "aws_subnet" "web" {\n  vpc_id     = aws_vpc.net.id\n'
    '  cidr_block = "10.0.1.0/24"\n\n'
    '  tags = {\n    "odin:node" = "web"\n  }\n}\n'
)
_INSTANCE = (
    'resource "aws_instance" "box" {\n'
    '  ami           = "ami-0c101f26f147fa7fd"\n'
    '  instance_type = "t3.micro"\n'
    "  subnet_id     = aws_subnet.web.id\n"
    "\n"
    "  tags = {\n"
    '    "odin:node" = "box"\n'
    "  }\n"
    "}\n"
)


def _attachment(name: str = "data_box_attach", **extra: str) -> str:
    args = {
        "device_name": '"/dev/sdf"',
        "instance_id": "aws_instance.box.id",
        "volume_id": "aws_ebs_volume.data.id",
        **extra,
    }
    body = "".join(f"  {key} = {value}\n" for key, value in args.items())
    return f'resource "aws_volume_attachment" "{name}" {{\n{body}}}\n'


def _project(*blocks: str) -> str:
    return "\n".join(blocks)


def _changed(result) -> list[str]:
    return [w for w in result.warnings if "CHANGED" in w]


def _lost(result) -> list[str]:
    return [w for w in result.warnings if "imported without" in w]


def test_a_volume_becomes_an_ebs_node_carrying_its_az_and_size():
    (node,) = parse_hcl_text(_VOLUME).nodes
    assert node["type"] == "ebs"
    # `size` is TEXT, matching the tile's own `defaultData: {size: '10'}`.
    assert node["data"] == {"label": "data", "size": "100", "az": "us-east-1b"}


def test_the_label_comes_from_the_odin_node_tag():
    """An `aws_ebs_volume` has no `name` argument at all (like `aws_instance`),
    so the label has to come from odin's own management tag -- which is why the
    type has no `_NAME_ATTR` entry."""
    tf = _VOLUME.replace('"odin:node" = "data"', '"odin:node" = "archive"')
    (node,) = parse_hcl_text(tf).nodes
    assert node["id"] == "archive" and node["data"]["label"] == "archive"


def test_an_attachment_becomes_a_volume_edge_between_the_two_nodes():
    result = parse_hcl_text(_project(_NETWORK, _VOLUME, _INSTANCE, _attachment()))
    assert result.unsupported == []
    (edge,) = result.edges
    assert edge == {"source": "data", "target": "box", "data": {"edgeType": "volume"}}
    # ...and it stays an edge: the attachment never becomes a node of its own.
    assert {n["type"] for n in result.nodes} == {"vpc", "subnet", "ebs", "ec2"}


def test_odins_own_attachment_imports_without_a_single_warning():
    """Warning noise is not harmless in a module whose whole value is that its
    warnings are worth reading -- and odin generates this exact file."""
    assert parse_hcl_text(_project(_NETWORK, _VOLUME, _INSTANCE, _attachment())).warnings == []


def test_an_attachment_naming_something_unimportable_is_reported_never_dropped():
    """A silently dropped attachment is the worst outcome in this module: the
    regenerated project would not attach the volume, and the apply would detach
    a live disk."""
    tf = _project(_VOLUME, _attachment())  # no aws_instance at all
    result = parse_hcl_text(tf)
    assert result.edges == []
    (entry,) = result.unsupported
    assert entry.type == "aws_volume_attachment" and entry.name == "data_box_attach"
    assert "instance_id" in entry.reason
    assert "would NOT attach this volume" in entry.reason


def test_a_volume_type_odin_cannot_emit_is_reported_as_CHANGED():
    """odin always emits gp3 (the substrate is a `limactl disk`, which has no
    volume type), so an io2 volume imported in silence would be a different
    device with different guarantees."""
    tf = _VOLUME.replace('"gp3"', '"io2"')
    result = parse_hcl_text(tf)
    (changed,) = _changed(result)
    assert "type=io2 (odin always emits gp3)" in changed
    assert _lost(result) == [], "a substituted value is not a missing argument"


def test_a_size_odin_cannot_read_is_reported_rather_than_defaulted_in_silence():
    """`size = var.disk` cannot be carried, and substituting odin's 10 GiB
    without a word is the elasticache bug in another costume."""
    tf = _VOLUME.replace("size              = 100", "size              = var.disk")
    result = parse_hcl_text(tf)
    (node,) = result.nodes
    assert node["data"]["size"] == "10"
    (changed,) = _changed(result)
    assert "size=${var.disk}" in changed and "odin's default 10 GiB" in changed


def test_an_argument_the_attachment_does_not_model_is_reported():
    """`force_detach` and `skip_destroy` both change what a destroy does to a
    disk that has data on it, and an edge cannot carry either."""
    tf = _project(_NETWORK, _VOLUME, _INSTANCE, _attachment(force_detach="true"))
    (lost,) = _lost(parse_hcl_text(tf))
    assert lost.startswith("data -> box (volume attachment): imported without unmodeled")
    assert "force_detach" in lost


def test_a_device_name_odin_will_not_reproduce_is_reported_as_CHANGED():
    """odin assigns device names itself, positionally per instance, so a source
    that names `/dev/xvdb` gets `/dev/sdf` back. (The name is advisory to the
    substrate either way -- the Lima guest calls the disk `/dev/vdb` -- but the
    Terraform argument does change, and this module reports changes.)"""
    tf = _project(_NETWORK, _VOLUME, _INSTANCE, _attachment(device_name='"/dev/xvdb"'))
    (changed,) = _changed(parse_hcl_text(tf))
    assert "device_name=/dev/xvdb (odin always emits /dev/sdf)" in changed


def test_the_second_volume_on_an_instance_is_measured_against_ITS_device():
    """The re-derivation is positional, so the check must be too: `/dev/sdg` on
    the second volume is what odin emits and must NOT be reported as changed."""
    second = _VOLUME.replace('"data"', '"logs"').replace(
        '"odin:node" = "data"', '"odin:node" = "logs"')
    attach_second = _attachment(
        "logs_box_attach", device_name='"/dev/sdg"', volume_id="aws_ebs_volume.logs.id")
    result = parse_hcl_text(_project(_NETWORK, _VOLUME, second, _INSTANCE, _attachment(), attach_second))
    assert result.warnings == []
    assert {(e["source"], e["target"]) for e in result.edges} == {("data", "box"), ("logs", "box")}


def test_a_generated_canvas_with_an_attachment_survives_generate_import_generate():
    """The end-to-end claim: byte-stable, WITH the attachment. Without the edge
    coming back, the second generate emits no `aws_volume_attachment` and the
    next apply detaches the disk."""
    canvas = {
        "nodes": [
            {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
            {"id": "s", "type": "subnet",
             "data": {"label": "web", "cidr": "10.0.1.0/24", "vpc": "net"}},
            {"id": "i", "type": "ec2", "data": {"label": "box", "vpc": "net", "subnet": "web"}},
            {"id": "d1", "type": "ebs", "data": {"label": "alpha", "az": "us-east-1c", "size": "40"}},
            {"id": "d2", "type": "ebs", "data": {"label": "beta", "size": "20"}},
        ],
        "edges": [
            {"id": "e1", "source": "d1", "target": "i", "data": {"edgeType": "volume"}},
            # Drawn the other way AND with the pre-registry type name, which is
            # what a canvas saved before v0.8.15 actually carries.
            {"id": "e2", "source": "i", "target": "d2", "data": {"edgeType": "network"}},
        ],
    }
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    imported = parse_hcl_text(main_tf)
    assert imported.unsupported == [] and imported.warnings == []
    assert main_tf.count('resource "aws_volume_attachment"') == 2

    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf
