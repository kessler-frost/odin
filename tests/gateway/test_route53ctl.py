"""gateway/models/route53ctl.py -- the Route 53 control plane.

Same test method as kmsctl/logsctl/secretsctl/ssmctl: every request is a REAL
boto3-signed capture (tests/gateway/harness.py's CaptureSink + the `route53`
client fixture) and every response round-trips through botocore's OWN
`RestXMLParser` against the REAL route53 service model -- so what is asserted is
that the wire bytes parse as route53, not that a string appears in them. Every
call ALSO routes through classify() -> await synth.pure_answer(), exercising the
`route53` branch of the dispatch pipeline end to end.

The WHY of this model is in route53ctl.py's own docstring, and the first line of
it is the one to carry into these tests: **odin serves no DNS.** Nothing here
asserts that a name resolves, because nothing resolves it. What is asserted is
that the control plane is real: zones and records persist, round-trip in
AWS-shaped bytes, refuse what real Route 53 refuses, and -- the one that can
cost a user an apply rather than a resource -- that `GetChange` reaches INSYNC
so the provider's waiter can terminate.
"""
from __future__ import annotations

from pathlib import Path

import botocore.session
import pytest
from botocore.awsrequest import HeadersDict
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import _route53_zone, classify
from odin.gateway.models import route53ctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
ZONE = "odin.internal"
LABEL = "dns"


def _parse(operation: str, response: Response, *, error: bool = False):
    """The response as botocore's REAL route53 parser reads it.

    `HeadersDict` rather than a plain dict, and the difference is load-bearing
    for exactly one member: `CreateHostedZone`'s `Location` lives in a HEADER,
    Starlette lowercases outgoing header names, and botocore looks it up by its
    modelled name `Location`. A plain `dict(response.headers)` therefore drops
    it and the test reads as a missing header when the header is really there --
    a harness artifact wearing a bug's clothes. A real client is handed the
    case-insensitive mapping this is."""
    model = _SESSION.get_service_model("route53")
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": HeadersDict(dict(response.headers)), "body": response.body}
    parsed = parser.parse(raw, model.operation_model(operation).output_shape)
    if error:
        assert response.status_code >= 300, f"expected an error status, got {response.status_code}"
        assert "Error" in parsed, f"no Error document in {response.body!r}"
    else:
        assert response.status_code < 300, f"unexpected error: {response.body!r}"
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


async def _answer(stores: SynthStores, req) -> Response:
    """One request through the REAL pipeline: classify() then pure_answer()."""
    path, query = split_url(req.url)
    classified = classify("route53", req.method, path, query, req.headers, req.body)
    assert classified is not None, f"a route53 request must never be unmappable: {req.method} {path}"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, "route53 is all-synth: pure_answer must never fall through"
    return response


def _classify(req) -> tuple[str, str]:
    path, query = split_url(req.url)
    classified = classify("route53", req.method, path, query, req.headers, req.body)
    assert classified is not None, f"unmappable: {req.method} {path}"
    return classified


async def _create_zone(stores, sink, route53, name: str = ZONE) -> Response:
    return await _answer(stores, sink.call(lambda: route53.create_hosted_zone(
        Name=name, CallerReference="cr-1",
        HostedZoneConfig={"Comment": "made by odin", "PrivateZone": False},
    )))


async def _change(stores, sink, route53, changes: list[dict], zone: str = ZONE) -> Response:
    return await _answer(stores, sink.call(lambda: route53.change_resource_record_sets(
        HostedZoneId=zone, ChangeBatch={"Comment": "c", "Changes": changes},
    )))


def _a_record(name: str, value: str = "10.0.0.5", ttl: int = 60) -> dict:
    return {"Name": name, "Type": "A", "TTL": ttl, "ResourceRecords": [{"Value": value}]}


# --- the operations ---------------------------------------------------------


async def test_create_hosted_zone_answers_a_parseable_zone(stores, sink, route53):
    parsed = _parse("CreateHostedZone", await _create_zone(stores, sink, route53))
    assert parsed["HostedZone"]["Id"] == f"/hostedzone/{ZONE}"
    assert parsed["HostedZone"]["Name"] == f"{ZONE}."
    assert parsed["HostedZone"]["Config"]["Comment"] == "made by odin"
    assert parsed["HostedZone"]["Config"]["PrivateZone"] is False
    # The SOA + NS pair real CreateHostedZone mints.
    assert parsed["HostedZone"]["ResourceRecordSetCount"] == 2
    assert parsed["ChangeInfo"]["Status"] == "INSYNC"
    assert len(parsed["DelegationSet"]["NameServers"]) == 4
    # `Location` is a HEADER member on this operation's output shape, so a body
    # element would be dropped by the parser and never reach the caller.
    assert parsed["Location"] == f"/2013-04-01/hostedzone/{ZONE}"


