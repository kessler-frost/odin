"""Terraform -> canvas for `aws_instance`.

An EC2 node is a real Lima VM to odin, which makes it the heaviest thing an
import can lose -- and it has three references that each decide something
different, so each is checked separately here:

  * `subnet_id`               -> containment. `hcl.py::_ec2` REFUSES to build an
                                 instance outside a subnet, so without this stamp
                                 the imported node is one Apply silently skips.
  * `vpc_security_group_ids`  -> the `securityGroups` label list, which is what
                                 the real Nebula firewall is compiled from.
  * `key_name`                -> a companion `aws_key_pair`'s `public_key`, i.e.
                                 whether anyone can log in to the VM.

`aws_instance` has no `name` argument at all, so its label comes from the
`odin:node` tag `_label` already falls back to -- worth an explicit test, because
a silent fall-through to the HCL resource name would RENAME a real instance.

The key pair is followed by REFERENCE rather than by reconstructing hcl.py's
`<instance>_key` naming convention: that convention is hcl.py's private
business, and a hand-authored project names its key pairs however it likes.
"""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexampleexampleexampleexam"

CANVAS = {
    "nodes": [
        {"id": "v1", "type": "vpc", "position": {"x": 0, "y": 0},
         "data": {"label": "prod-vpc", "cidr": "10.0.0.0/16"}},
        {"id": "s1", "type": "subnet", "position": {"x": 0, "y": 0},
         "data": {"label": "app-subnet", "vpc": "prod-vpc", "cidr": "10.0.1.0/24"}},
        {"id": "g1", "type": "sg", "position": {"x": 0, "y": 0},
         "data": {"label": "web-sg", "vpc": "prod-vpc", "ingressRules": "tcp:443:0.0.0.0/0"}},
        {"id": "e1", "type": "ec2", "position": {"x": 0, "y": 0},
         "data": {"label": "api-server", "instanceType": "t3.small",
                  "ami": "ami-0c101f26f147fa7fd", "subnet": "app-subnet",
                  "securityGroups": "web-sg", "key": KEY,
                  "userData": "#!/bin/bash\necho hello\n"}},
    ],
    "edges": [],
}


def _round_trip(canvas: dict):
    tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    return parse_hcl_text(tf), tf


def _node(result, label: str) -> dict:
    return next(n for n in result.nodes if n["id"] == label)


def test_an_instance_comes_back_as_a_node():
    result, _tf = _round_trip(CANVAS)
    assert "ec2" in {n["type"] for n in result.nodes}
    assert "aws_instance" not in {e.type for e in result.unsupported}


def test_the_label_comes_from_the_odin_node_tag_since_there_is_no_name_argument():
    """A fall-through to the HCL resource name would RENAME the instance."""
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "api-server")["data"]["label"] == "api-server"


def test_the_instance_type_and_ami_round_trip():
    result, _tf = _round_trip(CANVAS)
    data = _node(result, "api-server")["data"]
    assert data["instanceType"] == "t3.small"
    assert data["ami"] == "ami-0c101f26f147fa7fd"


def test_user_data_survives_including_its_newlines():
    """A shell script that lost its line breaks would run as one line, or not at
    all -- and it runs on a real VM."""
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "api-server")["data"]["userData"] == "#!/bin/bash\necho hello\n"


def test_containment_is_rebuilt_from_subnet_id():
    result, _tf = _round_trip(CANVAS)
    data = _node(result, "api-server")["data"]
    assert data["subnet"] == "app-subnet"
    assert data["vpc"] == "prod-vpc"


def test_security_groups_come_back_as_LABELS():
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "api-server")["data"]["securityGroups"] == "web-sg"


def test_the_ssh_key_is_recovered_from_the_companion_key_pair():
    result, _tf = _round_trip(CANVAS)
    assert _node(result, "api-server")["data"]["key"] == KEY


def test_the_key_pair_does_not_become_a_node_of_its_own():
    """It folds onto the instance, like a secret version onto its secret. A node
    per key pair would multiply resources on every round trip."""
    result, _tf = _round_trip(CANVAS)
    assert {n["type"] for n in result.nodes} == {"vpc", "subnet", "sg", "ec2"}
    assert "aws_key_pair" not in {e.type for e in result.unsupported}


def test_the_whole_thing_regenerates_byte_for_byte():
    _result, first = _round_trip(CANVAS)
    imported, _ = _round_trip(CANVAS)
    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == first


def test_odins_own_output_produces_no_warnings():
    result, _tf = _round_trip(CANVAS)
    assert result.warnings == [], result.warnings


# --- what an import cannot resolve, it must NAME ------------------------------

_ORPHANED = """
resource "aws_instance" "web" {
  ami           = "ami-123"
  instance_type = "t3.micro"
  subnet_id     = "subnet-outside-this-file"
  key_name      = aws_key_pair.missing.key_name

  vpc_security_group_ids = ["sg-not-imported"]

  tags = { "odin:node" = "orphan" }
}
"""


def test_every_unresolvable_reference_gets_its_own_warning():
    """One vague line for three different problems would be useless: each has a
    different consequence -- unappliable, less protected, unreachable."""
    result = parse_hcl_text(_ORPHANED)
    warnings = " | ".join(result.warnings)
    assert "Apply will skip it" in warnings, warnings
    assert "FEWER" in warnings, warnings
    assert "NO SSH key" in warnings, warnings


def test_an_instance_with_no_optional_fields_imports_cleanly():
    """No key, no user data, no groups -- the minimal instance must not invent
    fields or warn about the ones it never had."""
    tf = """
resource "aws_vpc" "net" {
  cidr_block = "10.0.0.0/16"
  tags = { "odin:node" = "net" }
}

resource "aws_subnet" "a" {
  vpc_id     = aws_vpc.net.id
  cidr_block = "10.0.1.0/24"
  tags = { "odin:node" = "sub" }
}

resource "aws_instance" "bare" {
  ami           = "ami-123"
  instance_type = "t3.micro"
  subnet_id     = aws_subnet.a.id
  tags = { "odin:node" = "bare-vm" }
}
"""
    result = parse_hcl_text(tf)
    data = _node(result, "bare-vm")["data"]
    assert data["subnet"] == "sub"
    assert "key" not in data
    assert "securityGroups" not in data
    assert "userData" not in data
    assert result.warnings == [], result.warnings
