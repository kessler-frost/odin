"""The gateway's Route 53 model -- and the ONE thing to know before reading the
API surface, because the obvious misreading of this module is dangerous.

**ODIN STILL SERVES NO DNS.** Nothing speaks the DNS protocol on any port, and
this module is a CONTROL PLANE: a hosted zone and its record sets are stored,
round-trip for terraform, and are enforceable targets for an IAM edge.

What a record now DOES do is resolve as an `/etc/hosts` entry, in exactly one
substrate. `gateway/route53_hosts.py` reads `rrset:{zone_id}` out of this store
and `server.py` calls it on every Apply (after the mesh pass -- a VM reaches
another VM by its NEBULA OVERLAY address, and that only exists once the mesh
pass has allocated it). So, VERIFIED against those call sites rather than
assumed:

  * A drawn name resolves inside a **RUNNING Lima VM odin manages**, and
    nowhere else. Not from the macOS host, not from any process odin did not
    launch. `dig` and `nslookup` will not find an odin record; `getent hosts`
    inside such a VM will.
  * A **NOT-running** instance is skipped entirely -- there is no guest to write
    to. It gets its records at boot instead, via `generate_cloud_init`.
  * A record whose target has no reachable address is **withheld from the guest
    and reported to World** as a non-healthy verdict, never written silently.
    That direction is deliberate: "the mesh gate withheld facts that never
    reached World" is a defect this repo already paid for once.
  * **The CONTAINER half is NOT wired.** `compute/hosts.py::container_hosts` is
    built and unit-tested and is called by NOTHING in `src/` -- verified, not
    inferred: its only non-docstring reference outside its own definition is
    `tests/test_compute/test_hosts.py`. Containers odin launches get no
    `--add-host` from a route53 record today.

That last clause is the one to keep explicit. "route53 works" heard as covering
both substrates is the claim that would have to be retracted, and hosts-file
injection is indistinguishable from DNS to a user until they try `dig` from the
Mac or reach for a container.

(This paragraph carried a KNOWN EXPIRY while the resolution layer was unwired,
and the expiry has now FIRED for the VM half. It is still armed for the
container half: when `container_hosts` gains a caller, the fourth bullet above
is what has to change, in that same commit.)

That stated, the module is real in the way that matters for the gateway's job:
without it a single `aws_route53_zone` in a generated project fails the user's
ENTIRE `tofu apply`, because `app.py` denies an unclassifiable service with
`errors.access_denied(service, "unmappable-action")` and
`simulate/runner.py` points ONE `AWS_ENDPOINT_URL` at this gateway for every
service at once. Every other resource in the plan dies with it.

WIRE SHAPE, MEASURED against botocore's own `route53` service model and against
real captured request bytes rather than assumed:

  protocol       rest-xml          (NOT query, NOT json)
  endpointPrefix route53
  globalEndpoint route53.amazonaws.com
  signatureVersion v4

Route 53 is a GLOBAL service, and the SigV4 credential scope is worth stating
because odin assumes `us-east-1` in several places. MEASURED with a boto3
client configured `region_name="us-west-2"` and an `endpoint_url` override:

  Credential=AKIAODINTEST/20260803/us-west-2/route53/aws4_request

-- the scope region is whatever the CLIENT is configured with, NOT a forced
`us-east-1`, once `endpoint_url` is overridden (which is exactly odin's case).
`gateway/sigv4.py` re-derives the region from the header it is verifying rather
than comparing against a constant, so this needs no special handling; the
SERVICE name in the scope is the plain `route53`, which is what `app.py`
dispatches `classify()` on.

The ten operations, with the method + requestUri MEASURED from
`op.http` per operation:

  CreateHostedZone         POST   /2013-04-01/hostedzone
  GetHostedZone            GET    /2013-04-01/hostedzone/{Id}
  ListHostedZones          GET    /2013-04-01/hostedzone
  ListHostedZonesByName    GET    /2013-04-01/hostedzonesbyname
  DeleteHostedZone         DELETE /2013-04-01/hostedzone/{Id}
  ChangeResourceRecordSets POST   /2013-04-01/hostedzone/{Id}/rrset/
  ListResourceRecordSets   GET    /2013-04-01/hostedzone/{Id}/rrset
  GetChange                GET    /2013-04-01/change/{Id}
  ChangeTagsForResource    POST   /2013-04-01/tags/{ResourceType}/{ResourceId}
  ListTagsForResource      GET    /2013-04-01/tags/{ResourceType}/{ResourceId}

Note the two pairs that differ ONLY by method (`/hostedzone` create vs list,
`/tags/...` write vs read), which is why `classify.py`'s route table anchors
every pattern with `$`.

The trailing slash on `.../rrset/` in that table is REAL but not RELIABLE, and
the difference cost an apply before it was measured. boto3 sends the slash on
ChangeResourceRecordSets because botocore's `requestUri` has it; the terraform
provider (5.100.0, on aws-sdk-go-v2) sends `.../rrset` with no slash and got
`unmappable-action` -> a 403 that failed the record after the zone had already
been created. `classify.py` accepts both spellings now, and its own comment
carries the measurement. The lesson generalises past this module: a wire fact
captured from botocore is a fact about BOTO3, not about every SDK that will
call odin.

THE HANG RISK, AND WHAT WAS AND WAS NOT MEASURED ABOUT IT
---------------------------------------------------------
`GetChange` is the operation that can hang an apply rather than fail it, and a
hang is indistinguishable from "still working". The real provider waits for a
change to reach `INSYNC`; a model that answers `PENDING` forever makes
`tofu apply` spin.

MEASURED, in the shipped provider binary itself
(`~/.terraform.d/plugin-cache/registry.opentofu.org/hashicorp/aws/5.100.0/
darwin_arm64/terraform-provider-aws`, scanned for literals):

  "waiting for Route 53 Hosted Zone (%s) synchronize: %s"
  "waiting for Route 53 Record (%s) synchronize: %s"

-- so BOTH `aws_route53_zone` and `aws_route53_record` really do wait on change
synchronization, which answers the "what does this resource actually poll?"
question the ROADMAP's ebs lesson says to ask. `INSYNC`, `PENDING`,
`NoSuchChange` and `InvalidChangeBatch` are all present in the same binary as
route53 error/enum literals.

NOT MEASURED FROM THE BINARY: the exact refresh loop -- how many times it polls,
its interval, and whether it calls `GetChange` even when the create response
already says `INSYNC`. Literals in a stripped Go binary cannot show control
flow. That gap is closed a different way, by `tests/gateway/test_route53ctl.py`,
which drives the REAL handlers; and `GetChange` is answered so that the
question cannot matter:

**Every change is `INSYNC` from the moment it is created.** The create response
carries `INSYNC`, and `GetChange` answers `INSYNC` for any change it knows.
There is no `PENDING` state anywhere in this module and no clock involved --
deliberately, because odin has no propagation to wait for: the record is in the
store synchronously and readable the instant the call returns. A `PENDING` here
would model a delay that does not exist and would exist only to be waited on.
So the provider's waiter terminates on its FIRST refresh whatever its interval,
and a polling loop we did not measure cannot spin.

An UNKNOWN change id is `NoSuchChange`, not a fabricated `INSYNC`. Terraform
never persists a change id across applies -- it polls only ids this gateway
minted inside the same apply -- so refusing an unknown one cannot wedge a real
run, and inventing a status for a change that never existed would be this
repo's most-repeated bug in a new costume.

THREE DELIBERATE DEVIATIONS from real AWS, each stated rather than implied:

1. **The hosted zone Id IS the domain name**, not an opaque `Z1D633PJN98FT9`.
   This is kmsctl deviation 1 one service over, for the same reason: it lets
   `classify.py::_classify_route53` reduce the `{Id}` path segment to the thing
   an IAM policy is written against with NO store access, which is what makes an
   edge drawn to a `route53` node enforce for real. The trailing dot is stripped
   (`odin.internal.` -> `odin.internal`) so that create-time and path-time
   normalisation cannot disagree -- the failure mode kmsctl MEASURED, where a
   create stored one spelling and every lookup asked for another, giving a green
   create for a resource that was dead on arrival.
2. **The canvas label rides on a tag that arrives LATER.** Unlike every other
   primary, a hosted zone's tags are NOT an argument on create: they are a
   SEPARATE `ChangeTagsForResource` call (MEASURED -- `CreateHostedZone`'s input
   shape has no tag member at all, and the captured create body carries only
   Name/CallerReference/HostedZoneConfig). So `odin:node` is written by
   `_change_tags_for_resource` into the SHARED tag store under
   `"route53:{zone_id}"`, and `reconcile/tf_status.py` must read it there. See
   `NODE_TAG` below for the exact key and shape.
3. **DeleteHostedZone refuses a zone that still has records**, matching real
   AWS's `HostedZoneNotEmpty` and ignoring the auto-created SOA/NS pair the way
   real Route 53 does. Terraform's own dependency ordering deletes
   `aws_route53_record` before the zone, so a normal destroy is unaffected.

Not modeled, and each is a closed-world deny at `classify.py` (an unrecognised
(method, path) returns None, which `app.py` answers as `unmappable-action`)
rather than a silent 200: health checks, traffic policies, DNSSEC, query
logging, delegation sets as anything but a fixed fabrication, VPC association
for private zones, CIDR collections, and `ListTagsForResources` (plural -- the
BATCH read; the provider was not observed using it for a single zone, and it is
not added speculatively).
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from starlette.responses import Response

from odin.gateway import errors
from odin.gateway.stores import SynthStores

SERVICE = "route53"

# The XML namespace every Route 53 request and response carries -- MEASURED off
# the captured `CreateHostedZoneRequest` bytes, not remembered.
NS = "https://route53.amazonaws.com/doc/2013-04-01/"

# The tag `iac/hcl.py::_tags_block` stamps on every resource it emits. For a
# hosted zone it arrives on a SEPARATE `ChangeTagsForResource` call (deviation
# 2), and this module writes it to the SHARED `tags` store under the key
# `"route53:{zone_id}"` -- a flat `{tag_key: tag_value}` dict, the same shape
# and store every other service's tags use. That is the key
# `reconcile/tf_status.py` must read to project World status for a route53 node.
NODE_TAG = "odin:node"

# The record types this model will accept, MEASURED as botocore's own `RRType`
# enum for the route53 service model rather than hand-listed. Anything outside
# it is an `InvalidChangeBatch`, which is what real Route 53 answers -- storing
# an unknown type would round-trip a record no DNS vocabulary contains.
RR_TYPES = frozenset({
    "SOA", "A", "TXT", "NS", "CNAME", "MX", "NAPTR", "PTR", "SRV", "SPF",
    "AAAA", "CAA", "DS", "TLSA", "SSHFP", "SVCB", "HTTPS",
})

# MEASURED as botocore's `ChangeAction` enum.
CHANGE_ACTIONS = frozenset({"CREATE", "DELETE", "UPSERT"})

# The SOA/NS pair real CreateHostedZone mints for you, and the pair
# `HostedZoneNotEmpty` ignores when it decides whether a zone is empty.
AUTO_TYPES = frozenset({"SOA", "NS"})

# A fixed delegation set. Real Route 53 allocates four name servers per zone;
# odin fabricates a stable four so `aws_route53_zone.name_servers` and
# `primary_name_server` round-trip without drift. They resolve nothing -- see
# the module docstring.
#
# THE DELEGATION SET CARRIES NO `Id`, AND THAT IS MEASURED, NOT AN OMISSION.
# Real Route 53 returns `DelegationSet.Id` only for a zone created against a
# REUSABLE delegation set; an ordinary zone gets name servers and no id. The
# first cut of this module fabricated one, and a real `tofu plan` right after a
# clean `tofu apply` came back exit 2:
#
#     - delegation_set_id = "N-odin-delegation-set" -> null # forces replacement
#
# -- the config never sets `delegation_set_id`, so reading one back is drift,
# and because it FORCES REPLACEMENT it cascaded: the zone was replaced, its
# `zone_id` became unknown, and the record was replaced too. A zero-drift plan
# after an apply is the whole bar for a control-plane model, and one fabricated
# optional field missed it.
NAME_SERVERS = (
    "ns-1.odin-dns.internal",
    "ns-2.odin-dns.internal",
    "ns-3.odin-dns.internal",
    "ns-4.odin-dns.internal",
)

DEFAULT_SOA_TTL = 900
DEFAULT_NS_TTL = 172800
DEFAULT_MAX_ITEMS = 100


# --- identity --------------------------------------------------------------


def zone_id(value: str) -> str:
    """The bare zone id out of every form an `{Id}` arrives in -- kept in
    lock-step with `classify.py::_route53_zone`, which must reach the SAME
    answer without touching the store.

    `/hostedzone/odin.internal` -> `odin.internal`; a trailing dot is stripped
    (`odin.internal.` -> `odin.internal`). Both halves matter: the provider
    sends the id back with the `/hostedzone/` prefix it was given, and DNS names
    are legal with or without the root dot, so without one canonical form a
    create and its lookups disagree silently."""
    tail = value.rpartition("/hostedzone/")[2] or value
    return tail.strip("/").rstrip(".")


def fqdn(name: str) -> str:
    """A record/zone name in the trailing-dot form Route 53 stores and echoes."""
    return name.rstrip(".") + "."


# --- store keys ------------------------------------------------------------


def _zone_key(zid: str) -> str:
    return f"zone:{zid}"


def _rrset_key(zid: str) -> str:
    return f"rrset:{zid}"


def _change_key(change_id: str) -> str:
    return f"change:{change_id}"


def _tag_key(zid: str) -> str:
    return f"{SERVICE}:{zid}"


def _zone(stores: SynthStores, env: str, zid: str) -> dict | None:
    return stores.route53ctl.get(env, _zone_key(zid))


def _zones(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.route53ctl.items(env).items() if k.startswith("zone:")]


def _rrsets(stores: SynthStores, env: str, zid: str) -> list[dict]:
    return stores.route53ctl.get(env, _rrset_key(zid), [])


def _tags_for(stores: SynthStores, env: str, zid: str) -> dict[str, str]:
    return stores.tags.get(env, _tag_key(zid), {})


# --- wire building: rest-xml ------------------------------------------------


def _response(op: str, body_xml: str, headers: dict[str, str] | None = None) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{op}Response xmlns="{NS}">{body_xml}</{op}Response>'
    )
    return Response(xml, media_type="text/xml", headers=headers or {})


def _elem(tag: str, value: object) -> str:
    """One XML element, or "" when the value is unset -- real AWS OMITS an unset
    optional member, and the provider reads an empty element as a real value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return f"<{tag}>{'true' if value else 'false'}</{tag}>"
    return f"<{tag}>{escape(str(value))}</{tag}>"


