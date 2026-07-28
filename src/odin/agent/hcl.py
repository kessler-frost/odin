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

import io
import json
import re
import zipfile

import hcl2
from pydantic import BaseModel

from odin.spec.models import REFERENCEABLE_KINDS, Ref, ResourceDesired, Stack

_REGION = "us-east-1"
_SANITIZE = re.compile(r"[^a-z0-9_]")

# kind -> human reason it can't be simulated yet. Anything not in the map (and
# not one of the supported kinds below) gets a generic fallback reason. Empty
# today: W2.7 moved the last entry (`rds`) onto Terraform, so every kind
# `spec/translate.py` knows about has a builder below. Kept (rather than
# deleted) because it's the per-kind half of the two-level honesty rule
# `generate_tf` implements -- a whole kind with no builder, versus one
# resource a builder declines (see `Built`).
_UNSUPPORTED_REASONS: dict[str, str] = {}

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
    # COVERAGE ONLY -- "odin cannot build this node", and nothing else. A
    # resource odin models but can't generate Terraform for (a kind with no
    # builder, a builder that declined this instance), with the reason. This
    # is what `server.not_covered` unions into the ONE array a CI gate reads,
    # so anything filed here is a claim that odin does not support the user's
    # node. See `wiring_errors` for the thing that is NOT that.
    unsupported: list[str] = []
    # NOT a coverage fact: the node IS built and IS applied, but a `${{...}}`
    # in its `env` names a producer that isn't on this canvas. Field test 5
    # found this sharing `unsupported`, which put a WIRING TYPO into
    # `not_covered` -- the CI gate v0.7.3 documented -- under a coverage
    # label, telling the user odin doesn't support a `lambda` it had just
    # applied successfully. Two different questions ("can odin build this?"
    # vs "did the user wire this correctly?") need two different fields; a
    # gate that answers one with the other is a gate you can pass and still
    # be wrong, which is the exact trap `not_covered` was created to close.
    wiring_errors: list[str] = []
    # V4c: a lambda node's zip'd deployment package -- filename (relative to
    # the tf workspace, e.g. "fn1.zip") -> raw bytes. NEVER text: `files`
    # stays `dict[str, str]` on purpose (every other builder emits HCL, and
    # `materialize()`/`resource_set()` both assume text there) -- a zip gets
    # its OWN dict rather than smuggled through as a decode-on-write string.
    binary_files: dict[str, bytes] = {}


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


def resource_attrs(files: dict[str, str]) -> dict[tuple[str, str], dict]:
    """(type, name) -> parsed attrs, for every resource block across `files`.
    S3b's guardrail keys the skeleton's and the agent's output by this same
    identity for its value-fidelity check (`values_preserved` below);
    `resource_set` is just this dict's keys."""
    return {(rtype, name): attrs for rtype, name, attrs in parse_tf(files)}


def resource_set(files: dict[str, str]) -> frozenset[tuple[str, str]]:
    """The (type, name) identity of every resource block across `files` —
    S3b's guardrail compares this set between the skeleton and the agent's
    refinement; they must be identical (the agent may edit arguments, never
    add/remove a resource)."""
    return frozenset(resource_attrs(files))


# python-hcl2's own synthetic marker on every parsed block -- not a real HCL
# argument, so `values_preserved` skips it rather than demanding the agent
# echo it back.
_BLOCK_MARKER = "__is_block__"


def values_preserved(skeleton_attrs: dict, agent_attrs: dict) -> bool:
    """True iff every argument VALUE the deterministic skeleton set survives,
    unchanged, in `agent_attrs` -- recursively, so a nested block/map (tags,
    an ingress {} rule, a dynamodb attribute {} block) is held to the same
    standard as a top-level argument. The agent MAY add a key the skeleton
    left unset at any level (a new tag, a new top-level argument, a whole new
    nested block) -- this only ever walks the SKELETON's keys, so an
    agent-only addition is invisible to it and never fails the check. This is
    S3b's drift guardrail: the agent's one unique capability (rewriting an
    argument's value, e.g. `instance_type` or a CIDR) is also its one
    liability, and nothing else compares agent output to the canvas — this
    does."""
    if isinstance(skeleton_attrs, dict):
        if not isinstance(agent_attrs, dict):
            return False
        return all(
            key in agent_attrs and values_preserved(value, agent_attrs[key])
            for key, value in skeleton_attrs.items() if key != _BLOCK_MARKER
        )
    return skeleton_attrs == agent_attrs


def _field(res: ResourceDesired, key: str, default: str) -> str:
    """The ONE accessor every builder reads a config field through, and the one
    place its `-> str` is made true.

    A `FieldValue.value` is `Any` on purpose: the canvas is an open document and
    `spec/translate.py`'s boundary check is deliberately permissive about
    `data.*` (only `label` and `env` are type-checked, because only their shapes
    are structural). But ten call sites here then do `.strip()` on the result,
    so a canvas carrying `"allocatedStorage": 20` -- a bare JSON number, which is
    what a hand-written canvas or an importer naturally writes where the config
    panel would have written "20" -- crashed the apply with `'int' object has no
    attribute 'strip'`. That is a 500 for what is at worst a client's harmless
    type choice, and at best exactly what they meant.

    So a scalar is coerced to the text it obviously denotes rather than refused:
    the alternative, type-checking every consumed field at the boundary, would
    reject `20` for `20` and buy nothing. A container (dict/list) is NOT
    coerced -- `str({...})` would emit Python repr into HCL, which is a silent
    wrong answer, and the honest response is the default. Non-scalars reaching
    here at all means a builder is reading a key whose shape it never modelled;
    `_ref_fault` and the boundary check cover the structural cases."""
    fv = res.fields.get(key)
    if fv is None or isinstance(fv.value, (dict, list)) or fv.value is None:
        return default
    return fv.value if isinstance(fv.value, str) else _scalar_text(fv.value)


