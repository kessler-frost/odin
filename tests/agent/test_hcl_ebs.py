"""EBS volumes and their attachments, generate-side.

An `aws_ebs_volume` alone attaches to nothing, so the whole feature is really the
COMPANION: an ebs node and an ec2 node joined by an edge is an
`aws_volume_attachment`. That companion is the reason this file exists rather
than a couple of lines in test_hcl.py, because it is where the expensive
mistakes live:

  * keying the pass on `edge.kind` instead of the two NODE KINDS would drop the
    attachment for every canvas saved before the edge-type registry (they all
    carry `kind: "network"`), and tofu would then DETACH a disk with data on it;
  * a device name reused on one instance is rejected by real AWS, so odin has to
    assign them itself and has to run out honestly;
  * an attachment referencing a volume or instance the generator DECLINED is an
    unresolvable reference, which fails `tofu plan` for the whole project -- not
    just for that node.
"""
from __future__ import annotations

from odin.iac.hcl import _EBS_DEVICE_NAMES, generate_tf
from odin.spec.translate import canvas_to_stack

_NETWORK = [
    {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
    {"id": "s", "type": "subnet", "data": {"label": "web", "cidr": "10.0.1.0/24", "vpc": "net"}},
]
_INSTANCE = {"id": "i", "type": "ec2", "data": {"label": "box", "vpc": "net", "subnet": "web"}}


def _volume(node_id: str, label: str, **data) -> dict:
    return {"id": node_id, "type": "ebs", "data": {"label": label, **data}}


def _edge(source: str, target: str, edge_type: str = "volume") -> dict:
    return {"id": f"{source}-{target}", "source": source, "target": target,
            "data": {"edgeType": edge_type}}


def _project(nodes: list[dict], edges: list[dict]):
    return generate_tf(canvas_to_stack({"nodes": nodes, "edges": edges}))


def test_a_volume_edged_to_an_instance_emits_the_volume_and_the_attachment():
    project = _project([*_NETWORK, _INSTANCE, _volume("d", "data", az="us-east-1b", size="50")],
                       [_edge("d", "i")])
    main_tf = project.files["main.tf"]
    assert project.unsupported == []
    assert 'resource "aws_ebs_volume" "data" {' in main_tf
    assert '  availability_zone = "us-east-1b"' in main_tf
    assert "  size              = 50" in main_tf
    assert '  type              = "gp3"' in main_tf
    # The whole companion, byte for byte: the references are what tofu resolves,
    # and a typo in either argument name is a plan failure for the whole project.
    assert (
        'resource "aws_volume_attachment" "data_box_attach" {\n'
        '  device_name = "/dev/sdf"\n'
        "  instance_id = aws_instance.box.id\n"
        "  volume_id   = aws_ebs_volume.data.id\n"
        "}"
    ) in main_tf


def test_the_reverse_direction_edge_produces_the_identical_file():
    """Which end the user started the drag from carries no meaning, so both
    orders read the same rather than one silently doing nothing."""
    nodes = [*_NETWORK, _INSTANCE, _volume("d", "data")]
    assert _project(nodes, [_edge("i", "d")]).files == _project(nodes, [_edge("d", "i")]).files


def test_an_edge_saved_before_the_edge_type_registry_still_attaches():
    """The destructive one. Every canvas saved before the registry carries
    `kind: "network"` on every edge, so a pass gated on the TYPE NAME would emit
    no attachment for them -- and the next apply would detach a live disk. The
    pass keys on the two node kinds, so the stored name cannot matter."""
    nodes = [*_NETWORK, _INSTANCE, _volume("d", "data")]
    legacy = _project(nodes, [_edge("d", "i", "network")]).files["main.tf"]
    assert "aws_volume_attachment" in legacy
    assert legacy == _project(nodes, [_edge("d", "i", "volume")]).files["main.tf"]


def test_two_volumes_on_one_instance_get_different_device_names():
    """AWS rejects two attachments claiming the same device on one instance."""
    main_tf = _project(
        [*_NETWORK, _INSTANCE, _volume("d1", "alpha"), _volume("d2", "beta")],
        [_edge("d1", "i"), _edge("d2", "i")],
    ).files["main.tf"]
    devices = [line.strip() for line in main_tf.splitlines() if "device_name" in line]
    assert devices == ['device_name = "/dev/sdf"', 'device_name = "/dev/sdg"']
    assert len(set(devices)) == 2


def test_the_device_names_come_from_the_sorted_volume_list_not_the_edge_order():
    """Deterministic and stable: the same canvas with its edges drawn in the
    other order is the same file, or an unrelated re-draw would renumber devices
    -- which tofu applies as a detach and reattach of live disks."""
    nodes = [*_NETWORK, _INSTANCE, _volume("d1", "alpha"), _volume("d2", "beta")]
    forward = _project(nodes, [_edge("d1", "i"), _edge("d2", "i")]).files["main.tf"]
    reversed_order = _project(nodes, [_edge("d2", "i"), _edge("d1", "i")]).files["main.tf"]
    assert forward == reversed_order


def test_a_declined_volume_does_not_renumber_the_devices_of_the_others():
    """The slot is the volume's position among ALL the instance's attached
    volumes, built or not. If a decline compacted the list, fixing one node's
    size would move another node's disk."""
    good = [_volume("d2", "beta"), _volume("d3", "gamma")]
    healthy = _project([*_NETWORK, _INSTANCE, *good],
                       [_edge("d2", "i"), _edge("d3", "i")]).files["main.tf"]
    # `alpha` sorts first and is declined, so it takes /dev/sdf with it.
    with_declined = _project(
        [*_NETWORK, _INSTANCE, _volume("d1", "alpha", size="lots"), *good],
        [_edge("d1", "i"), _edge("d2", "i"), _edge("d3", "i")],
    ).files["main.tf"]
    assert 'device_name = "/dev/sdf"' in healthy and 'device_name = "/dev/sdg"' in healthy
    assert 'device_name = "/dev/sdf"' not in with_declined
    assert 'device_name = "/dev/sdg"' in with_declined and 'device_name = "/dev/sdh"' in with_declined


def test_adding_an_earlier_sorting_volume_renumbers_the_others():
    """The RESIDUAL of a positional scheme, pinned rather than hoped away.

    `device_name` is ForceNew — measured against OpenTofu 1.12.3 with a real
    state file, changing it prints `# forces replacement` and `Plan: 1 to add,
    0 to change, 1 to destroy`, which is a detach and reattach of a live disk.
    So this test is not describing a cosmetic renumber: adding `archive` really
    does cause `data` and `logs` to be detached and reattached on the next
    apply, and nothing about the change the user made says so.

    It is pinned as CURRENT BEHAVIOUR, not as desired behaviour. `generate_tf`
    is a pure function of the canvas with no memory and the pool has 11 slots,
    so no rule here can be insertion-stable; the fix is a canvas field or a live
    read, both larger than this pass. If one lands, this test should FAIL and be
    deleted rather than adjusted — the same instruction
    `test_import_coverage_is_honest.py` carries about its own superseded claim.
    """
    def devices(labels: list[str]) -> dict[str, str]:
        volumes = [_volume(f"d{n}", label) for n, label in enumerate(labels)]
        main_tf = _project([*_NETWORK, _INSTANCE, *volumes],
                           [_edge(f"d{n}", "i") for n in range(len(labels))]).files["main.tf"]
        out, current = {}, ""
        for line in main_tf.splitlines():
            if line.startswith('resource "aws_volume_attachment"'):
                current = line.split('"')[3]
            if "device_name" in line and current:
                out[current] = line.split("=")[1].strip()
        return out

    before = devices(["logs", "data"])
    after = devices(["logs", "data", "archive"])
    assert before == {"data_box_attach": '"/dev/sdf"', "logs_box_attach": '"/dev/sdg"'}
    assert after == {
        "archive_box_attach": '"/dev/sdf"',
        "data_box_attach": '"/dev/sdg"',
        "logs_box_attach": '"/dev/sdh"',
    }


def test_the_twelfth_volume_on_one_instance_is_declined_by_name():
    """11 conventional device names, and a 12th volume must say so rather than
    reuse one -- a duplicate device is rejected by the provider for the whole
    apply, and by real AWS."""
    volumes = [_volume(f"d{n}", f"vol-{n:02d}") for n in range(len(_EBS_DEVICE_NAMES) + 1)]
    project = _project([*_NETWORK, _INSTANCE, *volumes],
                       [_edge(f"d{n}", "i") for n in range(len(volumes))])
    main_tf = project.files["main.tf"]
    assert main_tf.count("aws_volume_attachment") == len(_EBS_DEVICE_NAMES)
    (declined,) = project.unsupported
    assert declined.startswith("vol-11 (ebs): box already holds 11 volumes")
    assert "/dev/sdf" in declined and "/dev/sdp" in declined
    # The VOLUME itself is still real -- it is the attachment that is missing.
    assert 'resource "aws_ebs_volume" "vol_11"' in main_tf


def test_a_non_numeric_size_is_declined_by_name():
    project = _project([_volume("d", "data", size="50GB")], [])
    assert project.files["main.tf"].count("aws_ebs_volume") == 0
    assert project.unsupported == ["data (ebs): size must be a whole number of GiB (e.g. 10)"]


def test_a_declined_volume_never_leaves_an_attachment_pointing_at_nothing():
    """The `built_ids` gate. An attachment naming an `aws_ebs_volume` that was
    declined is an unresolvable reference, and `tofu plan` fails for the WHOLE
    project on one of those -- so every other resource stops applying too."""
    project = _project([*_NETWORK, _INSTANCE, _volume("d", "data", size="big")], [_edge("d", "i")])
    main_tf = project.files["main.tf"]
    assert "aws_volume_attachment" not in main_tf
    assert 'resource "aws_instance" "box"' in main_tf  # ...and the rest still builds
    assert project.unsupported == ["data (ebs): size must be a whole number of GiB (e.g. 10)"]


def test_an_attachment_to_a_declined_instance_is_not_emitted_either():
    """The same gate from the other side: an ec2 node drawn outside any subnet
    is declined by `_ec2`, so `aws_instance.box.id` would resolve to nothing."""
    orphan = {"id": "i", "type": "ec2", "data": {"label": "box"}}
    project = _project([orphan, _volume("d", "data")], [_edge("d", "i")])
    assert "aws_volume_attachment" not in project.files["main.tf"]
    assert 'resource "aws_ebs_volume" "data"' in project.files["main.tf"]
    assert any(entry.startswith("box (ec2):") for entry in project.unsupported)


def test_a_volume_with_no_edge_is_a_free_standing_volume():
    """An ebs node nobody attached is a real `available` volume, not an error."""
    project = _project([*_NETWORK, _INSTANCE, _volume("d", "data")], [])
    assert 'resource "aws_ebs_volume" "data"' in project.files["main.tf"]
    assert "aws_volume_attachment" not in project.files["main.tf"]
    assert project.unsupported == []


def test_a_volume_edged_to_two_instances_attaches_to_exactly_one_and_says_so():
    """A gp3 volume attaches to one instance, and a `limactl disk` to one VM.
    Emitting both attachments would produce a file that fails at APPLY, which is
    the worst place to find out."""
    second = {"id": "i2", "type": "ec2", "data": {"label": "other", "vpc": "net", "subnet": "web"}}
    project = _project([*_NETWORK, _INSTANCE, second, _volume("d", "data")],
                       [_edge("d", "i"), _edge("d", "i2")])
    main_tf = project.files["main.tf"]
    assert main_tf.count('resource "aws_volume_attachment"') == 1
    assert "instance_id = aws_instance.box.id" in main_tf
    (declined,) = project.unsupported
    assert declined.startswith("data (ebs): a second attachment edge, to other")
    assert "only the attachment to box is emitted" in declined


def test_an_ebs_edged_to_a_kind_that_is_not_a_volume_host_attaches_nothing():
    """`_VOLUME_HOST_KINDS` is `ec2` alone -- nothing else odin runs is a machine
    with a disk controller, so the edge stays the decorative line the canvas
    already labels 'Not modelled'."""
    fn = {"id": "f", "type": "lambda", "data": {"label": "worker"}}
    project = _project([fn, _volume("d", "data")], [_edge("d", "f", "unmodelled")])
    assert "aws_volume_attachment" not in project.files["main.tf"]
    assert project.unsupported == []


def test_two_attachments_that_compose_the_same_hcl_name_do_not_collide():
    """`a_b` + `c` and `a` + `b_c` both compose to `a_b_c_attach`, and two
    resources sharing an HCL name is a file that does not parse."""
    hosts = [
        {"id": "i1", "type": "ec2", "data": {"label": "b-c", "vpc": "net", "subnet": "web"}},
        {"id": "i2", "type": "ec2", "data": {"label": "c", "vpc": "net", "subnet": "web"}},
    ]
    project = _project(
        [*_NETWORK, *hosts, _volume("d1", "a"), _volume("d2", "a-b")],
        [_edge("d1", "i1"), _edge("d2", "i2")],
    )
    names = [line for line in project.files["main.tf"].splitlines()
             if line.startswith('resource "aws_volume_attachment"')]
    assert len(names) == 2 and len(set(names)) == 2
