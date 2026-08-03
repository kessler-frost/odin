"""Hosted zones and their records, generate-side.

An `aws_route53_zone` on its own resolves nothing, so the whole feature is really
the COMPANION: a route53 node and an ec2 node joined by an edge is an
`aws_route53_record`. But the expensive mistake here is not the companion, it is
WHICH TARGETS ARE ALLOWED TO HAVE ONE.

odin's substrate for a DNS record is a hosts entry -- `--add-host` on containers,
`/etc/hosts` on Lima VMs. A hosts entry is `<ip> <name>`: it carries no port and
no scheme. Exactly one canvas kind publishes an address that shape (`ec2`, whose
`PRIVATE_IP`/`MESH_IP` facts are bare IPv4); an `alb` publishes
`http://127.0.0.1:<dynamic port>` and an `rds` publishes
`host.docker.internal:<dynamic port>`. A record pointing at either would RESOLVE
and then FAIL TO CONNECT -- a green resource that does not work, which is the
failure this repo's honesty rules exist to prevent.

So the tests that matter most in this file are the REFUSALS, and they assert on
the reason text rather than merely on absence: "no record was emitted" is also
what a silently broken generator produces.
"""
from __future__ import annotations

import re

from odin.iac.hcl import _DNS_TARGET_KINDS, generate_tf
from odin.spec.translate import canvas_to_stack