def _scalar_text(value: object) -> str:
    """`20` -> "20", `True` -> "true" (HCL's spelling, not Python's `True`),
    `20.0` -> "20.0". Booleans are checked first because `bool` IS an `int`."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _block(resource_type: str, name: str, attrs: dict[str, str], nested: str = "") -> str:
    width = max(len(k) for k in attrs)
    lines = [f"  {k.ljust(width)} = {v}" for k, v in attrs.items()]
    if nested:
        lines += ["", nested]
    body = "\n".join(lines)
    return f'resource "{resource_type}" "{name}" {{\n{body}\n}}'


def _tags_block(res: ResourceDesired) -> str:
    """`tags = { <user tags...>, "odin:node" = <label> }` -- stamped on every
    PRIMARY canvas-node-backed resource (never a companion resource -- a key
    pair, an sns->sqs subscription, an inline role policy, a lambda's
    auto-generated execution role, the one shared ecs cluster, or an ecs
    node's task definition -- those aren't canvas nodes themselves).
    The node's own `tags` field (an imported bucket's user tags, finding #6)
    is merged in ahead of `odin:node` so a round-trip preserves them; the
    `odin:node` tag itself is the ONE mechanism two other odin subsystems key
    off: the reconciler's TF-owned-status World projection (vpc/subnet/ec2
    have no other AWS-native field carrying the canvas label back) and the
    gateway's substrate-launch credential issuance (EC2 cloud-init, an ECS
    task container, a Lambda RIE container all resolve which (env, node)
    keystore identity to inject from this same tag) -- see
    reconcile/tf_status.py and gateway/keys.py::workload_env."""
    user = res.fields.get("tags")
    tags = {**(user.value if user is not None and isinstance(user.value, dict) else {}), "odin:node": res.id}
    width = max(len(quote(k)) for k in tags)
    lines = "\n".join(f"    {quote(k).ljust(width)} = {quote(v)}" for k, v in tags.items())
    return f"  tags = {{\n{lines}\n  }}"


# Cross-resource references passed to every builder: resource id -> (kind,
# hcl_name). Fully populated BEFORE any builder runs (see generate_tf pass 1),
# so a subnet/sg can name its containing vpc regardless of sort order.
Refs = dict[str, tuple[str, str]]

# A builder returns (attrs, nested) — or a plain string: the human reason this
# specific resource can't be built (e.g. a subnet drawn outside any VPC),
# routed into `unsupported` exactly like a kind with no builder at all.
Built = tuple[dict[str, str], str] | str

# THE CONTAINMENT FIELDS, and why these messages name them.
#
# `vpc` and `subnet` are ordinary fields in a node's canvas JSON. The UI stamps
# both automatically when you drop a node inside a VPC/Subnet box
# (`ui/src/lib/containment.ts`), so a UI user never types either one and never
# sees these messages -- which means the ONLY readers are the hand-authored,
# `odin import-tf`-generated and CI canvases that a CI gate reads
# `not_covered` for. Telling that reader to "drag it into a VPC box" names a
# gesture they cannot make and sends them looking in the wrong place; the field
# name is the thing they can actually edit. The drag is kept as the parenthesis
# it is, for the UI reader who arrives here some other way.
_NOT_IN_VPC = (
    "vpc is missing or does not name a VPC node on this canvas — set it to the VPC's label "
    "(on the canvas, dropping the node inside a VPC box sets it)"
)

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


# CANVAS WIRING ordering (field test 2, the product hole). A workload node's
# `env` refs (`${{db.DATABASE_URL}}`) are delivered into the REAL container at
# LAUNCH TIME, by `gateway/wiring.py`, deliberately NOT interpolated into the
# generated HCL -- a resolved DATABASE_URL carries the database password, and
# putting it in `container_definitions`/`environment` would write it into
# `terraform.tfstate` in plaintext AND drift on every plan (the resolved value
# embeds a Docker-assigned host port). See gateway/wiring.py for the full
# rationale.
#
# The cost of that choice is the one thing an interpolated value would have
# given for free: ORDERING. With no reference in the HCL, tofu is free to create
# the service in parallel with the database, and the task would launch before
# any endpoint exists. `depends_on` buys exactly the ordering back and carries
# NO VALUES -- a real, portable Terraform argument -- so the producer is fully
# created (and, for rds/elasticache, `available`) before the consumer exists.
_WIRED_KINDS = ("ecs", "lambda")


def _ref_dependencies(res: ResourceDesired, refs: Refs) -> list[str]:
    """`[<tf type>.<hcl name>, ...]` for every DISTINCT node this resource's
    `${{...}}` refs point at, sorted for determinism. A ref whose target isn't a
    buildable canvas resource contributes nothing here and is reported by
    `_unwired_refs` instead of silently vanishing."""
    addresses = []
    for target_id in sorted({ref.target_id for ref in res.refs}):
        kind, name = refs.get(target_id, ("", ""))
        tf_type = _TF_TYPES.get(kind)
        if tf_type is not None:
            addresses.append(f"{tf_type}.{name}")
    return addresses


def _depends_on_block(res: ResourceDesired, refs: Refs, extra: list[str] | None = None) -> str:
    addresses = _ref_dependencies(res, refs) + list(extra or [])
    # de-duplicated and ordered, so a host that is ALSO a ref target appears once
    return f"  depends_on = [{', '.join(sorted(set(addresses)))}]" if addresses else ""


def _placement_dependency(res: ResourceDesired, refs: Refs) -> list[str]:
    """The EC2 instance a placed workload must not start before.

    One of the four costs `docs/intelligence-layer.md` named when placement was
    designed: the instance's VM has to exist before a task can be scheduled into
    it, and nothing sequenced an ecs node behind its ec2 node.

    `depends_on` is the honest fix rather than a wait loop: tofu already owns
    ordering here, the dependency is real (the container literally cannot launch
    into a VM that is not up), and it shows up in `tofu plan` rather than being
    an invisible sleep somewhere in the gateway.

    Silent when the host does not resolve to an ec2 node on this canvas -- an
    unresolvable placement is reported by `_ecs` itself, and inventing a
    dependency on nothing would only produce a worse error later.
    """
    host = _field(res, "host", "").strip()
    if not host:
        return []
    kind, name = refs.get(host, ("", ""))
    return [f"aws_instance.{name}"] if kind == "ec2" else []


def _ref_fault(res: ResourceDesired, ref: Ref, refs: Refs) -> str | None:
    """Why this `${{...}}` ref can NEVER be given a value, or None if it can.

    Two distinct faults, and the second one is field test 6's F3 sub-finding.
    Keyed off what the target actually IS rather than assembled per-branch, so a
    new unreferenceable kind gets the right sentence by default:

      NOT ON THE CANVAS -- a typo, or a node since deleted. `_ref_dependencies`
      silently omits it from `depends_on`, so without this it fails much later.

      ON THE CANVAS BUT NOT A REFERENCE PRODUCER -- `${{queue.QUEUE_URL}}`
      against an sqs node. Only `REFERENCEABLE_KINDS` publish wiring values
      (`gateway/wiring.py::producer_facts`), and the reason a user needs is NOT
      "that node publishes no facts": `odin world` shows an sqs node's
      `QUEUE_URL` the whole time (measured -- see `REFERENCEABLE_KINDS`). It
      publishes an OBSERVED fact, which is a different system from wiring, and
      the message says which kinds the wiring one covers plus the thing that
      actually works for these four kinds (`AWS_ENDPOINT_URL` + the resource
      name, both of which the workload already has -- `gateway/keys.py::
      workload_env`).

    WHERE THIS GOES, and why it is not `unsupported` (field test 5). These land
    in `TfProject.wiring_errors`, never in `unsupported`, because they are a
    USER ERROR on a node odin supports perfectly well -- the resource is built,
    applied and covered. Filing them under coverage is what put
    `worker (lambda): env ref ${{ghost.ENDPOINT}} names 'ghost'...` into
    `not_covered` for an applied lambda, so a wiring typo failed
    `jq -e '.not_covered | length == 0'` while telling the user odin doesn't
    support their node.

    IT IS THE SAME ERROR THE GATEWAY ALREADY FAILS ON, CAUGHT EARLIER -- not a
    third story. Neither fault can resolve at launch either, so
    `gateway/wiring.py::_resolve` raises `UnresolvedRef` for both regardless,
    which `ecsctl`/`lambdactl` turn into a task STOPPED / `State: Failed` with
    that reason, a `crashed` node, and a FAILED apply. This check reaches the
    identical verdict from static canvas data, before any container is
    launched. So a `wiring_errors` entry is never merely advisory: it names a
    workload that WILL fail."""
    kind = refs.get(ref.target_id, ("", ""))[0]
    target = "${{" + f"{ref.target_id}.{ref.target_attr}" + "}}"
    absent = (
        f"names {ref.target_id!r}, which is not a resource on this canvas"
        if _TF_TYPES.get(kind) is None else ""
    )
    unreferenceable = (
        f"names {ref.target_id!r} (kind: {kind}) — and no {kind} node publishes an endpoint a "
        f"reference can resolve. Only {_join_kinds(REFERENCEABLE_KINDS)} nodes do (`odin world` "
        f"may well show {ref.target_id!r} with facts of its own — those are OBSERVED state, not "
        f"wiring values). To reach a {kind} resource from this workload, use its name "
        f"({ref.target_id!r}) with the AWS SDK: odin already injects AWS_ENDPOINT_URL and this "
        f"node's own credentials into the container"
        if not absent and kind not in REFERENCEABLE_KINDS else ""
    )
    reason = absent or unreferenceable
    return (
        f"{res.id} ({res.kind}): env ref {target} {reason} — the variable {ref.var} will NOT "
        f"be set and the workload will fail to start"
    ) if reason else None


def _join_kinds(kinds: tuple[str, ...]) -> str:
    return f"{', '.join(kinds[:-1])} and {kinds[-1]}"


def _unwired_refs(res: ResourceDesired, refs: Refs) -> list[str]:
    """Human reasons for every ref this canvas CANNOT wire -- see `_ref_fault`
    for the two faults and why they are `wiring_errors` rather than
    `unsupported`."""
    return [fault for ref in res.refs for fault in [_ref_fault(res, ref, refs)] if fault]


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
    `protocol:port:source` (e.g. `tcp:443:0.0.0.0/0`). Returns (protocol, port,
    source) triples, or None when any non-empty line doesn't fit the format."""
    lines = [line.strip() for line in _field(res, "ingressRules", "").splitlines()]
    parsed = [tuple(line.split(":")) for line in lines if line]
    ok = all(len(p) == 3 and p[1].isdigit() for p in parsed)
    return parsed if ok else None


