"""Deterministic canvas -> Terraform skeleton generator (S3a).

Consumes a `Stack` (the output of `odin.spec.translate.canvas_to_stack`) and
emits a PORTABLE Terraform project: no endpoints, no `skip_*` flags, no
credentials. Those live in odin's runtime-generated `override.tf` + env vars
(docs/superpowers/plans/2026-07-22-s-simulate-translation.md, Global
Constraints) — never here. The translation agent (S3, later) refines/
annotates this skeleton; determinism comes first.

Output is fmt-canonical HCL (two-space indent, `=` aligned to the widest key
in each block) so `tofu fmt -check` accepts it unmodified.
"""
from __future__ import annotations

import json
import re

import hcl2
from pydantic import BaseModel

from odin.spec.models import ResourceDesired, Stack

_REGION = "us-east-1"
_SANITIZE = re.compile(r"[^a-z0-9_]")

# kind -> human reason it can't be simulated yet. Anything not in the map (and
# not one of the supported kinds below) gets a generic fallback reason.
_UNSUPPORTED_REASONS = {
    "rds": "Simulate v1 — stays on the reconciler path",
}

# Public: the translation agent (S3b) and TF import (S4) both need the
# terraform{} header (a live-import scratch project builds one from scratch)
# and the `resource "type" "name"` parsing below (S3b's guardrail, S4's
# HCL->canvas parser) -- shared here, next to the generator they must
# round-trip with, instead of duplicated per consumer.
HEADER = (
    'terraform {\n'
    '  required_providers {\n'
    '    aws = {\n'
    '      source  = "hashicorp/aws"\n'
    '      version = "~> 5.0"\n'
    '    }\n'
    '  }\n'
    '}'
)


class TfProject(BaseModel):
    model_config = {"frozen": True}
    files: dict[str, str] = {}
    unsupported: list[str] = []


def quote(value: object) -> str:
    """An HCL string literal. json.dumps shares HCL's basic-string escaping
    for the ASCII cases odin's labels can contain — no hand-rolled escaper."""
    return json.dumps(value)


def unquote(value: object) -> str | None:
    """The inverse of `quote`, for reading hcl2-parsed attribute values back:
    python-hcl2 keeps a quoted literal's surrounding `"` characters in the
    parsed string (verified empirically — `bucket = "x"` parses to the
    3-character string '"x"', not 'x'). Anything else (an interpolation like
    `${aws_sns_topic.t.arn}`, a bare number/bool, a nested block) is left
    alone — only a plain quoted-literal string gets unwrapped."""
    if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value if isinstance(value, str) else None


def sanitize_name(label: str) -> str:
    name = _SANITIZE.sub("_", label.lower())
    return f"_{name}" if name[:1].isdigit() else name


