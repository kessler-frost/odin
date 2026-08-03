"""route53 records -> per-consumer /etc/hosts entries.

The rule under test throughout: **nothing guesses.** A VM consumer needs the
target's Nebula OVERLAY address, because a VM cannot reach another VM's vzNAT
`private_ip` at all (100% loss, `fabric/nebula.py` R5). The stored record does
not carry the target's instance id, so the mapping goes through a reverse
lookup on address -- and a reverse lookup can be ambiguous or empty. Every one
of those cases must REFUSE with a reason rather than fall back to the stored
`private_ip`, which is the one address guaranteed not to work.
"""
from __future__ import annotations

from odin.compute.hosts import container_hosts, vm_hosts

WEB = {"instance_id": "i-web", "private_ip": "192.168.64.2", "public_ip": "192.168.64.2"}
DB = {"instance_id": "i-db", "private_ip": "192.168.64.3", "public_ip": "192.168.64.3"}
OVERLAY = {"i-web": "10.42.0.5", "i-db": "10.42.0.6"}


def _a(name: str = "api.odin.internal", *values: str) -> dict:
    """A record in the shape `route53ctl` REALLY stores.

    `name` is BARE -- no trailing dot. That is not cosmetic and it is not a
    guess: r53-gw canonicalises to bare on write because the AWS provider sends
    bare on create and appends a dot on read, so the store never holds the
    dotted form. These fixtures said `api.internal.` until that was confirmed,
    which would have proved this module against a signal the store does not
    send -- honesty rule 1, in a fixture. `test_both_name_forms_resolve` keeps
    the dotted form covered anyway, because a reader coming from
    `DescribeResourceRecordSets` output will have one."""
    return {"name": name, "type": "A", "ttl": 60, "set_identifier": None,
            "values": list(values), "alias": None}


# --- container consumer: the stored value is already right -------------------


def test_a_container_uses_the_records_own_address():
    """The one case where the portable AWS answer and the locally-useful one
    coincide -- container-to-VM on the vzNAT address is measured reachable."""
    plan = container_hosts([_a("api.odin.internal", "192.168.64.2")])
    assert plan.resolved == {"api.odin.internal": "192.168.64.2"}
    assert plan.unresolvable == {}


def test_both_name_forms_resolve():
    """The store holds BARE names, so that is the shape every other fixture
    here uses. This one covers the dotted form as well, because it is not
    hypothetical: the AWS provider appends a trailing dot on read, so a caller
    that hands this module something straight out of
    `DescribeResourceRecordSets` — or any future code that canonicalises the
    other way — must get the same `/etc/hosts` name and not a second entry
    differing only by a dot."""
    bare = container_hosts([_a("api.odin.internal", "10.0.0.1")]).resolved
    dotted = container_hosts([_a("api.odin.internal.", "10.0.0.1")]).resolved
    assert bare == dotted == {"api.odin.internal": "10.0.0.1"}


def test_zone_infrastructure_records_are_skipped_silently():
    """Every zone is seeded with SOA and NS records. They name no host, so
    they produce no entry -- and they are not "unresolvable" either, because
    reporting them would bury the real findings in noise."""
    records = [
        _a("api.odin.internal", "192.168.64.2"),
        {"name": "z.", "type": "SOA", "values": ["ns1. root. 1"], "alias": None},
        {"name": "z.", "type": "NS", "values": ["ns1."], "alias": None},
    ]
    plan = container_hosts(records)
    assert plan.resolved == {"api.odin.internal": "192.168.64.2"}
    assert plan.unresolvable == {}


def test_an_alias_record_names_no_address():
    """An alias points at another AWS resource by name, so there is no address
    to write even though its type is A.

    The fixture carries BOTH an alias and literal values on purpose. An alias
    record normally has empty `values`, and a fixture like that passes whether
    or not the alias check exists — it is excluded by the empty-values test
    alone, so it proves nothing about the alias branch. (Found by mutation:
    deleting `not r.get("alias")` killed no test.) The two fields are parsed
    independently from the wire (`route53ctl._rrset_from_wire` reads
    `ResourceRecords` and `AliasTarget` separately), so a record carrying both
    is representable in the store, and this pins which field decides."""
    alias = {"name": "api.internal.", "type": "A", "values": ["192.168.64.2"],
             "alias": {"dns_name": "lb-1.elb.amazonaws.com"}}
    assert container_hosts([alias]).resolved == {}
    assert vm_hosts([alias], [WEB], OVERLAY).resolved == {}


# --- VM consumer: the substitution, and every way it can refuse --------------


