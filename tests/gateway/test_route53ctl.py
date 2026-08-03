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

import ast
from pathlib import Path
from xml.etree import ElementTree

import botocore.session
import pytest
from botocore.awsrequest import HeadersDict
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import errors, synth
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


def _alias_change_body(zone: str, *, ttl: str = "", records: str = "") -> bytes:
    """A raw ChangeResourceRecordSets carrying AliasTarget plus whatever the
    caller asks for. Hand-built rather than boto3-built: boto3 will happily sign
    this combination, but building it through the client makes the test read as
    if the SDK endorsed the shape. The bytes are what the gateway must judge."""
    return (
        f'<ChangeResourceRecordSetsRequest xmlns="{route53ctl.NS}"><ChangeBatch><Changes><Change>'
        f"<Action>CREATE</Action><ResourceRecordSet><Name>cdn.{zone}.</Name><Type>A</Type>"
        f"{ttl}{records}"
        f"<AliasTarget><HostedZoneId>Z2FDTNDATAQYW2</HostedZoneId>"
        f"<DNSName>d111111.cloudfront.net.</DNSName>"
        f"<EvaluateTargetHealth>false</EvaluateTargetHealth></AliasTarget>"
        f"</ResourceRecordSet></Change></Changes></ChangeBatch></ChangeResourceRecordSetsRequest>"
    ).encode()