_NETWORK = [
    {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
    {"id": "s", "type": "subnet", "data": {"label": "web", "cidr": "10.0.1.0/24", "vpc": "net"}},
]
_INSTANCE = {"id": "i", "type": "ec2", "data": {"label": "api", "vpc": "net", "subnet": "web"}}
_ZONE = {"id": "z", "type": "route53", "data": {"label": "example.com"}}


def _edge(source: str, target: str, edge_type: str = "dns") -> dict:
    return {"id": f"{source}-{target}", "source": source, "target": target,
            "data": {"edgeType": edge_type}}


def _project(nodes: list[dict], edges: list[dict] | None = None):
    return generate_tf(canvas_to_stack({"nodes": nodes, "edges": edges or []}))


def test_a_zone_edged_to_an_instance_emits_the_zone_and_the_record():
    project = _project([*_NETWORK, _INSTANCE, _ZONE], [_edge("z", "i")])
    main_tf = project.files["main.tf"]
    assert project.unsupported == []
    assert 'resource "aws_route53_zone" "example_com" {\n  name = "example.com"' in main_tf
    # The whole companion, byte for byte: the references are what tofu resolves,
    # and a typo in either argument name is a plan failure for the whole project.
    assert (
        'resource "aws_route53_record" "example_com_api" {\n'
        "  zone_id = aws_route53_zone.example_com.zone_id\n"
        '  name    = "api.example.com"\n'
        '  type    = "A"\n'
        "  ttl     = 60\n"
        "  records = [aws_instance.api.private_ip]\n"
        "}"
    ) in main_tf


def test_a_zone_with_no_edge_is_an_empty_hosted_zone_and_no_record():
    """An `aws_route53_zone` alone is a real, legitimate AWS resource -- the edge
    is the only thing that can say what a name points at, so its absence is not
    an error."""
    project = _project([*_NETWORK, _INSTANCE, _ZONE])
    assert project.unsupported == []
    assert 'resource "aws_route53_zone" "example_com"' in project.files["main.tf"]
    assert "aws_route53_record" not in project.files["main.tf"]


def test_the_reverse_direction_edge_produces_the_identical_file():
    """Which end the user started the drag from carries no meaning."""
    nodes = [*_NETWORK, _INSTANCE, _ZONE]
    assert _project(nodes, [_edge("i", "z")]).files == _project(nodes, [_edge("z", "i")]).files


def test_an_edge_saved_before_the_edge_type_registry_still_resolves():
    """`route53` was a drawable catalog tile for a long time before it had a
    builder, so canvases already exist whose route53 edge is typed `network`. The
    record pass keys on the two NODE KINDS and never on `edge.kind`, so the
    stored name cannot matter -- gating on it would silently emit no record for
    every one of those canvases."""
    nodes = [*_NETWORK, _INSTANCE, _ZONE]
    legacy = _project(nodes, [_edge("z", "i", "network")]).files["main.tf"]
    assert "aws_route53_record" in legacy
    assert legacy == _project(nodes, [_edge("z", "i", "dns")]).files["main.tf"]


# --- the refusals: the whole point of this kind ------------------------------

def _alb(node_id: str, label: str) -> dict:
    return {"id": node_id, "type": "alb",
            "data": {"label": label, "vpc": "net", "subnet": "web"}}


def test_a_record_pointing_at_a_load_balancer_is_refused_naming_the_port():
    """THE TRAP. An ALB's endpoint is `http://127.0.0.1:<dynamic port>` -- odin
    publishes the proxy on a dynamic host port because a fixed 80 would collide
    across load balancers and envs. A hosts entry cannot carry a port, so this
    name would resolve to 127.0.0.1 and then fail to connect."""
    project = _project([*_NETWORK, _ZONE, _alb("l", "web-lb")], [_edge("z", "l")])
    assert "aws_route53_record" not in project.files["main.tf"]
    (refusal,) = [entry for entry in project.unsupported if "DNS record" in entry]
    assert "web-lb" in refusal and "alb" in refusal
    # The REASON, not merely the refusal: a generator that silently emitted
    # nothing would also pass an absence-only assertion.
    assert "ALB_ENDPOINT" in refusal
    assert "cannot carry a port" in refusal
    assert "resolve and then fail to connect" in refusal


def test_a_record_pointing_at_a_database_is_refused_naming_the_port():
    """The sibling, hunted rather than waited for: `rds`'s `endpoint` fact is
    `host.docker.internal:<dynamic port>`, the same shape as the ALB's."""
    project = _project([*_NETWORK, _ZONE, {"id": "d", "type": "rds", "data": {"label": "app-db"}}],
                       [_edge("z", "d")])
    assert "aws_route53_record" not in project.files["main.tf"]
    (refusal,) = [entry for entry in project.unsupported if "DNS record" in entry]
    assert "app-db" in refusal and "cannot carry a port" in refusal


def test_a_record_pointing_at_a_kind_with_no_address_at_all_is_refused():
    """Not every refusal is about a port. An s3 bucket publishes no address a
    hosts file could express at all, and the message says that instead of
    borrowing the port sentence."""
    project = _project([*_NETWORK, _ZONE, {"id": "b", "type": "s3", "data": {"label": "uploads"}}],
                       [_edge("z", "b")])
    assert "aws_route53_record" not in project.files["main.tf"]
    (refusal,) = [entry for entry in project.unsupported if "DNS record" in entry]
    assert "uploads" in refusal and "s3" in refusal
    assert "publishes no address a hosts file can express" in refusal
    assert "cannot carry a port" not in refusal, "the port sentence is about ported endpoints only"


def test_every_refused_target_says_what_to_use_instead():
    """A refusal that names no alternative sends the user looking. Every one
    points at the node's own endpoint fact, which is the thing that really does
    carry the port."""
    nodes = [*_NETWORK, _ZONE, _alb("l", "web-lb"),
             {"id": "d", "type": "rds", "data": {"label": "app-db"}},
             {"id": "b", "type": "s3", "data": {"label": "uploads"}}]
    project = _project(nodes, [_edge("z", "l"), _edge("z", "d"), _edge("z", "b")])
    refusals = [entry for entry in project.unsupported if "DNS record" in entry]
    assert len(refusals) == 3
    for refusal in refusals:
        assert "use the node's own endpoint fact instead" in refusal


def test_the_supported_target_set_is_exactly_ec2():
    """Guards the guard. Every assertion above about a refusal is vacuous if the
    allowed set silently grew -- and widening it is precisely the change that
    would reintroduce the trap."""
    assert _DNS_TARGET_KINDS == ("ec2",)


# --- names that could never resolve ------------------------------------------

def test_a_zone_whose_label_is_not_a_domain_is_refused():
    project = _project([{"id": "z", "type": "route53", "data": {"label": "not a domain"}}])
    assert "aws_route53_zone" not in project.files["main.tf"]
    (refusal,) = project.unsupported
    assert "is not a valid DNS name" in refusal


def test_an_instance_whose_label_is_not_a_dns_label_gets_no_record():
    """The record's name is `<instance label>.<zone>`, so an instance labelled
    with something DNS cannot express makes an entry no resolver would ever
    match. The instance itself is still built -- only the record is declined."""
    bad = {"id": "i", "type": "ec2", "data": {"label": "api server", "vpc": "net", "subnet": "web"}}
    project = _project([*_NETWORK, bad, _ZONE], [_edge("z", "i")])
    assert "aws_route53_record" not in project.files["main.tf"]
    assert 'resource "aws_instance"' in project.files["main.tf"]
    (refusal,) = [entry for entry in project.unsupported if "DNS record" in entry]
    assert "is not a valid DNS name" in refusal


def test_a_record_is_withheld_when_its_instance_was_declined():
    """A record naming an `aws_instance` pass 2 declined is an unresolvable
    reference, which fails `tofu plan` for the WHOLE project rather than for that
    one node. The instance's own decline already names the cause."""
    orphan = {"id": "i", "type": "ec2", "data": {"label": "api", "vpc": "net"}}  # no subnet
    project = _project([*_NETWORK, orphan, _ZONE], [_edge("z", "i")])
    assert "aws_route53_record" not in project.files["main.tf"]
    assert any("api (ec2)" in entry for entry in project.unsupported), project.unsupported


def test_a_record_is_withheld_when_its_zone_was_declined():
    """The other half of the same rule."""
    bad_zone = {"id": "z", "type": "route53", "data": {"label": "not a domain"}}
    project = _project([*_NETWORK, _INSTANCE, bad_zone], [_edge("z", "i")])
    assert "aws_route53_record" not in project.files["main.tf"]
    assert any("is not a valid DNS name" in entry for entry in project.unsupported)


# --- shape ------------------------------------------------------------------

def test_two_zones_over_one_instance_each_get_their_own_record():
    """Legal in AWS and legal here: the same host answers to two names."""
    second = {"id": "z2", "type": "route53", "data": {"label": "internal.test"}}
    project = _project([*_NETWORK, _INSTANCE, _ZONE, second],
                       [_edge("z", "i"), _edge("z2", "i")])
    names = sorted(re.findall(r'^  name    = "(.+)"$', project.files["main.tf"], re.M))
    assert names == ["api.example.com", "api.internal.test"]
    assert project.unsupported == []


def test_the_generated_file_does_not_depend_on_edge_ordering():
    nodes = [*_NETWORK, _INSTANCE, _ZONE, {"id": "z2", "type": "route53",
                                           "data": {"label": "internal.test"}}]
    forward = _project(nodes, [_edge("z", "i"), _edge("z2", "i")]).files["main.tf"]
    reverse = _project(nodes, [_edge("z2", "i"), _edge("z", "i")]).files["main.tf"]
    assert forward == reverse