def unique_name(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    while candidate in used:
        candidate, n = f"{name}_{n}", n + 1
    used.add(candidate)
    return candidate


def provider_block(region: str = _REGION) -> str:
    return f'provider "aws" {{\n  region = {quote(region)}\n}}'


def parse_tf(files: dict[str, str]) -> list[tuple[str, str, dict]]:
    """Parse every `resource "type" "name" { ... }` block across `files`
    (filename -> HCL text) into `(type, name, attrs)` triples — `type`/`name`
    unquoted, `attrs` left as hcl2's raw parse (see `unquote` for reading
    individual values back out). The one place odin reads HCL back in: S3b's
    guardrail (resource-SET equality between skeleton and agent output) and
    S4's HCL->canvas parser both call this instead of parsing HCL twice."""
    triples: list[tuple[str, str, dict]] = []
    for content in files.values():
        for block in hcl2.loads(content).get("resource", []):
            for raw_type, named in block.items():
                for raw_name, attrs in named.items():
                    triples.append((unquote(raw_type), unquote(raw_name), attrs))
    return triples


def resource_set(files: dict[str, str]) -> frozenset[tuple[str, str]]:
    """The (type, name) identity of every resource block across `files` —
    S3b's guardrail compares this set between the skeleton and the agent's
    refinement; they must be identical (the agent may edit arguments, never
    add/remove a resource)."""
    return frozenset((rtype, name) for rtype, name, _ in parse_tf(files))


def _field(res: ResourceDesired, key: str, default: str) -> str:
    fv = res.fields.get(key)
    return fv.value if fv is not None else default


def _block(resource_type: str, name: str, attrs: dict[str, str], nested: str = "") -> str:
    width = max(len(k) for k in attrs)
    lines = [f"  {k.ljust(width)} = {v}" for k, v in attrs.items()]
    if nested:
        lines += ["", nested]
    body = "\n".join(lines)
    return f'resource "{resource_type}" "{name}" {{\n{body}\n}}'


# Cross-resource references passed to every builder: resource id -> (kind,
# hcl_name). Fully populated BEFORE any builder runs (see generate_tf pass 1),
# so a subnet/sg can name its containing vpc regardless of sort order.
Refs = dict[str, tuple[str, str]]

# A builder returns (attrs, nested) — or a plain string: the human reason this
# specific resource can't be built (e.g. a subnet drawn outside any VPC),
# routed into `unsupported` exactly like a kind with no builder at all.
Built = tuple[dict[str, str], str] | str

_NOT_IN_VPC = "not contained inside a VPC on the canvas (drag it into a VPC box)"

# Every aws_security_group implicitly starts with AWS's seeded allow-all
# egress rule, but the TF provider REMOVES it when the config omits an egress
# block — emitting it explicitly keeps apply -> plan zero-drift against the
# gateway's pre-seeded default egress (research §2a / MiniStack digest).
_DEFAULT_EGRESS = (
    "  egress {\n"
    "    from_port   = 0\n"
    "    to_port     = 0\n"
    '    protocol    = "-1"\n'
    '    cidr_blocks = ["0.0.0.0/0"]\n'
    "  }"
)


def _vpc_ref(res: ResourceDesired, refs: Refs) -> str | None:
    """`aws_vpc.<name>.id` for res's containment-stamped `vpc` field (the
    containing VPC's canvas label == its Stack resource id), or None when the
    field is missing or names something that isn't a vpc resource."""
    kind, name = refs.get(_field(res, "vpc", ""), ("", ""))
    return f"aws_vpc.{name}.id" if kind == "vpc" else None


def _subnet_ref(res: ResourceDesired, refs: Refs) -> str | None:
    """`aws_subnet.<name>.id` for res's containment-stamped `subnet` field
    (V3c: an ec2 node's subnet_id), or None when missing/not a subnet."""
    kind, name = refs.get(_field(res, "subnet", ""), ("", ""))
    return f"aws_subnet.{name}.id" if kind == "subnet" else None


def _ingress_rules(res: ResourceDesired) -> list[tuple[str, str, str]] | None:
    """Parse the SG node's `ingressRules` field: one rule per line, formatted
    `protocol:port:cidr` (e.g. `tcp:443:0.0.0.0/0`). Returns (protocol, port,
    cidr) triples, or None when any non-empty line doesn't fit the format."""
    lines = [line.strip() for line in _field(res, "ingressRules", "").splitlines()]
    parsed = [tuple(line.split(":")) for line in lines if line]
    ok = all(len(p) == 3 and p[1].isdigit() for p in parsed)
    return parsed if ok else None


def _s3(res: ResourceDesired, refs: Refs) -> Built:
    return {"bucket": quote(res.id)}, ""


def _sqs(res: ResourceDesired, refs: Refs) -> Built:
    return {"name": quote(res.id)}, ""


def _sns(res: ResourceDesired, refs: Refs) -> Built:
    return {"name": quote(res.id)}, ""


def _dynamodb(res: ResourceDesired, refs: Refs) -> Built:
    hash_key = _field(res, "hashKey", "id")
    attrs = {
        "name": quote(res.id),
        "billing_mode": quote("PAY_PER_REQUEST"),
        "hash_key": quote(hash_key),
    }
    nested = f'  attribute {{\n    name = {quote(hash_key)}\n    type = "S"\n  }}'
    return attrs, nested


def _vpc(res: ResourceDesired, refs: Refs) -> Built:
    return {"cidr_block": quote(_field(res, "cidr", "10.0.0.0/16"))}, ""


def _subnet(res: ResourceDesired, refs: Refs) -> Built:
    vpc_id = _vpc_ref(res, refs)
    if vpc_id is None:
        return _NOT_IN_VPC
    return {"vpc_id": vpc_id, "cidr_block": quote(_field(res, "cidr", "10.0.1.0/24"))}, ""


# The standard Lambda execution-role trust document (research §2d's captured
# IAM+Lambda sequence: `aws_lambda_function` requires an `aws_iam_role` whose
# AssumeRolePolicyDocument trusts `lambda.amazonaws.com`) -- the default
# every iam_role node gets unless a later slice adds a way to author a
# different principal on the canvas.
_LAMBDA_TRUST_POLICY = (
    "jsonencode({\n"
    '    Version = "2012-10-17"\n'
    "    Statement = [{\n"
    '      Action    = "sts:AssumeRole"\n'
    '      Effect    = "Allow"\n'
    '      Principal = { Service = "lambda.amazonaws.com" }\n'
    "    }]\n"
    "  })"
)


def _iam_role(res: ResourceDesired, refs: Refs) -> Built:
    # assume_role_policy's value spans multiple lines (jsonencode({...})),
    # so it's passed as `nested` rather than joining `attrs`' aligned
    # cluster -- `tofu fmt` itself breaks alignment around a multi-line
    # value (verified empirically), and `_block`'s `width = max(len(k) for
    # k in attrs)` would otherwise force-pad "name" to match
    # "assume_role_policy"'s length, which real `tofu fmt` then un-pads.
    nested = f"  assume_role_policy = {_LAMBDA_TRUST_POLICY}"
    return {"name": quote(res.id)}, nested


def _ecr(res: ResourceDesired, refs: Refs) -> Built:
    return {"name": quote(res.id)}, ""


def _sg(res: ResourceDesired, refs: Refs) -> Built:
    vpc_id = _vpc_ref(res, refs)
    if vpc_id is None:
        return _NOT_IN_VPC
    rules = _ingress_rules(res)
    if rules is None:
        return 'invalid ingress rule — expected one "protocol:port:cidr" per line, e.g. tcp:443:0.0.0.0/0'
    ingress = [
        (
            "  ingress {\n"
            f"    from_port   = {port}\n"
            f"    to_port     = {port}\n"
            f"    protocol    = {quote(protocol)}\n"
            f"    cidr_blocks = [{quote(cidr)}]\n"
            "  }"
        )
        for protocol, port, cidr in rules
    ]
    return {"name": quote(res.id), "vpc_id": vpc_id}, "\n\n".join([*ingress, _DEFAULT_EGRESS])


# V3c: EC2 instances (real Lima VMs, gateway/models/ec2compute.py). Matches
# that module's own stub-catalog default (documentation only — ImageId is
# accepted verbatim, never validated) and default instance type.
_NOT_IN_SUBNET = "not contained inside a Subnet on the canvas (drag it into a Subnet box)"
_BAD_SECURITY_GROUPS = 'securityGroups names something that isn\'t a Security Group on the canvas'
_DEFAULT_AMI = "ami-0c101f26f147fa7fd"
_DEFAULT_INSTANCE_TYPE = "t3.micro"


def _security_group_refs(res: ResourceDesired, refs: Refs) -> list[str] | None:
    """The ec2 node's `securityGroups` field: one sg canvas label per line
    (SIMPLEST honest v1 — see the V3 brief: no implicit "same containment
    scope" placement, just an explicit list). Returns `aws_security_group.
    <name>.id` refs, `[]` for an empty field (the instance just gets the
    VPC's default SG, a legitimate case), or None if ANY line names
    something that isn't a sg resource."""
    lines = [line.strip() for line in _field(res, "securityGroups", "").splitlines() if line.strip()]
    resolved = []
    for label in lines:
        kind, name = refs.get(label, ("", ""))
        if kind != "sg":
            return None
        resolved.append(f"aws_security_group.{name}.id")
    return resolved


def _ec2(res: ResourceDesired, refs: Refs) -> Built:
    subnet_id = _subnet_ref(res, refs)
    if subnet_id is None:
        return _NOT_IN_SUBNET
    sg_ids = _security_group_refs(res, refs)
    if sg_ids is None:
        return _BAD_SECURITY_GROUPS
    attrs = {
        "ami": quote(_field(res, "ami", _DEFAULT_AMI)),
        "instance_type": quote(_field(res, "instanceType", _DEFAULT_INSTANCE_TYPE)),
        "subnet_id": subnet_id,
    }
    nested = []
    if sg_ids:
        nested.append(f"  vpc_security_group_ids = [{', '.join(sg_ids)}]")
    if _field(res, "key", ""):
        # The companion aws_key_pair block (below, generate_tf's 3rd pass)
        # is named deterministically off THIS instance's own hcl name --
        # already assigned in pass 1, so `refs[res.id]` is available here.
        _, own_name = refs[res.id]
        nested.append(f"  key_name = aws_key_pair.{own_name}_key.key_name")
    user_data = _field(res, "userData", "")
    if user_data:
        nested.append(f"  user_data = {quote(user_data)}")
    return attrs, "\n\n".join(nested)


# kind -> terraform resource type; kept separate from _BUILDERS so pass 1 of
# generate_tf can assign HCL names (scoped per resource type) without running
# any builder.
_TF_TYPES = {
    "s3": "aws_s3_bucket",
    "sqs": "aws_sqs_queue",
    "sns": "aws_sns_topic",
    "dynamodb": "aws_dynamodb_table",
    "vpc": "aws_vpc",
    "subnet": "aws_subnet",
    "sg": "aws_security_group",
    "iam_role": "aws_iam_role",
    "ecr": "aws_ecr_repository",
    "ec2": "aws_instance",
}

_BUILDERS = {
    "s3": _s3,
    "sqs": _sqs,
    "sns": _sns,
    "dynamodb": _dynamodb,
    "vpc": _vpc,
    "subnet": _subnet,
    "sg": _sg,
    "iam_role": _iam_role,
    "ecr": _ecr,
    "ec2": _ec2,
}


def generate_tf(stack: Stack) -> TfProject:
    by_id = {r.id: r for r in stack.resources}
    ordered = sorted(stack.resources, key=lambda r: (r.kind, r.id))
    used_names: dict[str, set[str]] = {}
    hcl_name_by_id: dict[str, str] = {}
    refs: Refs = {}
    blocks: list[tuple[tuple[str, str], str]] = []
    unsupported: list[str] = []

    # Pass 1 — assign every buildable resource its HCL name BEFORE any builder
    # runs. "subnet" sorts before "vpc", yet aws_subnet.vpc_id must reference
    # the vpc's name — a single interleaved pass would look it up too early.
    for res in ordered:
        if res.kind not in _BUILDERS:
            reason = _UNSUPPORTED_REASONS.get(res.kind, f"{res.kind} — not supported in Simulate v1")
            unsupported.append(f"{res.id} ({res.kind}): {reason}")
            continue
        name = unique_name(sanitize_name(res.id), used_names.setdefault(_TF_TYPES[res.kind], set()))
        hcl_name_by_id[res.id] = name
        refs[res.id] = (res.kind, name)

    # Pass 2 — build blocks with the name table complete. A builder may still
    # opt out for THIS resource (returns the reason string) — e.g. a subnet
    # not drawn inside any VPC — which lands in `unsupported`, never dropped.
    for res in ordered:
        if res.kind not in _BUILDERS:
            continue
        built = _BUILDERS[res.kind](res, refs)
        if isinstance(built, str):
            unsupported.append(f"{res.id} ({res.kind}): {built}")
            continue
        attrs, nested = built
        block = _block(_TF_TYPES[res.kind], hcl_name_by_id[res.id], attrs, nested)
        blocks.append(((res.kind, res.id), block))

    for edge in sorted(stack.edges, key=lambda e: (e.src, e.dst)):
        topic, queue = by_id.get(edge.src), by_id.get(edge.dst)
        if topic is None or queue is None or topic.kind != "sns" or queue.kind != "sqs":
            continue
        topic_name, queue_name = hcl_name_by_id[topic.id], hcl_name_by_id[queue.id]
        name = unique_name(
            sanitize_name(f"{topic_name}_{queue_name}"),
            used_names.setdefault("aws_sns_topic_subscription", set()),
        )
        attrs = {
            "topic_arn": f"aws_sns_topic.{topic_name}.arn",
            "protocol": quote("sqs"),
            "endpoint": f"aws_sqs_queue.{queue_name}.arn",
            "raw_message_delivery": "true",
        }
        block = _block("aws_sns_topic_subscription", name, attrs)
        blocks.append((("sns_subscription", f"{topic.id}.{queue.id}"), block))

    # iam_role's optional inline-policy textarea (V2c) becomes a SEPARATE
    # aws_iam_role_policy resource, not a nested block on aws_iam_role
    # itself — mirrors the sns_subscription pass above: one canvas node can
    # still emit more than one TF resource. `policy` takes the field's raw
    # text verbatim (a plain JSON string is a valid `aws_iam_role_policy`
    # argument; no jsonencode() round-trip needed since the user already
    # typed JSON into the textarea).
    for res in ordered:
        if res.kind != "iam_role":
            continue
        inline = _field(res, "inlinePolicy", "").strip()
        role_name = hcl_name_by_id.get(res.id)
        if not inline or role_name is None:
            continue
        name = unique_name(
            sanitize_name(f"{role_name}_inline"), used_names.setdefault("aws_iam_role_policy", set()),
        )
        attrs = {"name": quote(f"{res.id}-inline"), "role": f"aws_iam_role.{role_name}.name", "policy": quote(inline)}
        block = _block("aws_iam_role_policy", name, attrs)
        blocks.append((("iam_role_policy", res.id), block))

    # V3c: an ec2 node's optional `key` field (a raw SSH public key) becomes
    # a SEPARATE aws_key_pair resource, named deterministically off the
    # instance's own hcl name (`_ec2`'s builder references this exact same
    # name for `key_name` — see its docstring) — one more one-canvas-node-to
    # -two-tf-resources pass, same shape as sns_subscription/iam_role_policy
    # above.
    for res in ordered:
        if res.kind != "ec2":
            continue
        key = _field(res, "key", "").strip()
        instance_name = hcl_name_by_id.get(res.id)
        if not key or instance_name is None:
            continue
        name = f"{instance_name}_key"
        attrs = {"key_name": quote(f"{res.id}-key"), "public_key": quote(key)}
        block = _block("aws_key_pair", name, attrs)
        blocks.append((("aws_key_pair", res.id), block))

    blocks.sort(key=lambda b: b[0])
    main_tf = "\n\n".join([HEADER, provider_block(), *(text for _, text in blocks)]) + "\n"
    return TfProject(files={"main.tf": main_tf}, unsupported=unsupported)