def _wrap(tag: str, inner: str) -> str:
    return f"<{tag}>{inner}</{tag}>"


def _iso(seconds: float) -> str:
    """`SubmittedAt` is a `timestamp`, and rest-xml's default timestamp format
    is iso8601 -- so it goes out as one, with the `Z` botocore's parser needs to
    read it as UTC rather than as a naive local time."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _error(code: str, message: str, status: int = 400) -> Response:
    return errors.synth_error(SERVICE, code, message, status)


def _no_such_zone(zid: str) -> Response:
    return _error("NoSuchHostedZone", f"No hosted zone found with ID: {zid}", 404)


def _invalid_change_batch(message: str) -> Response:
    return _error("InvalidChangeBatch", message)


# --- record + zone rendering ------------------------------------------------


def _zone_xml(record: dict, count: int) -> str:
    config = _elem("Comment", record.get("comment")) + _elem("PrivateZone", record["private_zone"])
    return _wrap(
        "HostedZone",
        _elem("Id", f"/hostedzone/{record['zone_id']}")
        + _elem("Name", fqdn(record["name"]))
        + _elem("CallerReference", record["caller_reference"])
        + _wrap("Config", config)
        + _elem("ResourceRecordSetCount", count),
    )


def _delegation_set_xml() -> str:
    servers = "".join(_elem("NameServer", ns) for ns in NAME_SERVERS)
    return _wrap("DelegationSet", _wrap("NameServers", servers))


def _change_info_xml(change: dict) -> str:
    return _wrap(
        "ChangeInfo",
        _elem("Id", f"/change/{change['change_id']}")
        + _elem("Status", change["status"])
        + _elem("SubmittedAt", _iso(change["submitted_at"]))
        + _elem("Comment", change.get("comment")),
    )


def _alias_xml(alias: dict) -> str:
    return _wrap(
        "AliasTarget",
        _elem("HostedZoneId", alias.get("hosted_zone_id"))
        + _elem("DNSName", alias.get("dns_name"))
        + _elem("EvaluateTargetHealth", bool(alias.get("evaluate_target_health"))),
    )


def _rrset_xml(record: dict) -> str:
    """One `<ResourceRecordSet>`. An ALIAS record carries `AliasTarget` and NO
    TTL/ResourceRecords; every other kind carries the pair. `_elem` dropping a
    None is what keeps the two shapes from bleeding into each other."""
    values = record.get("values") or []
    resource_records = "".join(_wrap("ResourceRecord", _elem("Value", v)) for v in values)
    alias = record.get("alias")
    return _wrap(
        "ResourceRecordSet",
        _elem("Name", fqdn(record["name"]))
        + _elem("Type", record["type"])
        + _elem("SetIdentifier", record.get("set_identifier"))
        + _elem("TTL", record.get("ttl"))
        + (_wrap("ResourceRecords", resource_records) if resource_records else "")
        + (_alias_xml(alias) if alias else ""),
    )


# --- request parsing: rest-xml ---------------------------------------------


def _strip_ns(tag: str) -> str:
    return tag.rpartition("}")[2]


def _parse(body: bytes) -> dict:
    """The request body as a nested plain-dict tree, namespace-stripped.

    Repeated siblings become a LIST under their tag; a lone one stays a scalar
    or dict. That is enough for every shape this module reads
    (`ChangeBatch/Changes/Change`, `AddTags/Tag`, `ResourceRecords/
    ResourceRecord`) and it means no handler below ever touches ElementTree.

    A body that is absent or not XML parses to `{}` -- the handlers then answer
    the caller's real mistake (`a required member is missing`) instead of a 500
    from the parser, which is the same reason `kmsctl.pure_answer` tolerates an
    undecodable JSON body."""
    if not body:
        return {}
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return {}
    return _node(root)


def _node(element) -> dict | str:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    out: dict = {}
    for child in children:
        key = _strip_ns(child.tag)
        value = _node(child)
        existing = out.get(key)
        out[key] = value if key not in out else (
            [*existing, value] if isinstance(existing, list) else [existing, value]
        )
    return out


def _as_list(value: object) -> list:
    """A repeated XML member, whether it arrived once or many times."""
    return value if isinstance(value, list) else ([] if value in (None, "") else [value])


def _members(payload: dict, container: str, member: str) -> list:
    """The repeated children of a wrapper element, however many arrived.

    rest-xml wraps a list in a container tag whose children all share one name,
    and `_node` above collapses a lone child to a bare scalar/dict rather than a
    one-item list. Every list read in this module goes through here instead of
    re-deriving that, because getting it wrong in ONE place means a single-record
    ChangeBatch (which is what terraform always sends) silently reads as empty."""
    wrapper = payload.get(container)
    return _as_list(wrapper.get(member)) if isinstance(wrapper, dict) else []


def _tags_from(payload: dict, key: str) -> dict[str, str]:
    """Route 53 spells a tag `{"Key": ..., "Value": ...}` -- MEASURED off
    botocore's own `Tag` shape, and NOT the `{"TagKey": ..., "TagValue": ...}`
    spelling KMS uses. Reading the wrong one loses `odin:node`, which is the
    only carrier of the canvas label (deviation 2)."""
    return {
        e["Key"]: e.get("Value", "")
        for e in _members(payload, key, "Tag")
        if isinstance(e, dict) and e.get("Key")
    }


# --- handlers ---------------------------------------------------------------


def _record_change(stores: SynthStores, env: str, zid: str, comment: str | None) -> dict:
    """Mint an INSYNC change record. See the module docstring for why there is
    no PENDING state anywhere in this module."""
    change = {
        "change_id": f"C{uuid.uuid4().hex[:14].upper()}",
        "status": "INSYNC",
        "submitted_at": time.time(),
        "comment": comment or None,
        "zone_id": zid,
    }
    stores.route53ctl.set(env, _change_key(change["change_id"]), change)
    return change


def _create_hosted_zone(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    name = payload.get("Name")
    if not isinstance(name, str) or not name.strip():
        return _error("InvalidDomainName", "Name is required")
    zid = zone_id(name)
    wire_config = payload.get("HostedZoneConfig")
    # An EMPTY `<HostedZoneConfig/>` parses to the string `""`, not a dict --
    # normalise once here so neither read below has to guard separately.
    config = wire_config if isinstance(wire_config, dict) else {}
    caller_reference = payload.get("CallerReference") or uuid.uuid4().hex
    if _zone(stores, env, zid) is not None:
        # Real Route 53's own code for "this CallerReference already made this
        # zone". Re-creating would orphan the existing record sets behind a 200.
        return _error(
            "HostedZoneAlreadyExists",
            f"A hosted zone has already been created with the specified caller reference: {caller_reference}",
        )
    record = {
        "zone_id": zid,
        "name": zid,
        "caller_reference": caller_reference,
        "comment": config.get("Comment") or None,
        "private_zone": str(config.get("PrivateZone", "")).lower() == "true",
        "created_at": time.time(),
    }
    stores.route53ctl.set(env, _zone_key(zid), record)
    # The SOA/NS pair real CreateHostedZone mints. They are what make
    # `ResourceRecordSetCount` start at 2 rather than 0, and what
    # `HostedZoneNotEmpty` below has to ignore.
    stores.route53ctl.set(env, _rrset_key(zid), [
        {"name": zid, "type": "SOA", "ttl": DEFAULT_SOA_TTL, "set_identifier": None,
         "values": [f"{NAME_SERVERS[0]}. root.{zid}. 1 7200 900 1209600 86400"], "alias": None},
        {"name": zid, "type": "NS", "ttl": DEFAULT_NS_TTL, "set_identifier": None,
         "values": [f"{ns}." for ns in NAME_SERVERS], "alias": None},
    ])
    change = _record_change(stores, env, zid, "CreateHostedZone")
    body = _zone_xml(record, len(_rrsets(stores, env, zid))) + _change_info_xml(change) + _delegation_set_xml()
    # `Location` is a HEADER member on this operation's output shape (MEASURED),
    # not a body element.
    return _response("CreateHostedZone", body, {"Location": f"/2013-04-01/hostedzone/{zid}"})


def _get_hosted_zone(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    record = _zone(stores, env, resource)
    if record is None:
        return _no_such_zone(resource)
    body = _zone_xml(record, len(_rrsets(stores, env, resource))) + _delegation_set_xml()
    return _response("GetHostedZone", body)


def _hosted_zones_xml(stores: SynthStores, env: str, records: list[dict]) -> str:
    return _wrap("HostedZones", "".join(
        _zone_xml(r, len(_rrsets(stores, env, r["zone_id"]))) for r in records
    ))


def _list_hosted_zones(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    records = sorted(_zones(stores, env), key=lambda r: r["zone_id"])
    # No pagination state: `NextMarker` is never emitted, so a paginator stops
    # after this page -- kmsctl's `ListKeys` makes the same choice for the same
    # reason (an env's zones fit in one page by construction).
    body = (
        _hosted_zones_xml(stores, env, records)
        + _elem("IsTruncated", False)
        + _elem("MaxItems", DEFAULT_MAX_ITEMS)
    )
    return _response("ListHostedZones", body)


def _list_hosted_zones_by_name(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    records = sorted(_zones(stores, env), key=lambda r: r["zone_id"])
    body = (
        _hosted_zones_xml(stores, env, records)
        + _elem("IsTruncated", False)
        + _elem("MaxItems", DEFAULT_MAX_ITEMS)
    )
    return _response("ListHostedZonesByName", body)


def _delete_hosted_zone(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    """Deviation 3: REFUSES while non-SOA/NS records remain, exactly as real
    Route 53 does, and NAMES what is still standing (honesty rule 2).

    Terraform deletes `aws_route53_record` before the zone it depends on, so a
    normal destroy never reaches this branch. A record created outside the
    canvas does, and then the message says which one rather than leaving a
    delete that silently succeeded against a zone that still has contents."""
    record = _zone(stores, env, resource)
    if record is None:
        return _no_such_zone(resource)
    remaining = [r for r in _rrsets(stores, env, resource) if r["type"] not in AUTO_TYPES]
    if remaining:
        named = ", ".join(f"{fqdn(r['name'])} {r['type']}" for r in sorted(
            remaining, key=lambda r: (r["name"], r["type"])
        ))
        return _error(
            "HostedZoneNotEmpty",
            f"The hosted zone contains resource records that are not SOA or NS records and so "
            f"cannot be deleted: {named}. Delete those records first (removing them from the "
            f"canvas and re-applying does it), or destroy the whole environment, where the "
            f"dependency ordering removes them before the zone.",
        )
    stores.route53ctl.delete(env, _zone_key(resource))
    stores.route53ctl.delete(env, _rrset_key(resource))
    stores.tags.delete(env, _tag_key(resource))
    change = _record_change(stores, env, resource, "DeleteHostedZone")
    return _response("DeleteHostedZone", _change_info_xml(change))


def _identity(record: dict) -> tuple[str, str, str]:
    """What makes two record sets THE SAME record set, per Route 53: the name,
    the type, and the set identifier (which is what lets weighted/latency
    records share a name and type)."""
    return (fqdn(record["name"]), record["type"], record.get("set_identifier") or "")


def _rrset_from_wire(wire: dict) -> dict:
    values = [
        v.get("Value", "")
        for v in _members(wire, "ResourceRecords", "ResourceRecord")
        if isinstance(v, dict)
    ]
    alias_wire = wire.get("AliasTarget")
    alias = {
        "hosted_zone_id": alias_wire.get("HostedZoneId"),
        "dns_name": alias_wire.get("DNSName"),
        "evaluate_target_health": str(alias_wire.get("EvaluateTargetHealth", "")).lower() == "true",
    } if isinstance(alias_wire, dict) else None
    ttl = wire.get("TTL")
    return {
        "name": str(wire.get("Name") or "").rstrip("."),
        "type": str(wire.get("Type") or ""),
        "set_identifier": wire.get("SetIdentifier") or None,
        "ttl": int(ttl) if str(ttl).isdigit() else None,
        "values": values,
        "alias": alias,
    }


def _alias_conflict(record: dict) -> str | None:
    """The reason an ALIAS record set is malformed, or None.

    An alias record routes to another AWS resource, so it carries `AliasTarget`
    INSTEAD OF the `TTL`/`ResourceRecords` pair, never as well. MEASURED from
    AWS's own documentation, carried in botocore's `route53` service model:

        ResourceRecords  "If you're creating an alias resource record set,
                          omit ResourceRecords"
        TTL              "If you're creating or updating an alias resource
                          record set, omit TTL"

    Note TTL is in scope too, which is easy to miss when thinking of this as
    "AliasTarget vs ResourceRecords".

    NOT measured, and worth separating: that real Route 53 REJECTS the
    combination rather than ignoring the extra fields. The docs say "omit"; a
    teammate driving the real provider reports a rejection. Refusing is the
    honest choice either way -- storing a shape AWS documents as invalid makes
    odin round-trip a record real AWS would not have accepted, and `_rrset_from_wire`
    parses the two branches INDEPENDENTLY, so without this the store really can
    hold a record that is both at once and no reader can tell which field wins.

    NAMES THE FIELD TO REMOVE. "Invalid" alone leaves a user guessing that
    dropping `ResourceRecords` rather than `AliasTarget` is the fix."""
    if not record.get("alias"):
        return None
    offending = [
        name for name, present in
        (("ResourceRecords", bool(record.get("values"))), ("TTL", record.get("ttl") is not None))
        if present
    ]
    if not offending:
        return None
    return (
        f"[name='{fqdn(record['name'])}', type='{record['type']}'] is an alias resource record "
        f"set (it carries AliasTarget) and must not also carry "
        f"{' or '.join(offending)} -- remove {' and '.join(offending)}, or remove AliasTarget "
        f"if this was meant to be an ordinary record. Route 53 takes the TTL and the target from "
        f"the aliased resource."
    )


def _apply_change(current: list[dict], action: str, incoming: dict) -> tuple[list[dict], str | None]:
    """`current` with one change applied, or the reason it cannot be.

    DELETE is the branch worth stating: real Route 53 refuses a DELETE for a
    record set that is not there, and so does this -- a delete that silently
    succeeded against a record that never existed is the "reports success it did
    not achieve" shape, and it would also let a destroy report a clean teardown
    of something still standing."""
    key = _identity(incoming)
    kept = [r for r in current if _identity(r) != key]
    if action == "DELETE":
        if len(kept) == len(current):
            return current, (
                f"Tried to delete resource record set [name='{fqdn(incoming['name'])}', "
                f"type='{incoming['type']}'] but it was not found"
            )
        return kept, None
    if action == "CREATE" and len(kept) != len(current):
        return current, (
            f"Tried to create resource record set [name='{fqdn(incoming['name'])}', "
            f"type='{incoming['type']}'] but it already exists"
        )
    return [*kept, incoming], None


def _change_resource_record_sets(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    zone = _zone(stores, env, resource)
    if zone is None:
        return _no_such_zone(resource)
    batch = payload.get("ChangeBatch")
    if not isinstance(batch, dict):
        return _invalid_change_batch("ChangeBatch is required")
    changes = _members(batch, "Changes", "Change")
    if not changes:
        return _invalid_change_batch("ChangeBatch must contain at least one Change")
    working = list(_rrsets(stores, env, resource))
    for entry in changes:
        action = str((entry or {}).get("Action") or "")
        if action not in CHANGE_ACTIONS:
            return _invalid_change_batch(
                f"Invalid Action: '{action}' -- must be one of {sorted(CHANGE_ACTIONS)}"
            )
        wire = (entry or {}).get("ResourceRecordSet")
        if not isinstance(wire, dict):
            return _invalid_change_batch("Every Change requires a ResourceRecordSet")
        incoming = _rrset_from_wire(wire)
        if incoming["type"] not in RR_TYPES:
            # The guard the module docstring's RR_TYPES note exists for: an
            # unknown type stored here would round-trip a record no DNS
            # vocabulary contains, behind a 200.
            return _invalid_change_batch(
                f"Invalid Resource Record Type: '{incoming['type']}' for "
                f"[name='{fqdn(incoming['name'])}']"
            )
        if not incoming["name"]:
            return _invalid_change_batch("Every ResourceRecordSet requires a Name")
        conflict = _alias_conflict(incoming)
        if conflict is not None:
            return _invalid_change_batch(conflict)
        working, reason = _apply_change(working, action, incoming)
        if reason is not None:
            return _invalid_change_batch(reason)
    stores.route53ctl.set(env, _rrset_key(resource), working)
    change = _record_change(stores, env, resource, batch.get("Comment") or None)
    return _response("ChangeResourceRecordSets", _change_info_xml(change))


def _list_resource_record_sets(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    if _zone(stores, env, resource) is None:
        return _no_such_zone(resource)
    records = sorted(_rrsets(stores, env, resource), key=lambda r: (r["name"], r["type"]))
    body = (
        _wrap("ResourceRecordSets", "".join(_rrset_xml(r) for r in records))
        + _elem("IsTruncated", False)
        + _elem("MaxItems", DEFAULT_MAX_ITEMS)
    )
    return _response("ListResourceRecordSets", body)


def _get_change(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    """Always `INSYNC` for a change this gateway minted; `NoSuchChange` for one
    it did not. The module docstring states what was and was not measured about
    the provider's waiter, and why neither answer can hang an apply."""
    change = stores.route53ctl.get(env, _change_key(resource))
    if change is None:
        return _error("NoSuchChange", f"Could not find resource with ID: {resource}", 404)
    return _response("GetChange", _change_info_xml(change))