def _ingress_source(source: str, res: ResourceDesired, refs: Refs) -> str | None:
    """The `source` third of an ingress rule, as an HCL argument line.

    A CIDR (anything with a `/`) stays `cidr_blocks`. ANYTHING ELSE is read as
    another SG NODE's canvas label and becomes `security_groups` -- the
    AWS-idiomatic "only the web tier may reach the database" rule (W2.6:
    `sg_rules_to_firewall` compiles a UserIdGroupPairs rule to a nebula
    `group:` rule, which nebula matches against the PEER's certificate groups,
    so this is the one source form that gates by IDENTITY rather than by
    address -- and overlay addresses are not VPC addresses, so a VPC-CIDR rule
    could never gate mesh traffic anyway). None = unresolvable, which
    `_sg` turns into a human reason."""
    if "/" in source:
        return f"    cidr_blocks = [{quote(source)}]"
    if source == res.id:
        return None  # a self-reference needs TF's `self = true`; not modeled yet
    kind, name = refs.get(source, ("", ""))
    return f"    security_groups = [aws_security_group.{name}.id]" if kind == "sg" else None


def _s3(res: ResourceDesired, refs: Refs) -> Built:
    # force_destroy: `tofu destroy` empties the bucket before deleting it, so a
    # bucket with objects tears down cleanly (field-test finding #4) instead of
    # erroring BucketNotEmpty -- odin's "empty canvas = full destroy" story
    # depends on it, and local dev buckets are ephemeral.
    return {"bucket": quote(res.id), "force_destroy": "true"}, ""


def _sqs(res: ResourceDesired, refs: Refs) -> Built:
    return {"name": quote(res.id)}, ""


def _sns(res: ResourceDesired, refs: Refs) -> Built:
    return {"name": quote(res.id)}, ""


def _attribute_block(name: str, attr_type: str) -> str:
    return f"  attribute {{\n    name = {quote(name)}\n    type = {quote(attr_type)}\n  }}"


def _dynamodb(res: ResourceDesired, refs: Refs) -> Built:
    hash_key = _field(res, "hashKey", "id")
    attrs = {
        "name": quote(res.id),
        "billing_mode": quote("PAY_PER_REQUEST"),
        "hash_key": quote(hash_key),
    }
    blocks = [_attribute_block(hash_key, _field(res, "hashKeyType", "S"))]
    # A composite (hash + range) key: emit range_key + its own attribute block,
    # so an imported hash+range table round-trips instead of collapsing to
    # hash-only -- a real schema change (field-test finding #6).
    range_key = _field(res, "rangeKey", "")
    if range_key:
        attrs["range_key"] = quote(range_key)
        blocks.append(_attribute_block(range_key, _field(res, "rangeKeyType", "S")))
    return attrs, "\n\n".join(blocks)


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
        return ('ingressRules: expected one "protocol:port:source" rule per line, '
                "e.g. tcp:443:0.0.0.0/0")
    sources = [_ingress_source(source, res, refs) for _protocol, _port, source in rules]
    if None in sources:
        bad = [r[2] for r, s in zip(rules, sources, strict=True) if s is None]
        return (
            f"ingressRules: source {bad[0]!r} is neither a CIDR (like 10.0.0.0/16) "
            "nor the label of another Security Group node on this canvas"
        )
    ingress = [
        (
            "  ingress {\n"
            f"    from_port   = {port}\n"
            f"    to_port     = {port}\n"
            f"    protocol    = {quote(protocol)}\n"
            f"{source}\n"
            "  }"
        )
        for (protocol, port, _source), source in zip(rules, sources, strict=True)
    ]
    return {"name": quote(res.id), "vpc_id": vpc_id}, "\n\n".join([*ingress, _DEFAULT_EGRESS])


# V3c: EC2 instances (real Lima VMs, gateway/models/ec2compute.py). Matches
# that module's own stub-catalog default (documentation only — ImageId is
# accepted verbatim, never validated) and default instance type.
_NOT_IN_SUBNET = (  # see `_NOT_IN_VPC` above for why this names the field
    "subnet is missing or does not name a Subnet node on this canvas — set it to the Subnet's "
    "label (on the canvas, dropping the node inside a Subnet box sets it)"
)
_DEFAULT_AMI = "ami-0c101f26f147fa7fd"
_DEFAULT_INSTANCE_TYPE = "t3.micro"


def _bad_security_group(label: str) -> str:
    """Names the OFFENDING LINE, not just the field: `securityGroups` holds one
    label per line, so "it names something wrong" leaves a multi-line field's
    reader to diff it by eye."""
    return (
        f"securityGroups line {label!r} is not the label of a Security Group node on this canvas "
        "(the field holds one Security Group label per line)"
    )


def _security_group_refs(res: ResourceDesired, refs: Refs) -> list[str] | str:
    """The ec2 node's `securityGroups` field: one sg canvas label per line
    (SIMPLEST honest v1 — see the V3 brief: no implicit "same containment
    scope" placement, just an explicit list). Returns `aws_security_group.
    <name>.id` refs, `[]` for an empty field (the instance just gets the
    VPC's default SG, a legitimate case), or -- the `_alb_ports` idiom -- the
    human reason a line can't resolve."""
    lines = [line.strip() for line in _field(res, "securityGroups", "").splitlines() if line.strip()]
    resolved = []
    for label in lines:
        kind, name = refs.get(label, ("", ""))
        if kind != "sg":
            return _bad_security_group(label)
        resolved.append(f"aws_security_group.{name}.id")
    return resolved


def _ec2(res: ResourceDesired, refs: Refs) -> Built:
    subnet_id = _subnet_ref(res, refs)
    if subnet_id is None:
        return _NOT_IN_SUBNET
    sg_ids = _security_group_refs(res, refs)
    if isinstance(sg_ids, str):
        return sg_ids
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


