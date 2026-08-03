"""Terraform -> canvas for hosted zones and their records.

The zone is the easy half: one argument, and that argument IS the canvas label.
The RECORD is the half worth testing, and it is the `aws_volume_attachment`
shape one service over -- it becomes an EDGE, an edge carries no arguments, and
a dropped record is a name that stops resolving on the next apply (odin's
substrate is a hosts entry, so "stops resolving" is literal: the `--add-host`
line is simply not written).

The scope is DELIBERATELY narrow and is asserted as such below: only an
`aws_instance` can be the thing a name points at, because a hosts entry is
`<ip> <name>` and carries no port. Importing an edge to anything else would
build a canvas the generator then refuses to re-emit.
"""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

_ZONE = (
    'resource "aws_route53_zone" "example_com" {\n'
    '  name = "example.com"\n'
    "\n"
    "  tags = {\n"
    '    "odin:node" = "example.com"\n'
    "  }\n"
    "}\n"
)
# The instance is drawn inside a real VPC + Subnet for `test_import_ebs.py`'s
# reason: an `aws_instance` whose `subnet_id` names nothing imported warns about
# its containment, which would swamp the "odin's own file imports silently"
# assertions with a note that has nothing to do with DNS.
_NETWORK = (
    'resource "aws_vpc" "net" {\n  cidr_block = "10.0.0.0/16"\n\n'
    '  tags = {\n    "odin:node" = "net"\n  }\n}\n'
    "\n"
    'resource "aws_subnet" "web" {\n  vpc_id     = aws_vpc.net.id\n'
    '  cidr_block = "10.0.1.0/24"\n\n'
    '  tags = {\n    "odin:node" = "web"\n  }\n}\n'
)
_INSTANCE = (
    'resource "aws_instance" "api_server" {\n'
    '  ami           = "ami-0c101f26f147fa7fd"\n'
    '  instance_type = "t3.micro"\n'
    "  subnet_id     = aws_subnet.web.id\n"
    "\n"
    "  tags = {\n"
    '    "odin:node" = "api-server"\n'
    "  }\n"
    "}\n"
)


def _record(rname: str = "example_com_api_server", **extra: str) -> str:
    # `rname`, not `name`: the record HAS a `name` argument, and calling the
    # parameter that made `_record(name=...)` rename the HCL resource instead of
    # overriding the argument -- silently, since both spellings are valid HCL.
    args = {
        "zone_id": "aws_route53_zone.example_com.zone_id",
        "name": '"api-server.example.com"',
        "type": '"A"',
        "ttl": "60",
        "records": "[aws_instance.api_server.private_ip]",
        **extra,
    }
    body = "".join(f"  {key} = {value}\n" for key, value in args.items())
    return f'resource "aws_route53_record" "{rname}" {{\n{body}}}\n'


def _project(*blocks: str) -> str:
    return "\n".join(blocks)


def _changed(result) -> list[str]:
    return [w for w in result.warnings if "CHANGED" in w]


def _lost(result) -> list[str]:
    return [w for w in result.warnings if "imported without" in w]


def test_a_zone_becomes_a_route53_node_whose_label_is_the_domain():
    (node,) = parse_hcl_text(_ZONE).nodes
    assert node["type"] == "route53"
    assert node["id"] == "example.com"
    assert node["data"] == {"label": "example.com"}


def test_the_label_comes_from_the_NAME_argument_not_the_tag():
    """The distinction from `aws_instance`/`aws_ebs_volume`, which have no `name`
    at all. A zone's name IS the domain, so reading it from the tag when the two
    disagree would import a zone that answers for a different domain."""
    tf = _ZONE.replace('name = "example.com"', 'name = "prod.example.com"')
    (node,) = parse_hcl_text(tf).nodes
    assert node["id"] == "prod.example.com" and node["data"]["label"] == "prod.example.com"


def test_a_zones_user_tags_round_trip_and_odins_own_tag_does_not():
    tf = _ZONE.replace(
        '    "odin:node" = "example.com"\n',
        '    "team"      = "platform"\n    "odin:node" = "example.com"\n',
    )
    (node,) = parse_hcl_text(tf).nodes
    assert node["data"]["tags"] == {"team": "platform"}
    assert parse_hcl_text(tf).warnings == [], "odin's own tag must not warn"


def test_a_record_becomes_a_dns_edge_between_the_zone_and_the_instance():
    result = parse_hcl_text(_project(_NETWORK, _ZONE, _INSTANCE, _record()))
    assert result.unsupported == []
    (edge,) = result.edges
    assert edge == {"source": "example.com", "target": "api-server", "data": {"edgeType": "dns"}}
    # ...and it stays an edge: the record never becomes a node of its own.
    assert {n["type"] for n in result.nodes} == {"vpc", "subnet", "route53", "ec2"}


def test_odins_own_record_imports_without_a_single_warning():
    """odin generates this exact file. Warning noise is not harmless in a module
    whose whole value is that its warnings are worth reading."""
    assert parse_hcl_text(_project(_NETWORK, _ZONE, _INSTANCE, _record())).warnings == []