def _change_tags_for_resource(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    """Where the canvas label actually lands (deviation 2). Writes the SHARED
    `tags` store under `"route53:{zone_id}"`, so `reconcile/tf_status.py` reads
    `stores.tags.get(env, f"route53:{zone_id}")[NODE_TAG]`.

    THE MERGE IS A UNION, AND THAT IS ONLY CORRECT BECAUSE THE PROVIDER SENDS
    `RemoveTagKeys`. A key in neither `AddTags` nor `RemoveTagKeys` is KEPT, so
    a provider that instead re-sent the whole desired set with the dropped key
    simply omitted would leave that key here forever. `docs/limits.md` carried
    that as an untested risk until it was measured, because `RemoveTagKeys` had
    only ever been driven from boto3 -- which proves the parser, not the
    integration.

    MEASURED on 2026-08-03, OpenTofu 1.12.3 / hashicorp/aws 5.100.0, a real
    `tofu apply` against this gateway. Created with two user tags, then `tier`
    deleted from the config and re-applied. The provider's SECOND
    `ChangeTagsForResource` was, in full and byte for byte:

        <ChangeTagsForResourceRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <RemoveTagKeys><Key>tier</Key></RemoveTagKeys>
        </ChangeTagsForResourceRequest>

    -- a removal and NOTHING else: no `AddTags` element at all, so the union is
    the right shape and a replacement would have been the wrong one. The store
    went `{tier, team}` -> `{team}`, and the `tofu plan -detailed-exitcode`
    that followed returned **0**, which is the independent half: the provider
    read `ListTagsForResource` back and agreed, so nothing lingered.
    Pinned by `tests/simulate/test_route53_tags_tf_e2e.py`."""
    if _zone(stores, env, resource) is None:
        return _no_such_zone(resource)
    remove = {k for k in _members(payload, "RemoveTagKeys", "Key") if isinstance(k, str)}
    added = _tags_from(payload, "AddTags")

    def merge(current):
        kept = {k: v for k, v in (current or {}).items() if k not in remove}
        return {**kept, **added}

    stores.tags.update(env, _tag_key(resource), merge)
    return _response("ChangeTagsForResource", "")


def _list_tags_for_resource(payload: dict, resource: str, env: str, stores: SynthStores) -> Response:
    if _zone(stores, env, resource) is None:
        return _no_such_zone(resource)
    tags = _tags_for(stores, env, resource)
    body = _wrap(
        "ResourceTagSet",
        _elem("ResourceType", "hostedzone")
        + _elem("ResourceId", resource)
        + _wrap("Tags", "".join(
            _wrap("Tag", _elem("Key", k) + _elem("Value", v)) for k, v in sorted(tags.items())
        )),
    )
    return _response("ListTagsForResource", body)


# --- dispatch --------------------------------------------------------------

_Handler = Callable[[dict, str, str, SynthStores], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateHostedZone": _create_hosted_zone,
    "GetHostedZone": _get_hosted_zone,
    "ListHostedZones": _list_hosted_zones,
    "ListHostedZonesByName": _list_hosted_zones_by_name,
    "DeleteHostedZone": _delete_hosted_zone,
    "ChangeResourceRecordSets": _change_resource_record_sets,
    "ListResourceRecordSets": _list_resource_record_sets,
    "GetChange": _get_change,
    "ChangeTagsForResource": _change_tags_for_resource,
    "ListTagsForResource": _list_tags_for_resource,
}


async def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float
) -> Response:
    """The whole Route 53 answer -- same no-backing contract as iamctl/ecr/
    logsctl/secretsctl/ssmctl/kmsctl: an unmodeled action gets a
    protocol-correct error, never a 503 and never a silent forward.

    A coroutine that awaits nothing, like every other JSON/XML-sidecar model, and
    for the reason `synth.py` documents: while some models were coroutines and
    some were not, a branch that forgot its `await` returned a truthy COROUTINE
    OBJECT and the gateway answered the caller with it."""
    op = action.removeprefix(f"{SERVICE}:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return _error("InvalidAction", f"The action {op} is not valid.")
    return handler(_parse(body), resource, env, stores)