# V4c: Lambda functions (real RIE containers, gateway/models/lambdactl.py).
# odin materializes the zip itself, pre-tofu, into the workspace -- NOT a
# `data archive_file` block -- and references it by filename +
# filebase64sha256(), the simplest honest path per the brief (no dependency
# on the TF archive provider, no extra apply-time step). The zip's single
# entry filename + default handler both key off `runtime` (python vs
# node's different module/file conventions); the zip pass below (generate_tf)
# must derive the SAME entry filename this builder assumes.
_LAMBDA_RUNTIME_ENTRY: dict[str, tuple[str, str]] = {
    "python3.12": ("lambda_function.py", "lambda_function.lambda_handler"),
    "python3.13": ("lambda_function.py", "lambda_function.lambda_handler"),
    "nodejs20.x": ("index.js", "index.handler"),
    "nodejs22.x": ("index.js", "index.handler"),
}
_DEFAULT_LAMBDA_RUNTIME = "python3.12"
# The V4d integration test's own proof payload ("return event") IS this
# default -- an empty code textarea still gets a real, working function.
_DEFAULT_LAMBDA_CODE = "def lambda_handler(event, context):\n    return event\n"
_BAD_ROLE_REF = "role names something that isn't an IAM Role on the canvas"

# Field-test 2, finding HIGH-4: the deployment zip must be a pure function of
# the code, because `source_code_hash = filebase64sha256(<zip>)` hashes the
# ARCHIVE, not the source. `ZipFile.writestr(name, data)` stamps the CURRENT
# WALL CLOCK into the entry, so two translates of an unchanged canvas produced
# different bytes -> a different hash -> `Plan: 0 to add, 1 to change` forever,
# a function redeploy on every Apply, and `tofu plan -detailed-exitcode`
# useless as a drift check for any canvas with a Lambda. An explicit `ZipInfo`
# pins every host-dependent field instead: the ZIP epoch (the earliest
# timestamp the DOS format can express -- what reproducible-build tooling
# uses), 0644 permissions, and the unix create_system, so nothing about WHEN or
# WHERE the translate ran leaks into the archive. Member ORDER is stable too:
# v1 taskdefs/packages are single-entry, and the one entry's name is derived
# from `_lambda_entry` rather than a directory walk.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644  # regular file, rw-r--r--
_ZIP_UNIX = 3  # ZipInfo.create_system