_RECORDS_XML = "<ResourceRecords><ResourceRecord><Value>10.0.0.5</Value></ResourceRecord></ResourceRecords>"


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [
        ({"records": _RECORDS_XML}, "ResourceRecords"),
        ({"ttl": "<TTL>60</TTL>"}, "TTL"),
        ({"ttl": "<TTL>60</TTL>", "records": _RECORDS_XML}, "ResourceRecords"),
    ],
)
async def test_an_alias_record_carrying_ttl_or_resource_records_is_refused(stores, sink, route53, kwargs, named):
    """An ALIAS record routes to another resource, so it carries `AliasTarget`
    INSTEAD OF the TTL/ResourceRecords pair. AWS's own docs (in botocore's
    service model) say to omit BOTH; `_rrset_from_wire` parses the two branches
    independently, so without this guard the store can hold a record that is
    both at once and no reader can say which field wins.

    The error must NAME the field to remove -- "invalid" alone leaves a user
    guessing whether to drop AliasTarget or the other half."""
    await _create_zone(stores, sink, route53)
    response = await synth.pure_answer(
        "route53:ChangeResourceRecordSets", ZONE, ENV,
        _alias_change_body(ZONE, **kwargs), stores, 0.0,
    )
    parsed = _parse("ChangeResourceRecordSets", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidChangeBatch"
    assert named in parsed["Error"]["Message"], parsed["Error"]["Message"]
    assert f"cdn.{ZONE}." in parsed["Error"]["Message"]
    # ...and nothing was written.
    listed = _parse("ListResourceRecordSets", await _answer(
        stores, sink.call(lambda: route53.list_resource_record_sets(HostedZoneId=ZONE))
    ))
    assert [r for r in listed["ResourceRecordSets"] if r["Name"] == f"cdn.{ZONE}."] == []


async def test_a_well_formed_alias_record_is_still_accepted(stores, sink, route53):
    """The other direction, so the guard cannot be 'reject every alias'. A
    mutation that refuses all aliases must kill THIS test while the ones above
    stay green -- an inference guard pinned in both directions."""
    await _create_zone(stores, sink, route53)
    _parse("ChangeResourceRecordSets", await synth.pure_answer(
        "route53:ChangeResourceRecordSets", ZONE, ENV, _alias_change_body(ZONE), stores, 0.0,
    ))
    listed = _parse("ListResourceRecordSets", await _answer(
        stores, sink.call(lambda: route53.list_resource_record_sets(HostedZoneId=ZONE))
    ))
    alias = next(r for r in listed["ResourceRecordSets"] if r["Name"] == f"cdn.{ZONE}.")
    assert alias["AliasTarget"]["DNSName"] == "d111111.cloudfront.net."
    assert "TTL" not in alias
    assert "ResourceRecords" not in alias


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


@pytest.mark.parametrize("build", [
    lambda: errors.access_denied("route53", "route53:GetHostedZone", ZONE),
    lambda: errors.access_denied("route53", "unmappable-action"),
    lambda: errors.service_unavailable("route53"),
    lambda: errors.internal_failure("route53", "RuntimeError"),
    lambda: errors.auth_error("route53", "SignatureDoesNotMatch", "nope"),
    lambda: errors.synth_error("route53", "NoSuchHostedZone", "gone", 404),
])
def test_every_route53_error_is_xml_and_carries_odins_own_code(build):
    """route53 is rest-xml, and `errors.py` had no branch for it -- so every
    deny and every 5xx on this service fell through to the AWS-JSON body at the
    bottom of `_respond`/`synth_error`.

    MEASURED through the parser botocore really picks for route53
    (`RestXMLParser`), that body reads as `{'Code': '404', 'Message': 'Not
    Found'}` -- odin's own code and message gone. A teammate measured the same
    bug from the other side, feeding those bytes to a real `tofu apply` and
    getting `api error UnknownError: UnknownError`.

    This pins BOTH halves so the fix cannot silently regress: the response is
    XML, and the code that comes back out is the one odin put in. It covers
    every constructor in `errors.py`, not just `synth_error`, because the
    unmappable-action deny is the one a user hits first."""
    response = build()
    assert response.media_type in ("text/xml", "application/xml"), response.media_type
    assert response.body.lstrip().startswith(b"<?xml"), response.body[:80]
    model = _SESSION.get_service_model("route53")
    parser = create_parser(model.protocol)
    parsed = parser.parse(
        {"status_code": response.status_code, "headers": HeadersDict(dict(response.headers)),
         "body": response.body},
        model.operation_model("GetHostedZone").output_shape,
    )
    assert "Error" in parsed, f"no Error document in {response.body!r}"
    assert parsed["Error"]["Code"] not in ("404", "403", "500", "503", "UnknownError"), (
        f"odin's own error code was lost -- botocore read {parsed['Error']!r}"
    )


@pytest.mark.parametrize("build", [
    lambda: errors.access_denied("route53", "route53:GetHostedZone", ZONE),
    lambda: errors.access_denied("route53", "unmappable-action"),
    lambda: errors.service_unavailable("route53"),
    lambda: errors.internal_failure("route53", "RuntimeError"),
    lambda: errors.auth_error("route53", "SignatureDoesNotMatch", "nope"),
    lambda: errors.synth_error("route53", "NoSuchHostedZone", "gone", 404),
])
def test_route53_errors_use_the_ErrorResponse_envelope_not_s3s(build):
    """THE ROOT ELEMENT, asserted structurally -- and this test exists because
    every OTHER assertion in this file is blind to what it checks.

    botocore's `RestXMLParser` reads `<ErrorResponse><Error>` and S3's bare
    `<Error>` IDENTICALLY: both give `Code='AccessDenied'` with odin's message.
    So the test above, and every `_parse(..., error=True)` here, passes either
    way. MEASURED, not assumed: routing route53 through the `_s3_xml` branch was
    mutation-tested against the whole gateway suite and **1286 tests passed**.

    The two are not interchangeable. The terraform provider is aws-sdk-go-v2 and
    is the only principal that ever reaches route53, and across six real
    `tofu apply` runs it read the bare form as `UnknownError: UnknownError`
    while reading `<ErrorResponse>` as `AccessDenied` with odin's real message.
    For route53 the s3 envelope destroys odin's error exactly as thoroughly as
    the AWS-JSON fallthrough does -- it is just invisible from python.

    Hence a structural assertion on the wrapper rather than a parsed-code one:
    the code survives the mutation, the wrapper does not."""
    body = build().body
    root = ElementTree.fromstring(body)
    assert root.tag == "ErrorResponse", (
        f"route53 errors must use the <ErrorResponse> envelope -- aws-sdk-go-v2 reads "
        f"<{root.tag}> as 'UnknownError: UnknownError'. Got: {body[:120]!r}"
    )
    assert root.find("Error") is not None, f"no nested <Error> in {body[:120]!r}"
    assert root.find("Error/Code") is not None
    assert root.find("Error/Message") is not None


def test_route53_is_registered_as_an_xml_envelope_service():
    """The one-line registration the envelope above depends on, pinned directly.

    Both teammates who measured this asked for it by name. It is deliberately
    redundant with the structural test above -- that one proves the BYTES are
    right, this one names the mechanism, so a reader who breaks the constant
    gets a failure that says which line to look at rather than an XML diff."""
    assert "route53" in errors._QUERY_XML_SERVICES


def test_the_container_half_of_hosts_resolution_is_still_unwired():
    """A RATCHET on a docstring claim, because prose cannot fail a build.

    `route53ctl.py`'s module docstring states that the CONTAINER half of hosts
    resolution is not wired -- `compute/hosts.py::container_hosts` exists, is
    unit-tested, and is called by nothing. That sentence is true when written
    and is exactly the kind that decays silently: someone wires it, the docstring
    keeps saying it did not, and a reader designs around a limit that is gone.

    MATCHES THE AST, NOT THE SOURCE TEXT, and the first cut of this test did the
    latter -- an allowlist of files whose TEXT contains `container_hosts`. A
    teammate's `tf_status.py` guard hit the general form of that mistake and
    named it: *a text grep cannot prove what code DOES, and the better a module
    explains its own seam, the more likely its prose defeats the check.* The
    incentive is perverse -- the modules most worth trusting are the ones whose
    comments most reliably fool a grep.

    It bites HERE in a specific way. This very file's subject, `route53ctl.py`,
    contains `container_hosts` in a DOCSTRING and nothing else; the text version
    had to carve it into an allowlist to stay green. Any third module that so
    much as MENTIONS the name in a comment would then fail this test -- a false
    alarm, whose obvious fix is to widen the allowlist, which is exactly how a
    real caller gets waved through. An `ast.Call`/`ast.ImportFrom` cannot be
    satisfied by prose, so the allowlist disappears and with it the pressure to
    grow one.

    MEASURED at the time of writing: `container_hosts` is DEFINED in
    `compute/hosts.py`, imported in 0 files, called in 0 files, and mentioned as
    text-only in `gateway/models/route53ctl.py` (this claim's own docstring)."""
    src = Path(__file__).resolve().parents[2] / "src"
    wired: dict[str, str] = {}
    for path in src.rglob("*.py"):
        source = path.read_text()
        if "container_hosts" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "container_hosts" for alias in node.names
            ):
                wired[str(path.relative_to(src))] = "imports it"
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "container_hosts")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "container_hosts")
            ):
                wired[str(path.relative_to(src))] = "calls it"
    assert wired == {}, (
        f"`container_hosts` is now wired: {wired}. The container half of hosts resolution is "
        f"LIVE, so the fourth bullet of route53ctl.py's module docstring -- which says "
        f"containers get no records from a route53 record -- is false. Update it in THIS "
        f"commit, then delete this test."
    )


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
        # Paths that must not be swallowed by a neighbouring pattern.
        ("GET", "/2013-04-01/hostedzone/a/b"),
        ("GET", "/2013-04-01/healthcheck"),
        ("GET", f"/2013-04-01/tags/healthcheck/{ZONE}"),
        ("PUT", f"/2013-04-01/hostedzone/{ZONE}"),
        ("GET", "/2013-04-01/change"),
        ("POST", f"/2013-04-01/hostedzone/{ZONE}"),
        # THE `$` ANCHOR ITSELF, and these cases exist because a mutation test
        # found the gap: dropping `/?$` from the two rrset patterns left them
        # PREFIX-matching and every test above still passed. An unanchored write
        # route classifies an unmodeled path as ChangeResourceRecordSets, which
        # is an unknown request authorized as a known write rather than refused.
        ("POST", f"/2013-04-01/hostedzone/{ZONE}/rrsets"),
        ("GET", f"/2013-04-01/hostedzone/{ZONE}/rrsets"),
        ("POST", f"/2013-04-01/hostedzone/{ZONE}/rrset/extra"),
        ("GET", f"/2013-04-01/hostedzone/{ZONE}/rrset/extra"),
        ("POST", f"/2013-04-01/hostedzone/{ZONE}/rrset//"),
    ],
)
def test_a_path_no_route_owns_is_unmappable(method, path):
    assert classify("route53", method, path, {}, {}, b"") is None