async def test_get_hosted_zone_reads_back_what_create_wrote(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    req = sink.call(lambda: route53.get_hosted_zone(Id=ZONE))
    parsed = _parse("GetHostedZone", await _answer(stores, req))
    assert parsed["HostedZone"]["Name"] == f"{ZONE}."
    assert parsed["HostedZone"]["CallerReference"] == "cr-1"


async def test_get_hosted_zone_is_not_found_for_a_zone_that_never_existed(stores, sink, route53):
    req = sink.call(lambda: route53.get_hosted_zone(Id="nope.internal"))
    parsed = _parse("GetHostedZone", await _answer(stores, req), error=True)
    assert parsed["Error"]["Code"] == "NoSuchHostedZone"
    assert "nope.internal" in parsed["Error"]["Message"]


async def test_list_hosted_zones_lists_every_created_zone(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    await _create_zone(stores, sink, route53, "other.internal")
    req = sink.call(route53.list_hosted_zones)
    parsed = _parse("ListHostedZones", await _answer(stores, req))
    assert [z["Name"] for z in parsed["HostedZones"]] == ["odin.internal.", "other.internal."]
    assert parsed["IsTruncated"] is False


async def test_list_hosted_zones_by_name_is_its_own_route(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    req = sink.call(lambda: route53.list_hosted_zones_by_name(DNSName=f"{ZONE}."))
    action, _ = _classify(req)
    assert action == "route53:ListHostedZonesByName"
    parsed = _parse("ListHostedZonesByName", await _answer(stores, req))
    assert [z["Name"] for z in parsed["HostedZones"]] == [f"{ZONE}."]


async def test_delete_hosted_zone_removes_it(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    parsed = _parse("DeleteHostedZone", await _answer(
        stores, sink.call(lambda: route53.delete_hosted_zone(Id=ZONE))
    ))
    assert parsed["ChangeInfo"]["Status"] == "INSYNC"
    gone = _parse("GetHostedZone", await _answer(
        stores, sink.call(lambda: route53.get_hosted_zone(Id=ZONE))
    ), error=True)
    assert gone["Error"]["Code"] == "NoSuchHostedZone"


async def test_delete_hosted_zone_refuses_while_a_record_stands_and_names_it(stores, sink, route53):
    """Deviation 3, and honesty rule 2's "name what is still standing"."""
    await _create_zone(stores, sink, route53)
    await _change(stores, sink, route53, [
        {"Action": "CREATE", "ResourceRecordSet": _a_record(f"api.{ZONE}")},
    ])
    parsed = _parse("DeleteHostedZone", await _answer(
        stores, sink.call(lambda: route53.delete_hosted_zone(Id=ZONE))
    ), error=True)
    assert parsed["Error"]["Code"] == "HostedZoneNotEmpty"
    assert f"api.{ZONE}. A" in parsed["Error"]["Message"]
    # ...and the zone really is still there, rather than half-deleted.
    _parse("GetHostedZone", await _answer(stores, sink.call(lambda: route53.get_hosted_zone(Id=ZONE))))


async def test_change_resource_record_sets_creates_a_readable_record(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    changed = _parse("ChangeResourceRecordSets", await _change(stores, sink, route53, [
        {"Action": "CREATE", "ResourceRecordSet": _a_record(f"api.{ZONE}")},
    ]))
    assert changed["ChangeInfo"]["Status"] == "INSYNC"
    listed = _parse("ListResourceRecordSets", await _answer(
        stores, sink.call(lambda: route53.list_resource_record_sets(HostedZoneId=ZONE))
    ))
    records = {(r["Name"], r["Type"]): r for r in listed["ResourceRecordSets"]}
    api = records[(f"api.{ZONE}.", "A")]
    assert api["TTL"] == 60
    assert [rr["Value"] for rr in api["ResourceRecords"]] == ["10.0.0.5"]
    # The auto-created pair is still there beside it.
    assert (f"{ZONE}.", "SOA") in records
    assert (f"{ZONE}.", "NS") in records


async def test_upsert_replaces_the_value_rather_than_duplicating_the_record(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    await _change(stores, sink, route53, [
        {"Action": "CREATE", "ResourceRecordSet": _a_record(f"api.{ZONE}", "10.0.0.5")},
    ])
    await _change(stores, sink, route53, [
        {"Action": "UPSERT", "ResourceRecordSet": _a_record(f"api.{ZONE}", "10.0.0.9", ttl=300)},
    ])
    listed = _parse("ListResourceRecordSets", await _answer(
        stores, sink.call(lambda: route53.list_resource_record_sets(HostedZoneId=ZONE))
    ))
    matching = [r for r in listed["ResourceRecordSets"] if r["Name"] == f"api.{ZONE}."]
    assert len(matching) == 1, "UPSERT must replace, never append a second record set"
    assert matching[0]["TTL"] == 300
    assert [rr["Value"] for rr in matching[0]["ResourceRecords"]] == ["10.0.0.9"]


async def test_change_resource_record_sets_with_a_delete_removes_the_record(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    await _change(stores, sink, route53, [
        {"Action": "CREATE", "ResourceRecordSet": _a_record(f"api.{ZONE}")},
    ])
    _parse("ChangeResourceRecordSets", await _change(stores, sink, route53, [
        {"Action": "DELETE", "ResourceRecordSet": _a_record(f"api.{ZONE}")},
    ]))
    listed = _parse("ListResourceRecordSets", await _answer(
        stores, sink.call(lambda: route53.list_resource_record_sets(HostedZoneId=ZONE))
    ))
    assert [r["Name"] for r in listed["ResourceRecordSets"] if r["Type"] == "A"] == []


async def test_deleting_a_record_that_is_not_there_is_refused(stores, sink, route53):
    """A delete that reported success against a record that never existed would
    let a destroy claim a teardown it did not perform."""
    await _create_zone(stores, sink, route53)
    parsed = _parse("ChangeResourceRecordSets", await _change(stores, sink, route53, [
        {"Action": "DELETE", "ResourceRecordSet": _a_record(f"ghost.{ZONE}")},
    ]), error=True)
    assert parsed["Error"]["Code"] == "InvalidChangeBatch"
    assert f"ghost.{ZONE}." in parsed["Error"]["Message"]


async def test_get_change_reaches_insync(stores, sink, route53):
    """THE test this model exists to pass. A `PENDING` odin never leaves makes
    `tofu apply` hang rather than fail, and a hang is indistinguishable from
    "still working" -- so the status the provider's waiter polls for has to be
    the one it gets, on the FIRST refresh, with no clock involved."""
    created = _parse("CreateHostedZone", await _create_zone(stores, sink, route53))
    change_id = created["ChangeInfo"]["Id"].rpartition("/")[2]
    parsed = _parse("GetChange", await _answer(
        stores, sink.call(lambda: route53.get_change(Id=change_id))
    ))
    assert parsed["ChangeInfo"]["Status"] == "INSYNC"
    assert parsed["ChangeInfo"]["Id"] == f"/change/{change_id}"
    # Polling again must not flip it back -- a waiter refreshes more than once.
    again = _parse("GetChange", await _answer(
        stores, sink.call(lambda: route53.get_change(Id=change_id))
    ))
    assert again["ChangeInfo"]["Status"] == "INSYNC"


async def test_get_change_for_a_change_that_was_never_minted_is_refused(stores, sink, route53):
    parsed = _parse("GetChange", await _answer(
        stores, sink.call(lambda: route53.get_change(Id="C00000000000000"))
    ), error=True)
    assert parsed["Error"]["Code"] == "NoSuchChange"


async def test_a_record_change_also_mints_an_insync_change(stores, sink, route53):
    """`aws_route53_record` waits on synchronize too -- MEASURED as the literal
    "waiting for Route 53 Record (%s) synchronize" in the shipped provider
    binary -- so the change a record write mints must reach INSYNC as well."""
    await _create_zone(stores, sink, route53)
    changed = _parse("ChangeResourceRecordSets", await _change(stores, sink, route53, [
        {"Action": "CREATE", "ResourceRecordSet": _a_record(f"api.{ZONE}")},
    ]))
    change_id = changed["ChangeInfo"]["Id"].rpartition("/")[2]
    parsed = _parse("GetChange", await _answer(
        stores, sink.call(lambda: route53.get_change(Id=change_id))
    ))
    assert parsed["ChangeInfo"]["Status"] == "INSYNC"


# --- the guards -------------------------------------------------------------


async def test_an_unsupported_record_type_is_refused(stores, sink, route53):
    """Mutation target (c). `WKS` is a real DNS type and is deliberately NOT in
    botocore's `RRType` enum, so storing it would round-trip a record no Route
    53 vocabulary contains, behind a 200.

    Sent as raw bytes rather than through boto3: the client validates `Type`
    against the enum itself and would refuse to sign the request, so a
    boto3-built capture cannot reach the gateway's own guard at all."""
    await _create_zone(stores, sink, route53)
    body = (
        f'<ChangeResourceRecordSetsRequest xmlns="{route53ctl.NS}"><ChangeBatch><Changes><Change>'
        f"<Action>CREATE</Action><ResourceRecordSet><Name>api.{ZONE}.</Name><Type>WKS</Type>"
        f"<TTL>60</TTL><ResourceRecords><ResourceRecord><Value>10.0.0.5</Value></ResourceRecord>"
        f"</ResourceRecords></ResourceRecordSet></Change></Changes></ChangeBatch>"
        f"</ChangeResourceRecordSetsRequest>"
    ).encode()
    response = await synth.pure_answer(
        "route53:ChangeResourceRecordSets", ZONE, ENV, body, stores, 0.0
    )
    parsed = _parse("ChangeResourceRecordSets", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidChangeBatch"
    assert "WKS" in parsed["Error"]["Message"]
    # ...and nothing was written.
    listed = _parse("ListResourceRecordSets", await _answer(
        stores, sink.call(lambda: route53.list_resource_record_sets(HostedZoneId=ZONE))
    ))
    assert [r for r in listed["ResourceRecordSets"] if r["Type"] == "WKS"] == []


async def test_an_unknown_route53_action_is_refused_not_silently_accepted(stores):
    """The model's own belt-and-braces: `classify` already refuses an
    unrecognised (method, path), but a caller reaching `pure_answer` with an
    action this module does not model must get a protocol-correct error rather
    than a silent 200 or a None that would make `app.py` try to forward to a
    backing route53 does not have."""
    response = await synth.pure_answer(
        "route53:CreateHealthCheck", "*", ENV, b"", stores, 0.0
    )
    assert response is not None, "route53 is all-synth: it must never fall through to a forward"
    assert response.status_code >= 300
    assert b"InvalidAction" in response.body
    assert b"CreateHealthCheck" in response.body


def test_an_unmodeled_route53_path_is_unmappable(sink, route53):
    """The closed-world deny one layer out: a health-check route is real Route
    53 and is deliberately not in the table, so it classifies as None and
    `app.py` answers `unmappable-action` rather than guessing."""
    req = sink.call(lambda: route53.list_health_checks())
    path, query = split_url(req.url)
    assert classify("route53", req.method, path, query, req.headers, req.body) is None


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/2013-04-01/hostedzone", "route53:CreateHostedZone"),
        ("GET", "/2013-04-01/hostedzone", "route53:ListHostedZones"),
        ("GET", "/2013-04-01/hostedzonesbyname", "route53:ListHostedZonesByName"),
        ("GET", f"/2013-04-01/hostedzone/{ZONE}", "route53:GetHostedZone"),
        ("DELETE", f"/2013-04-01/hostedzone/{ZONE}", "route53:DeleteHostedZone"),
        ("POST", f"/2013-04-01/hostedzone/{ZONE}/rrset/", "route53:ChangeResourceRecordSets"),
        ("GET", f"/2013-04-01/hostedzone/{ZONE}/rrset", "route53:ListResourceRecordSets"),
        ("GET", "/2013-04-01/change/C123", "route53:GetChange"),
        ("POST", f"/2013-04-01/tags/hostedzone/{ZONE}", "route53:ChangeTagsForResource"),
        ("GET", f"/2013-04-01/tags/hostedzone/{ZONE}", "route53:ListTagsForResource"),
    ],
)
def test_every_modeled_route_classifies_to_its_own_action(method, path, expected):
    classified = classify("route53", method, path, {}, {}, b"")
    assert classified is not None, f"{method} {path} must classify"
    assert classified[0] == expected


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # Mutation target (b): the trailing slash is the ONLY thing separating
        # the rrset WRITE route from the rrset READ route, so an unanchored or
        # slash-tolerant pattern classifies a write as a read -- a policy hole,
        # not a 404.
        ("POST", f"/2013-04-01/hostedzone/{ZONE}/rrset"),
        ("GET", f"/2013-04-01/hostedzone/{ZONE}/rrset/"),
        # Paths that must not be swallowed by a neighbouring pattern.
        ("GET", "/2013-04-01/hostedzone/a/b"),
        ("GET", "/2013-04-01/healthcheck"),
        ("GET", f"/2013-04-01/tags/healthcheck/{ZONE}"),
        ("PUT", f"/2013-04-01/hostedzone/{ZONE}"),
        ("GET", "/2013-04-01/change"),
        ("POST", f"/2013-04-01/hostedzone/{ZONE}"),
    ],
)
def test_a_path_no_route_owns_is_unmappable(method, path):
    assert classify("route53", method, path, {}, {}, b"") is None


def test_no_two_routes_match_the_same_method_and_path():
    """The `$`-anchoring invariant, asserted rather than trusted: every pattern
    is checked against every other route's own example path, and at most one
    may match. A pattern that lost its anchor fails HERE rather than by
    classifying a write as a read in production."""
    examples = [
        ("POST", "/2013-04-01/hostedzone"),
        ("GET", "/2013-04-01/hostedzone"),
        ("GET", "/2013-04-01/hostedzonesbyname"),
        ("GET", f"/2013-04-01/hostedzone/{ZONE}"),
        ("DELETE", f"/2013-04-01/hostedzone/{ZONE}"),
        ("POST", f"/2013-04-01/hostedzone/{ZONE}/rrset/"),
        ("GET", f"/2013-04-01/hostedzone/{ZONE}/rrset"),
        ("GET", "/2013-04-01/change/C123"),
        ("POST", f"/2013-04-01/tags/hostedzone/{ZONE}"),
        ("GET", f"/2013-04-01/tags/hostedzone/{ZONE}"),
    ]
    from odin.gateway.classify import _ROUTE53_ROUTES

    for method, path in examples:
        hits = [op for m, pattern, op in _ROUTE53_ROUTES if m == method and pattern.match(path)]
        assert len(hits) == 1, f"{method} {path} matched {hits}"


def test_create_hosted_zone_classifies_to_the_zone_in_its_body(sink, route53):
    """The one route whose path carries no zone."""
    req = sink.call(lambda: route53.create_hosted_zone(Name=f"{ZONE}.", CallerReference="cr-1"))
    assert _classify(req) == ("route53:CreateHostedZone", ZONE)


def test_get_change_does_not_report_a_change_id_as_a_zone(sink, route53):
    """A policy written against a zone must not accidentally match a GetChange
    for some other zone's change."""
    req = sink.call(lambda: route53.get_change(Id="C0123456789ABCD"))
    action, resource = _classify(req)
    assert action == "route53:GetChange"
    assert resource == "C0123456789ABCD"


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        (ZONE, ZONE),
        (f"{ZONE}.", ZONE),
        (f"/hostedzone/{ZONE}", ZONE),
        (f"/hostedzone/{ZONE}.", ZONE),
        (f"/hostedzone/{ZONE}/", ZONE),
        ("Z1D633PJN98FT9", "Z1D633PJN98FT9"),
    ],
)
def test_classify_and_model_agree_on_every_zone_id_spelling(spelling, expected):
    """`classify._route53_zone` is a deliberate DUPLICATE of
    `route53ctl.zone_id` (classify.py imports nothing from odin, and keeping it
    a leaf is worth more than sharing three lines). Duplication drifts, and
    drift here is silent: if create stores one spelling while classify reports
    another, an IAM edge to that zone denies with no explanation. So the
    agreement is a test rather than a comment."""
    assert route53ctl.zone_id(spelling) == expected
    assert _route53_zone(spelling) == expected


# --- the canvas label -------------------------------------------------------


async def test_the_canvas_label_arrives_on_a_separate_tag_call_and_is_stored(stores, sink, route53):
    """Deviation 2, and the contract `reconcile/tf_status.py` reads.

    A hosted zone's tags are NOT an argument on create -- MEASURED:
    `CreateHostedZone`'s input shape has no tag member at all. They arrive on a
    separate `ChangeTagsForResource`, so this asserts the exact store key and
    shape the World projection has to look under."""
    await _create_zone(stores, sink, route53)
    assert stores.tags.get(ENV, f"route53:{ZONE}") is None, "no tags before the tag call"
    await _answer(stores, sink.call(lambda: route53.change_tags_for_resource(
        ResourceType="hostedzone", ResourceId=ZONE,
        AddTags=[{"Key": route53ctl.NODE_TAG, "Value": LABEL}, {"Key": "env", "Value": "dev"}],
    )))
    assert stores.tags.get(ENV, f"route53:{ZONE}") == {route53ctl.NODE_TAG: LABEL, "env": "dev"}


async def test_list_tags_for_resource_reads_the_label_back(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    await _answer(stores, sink.call(lambda: route53.change_tags_for_resource(
        ResourceType="hostedzone", ResourceId=ZONE,
        AddTags=[{"Key": route53ctl.NODE_TAG, "Value": LABEL}],
    )))
    parsed = _parse("ListTagsForResource", await _answer(
        stores, sink.call(lambda: route53.list_tags_for_resource(ResourceType="hostedzone", ResourceId=ZONE))
    ))
    tag_set = parsed["ResourceTagSet"]
    assert tag_set["ResourceId"] == ZONE
    assert tag_set["ResourceType"] == "hostedzone"
    assert {t["Key"]: t["Value"] for t in tag_set["Tags"]} == {route53ctl.NODE_TAG: LABEL}


async def test_removing_a_tag_key_removes_only_that_key(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    await _answer(stores, sink.call(lambda: route53.change_tags_for_resource(
        ResourceType="hostedzone", ResourceId=ZONE,
        AddTags=[{"Key": route53ctl.NODE_TAG, "Value": LABEL}, {"Key": "env", "Value": "dev"}],
    )))
    await _answer(stores, sink.call(lambda: route53.change_tags_for_resource(
        ResourceType="hostedzone", ResourceId=ZONE, RemoveTagKeys=["env"],
    )))
    assert stores.tags.get(ENV, f"route53:{ZONE}") == {route53ctl.NODE_TAG: LABEL}


async def test_tagging_a_zone_that_does_not_exist_is_refused(stores, sink, route53):
    parsed = _parse("ChangeTagsForResource", await _answer(
        stores, sink.call(lambda: route53.change_tags_for_resource(
            ResourceType="hostedzone", ResourceId="nope.internal",
            AddTags=[{"Key": route53ctl.NODE_TAG, "Value": LABEL}],
        ))
    ), error=True)
    assert parsed["Error"]["Code"] == "NoSuchHostedZone"


# --- persistence ------------------------------------------------------------


async def test_the_store_survives_a_reload_and_validates(stores, sink, route53, tmp_path):
    """The records the model writes must be readable by `records.validate` --
    a shape this module writes but that file rejects would make the whole env
    unreadable on the NEXT process, which no in-memory test would catch."""
    await _create_zone(stores, sink, route53)
    await _change(stores, sink, route53, [
        {"Action": "CREATE", "ResourceRecordSet": _a_record(f"api.{ZONE}")},
    ])
    await _answer(stores, sink.call(lambda: route53.change_tags_for_resource(
        ResourceType="hostedzone", ResourceId=ZONE,
        AddTags=[{"Key": route53ctl.NODE_TAG, "Value": LABEL}],
    )))
    assert (tmp_path / ENV / "gateway" / "route53ctl.json").exists()
    reloaded = SynthStores(tmp_path)
    zone = reloaded.route53ctl.get(ENV, f"zone:{ZONE}")
    assert zone["zone_id"] == ZONE
    assert zone["private_zone"] is False
    records = reloaded.route53ctl.get(ENV, f"rrset:{ZONE}")
    assert isinstance(records, list)
    assert {r["type"] for r in records} == {"SOA", "NS", "A"}
    assert reloaded.tags.get(ENV, f"route53:{ZONE}") == {route53ctl.NODE_TAG: LABEL}


async def test_forget_env_sweeps_the_route53_store(stores, sink, route53):
    """`SynthStores.forget_env` derives its list from the attributes, so a new
    store needs no edit there -- asserted rather than assumed."""
    await _create_zone(stores, sink, route53)
    assert "route53ctl" in stores.forget_env(ENV)


async def test_recreating_the_same_zone_is_refused_rather_than_orphaning_records(stores, sink, route53):
    await _create_zone(stores, sink, route53)
    await _change(stores, sink, route53, [
        {"Action": "CREATE", "ResourceRecordSet": _a_record(f"api.{ZONE}")},
    ])
    parsed = _parse("CreateHostedZone", await _create_zone(stores, sink, route53), error=True)
    assert parsed["Error"]["Code"] == "HostedZoneAlreadyExists"
    listed = _parse("ListResourceRecordSets", await _answer(
        stores, sink.call(lambda: route53.list_resource_record_sets(HostedZoneId=ZONE))
    ))
    assert any(r["Name"] == f"api.{ZONE}." for r in listed["ResourceRecordSets"])