def _deterministic_zip(entry_filename: str, code: str) -> bytes:
    info = zipfile.ZipInfo(entry_filename, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = _ZIP_FILE_MODE << 16
    info.create_system = _ZIP_UNIX
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(info, code)
    return buf.getvalue()


def _lambda_entry(runtime: str) -> tuple[str, str]:
    return _LAMBDA_RUNTIME_ENTRY.get(runtime, _LAMBDA_RUNTIME_ENTRY[_DEFAULT_LAMBDA_RUNTIME])


def _lambda_role_key(lambda_id: str) -> str:
    """A synthetic `refs` key (never a real canvas id -- see the module's
    Refs type) for a lambda's AUTO-GENERATED execution role, reserved in
    generate_tf's pass 1 so this builder can already reference its HCL name
    before the companion `aws_iam_role` block itself is ever built."""
    return f"__lambda_role__{lambda_id}"


def _lambda(res: ResourceDesired, refs: Refs) -> Built:
    role_label = _field(res, "role", "").strip()
    if role_label:
        kind, role_name = refs.get(role_label, ("", ""))
        if kind != "iam_role":
            return _BAD_ROLE_REF
    else:
        # No role drawn on the canvas: the DX magic -- draw a lambda, paste
        # code, Apply -- an execution role is auto-generated (pass 1 below
        # reserved its name; the companion pass after pass 2 builds it).
        _, role_name = refs[_lambda_role_key(res.id)]
    runtime = _field(res, "runtime", _DEFAULT_LAMBDA_RUNTIME)
    _, own_name = refs[res.id]
    zip_name = quote(f"{own_name}.zip")
    attrs = {
        "function_name": quote(res.id),
        "role": f"aws_iam_role.{role_name}.arn",
        "handler": quote(_field(res, "handler", _lambda_entry(runtime)[1])),
        "runtime": quote(runtime),
        "filename": zip_name,
        "source_code_hash": f"filebase64sha256({zip_name})",
    }
    # No `environment` block: the node's env map is injected at container launch
    # (`gateway/wiring.py`); this only orders the producers ahead of it.
    return attrs, _depends_on_block(res, refs)


# V5c: ECS services (real per-task Colima containers, gateway/models/
# ecsctl.py). "The drawn node IS the service+taskdef pair" (the brief's own
# words) -- v1 single-container taskdefs, so one ecs canvas node emits BOTH
# an `aws_ecs_service` (this builder's primary resource, _TF_TYPES below)
# AND a companion `aws_ecs_task_definition` (the zip/aws_key_pair/aws_iam_role
# pattern: one canvas node -> more than one TF resource, built in its own
# pass after pass 2). Every ecs node on the canvas shares ONE
# `aws_ecs_cluster`, reserved the first time pass 1 sees an ecs node --
# exactly the `_lambda_role_key` auto-role reservation technique, just keyed
# by a single constant instead of per-node.
#
# Containment/networking: deliberately launch_type="EC2" + networkMode
# "bridge" (ecsctl.py's own default), NOT FARGATE/awsvpc -- the LEAST-FICTION
# choice research-coverage.md's V5 brief calls for. `network_configuration`
# is only meaningful for FARGATE or awsvpc-mode EC2 tasks; bridge-mode EC2
# tasks need none at all, so an ecs node needs no vpc/subnet containment
# (unlike `_ec2`'s hard subnet requirement) -- odin has no ENI/awsvpc model
# to stand behind that block anyway.
_ECS_CLUSTER_KEY = "__ecs_cluster__"
_DEFAULT_ECS_IMAGE = "nginx:alpine"
_DEFAULT_ECS_COUNT = "1"
_DEFAULT_ECS_PORT = "80"
_BAD_ECS_COUNT = "count must be a whole number (e.g. 2)"
_BAD_ECS_PORT = "port must be a whole number (e.g. 80)"
# field-test finding #3: apply must WAIT for the service to actually converge
# and FAIL (bounded by this timeout) if the tasks can't reach RUNNING -- a bad
# image / crash-on-start otherwise made apply silently "succeed" with a service
# that never runs, the failure never surfaced. Bounded so "never converges"
# becomes a fast, honest apply failure instead of the provider's long default
# stabilization wait. A genuinely slow first image pull that exceeds this fails
# apply too (retry re-uses the now-cached image) -- an accepted local-dev
# trade-off for never silently shipping a broken service.
_ECS_CONVERGE_TIMEOUT = "60s"

# Field test 3: the rolling-update contract, emitted EXPLICITLY rather than
# left to the provider's schema defaults. `minimum_healthy_percent = 100` is
# what odin's own ECS scheduler reads (gateway/models/ecsctl.py's
# `_serving_floor` / `_retire_stale`) to keep the PREVIOUS revision's tasks
# serving while a new one comes up -- without it, a typo'd image tag took a
# healthy 3-task service to zero tasks in ~4 seconds and left it there. The
# 200% ceiling is the surge headroom that makes that possible (a 3-task
# service may run 6 tasks mid-rollout). Same values the provider defaults to,
# so this changes no plan; written down because the behavior is now
# load-bearing and a silent default is a bad place for it.
_ECS_MIN_HEALTHY_PERCENT = "100"
_ECS_MAX_PERCENT = "200"


def _ecs(res: ResourceDesired, refs: Refs) -> Built:
    count = _field(res, "count", _DEFAULT_ECS_COUNT)
    if not count.isdigit():
        return _BAD_ECS_COUNT
    port = _field(res, "port", _DEFAULT_ECS_PORT)
    if not port.isdigit():
        return _BAD_ECS_PORT
    _, cluster_name = refs[_ECS_CLUSTER_KEY]
    _, own_name = refs[res.id]
    attrs = {
        "name": quote(res.id),
        "cluster": f"aws_ecs_cluster.{cluster_name}.id",
        "task_definition": f"aws_ecs_task_definition.{own_name}_taskdef.arn",
        "desired_count": count,
        "launch_type": quote("EC2"),
        "wait_for_steady_state": "true",
        "deployment_minimum_healthy_percent": _ECS_MIN_HEALTHY_PERCENT,
        "deployment_maximum_percent": _ECS_MAX_PERCENT,
    }
    blocks = [
        "  timeouts {\n"
        f'    create = {quote(_ECS_CONVERGE_TIMEOUT)}\n'
        f'    update = {quote(_ECS_CONVERGE_TIMEOUT)}\n'
        f'    delete = {quote(_ECS_CONVERGE_TIMEOUT)}\n'
        "  }"
    ]
    # PLACEMENT: this service was drawn INSIDE an ec2 node, so its tasks belong
    # on that instance rather than on the shared host (the owner's
    # "ecs inside the ec2 box means ecs ON ec2" gesture, stamped as `host` by
    # `ui/src/lib/containment.ts`).
    #
    # Expressed as a real `placement_constraints { type = "memberOf" }`, which is
    # how AWS itself pins a task to instances, rather than as an odin-only field:
    # it shows up in `tofu plan`, it round-trips through the provider, and the
    # gateway reads it back out of CreateService. The attribute name is odin's
    # own (`odin.instance`), the way a real cluster uses custom instance
    # attributes.
    #
    # NOT a launch-type switch: odin already emits `launch_type = "EC2"`
    # unconditionally and has no Fargate substrate at all, so flipping that
    # label would claim a distinction odin cannot back. Placement is the part
    # that is real -- an EC2 node IS a Lima VM and the task can genuinely run
    # inside it.
    host = _field(res, "host", "").strip()
    if host:
        blocks.append(
            "  placement_constraints {\n"
            f'    type       = {quote("memberOf")}\n'
            f'    expression = {quote(f"attribute:odin.instance == {host}")}\n'
            "  }"
        )
    # Canvas wiring: order every `${{producer.ATTR}}` target ahead of this
    # service, so its tasks never launch before the endpoint they consume
    # exists. The VALUES arrive at container launch, not through the HCL --
    # `_WIRED_KINDS` above.
    depends_on = _depends_on_block(res, refs, _placement_dependency(res, refs))
    if depends_on:
        blocks.append(depends_on)
    # W2.5: an `alb` node edged to this service fronts it -- which in real AWS
    # is a `load_balancer` block on the SERVICE (the ECS scheduler then
    # registers each task with that target group), not a
    # `aws_lb_target_group_attachment` tofu would have to know the tasks for.
    # `refs` carries the target group's HCL name under a synthetic key that
    # pass 1.5 reserved (`_alb_target_key`) -- the same technique the lambda
    # auto-role and the shared ecs cluster already use.
    kind, tg_name = refs.get(_alb_target_key(res.id), ("", ""))
    if kind == "alb_target_group":
        blocks.append(
            "  load_balancer {\n"
            f"    target_group_arn = aws_lb_target_group.{tg_name}.arn\n"
            f"    container_name   = {quote(res.id)}\n"
            f"    container_port   = {port}\n"
            "  }"
        )
    return attrs, "\n\n".join(blocks)


# W2.5: ALB (gateway/models/elbv2ctl.py + a REAL nginx container per load
# balancer, compute/proxy.py). ONE `alb` canvas node expands to THREE tf
# resources -- `aws_lb` (this builder's primary, `_TF_TYPES` below) plus a
# companion `aws_lb_target_group` and `aws_lb_listener`, built in their own
# pass after pass 2 exactly like ecs's task definition and lambda's auto-role.
#
# Containment: an alb node must be drawn inside a Subnet (like `_ec2`) -- the
# subnet is what gives `aws_lb.subnets` a value and, transitively, the target
# group its `vpc_id` (the canvas stamps BOTH `vpc` and `subnet` on a leaf
# inside a subnet, `ui/src/lib/containment.ts`).
#
# `internal = true` is emitted explicitly: odin has no internet gateway, so an
# internet-facing scheme would be a claim nothing backs. Only `application`
# type is modeled (an NLB would need nginx's stream module and a TCP-only
# proxy shape) -- a `network` node reports itself unsupported rather than
# quietly getting an ALB.
_ALB_TYPE_APPLICATION = "application"
_DEFAULT_ALB_LISTENER_PORT = "80"
_DEFAULT_ALB_TARGET_PORT = "80"
_DEFAULT_ALB_HEALTH_CHECK_PATH = "/"
_BAD_ALB_LISTENER_PORT = "listenerPort must be a whole number (e.g. 80)"
_BAD_ALB_TARGET_PORT = "port must be a whole number (e.g. 80)"
_ALB_NLB_UNSUPPORTED = (
    "lbType 'network' is not supported in Simulate v1 "
    "(the real substrate is an HTTP reverse proxy) — set lbType to 'application'"
)  # `Type` is the UI's label for it; `lbType` is the field in the canvas JSON


def _alb_target_key(target_id: str) -> str:
    """A synthetic `refs` key (never a real canvas id) meaning "this compute
    node is a target of some alb's target group". Reserved by pass 1.5, read by
    `_ecs`."""
    return f"__alb_target__{target_id}"


def _alb_ports(res: ResourceDesired) -> tuple[str, str] | str:
    listener_port = _field(res, "listenerPort", _DEFAULT_ALB_LISTENER_PORT)
    if not listener_port.isdigit():
        return _BAD_ALB_LISTENER_PORT
    target_port = _field(res, "port", _DEFAULT_ALB_TARGET_PORT)
    if not target_port.isdigit():
        return _BAD_ALB_TARGET_PORT
    return listener_port, target_port


def _alb(res: ResourceDesired, refs: Refs) -> Built:
    """The PRIMARY `aws_lb` block. Gating both the subnet AND the vpc reference
    here is deliberate: the companion target group (built in its own pass
    below) needs `vpc_id`, and a node that gets past this builder is guaranteed
    to have both, so that pass never has to re-report a containment problem.

    They are gated SEPARATELY because they are two different fields. One
    `if subnet is None or vpc is None: return _NOT_IN_SUBNET` reported the
    subnet for both, so an alb carrying `subnet` and missing `vpc` was told to
    "drag it into a Subnet box" -- a fix it had already applied, for a field
    that was already correct. A message confidently wrong about the cause is
    worse than no message: it sends the reader to the wrong field.
    """
    if _field(res, "lbType", _ALB_TYPE_APPLICATION) != _ALB_TYPE_APPLICATION:
        return _ALB_NLB_UNSUPPORTED
    subnet_id = _subnet_ref(res, refs)
    if subnet_id is None:
        return _NOT_IN_SUBNET
    if _vpc_ref(res, refs) is None:
        return _NOT_IN_VPC
    ports = _alb_ports(res)
    if isinstance(ports, str):
        return ports
    attrs = {
        "name": quote(res.id),
        "internal": "true",
        "load_balancer_type": quote(_ALB_TYPE_APPLICATION),
    }
    return attrs, f"  subnets = [{subnet_id}]"


# W2.8: ElastiCache (redis) clusters -- a REAL `redis:7-alpine` container per
# cluster (gateway/models/cachectl.py + aws/cache.py). SINGLE NODE in v1
# (`num_cache_nodes = 1`, hardcoded rather than offered as a canvas field that
# would only ever be rejected -- cachectl.py returns a real
# InvalidParameterValue for anything else, and ROADMAP records the limit).
#
# Deliberately NOT emitted: `port` and `engine_version`. Both are
# Optional+Computed on `aws_elasticache_cluster`, and the REAL published host
# port is whatever Docker picked -- pinning `port = 6379` in the config while
# the API honestly reports the published port is a guaranteed plan diff on
# every apply. Left computed, they read back from the API and plan clean.
_DEFAULT_CACHE_NODE_TYPE = "cache.t3.micro"


def _elasticache(res: ResourceDesired, refs: Refs) -> Built:
    return {
        "cluster_id": quote(res.id),
        "engine": quote("redis"),
        "node_type": quote(_field(res, "nodeType", _DEFAULT_CACHE_NODE_TYPE)),
        "num_cache_nodes": "1",
    }, ""


def _ecs_container_definitions(res: ResourceDesired) -> list[dict]:
    port = _field(res, "port", _DEFAULT_ECS_PORT)
    port_int = int(port) if port.isdigit() else int(_DEFAULT_ECS_PORT)
    return [{
        "name": res.id,
        "image": _field(res, "image", _DEFAULT_ECS_IMAGE),
        "essential": True,
        "portMappings": [{"containerPort": port_int, "hostPort": 0, "protocol": "tcp"}],
    }]


# W2.1: CloudWatch log groups (gateway/models/logsctl.py -- odin's one log
# SINK, control plane + data plane, no backing container). The canvas label IS
# the log group name, deliberately: `classify.py`'s `_classify_logs` reports
# the bare group name as the IAM resource, so a `logs:PutLogEvents` edge drawn
# to this node only enforces correctly while name == label (the same identity
# rule s3's bucket / sqs's queue name already carry).
_BAD_LOGS_RETENTION = "retentionInDays must be a whole number of days (e.g. 14)"


def _logs(res: ResourceDesired, refs: Refs) -> Built:
    # No retention field = no `retention_in_days` argument at all, which is
    # AWS's own "never expire" default -- emitting a made-up number instead
    # would silently expire a user's logs.
    retention = _field(res, "retentionInDays", "").strip()
    if retention and not retention.isdigit():
        return _BAD_LOGS_RETENTION
    attrs = {"name": quote(res.id)}
    if retention:
        attrs["retention_in_days"] = retention
    return attrs, ""


# W2.4: Secrets Manager secrets (gateway/models/secretsctl.py) and SSM
# parameters (gateway/models/ssmctl.py) -- the two kinds whose FIELD IS A
# SECRET. The canvas label IS the secret/parameter name, deliberately, for the
# same reason logs' label is its group name: `classify.py` reports that bare
# name as the IAM resource, so an edge drawn to this node only enforces while
# name == label.
#
# The generated HCL is the ONE place a secret's plaintext legitimately appears
# (tofu has to send it), which is why `spec/translate.py` marks these fields
# sensitive: that flag is what keeps the same value out of the translation
# agent's prompt and out of every streamed `tofu` log line
# (spec/models.py::scrub, simulate/runner.py).
#
# `recovery_window_in_days = 0` is emitted always: odin's DeleteSecret is
# immediate (secretsctl.py's deviation 1), and saying so in the HCL keeps the
# generated project honest -- applied against real AWS it would behave the same
# way odin does, instead of scheduling a 30-day window odin doesn't have.
_SECRET_RECOVERY_WINDOW = "0"
# Kept in lock-step with gateway/models/ssmctl.py's VALID_TYPES (imported
# nowhere: the deterministic translator stays independent of the gateway).
_SSM_TYPES = ("String", "StringList", "SecureString")
# There is no `Value` field and no `type` field. `Value`/`Type` are the UI's
# labels; the canvas JSON carries `paramValue` and `paramType`, and a message
# that names the label sends a CLI reader hunting for a key that does not exist.
_SSM_NEEDS_VALUE = "paramValue is empty — an SSM parameter cannot exist without a value"
_BAD_SSM_TYPE = f"paramType must be one of {', '.join(_SSM_TYPES)}"


def _secret(res: ResourceDesired, refs: Refs) -> Built:
    attrs = {"name": quote(res.id), "recovery_window_in_days": _SECRET_RECOVERY_WINDOW}
    description = _field(res, "description", "")
    if description:
        attrs["description"] = quote(description)
    return attrs, ""


def _ssm(res: ResourceDesired, refs: Refs) -> Built:
    # NOT stripped: leading/trailing whitespace is part of a secret value, so
    # emptiness here is plain falsiness rather than the `.strip()` every other
    # builder's optional-field check uses.
    value = _field(res, "paramValue", "")
    if not value:
        return _SSM_NEEDS_VALUE
    param_type = _field(res, "paramType", "String")
    if param_type not in _SSM_TYPES:
        return _BAD_SSM_TYPE
    attrs = {"name": quote(res.id), "type": quote(param_type), "value": quote(value)}
    description = _field(res, "description", "")
    if description:
        attrs["description"] = quote(description)
    return attrs, ""


# W2.7: RDS instances (a real Postgres container per instance, gateway/models/
# rdsctl.py). Two things make this builder unlike the others:
#
# 1. The canvas label IS the `identifier`, which real RDS constrains harder
#    than any other name odin emits (letters/digits/hyphens, must start with a
#    letter, no trailing or doubled hyphen -- terraform-provider-aws validates
#    it client-side, so a bad one fails at plan time, before the gateway is
#    even reached). Rather than let that surface as a raw provider error, or
#    quietly rename the node behind the user's back (which would break the
#    `${{db.DATABASE_URL}}` ref path AND the container name), the resource is
#    declined with a reason that says exactly what to fix.
# 2. `engine` is honest, not decorative. The substrate is Postgres; a canvas
#    that asks for mysql/mariadb gets declined rather than silently handed a
#    Postgres container (the pre-W2.7 reconciler path did exactly that).
_RDS_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_RDS_ENGINE = "postgres"
_DEFAULT_DB_INSTANCE_CLASS = "db.t3.micro"
_DEFAULT_ALLOCATED_STORAGE = "20"
_DEFAULT_DB_USERNAME = "app"
_DEFAULT_DB_PASSWORD = "apppass123"
_DEFAULT_DB_NAME = "postgres"
_BAD_RDS_IDENTIFIER = (
    "an RDS name must be lowercase letters/digits separated by single hyphens "
    "and start with a letter (e.g. app-db) — the node's name IS the identifier, so change "
    "data.label"
)  # "rename the node" was a gesture; `data.label` is the key that holds the name
_BAD_RDS_STORAGE = "allocatedStorage must be a whole number of GiB (e.g. 20)"


def _rds_engine_unsupported(engine: str) -> str:
    return f"engine {engine!r} has no local substrate — odin runs a real Postgres, so only postgres is supported"


def _rds(res: ResourceDesired, refs: Refs) -> Built:
    if not _RDS_IDENTIFIER.match(res.id):
        return _BAD_RDS_IDENTIFIER
    engine = _field(res, "engine", _RDS_ENGINE)
    if engine != _RDS_ENGINE:
        return _rds_engine_unsupported(engine)
    storage = _field(res, "allocatedStorage", _DEFAULT_ALLOCATED_STORAGE).strip()
    if not storage.isdigit():
        return _BAD_RDS_STORAGE
    # W2.6: the same `securityGroups` field an ec2 node uses, and the same
    # builder -- so the SG a canvas draws for its database travels to the
    # gateway through TERRAFORM (`vpc_security_group_ids`), exactly the way an
    # instance's does, and shows up in `tofu plan`. The gateway then gates the
    # real Postgres container's mesh membership with those groups' compiled
    # firewall (`gateway/models/rdsctl.py::_db_firewall`).
    sg_ids = _security_group_refs(res, refs)
    if isinstance(sg_ids, str):
        return sg_ids
    nested = f"  vpc_security_group_ids = [{', '.join(sg_ids)}]" if sg_ids else ""
    return {
        "identifier": quote(res.id),
        "engine": quote(engine),
        "instance_class": quote(_field(res, "instanceClass", _DEFAULT_DB_INSTANCE_CLASS)),
        "allocated_storage": storage,
        "db_name": quote(_field(res, "dbName", _DEFAULT_DB_NAME)),
        "username": quote(_field(res, "username", _DEFAULT_DB_USERNAME)),
        "password": quote(_field(res, "password", _DEFAULT_DB_PASSWORD)),
        # A final snapshot is meaningless for a local dev database (there is no
        # snapshot surface at all -- gateway/models/rdsctl.py's own limit), and
        # leaving it false makes `tofu destroy` refuse without a
        # `final_snapshot_identifier`: the same spirit as s3's `force_destroy`,
        # and what keeps "empty canvas + Apply = full teardown" true.
        "skip_final_snapshot": "true",
    }, nested


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
    "lambda": "aws_lambda_function",
    "ecs": "aws_ecs_service",
    "logs": "aws_cloudwatch_log_group",
    "secret": "aws_secretsmanager_secret",
    "ssm": "aws_ssm_parameter",
    "elasticache": "aws_elasticache_cluster",
    "rds": "aws_db_instance",
    "alb": "aws_lb",
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
    "lambda": _lambda,
    "ecs": _ecs,
    "logs": _logs,
    "secret": _secret,
    "ssm": _ssm,
    "elasticache": _elasticache,
    "rds": _rds,
    "alb": _alb,
}

