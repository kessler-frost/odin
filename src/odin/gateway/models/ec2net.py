"""The gateway's EC2-network model (task V1a): VPC / Subnet / Security Group,
built to the captured provider call surface in
docs/superpowers/research/research-coverage.md §2a and MiniStack's model
shapes (§2.6) -- adopted as designs, never as a dependency (NORTHSTAR
directive 5).

Unlike synth.py's other handlers (gap-fillers around a real goaws/dynalite/
RustFS backing), EC2 has NO backing container at all: this module is the
WHOLE answer for every `ec2:*` action the gateway classifies. Returning
`None` from `pure_answer` would fall through to `state.backing_port(env,
"ec2")` -- always unregistered -- and 503 the call, so even an unmodeled EC2
action gets a protocol-correct `InvalidAction` error (the shape research
§2b observed the TF provider tolerating from MiniStack) instead.

Model decisions, each traced to the research:
- CreateVpc auto-mints the 3 children the provider immediately reads back
  (finding #1): a default NACL id, a main route-table id, and a default
  security group -- answered via DescribeNetworkAcls/RouteTables/
  SecurityGroups filtered by vpc-id.
- DescribeVpcAttribute answers enableDnsSupport/enableDnsHostnames with
  defaults true/false (finding #2).
- CreateSecurityGroup seeds a default allow-all IPv4 egress rule (finding
  #3) -- the provider revokes it then authorizes its own config.
- Authorize/Revoke are idempotent by content: each individual permission
  (one protocol/port span + one range-or-group pairing) gets an `sgr-` id
  derived from sha1 over its identity fields (§2.6: "content-derived sgr-
  rule ids"), so re-authorizing identical content overwrites in place and
  revoke matches by recomputing the same hash. Description is deliberately
  NOT identity (matching real AWS, where a same-content/different-
  description authorize is a duplicate, not a new rule).
- Delete-confirm (finding #4): deletes are HARD deletes -- ids are random
  and never reused, so the post-delete Describe-by-id lands in the ordinary
  unknown-id path and returns the real EC2 error envelope
  (InvalidVpcID.NotFound / InvalidSubnetID.NotFound / InvalidGroup.NotFound).
  No SQS-style grace window: verified empirically by the tofu integration
  test (tests/simulate/test_ec2net_tf_e2e.py) -- the provider's destroy
  sweep tolerates an immediate 400, exactly as §2a's capture showed.
- Ids are random-hex of the AWS-correct prefix+length (`vpc-<17hex>` etc.)
  -- explicitly NOT MiniStack's counter-style default-VPC ids, which the
  research calls out as the one shape not to adopt.

Every response wire tag below was verified against botocore's own EC2
service model (`shape.serialization["name"]` -- e.g. Vpc.VpcId -> `vpcId`,
Tags -> `tagSet` of `item`s) and every response round-trips through
botocore's `EC2QueryParser` in tests/gateway/test_ec2net.py. Tags share
S1's `stores.tags` store, keyed `"ec2:{resource_id}"`.

Persistence: one `JsonStore` at `.odin/{env}/gateway/ec2net.json`
(`stores.ec2net`), flat keys `"vpc:{id}"` / `"subnet:{id}"` / `"sg:{id}"`.
The default NACL/route-table are not independent records -- their ids live
on the VPC record and their describes are synthesized from it (nothing in
V1 ever mutates them).

Nebula compile (task V1b, NORTHSTAR directive 6 -- "Nebula is the network
layer"): mutations push the env's desired network state through
`fabric/nebula.py`'s EXISTING primitives, never parallel structures.
- CreateVpc calls `ensure_network(stores.root, env, "127.0.0.1")` -- real
  CA/cert artifacts via nebula-cert plus overlay bookkeeping; no lighthouse
  PROCESS ever starts in V1, and "127.0.0.1" is a deliberate placeholder
  underlay until a real multi-Mac underlay exists. odin's mesh is per-ENV
  and V1 canvases carry one VPC per env, so the VPC -> Nebula-network
  mapping is 1:1 for now (`nebula_network` == env on the VPC record);
  multi-VPC-per-env topology is deliberately NOT modeled yet.
- Every SG mutation (create / authorize / revoke) recompiles the group's
  ingress rule set through `sg_rules_to_firewall` -- research finding #3:
  the IpPermissions shape IS that function's input, no adapter -- and
  stores the resulting `FirewallRules` dump on the SG record itself
  (`sg["firewall"]`), so it dies with the group and `fabric.nebula
  .mesh_state` can read it straight off this module's sidecar file. The
  artifacts are exactly what a Nebula node config consumes at V3
  (golden-tested through `NebulaManager.generate_config`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
from collections.abc import Callable
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.fabric.models import FirewallRules
from odin.fabric.nebula import LighthouseManager, ensure_network, sg_rules_to_firewall
from odin.gateway import errors
from odin.gateway.stores import SynthStores

log = logging.getLogger("odin.gateway.ec2net")

_EC2_NS = "http://ec2.amazonaws.com/doc/2016-11-15/"
_REQUEST_ID = "00000000-0000-0000-0000-000000000000"

# Research finding #2: DescribeVpcAttribute defaults. The attribute name on
# the wire doubles as the response element tag (verified via botocore:
# DescribeVpcAttributeResult members serialize under these exact names).
_VPC_ATTRIBUTE_DEFAULTS = {
    "enableDnsSupport": "true",
    "enableDnsHostnames": "false",
    "enableNetworkAddressUsageMetrics": "false",
}

# Research finding #4: the REAL per-kind EC2 NotFound codes (real AWS's
# casing -- `InvalidVpcID` with capital ID is the genuine article).
_NOT_FOUND = {
    "vpc": "InvalidVpcID.NotFound",
    "subnet": "InvalidSubnetID.NotFound",
    "sg": "InvalidGroup.NotFound",
}

# "i"/"vol"/"key" (V3): the compute model's instance/volume/key-pair id
# prefixes -- `_describe_tags` below is resource-id-agnostic (scans every
# "ec2:{id}" tag key regardless of which module created it), so ec2compute.py
# never needs its own DescribeTags/CreateTags/DeleteTags handlers; this table
# just needs their prefixes to report the right `resourceType`.
_RESOURCE_TYPES = {
    "vpc": "vpc", "subnet": "subnet", "sg": "security-group",
    "i": "instance", "vol": "volume", "key": "key-pair",
}


def _mint(prefix: str) -> str:
    """A random-hex id of the AWS-observed shape: `prefix-<17 hex chars>`."""
    return f"{prefix}-{secrets.token_hex(9)[:17]}"


# --- request parsing: EC2 query-protocol serialization ----------------------
# EC2 lists are `Prefix.N[...]` (no `.member.` -- distinct from SNS's shape
# that synth.py's _parse_struct_list handles), nested to two levels for
# IpPermissions: `IpPermissions.N.IpRanges.M.CidrIp`. All shapes below were
# captured from real boto3-signed requests (tests/gateway/conftest.py's ec2
# fixture does the same live).


def _params(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _indexed(params: dict[str, str], prefix: str) -> list[dict[str, str]]:
    """`Prefix.N.Rest -> value` params -> an N-ordered list of `{Rest: value}`
    dicts. A scalar entry (`Prefix.N = value`) lands under the `""` key, and
    `Rest` keeps its own dots -- so one level of this parser composes for the
    doubly-nested IpPermissions shape."""
    grouped: dict[int, dict[str, str]] = {}
    for key, value in params.items():
        if not key.startswith(f"{prefix}."):
            continue
        index, _, rest = key[len(prefix) + 1:].partition(".")
        if index.isdigit():
            grouped.setdefault(int(index), {})[rest] = value
    return [grouped[i] for i in sorted(grouped)]


def _scalars(params: dict[str, str], prefix: str) -> list[str]:
    """`Prefix.N = value` -> [value, ...] (e.g. `VpcId.1`, `ResourceId.2`)."""
    return [item[""] for item in _indexed(params, prefix) if "" in item]


def _filters(params: dict[str, str]) -> dict[str, list[str]]:
    """`Filter.N.Name` + `Filter.N.Value.M` -> {name: [values]}."""
    return {f["Name"]: _scalars(f, "Value") for f in _indexed(params, "Filter") if "Name" in f}


def _matches(filters: dict[str, list[str]], attrs: dict[str, str]) -> bool:
    """Closed-world filter match: every requested filter name must be a known
    attr of the item and its value in the requested set."""
    return all(attrs.get(name) in values for name, values in filters.items())


def _spec_tags(params: dict[str, str]) -> dict[str, str]:
    """`TagSpecification.N.Tag.M.{Key,Value}` -> flat tags (the create-time
    tagging shape; the ResourceType member always names the resource being
    created, so all specs merge)."""
    return {
        tag["Key"]: tag.get("Value", "")
        for spec in _indexed(params, "TagSpecification")
        for tag in _indexed(spec, "Tag")
    }


def _permissions(params: dict[str, str]) -> list[dict]:
    """`IpPermissions.N.*` -> the canonical IpPermissions dict shape (the
    same shape `fabric.nebula.sg_rules_to_firewall` consumes -- research
    finding #3: "an exact match, no new translation needed"). On the wire
    UserIdGroupPairs serializes as `Groups.M.*` (botocore: the member's
    query name is `Groups`)."""
    perms = []
    for perm in _indexed(params, "IpPermissions"):
        perms.append({
            "IpProtocol": perm.get("IpProtocol", "-1"),
            "FromPort": int(perm["FromPort"]) if "FromPort" in perm else None,
            "ToPort": int(perm["ToPort"]) if "ToPort" in perm else None,
            "IpRanges": _indexed(perm, "IpRanges"),
            "Ipv6Ranges": _indexed(perm, "Ipv6Ranges"),
            "UserIdGroupPairs": _indexed(perm, "Groups"),
        })
    return perms


# --- the rule set: content-derived sgr- ids ----------------------------------


def _rule_id(group_id: str, rule: dict) -> str:
    """`sgr-<17hex>` = sha1 over the rule's identity fields (group, side,
    protocol, port span, and exactly one range/group pairing). Description is
    NOT identity -- see module docstring."""
    content = json.dumps([
        group_id, rule["is_egress"], rule["ip_protocol"], rule["from_port"],
        rule["to_port"], rule["cidr_ipv4"], rule["cidr_ipv6"], rule["referenced_group_id"],
    ])
    return f"sgr-{hashlib.sha1(content.encode()).hexdigest()[:17]}"


def _rules_from_permission(perm: dict, is_egress: bool) -> list[dict]:
    """One stored rule per range/group pairing -- AWS's own normalization
    (each pairing gets its own sgr- id). For protocol "-1" (all traffic)
    ports are NOT identity: real AWS ignores them, and the TF provider
    revokes the seeded default egress as `FromPort=0, ToPort=0,
    IpProtocol=-1` (verified live by the tofu integration test -- the
    content hash must land on the seeded rule, which has no ports)."""
    all_protocols = perm["IpProtocol"] == "-1"
    base = {
        "is_egress": is_egress, "ip_protocol": perm["IpProtocol"],
        "from_port": None if all_protocols else perm["FromPort"],
        "to_port": None if all_protocols else perm["ToPort"],
        "cidr_ipv4": None, "cidr_ipv6": None, "referenced_group_id": None, "description": None,
    }
    rules = [
        *({**base, "cidr_ipv4": r.get("CidrIp"), "description": r.get("Description")} for r in perm["IpRanges"]),
        *({**base, "cidr_ipv6": r.get("CidrIpv6"), "description": r.get("Description")} for r in perm["Ipv6Ranges"]),
        *({**base, "referenced_group_id": g.get("GroupId"), "description": g.get("Description")} for g in perm["UserIdGroupPairs"]),
    ]
    return rules or [base]


def aggregate_permissions(rules: list[dict]) -> list[dict]:
    """Stored rules (one pairing each) -> IpPermissions dicts grouped by
    (protocol, port span) with ranges/pairs merged -- both the shape
    DescribeSecurityGroups serializes back to the provider and the exact
    input `fabric.nebula.sg_rules_to_firewall` consumes."""
    grouped: dict[tuple, dict] = {}
    for rule in rules:
        key = (rule["ip_protocol"], rule["from_port"], rule["to_port"])
        perm = grouped.setdefault(key, {
            "IpProtocol": rule["ip_protocol"], "IpRanges": [], "Ipv6Ranges": [], "UserIdGroupPairs": [],
        })
        if rule["from_port"] is not None:
            perm["FromPort"], perm["ToPort"] = rule["from_port"], rule["to_port"]
        if rule["cidr_ipv4"]:
            perm["IpRanges"].append({"CidrIp": rule["cidr_ipv4"]})
        if rule["cidr_ipv6"]:
            perm["Ipv6Ranges"].append({"CidrIpv6": rule["cidr_ipv6"]})
        if rule["referenced_group_id"]:
            perm["UserIdGroupPairs"].append({"GroupId": rule["referenced_group_id"]})
    return list(grouped.values())


# --- store access -------------------------------------------------------------


def _key(kind: str, resource_id: str) -> str:
    return f"{kind}:{resource_id}"


def _get(stores: SynthStores, env: str, kind: str, resource_id: str) -> dict | None:
    return stores.ec2net.get(env, _key(kind, resource_id))


def _records(stores: SynthStores, env: str, kind: str) -> list[dict]:
    return [v for k, v in stores.ec2net.items(env).items() if k.startswith(f"{kind}:")]


def _res_tags(stores: SynthStores, env: str, resource_id: str) -> dict[str, str]:
    return stores.tags.get(env, f"ec2:{resource_id}", {})


# --- wire building: EC2-protocol XML ------------------------------------------
# EC2 responses have NO Result wrapper: members sit directly under
# `<{Action}Response>`, next to a `requestId` botocore lifts into
# ResponseMetadata. All tag names are the lowercase serialization names from
# botocore's own EC2 model.


def _response(action_name: str, inner: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{action_name}Response xmlns="{_EC2_NS}">'
        f"<requestId>{_REQUEST_ID}</requestId>{inner}</{action_name}Response>"
    )
    return Response(xml, media_type="text/xml")


def _not_found(kind: str, resource_id: str) -> Response:
    return errors.synth_error("ec2", _NOT_FOUND[kind], f"The {kind} ID '{resource_id}' does not exist", 400)


def _tag_set_xml(tags: dict[str, str]) -> str:
    items = "".join(f"<item><key>{escape(k)}</key><value>{escape(v)}</value></item>" for k, v in tags.items())
    return f"<tagSet>{items}</tagSet>"


def _vpc_xml(vpc: dict, tags: dict[str, str]) -> str:
    return (
        f"<vpcId>{vpc['vpc_id']}</vpcId><ownerId>{ACCOUNT}</ownerId><state>available</state>"
        f"<cidrBlock>{vpc['cidr_block']}</cidrBlock>"
        f"<dhcpOptionsId>{vpc['dhcp_options_id']}</dhcpOptionsId>"
        f"<instanceTenancy>{escape(vpc['instance_tenancy'])}</instanceTenancy>"
        "<isDefault>false</isDefault>"
        "<cidrBlockAssociationSet><item>"
        f"<associationId>{vpc['cidr_association_id']}</associationId>"
        f"<cidrBlock>{vpc['cidr_block']}</cidrBlock>"
        "<cidrBlockState><state>associated</state></cidrBlockState>"
        "</item></cidrBlockAssociationSet>"
        "<ipv6CidrBlockAssociationSet/>" + _tag_set_xml(tags)
    )


def _subnet_xml(subnet: dict, tags: dict[str, str]) -> str:
    return (
        f"<subnetId>{subnet['subnet_id']}</subnetId><state>available</state>"
        f"<ownerId>{ACCOUNT}</ownerId><vpcId>{subnet['vpc_id']}</vpcId>"
        f"<cidrBlock>{subnet['cidr_block']}</cidrBlock>"
        "<availableIpAddressCount>251</availableIpAddressCount>"
        f"<availabilityZone>{escape(subnet['availability_zone'])}</availabilityZone>"
        f"<availabilityZoneId>{REGION.replace('-', '')[:4]}-az1</availabilityZoneId>"
        "<defaultForAz>false</defaultForAz><mapPublicIpOnLaunch>false</mapPublicIpOnLaunch>"
        "<assignIpv6AddressOnCreation>false</assignIpv6AddressOnCreation>"
        "<enableDns64>false</enableDns64><ipv6Native>false</ipv6Native>"
        "<privateDnsNameOptionsOnLaunch><hostnameType>ip-name</hostnameType>"
        "<enableResourceNameDnsARecord>false</enableResourceNameDnsARecord>"
        "<enableResourceNameDnsAAAARecord>false</enableResourceNameDnsAAAARecord>"
        "</privateDnsNameOptionsOnLaunch>"
        "<ipv6CidrBlockAssociationSet/>"
        f"<subnetArn>arn:aws:ec2:{REGION}:{ACCOUNT}:subnet/{subnet['subnet_id']}</subnetArn>"
        + _tag_set_xml(tags)
    )


def _range_items_xml(perm: dict) -> str:
    ip_ranges = "".join(f"<item><cidrIp>{escape(r['CidrIp'])}</cidrIp></item>" for r in perm["IpRanges"])
    ipv6_ranges = "".join(f"<item><cidrIpv6>{escape(r['CidrIpv6'])}</cidrIpv6></item>" for r in perm["Ipv6Ranges"])
    groups = "".join(
        f"<item><userId>{ACCOUNT}</userId><groupId>{g['GroupId']}</groupId></item>"
        for g in perm["UserIdGroupPairs"]
    )
    return f"<ipRanges>{ip_ranges}</ipRanges><ipv6Ranges>{ipv6_ranges}</ipv6Ranges><groups>{groups}</groups>"


def _permissions_xml(rules: list[dict], wrapper: str) -> str:
    items = []
    for perm in aggregate_permissions(rules):
        ports = "".join(
            f"<{tag}>{perm[member]}</{tag}>"
            for member, tag in (("FromPort", "fromPort"), ("ToPort", "toPort")) if member in perm
        )
        items.append(
            f"<item><ipProtocol>{escape(perm['IpProtocol'])}</ipProtocol>{ports}{_range_items_xml(perm)}</item>"
        )
    return f"<{wrapper}>{''.join(items)}</{wrapper}>"


def _sg_xml(sg: dict, tags: dict[str, str]) -> str:
    rules = list(sg["rules"].values())
    return (
        f"<ownerId>{ACCOUNT}</ownerId><groupId>{sg['group_id']}</groupId>"
        f"<groupName>{escape(sg['group_name'])}</groupName>"
        f"<groupDescription>{escape(sg['description'])}</groupDescription>"
        f"<vpcId>{sg['vpc_id']}</vpcId>"
        + _permissions_xml([r for r in rules if not r["is_egress"]], "ipPermissions")
        + _permissions_xml([r for r in rules if r["is_egress"]], "ipPermissionsEgress")
        + _tag_set_xml(tags)
    )


def _sg_rule_xml(rule_id: str, group_id: str, rule: dict) -> str:
    parts = [
        f"<securityGroupRuleId>{rule_id}</securityGroupRuleId>",
        f"<groupId>{group_id}</groupId><groupOwnerId>{ACCOUNT}</groupOwnerId>",
        f"<isEgress>{'true' if rule['is_egress'] else 'false'}</isEgress>",
        f"<ipProtocol>{escape(rule['ip_protocol'])}</ipProtocol>",
    ]
    parts += [f"<{tag}>{rule[m]}</{tag}>" for m, tag in (("from_port", "fromPort"), ("to_port", "toPort")) if rule[m] is not None]
    parts += [f"<{tag}>{escape(rule[m])}</{tag}>" for m, tag in (("cidr_ipv4", "cidrIpv4"), ("cidr_ipv6", "cidrIpv6"), ("description", "description")) if rule[m]]
    if rule["referenced_group_id"]:
        parts.append(f"<referencedGroupInfo><groupId>{rule['referenced_group_id']}</groupId></referencedGroupInfo>")
    return "".join(parts)


# --- VPC ----------------------------------------------------------------------


def _create_vpc(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    vpc_id = _mint("vpc")
    tags = _spec_tags(params)
    default_sg = _new_sg(stores, env, vpc_id, "default", "default VPC security group", is_default=True)
    # V1b: a VPC joins the env's Nebula network (1:1 for now -- see module
    # docstring). Real CA/cert artifacts, idempotent, no daemon started.
    network = ensure_network(stores.root, env, "127.0.0.1")
    vpc = {
        "vpc_id": vpc_id,
        "cidr_block": params.get("CidrBlock", "10.0.0.0/16"),
        "instance_tenancy": params.get("InstanceTenancy", "default"),
        "dhcp_options_id": _mint("dopt"),
        "cidr_association_id": _mint("vpc-cidr-assoc"),
        "network_acl_id": _mint("acl"),
        "route_table_id": _mint("rtb"),
        "route_table_association_id": _mint("rtbassoc"),
        "default_sg_id": default_sg["group_id"],
        "nebula_network": network.network,
    }
    stores.ec2net.set(env, _key("vpc", vpc_id), vpc)
    stores.tags.set(env, f"ec2:{vpc_id}", tags)
    return _response("CreateVpc", f"<vpc>{_vpc_xml(vpc, tags)}</vpc>")


def _describe_vpcs(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    vpc_ids = _scalars(params, "VpcId")
    filters = _filters(params)
    vpcs = _records(stores, env, "vpc")
    missing = [i for i in vpc_ids if i not in {v["vpc_id"] for v in vpcs}]
    if missing:
        return _not_found("vpc", missing[0])
    selected = [
        v for v in vpcs
        if (not vpc_ids or v["vpc_id"] in vpc_ids)
        and _matches(filters, {"vpc-id": v["vpc_id"], "cidr": v["cidr_block"], "state": "available"})
    ]
    items = "".join(f"<item>{_vpc_xml(v, _res_tags(stores, env, v['vpc_id']))}</item>" for v in selected)
    return _response("DescribeVpcs", f"<vpcSet>{items}</vpcSet>")


def _describe_vpc_attribute(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    vpc_id = params.get("VpcId", "")
    if _get(stores, env, "vpc", vpc_id) is None:
        return _not_found("vpc", vpc_id)
    attribute = params.get("Attribute", "")
    value = _VPC_ATTRIBUTE_DEFAULTS.get(attribute, "false")
    return _response(
        "DescribeVpcAttribute",
        f"<vpcId>{vpc_id}</vpcId><{attribute}><value>{value}</value></{attribute}>",
    )


def purge_env(stores: SynthStores, env: str) -> list[str]:
    """Forget every VPC / subnet / security-group record this env holds, and
    take its Nebula network down with them. Returns the keys it forgot.

    `/destroy`'s network half, and the other end of field test 3 HIGH-B. When
    an apply is interrupted (`kill -9` on tofu, an OOM, a closed laptop), the
    gateway has already created these records but tofu's state has not
    recorded them -- so `tofu destroy` has nothing to destroy, `_delete_vpc`
    is never called, and they survive every subsequent destroy forever. That
    is why `/world` went on listing a VPC and subnets that no longer exist for
    an env the user had destroyed. It also quietly re-opened HIGH-A: the
    lighthouse stop lives on the VPC-delete path, so a VPC record that never
    gets deleted is a lighthouse that never gets stopped.

    A no-op for a NORMAL destroy, where tofu deleted each of these through
    `_delete_vpc` already and there is nothing left to forget."""
    keys = [k for k in stores.ec2net.items(env) if k.split(":", 1)[0] in ("vpc", "subnet", "sg")]
    for key in keys:
        stores.tags.set(env, f"ec2:{key.split(':', 1)[1]}", {})
        stores.ec2net.delete(env, key)
    if keys:
        log.warning("destroy forgot %d orphaned ec2 network record(s) for env %r: %s", len(keys), env, keys)
    # Same order as `_delete_vpc`, for the same reason: the pidfile that names
    # the lighthouse process lives inside the directory being removed.
    LighthouseManager().ensure_stopped(stores.root, env)
    shutil.rmtree(stores.root / env / "nebula", ignore_errors=True)
    return keys


def _delete_vpc(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    vpc_id = params.get("VpcId", "")
    vpc = _get(stores, env, "vpc", vpc_id)
    if vpc is None:
        return _not_found("vpc", vpc_id)
    subnets = [s for s in _records(stores, env, "subnet") if s["vpc_id"] == vpc_id]
    other_sgs = [g for g in _records(stores, env, "sg") if g["vpc_id"] == vpc_id and not g["is_default"]]
    if subnets or other_sgs:
        return errors.synth_error(
            "ec2", "DependencyViolation",
            f"The vpc '{vpc_id}' has dependencies and cannot be deleted.", 400,
        )
    stores.ec2net.delete(env, _key("sg", vpc["default_sg_id"]))
    stores.ec2net.delete(env, _key("vpc", vpc_id))
    # V1b symmetry with _create_vpc's ensure_network: the env's Nebula CA
    # lives at .odin/{env}/nebula/ and belongs to the env's VPCs. When the
    # last VPC goes, the CA goes too -- a recreated VPC mints a fresh one.
    if not _records(stores, env, "vpc"):
        # ...and so does the lighthouse PROCESS, which must be stopped BEFORE
        # its directory disappears: `ensure_stopped` finds it through the
        # pidfile that lives in there, so deleting first strands a real
        # process holding a real UDP port with nothing left able to name it.
        # Field test 3 HIGH-A: an env of a VPC + one S3 bucket (no EC2 at all,
        # so `ec2compute._finish_terminate`'s "last VM leaves" stop never ran)
        # leaked one lighthouse and one port per apply/destroy cycle -- three
        # orphans measured, and ~100 cycles would exhaust the whole 4342-4441
        # range. `fabric/nebula.py::reap_orphaned_lighthouses` is the startup
        # backstop for one that leaked before this existed, or for a crash
        # between these two lines.
        LighthouseManager().ensure_stopped(stores.root, env)
        shutil.rmtree(stores.root / env / "nebula", ignore_errors=True)
    return _response("DeleteVpc", "<return>true</return>")


# --- the VPC's auto-created sidecars (research finding #1) --------------------


def _describe_network_acls(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    filters = _filters(params)
    items = "".join(
        "<item>"
        f"<networkAclId>{v['network_acl_id']}</networkAclId><vpcId>{v['vpc_id']}</vpcId>"
        f"<default>true</default><ownerId>{ACCOUNT}</ownerId>"
        "<associationSet/><entrySet/><tagSet/></item>"
        for v in _records(stores, env, "vpc")
        if _matches(filters, {"vpc-id": v["vpc_id"], "default": "true", "network-acl-id": v["network_acl_id"]})
    )
    return _response("DescribeNetworkAcls", f"<networkAclSet>{items}</networkAclSet>")


def _describe_route_tables(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    filters = _filters(params)
    items = "".join(
        "<item>"
        f"<routeTableId>{v['route_table_id']}</routeTableId><vpcId>{v['vpc_id']}</vpcId>"
        f"<ownerId>{ACCOUNT}</ownerId>"
        "<associationSet><item>"
        f"<routeTableAssociationId>{v['route_table_association_id']}</routeTableAssociationId>"
        f"<routeTableId>{v['route_table_id']}</routeTableId><main>true</main>"
        "<associationState><state>associated</state></associationState>"
        "</item></associationSet>"
        "<routeSet><item>"
        f"<destinationCidrBlock>{v['cidr_block']}</destinationCidrBlock>"
        "<gatewayId>local</gatewayId><state>active</state><origin>CreateRouteTable</origin>"
        "</item></routeSet><tagSet/></item>"
        for v in _records(stores, env, "vpc")
        if _matches(filters, {
            "vpc-id": v["vpc_id"], "association.main": "true", "route-table-id": v["route_table_id"],
        })
    )
    return _response("DescribeRouteTables", f"<routeTableSet>{items}</routeTableSet>")


def _describe_network_interfaces(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    """The provider's pre-delete ENI sweep. V1 has no EC2 instances (V3), so
    no ENIs ever exist -- always empty."""
    return _response("DescribeNetworkInterfaces", "<networkInterfaceSet/>")


# --- Subnet -------------------------------------------------------------------


def _create_subnet(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    vpc_id = params.get("VpcId", "")
    if _get(stores, env, "vpc", vpc_id) is None:
        return _not_found("vpc", vpc_id)
    subnet = {
        "subnet_id": _mint("subnet"),
        "vpc_id": vpc_id,
        "cidr_block": params.get("CidrBlock", ""),
        "availability_zone": params.get("AvailabilityZone") or f"{REGION}a",
    }
    tags = _spec_tags(params)
    stores.ec2net.set(env, _key("subnet", subnet["subnet_id"]), subnet)
    stores.tags.set(env, f"ec2:{subnet['subnet_id']}", tags)
    return _response("CreateSubnet", f"<subnet>{_subnet_xml(subnet, tags)}</subnet>")


def _describe_subnets(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    subnet_ids = _scalars(params, "SubnetId")
    filters = _filters(params)
    subnets = _records(stores, env, "subnet")
    missing = [i for i in subnet_ids if i not in {s["subnet_id"] for s in subnets}]
    if missing:
        return _not_found("subnet", missing[0])
    selected = [
        s for s in subnets
        if (not subnet_ids or s["subnet_id"] in subnet_ids)
        and _matches(filters, {
            "subnet-id": s["subnet_id"], "vpc-id": s["vpc_id"],
            "cidr-block": s["cidr_block"], "availability-zone": s["availability_zone"],
        })
    ]
    items = "".join(f"<item>{_subnet_xml(s, _res_tags(stores, env, s['subnet_id']))}</item>" for s in selected)
    return _response("DescribeSubnets", f"<subnetSet>{items}</subnetSet>")


def _delete_subnet(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    subnet_id = params.get("SubnetId", "")
    if _get(stores, env, "subnet", subnet_id) is None:
        return _not_found("subnet", subnet_id)
    stores.ec2net.delete(env, _key("subnet", subnet_id))
    return _response("DeleteSubnet", "<return>true</return>")


# --- Security Group -----------------------------------------------------------


def _compiled_firewall(sg: dict) -> dict:
    """The SG's ingress rule set through `fabric.nebula.sg_rules_to_firewall`
    (V1b) -- `aggregate_permissions` already emits that function's exact
    input shape. Stored as a plain dump on the SG record so it dies with the
    group and `mesh_state` reads it without importing gateway code."""
    ingress = [r for r in sg["rules"].values() if not r["is_egress"]]
    return sg_rules_to_firewall(aggregate_permissions(ingress)).model_dump()


def compiled_firewall(stores: SynthStores, env: str, group_id: str) -> FirewallRules | None:
    """One security group's ALREADY-compiled Nebula firewall, read back off the
    SG record (`_compiled_firewall` above recomputes it on every rule
    mutation). None when the group doesn't exist yet or carries no compiled
    firewall.

    Public because TWO gateway models gate real substrates with it -- an EC2
    instance's VM (`ec2compute.py::_instance_firewall`) and an RDS instance's
    Postgres container (`rdsctl.py::_db_firewall`) -- and both must read the
    SAME bytes, so a database and a VM in one group get byte-identical rules.
    It lives here, with the store that owns the record, rather than being
    reached into from either model."""
    sg = stores.ec2net.get(env, _key("sg", group_id))
    return FirewallRules.model_validate(sg["firewall"]) if sg and sg.get("firewall") else None


def _new_sg(stores: SynthStores, env: str, vpc_id: str, name: str, description: str, is_default: bool) -> dict:
    """Mint + store an SG seeded with the default allow-all IPv4 egress rule
    (research finding #3: the provider revokes it, so it must exist to be
    revoked -- and its content hash must match the revoke request's)."""
    group_id = _mint("sg")
    egress_all = {
        "is_egress": True, "ip_protocol": "-1", "from_port": None, "to_port": None,
        "cidr_ipv4": "0.0.0.0/0", "cidr_ipv6": None, "referenced_group_id": None, "description": None,
    }
    sg = {
        "group_id": group_id, "group_name": name, "description": description,
        "vpc_id": vpc_id, "is_default": is_default,
        "rules": {_rule_id(group_id, egress_all): egress_all},
    }
    sg["firewall"] = _compiled_firewall(sg)
    stores.ec2net.set(env, _key("sg", group_id), sg)
    return sg


def _create_security_group(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    vpc_id = params.get("VpcId", "")
    if _get(stores, env, "vpc", vpc_id) is None:
        return _not_found("vpc", vpc_id)
    sg = _new_sg(
        stores, env, vpc_id,
        params.get("GroupName", ""), params.get("GroupDescription", ""), is_default=False,
    )
    tags = _spec_tags(params)
    stores.tags.set(env, f"ec2:{sg['group_id']}", tags)
    return _response(
        "CreateSecurityGroup",
        f"<groupId>{sg['group_id']}</groupId>"
        f"<securityGroupArn>arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{sg['group_id']}</securityGroupArn>"
        + _tag_set_xml(tags),
    )


def _describe_security_groups(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    group_ids = _scalars(params, "GroupId")
    group_names = _scalars(params, "GroupName")
    filters = _filters(params)
    sgs = _records(stores, env, "sg")
    missing = [i for i in group_ids if i not in {g["group_id"] for g in sgs}]
    if missing:
        return _not_found("sg", missing[0])
    selected = [
        g for g in sgs
        if (not group_ids or g["group_id"] in group_ids)
        and (not group_names or g["group_name"] in group_names)
        and _matches(filters, {
            "group-id": g["group_id"], "group-name": g["group_name"], "vpc-id": g["vpc_id"],
        })
    ]
    items = "".join(f"<item>{_sg_xml(g, _res_tags(stores, env, g['group_id']))}</item>" for g in selected)
    return _response("DescribeSecurityGroups", f"<securityGroupInfo>{items}</securityGroupInfo>")


def _delete_security_group(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    group_id = params.get("GroupId", "")
    if _get(stores, env, "sg", group_id) is None:
        return _not_found("sg", group_id)
    stores.ec2net.delete(env, _key("sg", group_id))
    return _response("DeleteSecurityGroup", "<return>true</return>")


def _mutate_rules(params: dict[str, str], env: str, stores: SynthStores, is_egress: bool, action_name: str) -> Response:
    """Shared Authorize*/Revoke* body: both sides resolve each permission to
    content-hashed rules; authorize overwrites in place (idempotent -- same
    content, same sgr- id, no duplicate), revoke pops by the same hash."""
    group_id = params.get("GroupId", "")
    sg = _get(stores, env, "sg", group_id)
    if sg is None:
        return _not_found("sg", group_id)
    authorize = action_name.startswith("Authorize")
    rule_items = []
    for perm in _permissions(params):
        for rule in _rules_from_permission(perm, is_egress):
            rule_id = _rule_id(group_id, rule)
            if authorize:
                sg["rules"][rule_id] = rule
                rule_items.append(f"<item>{_sg_rule_xml(rule_id, group_id, rule)}</item>")
            else:
                sg["rules"].pop(rule_id, None)
    sg["firewall"] = _compiled_firewall(sg)  # V1b: every mutation recompiles
    stores.ec2net.set(env, _key("sg", group_id), sg)
    rule_set = f"<securityGroupRuleSet>{''.join(rule_items)}</securityGroupRuleSet>" if authorize else ""
    return _response(action_name, f"<return>true</return>{rule_set}")


def _authorize_ingress(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    return _mutate_rules(params, env, stores, is_egress=False, action_name="AuthorizeSecurityGroupIngress")


def _authorize_egress(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    return _mutate_rules(params, env, stores, is_egress=True, action_name="AuthorizeSecurityGroupEgress")


def _revoke_ingress(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    return _mutate_rules(params, env, stores, is_egress=False, action_name="RevokeSecurityGroupIngress")


def _revoke_egress(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    return _mutate_rules(params, env, stores, is_egress=True, action_name="RevokeSecurityGroupEgress")


def _describe_security_group_rules(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    filters = _filters(params)
    rule_ids = _scalars(params, "SecurityGroupRuleId")
    items = "".join(
        f"<item>{_sg_rule_xml(rule_id, sg['group_id'], rule)}</item>"
        for sg in _records(stores, env, "sg")
        for rule_id, rule in sg["rules"].items()
        if (not rule_ids or rule_id in rule_ids)
        and _matches(filters, {"group-id": sg["group_id"], "security-group-rule-id": rule_id})
    )
    return _response("DescribeSecurityGroupRules", f"<securityGroupRuleSet>{items}</securityGroupRuleSet>")


# --- Tags (EC2's own wire shape -- distinct from sqs/sns/dynamodb's) ----------


def _create_tags(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    new_tags = {t["Key"]: t.get("Value", "") for t in _indexed(params, "Tag")}
    for resource_id in _scalars(params, "ResourceId"):
        key = f"ec2:{resource_id}"
        stores.tags.set(env, key, {**stores.tags.get(env, key, {}), **new_tags})
    return _response("CreateTags", "<return>true</return>")


def _delete_tags(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    """Tag.N present: delete only matching keys (a Tag.N.Value further
    requires the value to match). No Tag.N at all: delete ALL tags."""
    tag_items = _indexed(params, "Tag")
    for resource_id in _scalars(params, "ResourceId"):
        key = f"ec2:{resource_id}"
        current = stores.tags.get(env, key, {})
        kept = {
            k: v for k, v in current.items()
            if tag_items and not any(t["Key"] == k and t.get("Value", v) == v for t in tag_items)
        }
        stores.tags.set(env, key, kept)
    return _response("DeleteTags", "<return>true</return>")


def _describe_tags(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    filters = _filters(params)
    items = []
    for key, tags in stores.tags.items(env).items():
        if not key.startswith("ec2:"):
            continue
        resource_id = key.removeprefix("ec2:")
        resource_type = _RESOURCE_TYPES.get(resource_id.split("-", 1)[0], "vpc")
        for k, v in tags.items():
            attrs = {"resource-id": resource_id, "resource-type": resource_type, "key": k, "value": v}
            if _matches(filters, attrs):
                items.append(
                    f"<item><resourceId>{resource_id}</resourceId>"
                    f"<resourceType>{resource_type}</resourceType>"
                    f"<key>{escape(k)}</key><value>{escape(v)}</value></item>"
                )
    return _response("DescribeTags", f"<tagSet>{''.join(items)}</tagSet>")


# --- dispatch -------------------------------------------------------------------


_Handler = Callable[[dict[str, str], str, SynthStores], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateVpc": _create_vpc,
    "DescribeVpcs": _describe_vpcs,
    "DescribeVpcAttribute": _describe_vpc_attribute,
    "DeleteVpc": _delete_vpc,
    "DescribeNetworkAcls": _describe_network_acls,
    "DescribeRouteTables": _describe_route_tables,
    "DescribeNetworkInterfaces": _describe_network_interfaces,
    "CreateSubnet": _create_subnet,
    "DescribeSubnets": _describe_subnets,
    "DeleteSubnet": _delete_subnet,
    "CreateSecurityGroup": _create_security_group,
    "DescribeSecurityGroups": _describe_security_groups,
    "DeleteSecurityGroup": _delete_security_group,
    "AuthorizeSecurityGroupIngress": _authorize_ingress,
    "AuthorizeSecurityGroupEgress": _authorize_egress,
    "RevokeSecurityGroupIngress": _revoke_ingress,
    "RevokeSecurityGroupEgress": _revoke_egress,
    "DescribeSecurityGroupRules": _describe_security_group_rules,
    "CreateTags": _create_tags,
    "DeleteTags": _delete_tags,
    "DescribeTags": _describe_tags,
}


def pure_answer(action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response | None:
    """The whole EC2 answer -- same signature as synth.pure_answer, which
    dispatches every `ec2:*` action here. Never returns None for an ec2
    action: EC2 has no backing to fall through to (an unknown action gets
    the InvalidAction envelope research §2b observed the provider
    tolerating)."""
    op = action.removeprefix("ec2:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("ec2", "InvalidAction", f"The action {op} is not valid for this web service.", 400)
    return handler(_params(body), env, stores)