@pytest.mark.parametrize("path", [
    f"/2013-04-01/hostedzone/{ZONE}/rrset/",   # what boto3 sends
    f"/2013-04-01/hostedzone/{ZONE}/rrset",    # what the terraform provider sends
])
def test_both_rrset_spellings_reach_the_write_route(path):
    """A REGRESSION TEST for a bug the real provider found and boto3 could not.

    botocore's `requestUri` for ChangeResourceRecordSets carries a trailing
    slash, so a route table built from captured boto3 bytes requires one. The
    terraform provider is aws-sdk-go-v2 and sends no slash: measured against a
    real `tofu apply`, that path classified as UNMAPPABLE and the record failed
    with `403 AccessDenied: unmappable-action` AFTER the zone had been created.
    Both spellings must reach the WRITE route -- and note the pair below proves
    the widening did not turn a write into a read, which is the failure that
    would matter more."""
    classified = classify("route53", "POST", path, {}, {}, b"")
    assert classified is not None, f"POST {path} must classify -- the real provider sends it"
    assert classified[0] == "route53:ChangeResourceRecordSets"
    assert classified[1] == ZONE


@pytest.mark.parametrize("path", [
    f"/2013-04-01/hostedzone/{ZONE}/rrset/",
    f"/2013-04-01/hostedzone/{ZONE}/rrset",
])
def test_a_read_of_either_rrset_spelling_is_never_the_write_action(path):
    """The half of the slash-widening that is a POLICY property, not a
    convenience: METHOD is now the only thing separating the rrset write from
    the rrset read, so a GET must never classify as ChangeResourceRecordSets.
    Getting this wrong is a write authorized as a read, which is a hole rather
    than a 404."""
    classified = classify("route53", "GET", path, {}, {}, b"")
    assert classified is not None
    assert classified[0] == "route53:ListResourceRecordSets"


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
        ("POST", f"/2013-04-01/hostedzone/{ZONE}/rrset"),
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