# W2.5: which canvas kinds can actually BE an ALB target in v1. An ECS service
# registers its own tasks (real ECS's scheduler behaviour, modeled in
# gateway/models/ecsctl.py); an ec2 instance would need an
# `aws_lb_target_group_attachment` and is recorded as an unbuilt limit instead
# of silently doing nothing with the edge the user drew.
_ALB_TARGET_KINDS = ("ecs",)


def generate_tf(stack: Stack) -> TfProject:
    by_id = {r.id: r for r in stack.resources}
    ordered = sorted(stack.resources, key=lambda r: (r.kind, r.id))
    used_names: dict[str, set[str]] = {}
    hcl_name_by_id: dict[str, str] = {}
    refs: Refs = {}
    blocks: list[tuple[tuple[str, str], str]] = []
    unsupported: list[str] = []
    wiring_errors: list[str] = []

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
        # V4c: a lambda with no explicit `role` field gets its companion
        # auto-role's HCL name reserved HERE (same reasoning as the vpc-
        # before-subnet ordering this pass already exists for) so `_lambda`'s
        # builder, which runs in pass 2, can already reference it.
        if res.kind == "lambda" and not _field(res, "role", "").strip():
            role_name = unique_name(
                sanitize_name(f"{res.id}_role"), used_names.setdefault("aws_iam_role", set()),
            )
            refs[_lambda_role_key(res.id)] = ("iam_role", role_name)
        # V5c: the FIRST ecs node seen reserves the one shared cluster's HCL
        # name -- every later ecs node's `_ecs` builder (pass 2) just reads
        # it back, same reservation technique as the lambda auto-role above.
        if res.kind == "ecs" and _ECS_CLUSTER_KEY not in refs:
            cluster_name = unique_name(sanitize_name("odin"), used_names.setdefault("aws_ecs_cluster", set()))
            refs[_ECS_CLUSTER_KEY] = ("ecs_cluster", cluster_name)

    # Pass 1.5 (W2.5) — resolve ALB TARGET EDGES, which pass 1 can't do (it
    # walks resources, and an edge's other end may not be named yet) and pass 2
    # can't either (a builder only sees `(res, refs)`, never the edge list). A
    # NETWORK edge between an `alb` node and a compute node means "this load
    # balancer fronts that compute": accepted in EITHER drawn direction, since
    # which end the user started from carries no meaning. The result is a
    # synthetic `refs` entry per target, which `_ecs` reads to emit its
    # `load_balancer` block. `alb` deliberately isn't an IAM target on the
    # canvas (see ui/src/lib/iam.ts), so an alb<->compute edge is unambiguously
    # this and nothing else.
    for edge in sorted(stack.edges, key=lambda e: (e.src, e.dst)):
        for alb_id, target_id in ((edge.src, edge.dst), (edge.dst, edge.src)):
            alb_res, target_res = by_id.get(alb_id), by_id.get(target_id)
            if alb_res is None or target_res is None or alb_res.kind != "alb" or alb_id not in hcl_name_by_id:
                continue
            if target_res.kind in _ALB_TARGET_KINDS:
                refs[_alb_target_key(target_id)] = ("alb_target_group", f"{hcl_name_by_id[alb_id]}_tg")
            else:
                unsupported.append(
                    f"{alb_id} (alb): target edge to {target_id} ({target_res.kind}) — only "
                    f"{'/'.join(_ALB_TARGET_KINDS)} nodes can be load-balancer targets in Simulate v1"
                )
            break  # one edge is one (alb, target) pair, whichever way it was drawn

    # Pass 2 — build blocks with the name table complete. A builder may still
    # opt out for THIS resource (returns the reason string) — e.g. a subnet
    # not drawn inside any VPC — which lands in `unsupported`, never dropped.
    #
    # `built_ids` records what pass 2 ACTUALLY emitted, and it is the only
    # honest gate for a companion pass whose block REFERENCES its primary
    # (`aws_lb_listener.load_balancer_arn`,
    # `aws_secretsmanager_secret_version.secret_id`). Re-deriving the primary
    # builder's opt-out conditions in the companion pass instead was a real bug
    # found in review: an alb with `lbType = "network"`, or one drawn in a VPC
    # but not a Subnet, withheld its `aws_lb` while the companion pass still
    # emitted a listener pointing at `aws_lb.<name>.arn` — an unresolvable
    # reference, which fails `tofu plan` for the WHOLE project and so stops
    # every other resource on the canvas from applying. The companions that do
    # NOT reference their primary (an ecs task definition, a lambda auto-role,
    # an `aws_key_pair`) are deliberately left ungated: without their primary
    # they're merely unused, never invalid.
    built_ids: set[str] = set()
    for res in ordered:
        if res.kind not in _BUILDERS:
            continue
        built = _BUILDERS[res.kind](res, refs)
        if isinstance(built, str):
            unsupported.append(f"{res.id} ({res.kind}): {built}")
            continue
        attrs, nested = built
        nested = "\n\n".join(part for part in (nested, _tags_block(res)) if part)
        block = _block(_TF_TYPES[res.kind], hcl_name_by_id[res.id], attrs, nested)
        blocks.append(((res.kind, res.id), block))
        built_ids.add(res.id)

    # Canvas wiring, the honesty half: a workload node whose `env` references a
    # node that isn't on the canvas can never have that variable set, and the
    # `depends_on` above silently omits it. Say so in the apply response instead
    # (northstar directive 5) -- the resource itself is still built, because the
    # rest of it is perfectly valid. Which is exactly why this is NOT
    # `unsupported`: see `_unwired_refs` and `TfProject.wiring_errors`.
    for res in ordered:
        if res.kind in _WIRED_KINDS and res.id in built_ids:
            wiring_errors.extend(_unwired_refs(res, refs))

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

    # V4c: a lambda's AUTO-GENERATED execution role (no `role` field drawn)
    # -- the companion `aws_iam_role` block for the name pass 1 reserved,
    # using the SAME default Lambda trust policy `_iam_role`'s own builder
    # emits. One more one-canvas-node-to-two-tf-resources pass, same shape
    # as sns_subscription/iam_role_policy/aws_key_pair above.
    for res in ordered:
        if res.kind != "lambda" or _field(res, "role", "").strip():
            continue
        key = _lambda_role_key(res.id)
        if key not in refs:
            continue
        _, role_name = refs[key]
        nested = f"  assume_role_policy = {_LAMBDA_TRUST_POLICY}"
        block = _block("aws_iam_role", role_name, {"name": quote(f"{res.id}-role")}, nested)
        blocks.append((("aws_iam_role", res.id), block))

    # W2.4: a secret node's VALUE becomes a companion
    # `aws_secretsmanager_secret_version` -- the same one-canvas-node-to-two-
    # tf-resources shape as aws_key_pair above, and it's how the AWS provider
    # models it too (the secret is the container, the version holds the bytes).
    # A secret with an empty value emits NO version block at all: an
    # existing-but-valueless secret is a real, legitimate AWS state (put the
    # value in later with `aws secretsmanager put-secret-value`), whereas
    # emitting `secret_string = ""` would assert a value nobody typed.
    for res in ordered:
        if res.kind != "secret" or res.id not in built_ids:  # `built_ids`: see pass 2
            continue
        value = _field(res, "secretString", "")
        secret_name = hcl_name_by_id.get(res.id)
        if not value or secret_name is None:
            continue
        attrs = {
            "secret_id": f"aws_secretsmanager_secret.{secret_name}.id",
            "secret_string": quote(value),
        }
        block = _block("aws_secretsmanager_secret_version", f"{secret_name}_version", attrs)
        blocks.append((("aws_secretsmanager_secret_version", res.id), block))

    # V5c: the one shared `aws_ecs_cluster` -- emitted only if some ecs node
    # actually reserved it in pass 1 (never a dangling resource on a canvas
    # with no ecs nodes at all).
    if _ECS_CLUSTER_KEY in refs:
        _, cluster_name = refs[_ECS_CLUSTER_KEY]
        block = _block("aws_ecs_cluster", cluster_name, {"name": quote("odin")})
        blocks.append((("aws_ecs_cluster", "__cluster__"), block))

    # V5c: each ecs node's companion `aws_ecs_task_definition` -- named
    # deterministically off the node's own hcl name (`_ecs`'s builder
    # references this exact same name for `task_definition`), same
    # one-canvas-node-to-two-tf-resources shape as aws_key_pair/aws_iam_role
    # above. `container_definitions` is a plain JSON STRING literal (not
    # `jsonencode(<native HCL>)`) -- both compile to the identical wire
    # value once TF sends the RegisterTaskDefinition call, and a string
    # literal needs no hand-rolled HCL-native serializer here.
    for res in ordered:
        if res.kind != "ecs":
            continue
        own_name = hcl_name_by_id.get(res.id)
        if own_name is None:
            continue
        container_json = quote(json.dumps(_ecs_container_definitions(res)))
        nested = f"  container_definitions = {container_json}"
        attrs = {
            "family": quote(res.id),
            "requires_compatibilities": '["EC2"]',
            "network_mode": quote("bridge"),
        }
        block = _block("aws_ecs_task_definition", f"{own_name}_taskdef", attrs, nested)
        blocks.append((("aws_ecs_task_definition", res.id), block))

    # W2.5: each alb node's companion `aws_lb_target_group` + `aws_lb_listener`
    # -- named deterministically off the node's own hcl name (`_ecs`'s
    # `load_balancer` block references the `_tg` name; the listener references
    # both). Same one-canvas-node-to-N-tf-resources shape as the ecs task
    # definition above, just twice. `_alb` already guaranteed the subnet+vpc
    # refs and the two port fields parse, so nothing here can fail.
    for res in ordered:
        own_name = hcl_name_by_id.get(res.id)
        # `res.id in built_ids` is the ONLY gate here (see pass 2's note): pass 2
        # emitting the `aws_lb` is exactly the condition under which a listener
        # may reference it, and re-deriving `_alb`'s own opt-out checks instead
        # let a withheld load balancer keep its companions.
        if res.kind != "alb" or own_name is None or res.id not in built_ids:
            continue
        listener_port, target_port = _alb_ports(res)  # pass 2 already proved these parse
        vpc_id = _vpc_ref(res, refs)  # and that this resolves
        target_group = _block(
            "aws_lb_target_group", f"{own_name}_tg",
            {
                "name": quote(f"{res.id}-tg"),
                "port": target_port,
                "protocol": quote("HTTP"),
                "vpc_id": vpc_id,
                "target_type": quote("instance"),
            },
            # Only the canvas-authored knob is emitted; every other health-check
            # member is left for the provider to read back from the gateway,
            # which keeps the config surface (and so the drift surface) minimal.
            "  health_check {\n"
            f"    path = {quote(_field(res, 'healthCheckPath', _DEFAULT_ALB_HEALTH_CHECK_PATH))}\n"
            "  }",
        )
        blocks.append((("aws_lb_target_group", res.id), target_group))
        listener = _block(
            "aws_lb_listener", f"{own_name}_listener",
            {
                "load_balancer_arn": f"aws_lb.{own_name}.arn",
                "port": listener_port,
                "protocol": quote("HTTP"),
            },
            "  default_action {\n"
            '    type             = "forward"\n'
            f"    target_group_arn = aws_lb_target_group.{own_name}_tg.arn\n"
            "  }",
        )
        blocks.append((("aws_lb_listener", res.id), listener))

    blocks.sort(key=lambda b: b[0])
    main_tf = "\n\n".join([HEADER, provider_block(), *(text for _, text in blocks)]) + "\n"

    # V4c: materialize each lambda's pasted code into the zip its own HCL
    # block references by filename -- odin owns this pre-tofu, not a
    # `data archive_file` round-trip (module docstring). The entry filename
    # MUST match `_lambda_entry`'s choice for the SAME runtime, or the
    # deployed zip and the `handler` string would disagree. BYTE-DETERMINISTIC
    # (`_deterministic_zip`): identical code must produce an identical archive,
    # or `source_code_hash` churns on every translate.
    binary_files: dict[str, bytes] = {}
    for res in ordered:
        name = hcl_name_by_id.get(res.id)
        if res.kind != "lambda" or name is None:
            continue
        runtime = _field(res, "runtime", _DEFAULT_LAMBDA_RUNTIME)
        entry_filename, _ = _lambda_entry(runtime)
        code = _field(res, "code", "") or _DEFAULT_LAMBDA_CODE
        binary_files[f"{name}.zip"] = _deterministic_zip(entry_filename, code)

    return TfProject(
        files={"main.tf": main_tf}, unsupported=unsupported,
        wiring_errors=wiring_errors, binary_files=binary_files,
    )