def test_a_vm_gets_the_overlay_address_not_the_private_ip():
    """THE substitution. Resolving to `192.168.64.2` here would produce a name
    that resolves and never connects."""
    plan = vm_hosts([_a("api.odin.internal", "192.168.64.2")], [WEB, DB], OVERLAY)
    assert plan.resolved == {"api.odin.internal": "10.42.0.5"}
    assert plan.unresolvable == {}


def test_an_env_with_no_mesh_resolves_nothing_and_says_why():
    """An empty overlay map is what a mesh-less env passes. Silently resolving
    to the private_ip would be the worst available answer."""
    plan = vm_hosts([_a("api.odin.internal", "192.168.64.2")], [WEB], {})
    assert plan.resolved == {}
    assert "no Nebula overlay address" in plan.unresolvable["api.odin.internal"]
    assert "100% loss" in plan.unresolvable["api.odin.internal"]
    assert plan.names == ("api.odin.internal",)


def test_an_address_no_instance_reports_is_refused_not_passed_through():
    plan = vm_hosts([_a("api.odin.internal", "10.9.9.9")], [WEB, DB], OVERLAY)
    assert plan.resolved == {}
    reason = plan.unresolvable["api.odin.internal"]
    assert "10.9.9.9" in reason
    assert "no EC2 instance in this environment reports" in reason


def test_two_instances_sharing_an_address_is_refused_and_names_both():
    """Ambiguity must not be resolved by picking one. The reason names both
    instances, because the user's fix is to deduplicate them."""
    twin = {"instance_id": "i-twin", "private_ip": "192.168.64.2", "public_ip": None}
    plan = vm_hosts([_a("api.odin.internal", "192.168.64.2")], [WEB, twin], OVERLAY)
    assert plan.resolved == {}
    reason = plan.unresolvable["api.odin.internal"]
    assert "i-twin" in reason and "i-web" in reason
    assert "cannot tell which instance" in reason


def test_ambiguous_and_missing_are_different_answers():
    """They need opposite fixes from a person, so they must not collapse into
    one unhelpful "could not resolve"."""
    twin = {"instance_id": "i-twin", "private_ip": "192.168.64.2", "public_ip": None}
    ambiguous = vm_hosts([_a("a.odin.internal", "192.168.64.2")], [WEB, twin], OVERLAY)
    missing = vm_hosts([_a("b.odin.internal", "10.9.9.9")], [WEB], OVERLAY)
    assert ambiguous.unresolvable["a.odin.internal"] != missing.unresolvable["b.odin.internal"]


def test_a_public_ip_match_resolves_too():
    """`ec2compute._finish_boot` writes the same discovered address into both
    `private_ip` and `public_ip`, so a record generated from either must reach
    the same VM."""
    only_public = {"instance_id": "i-pub", "private_ip": None, "public_ip": "192.168.64.7"}
    plan = vm_hosts([_a("api.odin.internal", "192.168.64.7")], [only_public], {"i-pub": "10.42.0.9"})
    assert plan.resolved == {"api.odin.internal": "10.42.0.9"}


def test_one_bad_record_does_not_suppress_the_good_ones():
    """A partial answer is the useful one: the names that CAN resolve should,
    and only the broken one is reported."""
    plan = vm_hosts(
        [_a("good.odin.internal", "192.168.64.2"), _a("bad.odin.internal", "10.9.9.9")],
        [WEB], OVERLAY,
    )
    assert plan.resolved == {"good.odin.internal": "10.42.0.5"}
    assert plan.names == ("bad.odin.internal",)


# --- round-robin: refused identically on BOTH consumer paths ----------------


def test_a_multi_value_record_is_refused_for_a_vm():
    """/etc/hosts cannot express round-robin -- a resolver answers with
    whichever line came first, which is not what the record means."""
    plan = vm_hosts([_a("api.odin.internal", "192.168.64.2", "192.168.64.3")], [WEB, DB], OVERLAY)
    assert plan.resolved == {}
    assert "round-robin" in plan.unresolvable["api.odin.internal"]


def test_a_multi_value_record_is_refused_for_a_container_too():
    """Refused on BOTH paths deliberately. Collapsing it on the container path
    while refusing it on the VM path would make one name behave differently
    depending on who asked, which is worse than either answer alone."""
    plan = container_hosts([_a("api.odin.internal", "192.168.64.2", "192.168.64.3")])
    assert plan.resolved == {}
    assert "round-robin" in plan.unresolvable["api.odin.internal"]


def test_no_records_is_an_empty_plan_not_an_error():
    assert vm_hosts([], [WEB], OVERLAY).resolved == {}
    assert vm_hosts([], [WEB], OVERLAY).unresolvable == {}
    assert container_hosts([]).resolved == {}