def test_a_record_naming_something_unimportable_is_reported_never_dropped():
    """The `records` reference is a LIST holding an interpolation, which is the
    one reference shape in `import_tf` that is not a bare string -- so this also
    pins that `_record_reference` unwraps it rather than reporting every record
    as unresolvable."""
    tf = _project(_ZONE, _record())  # no aws_instance at all
    result = parse_hcl_text(tf)
    assert result.edges == []
    (entry,) = result.unsupported
    assert entry.type == "aws_route53_record" and entry.name == "example_com_api_server"
    assert "records" in entry.reason
    assert "would NOT resolve this name" in entry.reason


def test_a_record_pointing_at_a_zone_odin_did_not_import_is_reported_too():
    """The other end of the same reference. A record whose zone is missing has no
    canvas edge to be."""
    tf = _project(_NETWORK, _INSTANCE, _record())  # no aws_route53_zone
    result = parse_hcl_text(tf)
    assert result.edges == []
    (entry,) = result.unsupported
    assert entry.type == "aws_route53_record"
    assert "zone_id" in entry.reason


def test_a_record_type_odin_cannot_emit_is_reported_as_CHANGED():
    """odin's substrate is a hosts entry, which has no record type -- so a CNAME
    comes back as an A record answering with an IP. That is a different kind of
    answer, and importing it in silence is the elasticache bug in another
    costume."""
    result = parse_hcl_text(_project(_NETWORK, _ZONE, _INSTANCE, _record(type='"CNAME"')))
    (changed,) = _changed(result)
    assert "type=cname (odin always emits a)" in changed
    assert _lost(result) == [], "a substituted value is not a missing argument"


def test_a_ttl_odin_cannot_emit_is_reported_as_CHANGED():
    """Same reason, second argument: a hosts file has no TTL either, so a
    5-minute record comes back as 60 seconds."""
    result = parse_hcl_text(_project(_NETWORK, _ZONE, _INSTANCE, _record(ttl="300")))
    (changed,) = _changed(result)
    assert "ttl=300 (odin always emits 60)" in changed


def test_a_record_name_odin_will_not_reproduce_is_reported_as_CHANGED():
    """The record's name is RE-DERIVED as `<ec2 label>.<zone label>`, so a source
    that called it `www` gets `api-server.example.com` back -- a renamed DNS
    record is a name that stops answering."""
    tf = _project(_NETWORK, _ZONE, _INSTANCE, _record(name='"www.example.com"'))
    (changed,) = _changed(parse_hcl_text(tf))
    assert "name=www.example.com (odin always emits api-server.example.com)" in changed


def test_an_argument_the_record_does_not_model_is_reported():
    """A routing policy decides WHICH address the name answers with, and an edge
    cannot carry one -- so dropping it silently would be a different record
    wearing the same name."""
    tf = _project(_NETWORK, _ZONE, _INSTANCE,
                  _record(set_identifier='"blue"', health_check_id='"abc123"'))
    (lost,) = _lost(parse_hcl_text(tf))
    assert lost.startswith("example.com -> api-server (dns record): imported without unmodeled")
    assert "set_identifier" in lost and "health_check_id" in lost


def test_a_round_robin_record_reports_the_addresses_odin_will_not_re_emit():
    """odin emits one address per record. A second one is dropped by the round
    trip, and a name that answered with two hosts answering with one is a real
    change in what the name does."""
    tf = _project(
        _NETWORK, _ZONE, _INSTANCE,
        _INSTANCE.replace('"api_server"', '"api_server_2"').replace(
            '"odin:node" = "api-server"', '"odin:node" = "api-server-2"'),
        _record(records="[aws_instance.api_server.private_ip, "
                        "aws_instance.api_server_2.private_ip]"),
    )
    result = parse_hcl_text(tf)
    (edge,) = [e for e in result.edges if (e.get("data") or {}).get("edgeType") == "dns"]
    assert edge["target"] == "api-server", "the first address is the one that survives"
    (changed,) = _changed(result)
    assert "records=" in changed and "odin emits ONE address per record" in changed


def test_an_unmodeled_argument_on_the_ZONE_itself_is_reported():
    """A private zone's `vpc {}` block and a `comment` are both real arguments a
    one-field canvas tile cannot hold."""
    tf = _ZONE.replace('  name = "example.com"\n',
                       '  name    = "example.com"\n  comment = "internal only"\n')
    (lost,) = _lost(parse_hcl_text(tf))
    assert lost.startswith("example.com (route53): imported without unmodeled")
    assert "comment" in lost


def test_a_generated_canvas_with_a_record_survives_generate_import_generate():
    """The end-to-end claim: byte-stable, WITH the record. Without the edge
    coming back, the second generate emits no `aws_route53_record` and the next
    apply drops the hosts entry the first one created."""
    canvas = {
        "nodes": [
            {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
            {"id": "s", "type": "subnet",
             "data": {"label": "web", "cidr": "10.0.1.0/24", "vpc": "net"}},
            {"id": "i", "type": "ec2", "data": {"label": "api-server", "vpc": "net", "subnet": "web"}},
            {"id": "z", "type": "route53", "data": {"label": "example.com"}},
        ],
        "edges": [
            # Drawn instance -> zone AND with the pre-registry type name, which is
            # what a canvas saved before `dns` existed actually carries -- the
            # generator keys on the two node KINDS, so both must still produce a
            # record.
            {"id": "e1", "source": "i", "target": "z", "data": {"edgeType": "network"}},
        ],
    }
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert main_tf.count('resource "aws_route53_record"') == 1

    imported = parse_hcl_text(main_tf)
    assert imported.unsupported == [] and imported.warnings == []

    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf
