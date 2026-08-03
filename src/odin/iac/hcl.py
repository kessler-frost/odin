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
from pathlib import Path, PurePosixPath

import hcl2
from pydantic import BaseModel

from odin.spec.models import REFERENCEABLE_KINDS, Ref, ResourceDesired, Stack

_REGION = "us-east-1"
# The account every ARN odin emits is scoped to. Duplicated from
# `aws/backings.py` rather than imported, the same way `_REGION` and `_SSM_TYPES`
# are: the deterministic translator does not depend on the gateway. Prose
# lock-step is what goes stale here, so
# `tests/agent/test_hcl_iam_arns.py::test_the_arn_constants_match_the_gateways`
# fails the build if the two ever disagree.
_ACCOUNT = "000000000000"
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
    # A THIRD question, and the comments above already argue why it needs its own
    # field: not "can odin build this?" (`unsupported`) and not "did you wire it
    # correctly?" (`wiring_errors`), but "does the generated Terraform CARRY it?"
    #
    # A drawn IAM edge is enforced for real -- `gateway/policy.py::compile_policies`
    # builds it from `stack.edges` and the gateway denies a request with no
    # matching grant -- but that happens in odin's gateway, not through Terraform,
    # so nothing about it reaches `main.tf`. Found by field test 7: a canvas
    # granting a lambda `s3:GetObject` on a bucket produced five resources and
    # ZERO mentions of the permission, and importing that file back returned the
    # nodes with no edges and no warning at all.
    #
    # It matters twice: `odin translate > main.tf` handed to real AWS gives that
    # lambda no permissions, and a canvas round-tripped through Terraform loses
    # its entire security posture in silence.
    not_in_terraform: list[str] = []


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
    alone — only a plain quoted-literal string gets unwrapped.

    `json.loads` rather than a slice, because `quote` is `json.dumps` and stripping
    the quotes is only HALF its inverse: it leaves the ESCAPES in place. Found
    importing an ec2 node's `userData` — a shell script came back with a literal
    two-character `\\n` instead of a newline, so it would have run as one line on
    a real VM, and re-emitting doubled the escape (`\\\\n`) so the round trip was
    not even stable. Any field that can hold a newline or a quote had the same
    defect: an ssm parameter's value, a secret's value, an iam policy.

    The slice stays as the fallback for a quoted string that is not valid JSON —
    an HCL expression like `"a" + "b"`, or a Windows path with an invalid escape.
    Those keep exactly their previous behaviour rather than raising."""
    if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
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


# v0.8.14: the CANVAS WIRING, carried in the file at last -- the tag key prefix
# under which a node's `${{producer.ATTR}}` references travel.
#
# THE POINT IS THAT A REFERENCE IS NOT A SECRET; ONLY ITS RESOLVED VALUE IS.
# `${{db.DATABASE_URL}}` names a producer and an attribute. The string it
# resolves to at container launch (`gateway/wiring.py`) carries the database
# password, which is why it is deliberately never interpolated into the HCL --
# it would land in `terraform.tfstate` in plaintext and drift on every plan.
# The reference ITSELF carries no value at all, so writing it down costs
# nothing and buys back the two things the old silence threw away: an import
# can rebuild the wiring, and the `depends_on` odin re-derives FROM those
# refs comes back with it.
#
# WHY THE TAG VALUE CANNOT LEAK A RESOLVED SECRET, precisely:
#   * it is built from `Ref.target_id` and `Ref.target_attr` and nothing else --
#     two structural fields `spec/translate.py::parse_ref` fills from the regex
#     groups of the canvas text, never from a value;
#   * `generate_tf` never calls the resolver. `gateway/wiring.py` runs at
#     container launch, long after this file is written, so no resolved value
#     exists here to leak;
#   * a node's STATIC env entries (`API_TOKEN = "tok-live-..."`, a literal a
#     user may well have typed a secret into) are NOT emitted at all. Only
#     refs are. `tests/agent/test_hcl_wiring_tags.py` pins that with a canvas
#     carrying both, and is the mutation target for this whole mechanism.
#
# WHY `odin:ref:` AND NOT `odin:env:`: a `Ref` reaches the Stack from a node's
# `env` map OR from a top-level `${{...}}` field, and `_resource` merges the two
# without recording which. "env" would be a claim the Stack cannot back.
#
# WHY NOT THE LITERAL `${{...}}` TEXT: measured against OpenTofu 1.12.3 --
# `value = "${{db.DATABASE_URL}}"` is a PARSE error ("Missing key/value
# separator"), which fails the whole project, not just that resource. The
# escaped `"$${{...}}"` does parse, but `$`/`{`/`}` are outside AWS's documented
# tag-value character set (letters, digits, spaces and `_ . : / = + - @`), so it
# would fail on the real Amazon this file is meant to be portable to. The
# unwrapped `producer.attr` form has neither problem and needs no unescaping.
_REF_TAG = "odin:ref:"


def _ref_tags(res: ResourceDesired) -> dict[str, str]:
    """`{"odin:ref:<VAR>": "<producer>.<attr>"}` for every `${{...}}` this node
    carries, ordered by VAR so generate -> import -> generate is byte-stable."""
    return {
        f"{_REF_TAG}{ref.var}": f"{ref.target_id}.{ref.target_attr}"
        for ref in sorted(res.refs, key=lambda ref: ref.var)
    }


def _tags_block(res: ResourceDesired) -> str:
    """`tags = { <user tags...>, <ref tags...>, "odin:node" = <label> }` -- stamped on every
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
    tags = {
        **(user.value if user is not None and isinstance(user.value, dict) else {}),
        **_ref_tags(res),
        "odin:node": res.id,
    }
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
#
# v0.8.14: this is now the default for an sg node with an EMPTY `egressRules`
# field, not the only thing odin can emit. A node that authors outbound rules
# gets those instead, and nothing appends this (`_sg`) — otherwise a restricted
# egress would sit next to an allow-all one and mean nothing.
#
# BUILT BY THE SAME BUILDER an authored rule goes through, so the canvas line
# `-1:0:0.0.0.0/0` produces BYTES IDENTICAL to this default. That is what lets
# an import canonicalize a wide-open egress either way — as an empty field or
# as that one line — and regenerate the same file. Two hand-kept copies would
# have made "identical" a hope; one builder makes it arithmetic.
#
# v0.8.17 goes one step further and stores it as the LINE rather than as a
# pre-split tuple, so `_default_egress_block` runs it through `parse_sg_rule`
# like any other canvas line. "Spellable in the grammar" was previously a test
# assertion about a tuple; now it is the only way this constant reaches the
# emitter at all.
_DEFAULT_EGRESS_LINE = "-1:0:0.0.0.0/0"


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


# ---------------------------------------------------------------------------
# EDGE-AUTHORED FIELDS (v0.8.15) -- an edge that grants a permission must also
# WIRE the thing the permission is for, or it is a decorative line.
#
# Three passes below read canvas edges to author a value a builder would
# otherwise take from a hand-typed field (or from nothing at all): a log
# group's NAME, an ecs service's IMAGE, and an alb's ec2 TARGETS.
#
# THEY MATCH ON NODE KINDS AND NEVER ON `edge.kind`, and that is not a
# stylistic choice. Every canvas saved before the edge-type registry existed
# carries `kind: "network"` on every edge (`spec/translate.py::
# LEGACY_UNMODELLED`), so a builder that required a new type name would drop
# the resource from the generated HCL for those canvases -- and `tofu destroy`
# the live one on the next Apply. A reconciler test stays green straight
# through that, because `_desired_subs` only ever ADDS. The sns->sqs
# subscription pass and the alb target pass already work this way; these
# follow them.
#
# DIRECTION IS NOT SIGNIFICANT either, for the reason `spec/translate.py::
# _merge_sg_edges` gives: which end the user started the drag from carries no
# meaning, so both orders are read the same way rather than one silently doing
# nothing.
def _kind_pair_edges(
    stack: Stack, by_id: dict[str, ResourceDesired], left: tuple[str, ...], right: tuple[str, ...],
) -> dict[str, list[str]]:
    """`{left-node id: [right-node ids]}` for every edge joining a node whose
    kind is in `left` to one whose kind is in `right`, in EITHER drawn
    direction, sorted and de-duplicated so the generated file never depends on
    edge ordering."""
    out: dict[str, list[str]] = {}
    for edge in sorted(stack.edges, key=lambda e: (e.src, e.dst)):
        matched = [
            (a, b) for a, b in ((edge.src, edge.dst), (edge.dst, edge.src))
            if (by_id.get(a) or _NO_RES).kind in left and (by_id.get(b) or _NO_RES).kind in right
        ]
        for a, b in matched[:1]:
            out.setdefault(a, [])
            out[a] += [b] if b not in out[a] else []
    return out


# A stand-in for "no such node", so `_kind_pair_edges` reads one attribute
# instead of branching on None twice per direction. Its kind matches nothing.
_NO_RES = ResourceDesired(id="", kind="")


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


def _grant_role_ref(res: ResourceDesired, stack: Stack, refs: Refs) -> tuple[str, str] | None:
    """The role a workload's drawn permissions hang on, or None if it has none.

    A policy needs a role, so this is the single gate on whether a granted
    workload gets an `aws_iam_role_policy` at all. Both the pass that reserves
    the policy's name and the pass that emits it call this, so the two cannot
    disagree -- a name reserved but never emitted is a `depends_on` pointing at
    nothing, which fails `tofu plan` for every resource on the canvas.
    """
    if not [e for e in stack.edges if e.kind == "iam" and e.src == res.id and e.perms]:
        return None
    role_ref = refs.get(_workload_role_key(res.id)) or refs.get(_lambda_role_key(res.id))
    if role_ref is not None:
        return role_ref
    # A lambda with a DRAWN role hangs its policy on that role instead.
    drawn = _field(res, "role", "").strip()
    role_ref = refs.get(drawn) if drawn else None
    return role_ref if role_ref is not None and role_ref[0] == "iam_role" else None


def _grant_dependency(res: ResourceDesired, refs: Refs) -> list[str]:
    """The policy this workload must not start before.

    tofu is free to order two resources that merely share a role either way, so
    without this a container could come up, call S3 and be denied a permission
    that was drawn and applied. That failure would look exactly like a wrong
    grant, which is the most expensive kind of bug to chase.
    """
    grants = refs.get(_grants_key(res.id))
    return [f"aws_iam_role_policy.{grants[1]}"] if grants else []


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


# The SG node's TWO rule fields, and the word each one's messages use for the
# far end of a rule. ONE parser and ONE block builder serve both directions --
# `ingress {}` and `egress {}` take the identical arguments in the AWS provider,
# so a second copy would only be a second place to drift.
_SG_RULE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("ingress", "ingressRules", "source"),
    ("egress", "egressRules", "destination"),
)


# `-1` in the PORT position means "all of them", and it is a LITERAL AWS itself
# writes: an ICMP rule is `from_port = -1, to_port = -1` (the type and code,
# both "any"), and that is what the console produces for "allow ping". It was
# declined until v0.8.21, which made an ordinary AWS security group unimportable
# -- MEASURED on the real path: a group with a `tcp:443` rule and an ICMP rule
# imported as `ingressRules = 'tcp:443:0.0.0.0/0'` with a warning blaming "a
# port that is not a literal number", and the regenerated group allowed no ping.
# Narrower than the Terraform you handed it, which is the same correctness bug
# wearing a limit's clothes that port RANGES were until v0.8.17.
#
# ONLY THESE PROTOCOLS, and the restriction is a daemon-down hazard, not
# fussiness. `fabric/nebula.py::_compile_side` elides the port for `icmp` /
# `icmpv6` / `-1` and passes it through verbatim for everything else, and its
# own comment records what a verbatim `-1` does: nebula REFUSES TO START
# ("port appears to be a range but could not be parsed"). So accepting
# `tcp:-1:...` here would take the whole mesh down over one rule. AWS does not
# accept it either -- `-1` ports are only meaningful where there are no ports.
# `tests/agent/test_sg_port_ranges.py` pins the pair BEHAVIOURALLY, by
# compiling each of these through the real `sg_rules_to_firewall` and checking
# no `-1` survives into a nebula port, rather than by comparing two constants
# that could agree while both being wrong.
_ALL_PORTS = "-1"
_ALL_PORTS_PROTOCOLS = frozenset({"icmp", "icmpv6", "-1"})


def _port_span(text: str, protocol: str) -> tuple[str, str] | None:
    """The PORT field of a rule line, as the `(from_port, to_port)` pair AWS's
    model is actually made of. None when the text is not a port span at all.

    `protocol` is here ONLY for the `-1` spelling above, which is legal for
    some protocols and a mesh outage for others -- see `_ALL_PORTS_PROTOCOLS`.
    Everything else in this function ignores it.

    A SINGLE PORT IS THE DEGENERATE RANGE. `443` is `("443", "443")`, which is
    what `_sg_rule_block` already emitted for every rule ever written, so
    extending the grammar changes no existing canvas's bytes -- pinned by
    `tests/agent/test_sg_port_ranges.py::test_a_single_port_emits_exactly_the_
    bytes_it_always_did`.

    `partition` and not `split("-")`, and the `if sep` is the whole point: with
    `split` plus a `high or low` fallback, `8000-` would parse as `8000-8000`
    and a user who typed half a range would get a firewall a thousand ports
    narrower than the one they were editing, silently. A separator with nothing
    after it is a MALFORMED range, not a single port, so it declines here and
    `_rule_reason` names the line.

    A reversed span (`8100-8000`) is declined too: real AWS rejects
    `to_port < from_port` (InvalidParameterValue), and nebula's
    `f"{from_port}-{to_port}"` would emit a range no packet can be in -- a rule
    that looks enforced and matches nothing.
    """
    if text == _ALL_PORTS:
        return (_ALL_PORTS, _ALL_PORTS) if protocol in _ALL_PORTS_PROTOCOLS else None
    low, sep, high = text.partition("-")
    high = high if sep else low
    ordered = low.isdigit() and high.isdigit() and int(low) <= int(high)
    return (low, high) if ordered else None


def parse_sg_rule(line: str) -> tuple[str, str, str, str] | None:
    """One canvas rule line -> `(protocol, from_port, to_port, peer)`, or None
    when the line does not fit the grammar.

    THE GRAMMAR, in one place, because `import_tf.py` writes lines that this
    has to read back. It is `protocol:port:peer` (e.g. `tcp:443:0.0.0.0/0`),
    where `port` is a single port or a `low-high` RANGE (`tcp:8000-8100:
    0.0.0.0/0`). `import_tf._readable_rule` is literally `parse_sg_rule(line)
    is not None`, so the two directions cannot drift apart into a line odin
    emits happily and then cannot parse -- which is exactly the bug that once
    made an IPv6 import delete a whole security group.

    `split(":", 2)` and NOT a bare `split(":")`: AN IPv6 CIDR CONTAINS COLONS.
    Found from the import side by the agent building the inverse of this
    grammar, and it is the worse half of the pair -- a bare split turned
    `tcp:443:2001:db8::/32` into five fields, failed the arity check, and
    declined the WHOLE security group, taking every other (perfectly readable)
    rule on it with it. The peer is the last field by definition, so bounding
    the split is both the fix and what makes a `":".join(...)` writer a true
    inverse. The protocol and port cannot contain a colon, so nothing else
    changes; a line with a fourth field now resolves a peer that no group
    matches and is declined by `_sg_peer` with the reason for THAT, rather
    than on arity.

    THE RANGE SEPARATOR DOES NOT INTERACT WITH THAT FIX, and it was checked
    rather than assumed. `-` is read only out of `parts[1]`, which `split(":",
    2)` has already bounded, so neither an IPv6 CIDR (no `-` in the notation,
    and it lands in `parts[2]` regardless) nor a hyphenated canvas label
    (`web-sg`, also `parts[2]`) can reach it. The `-1` all-protocols spelling
    sits in `parts[0]`; a `-1` in the PORT position is read by `_port_span`,
    which accepts it only for the protocols that HAVE no ports -- so
    `icmp:-1:10.0.0.0/16` (the "allow ping" rule AWS's own console writes) is
    drawable since v0.8.21 and `tcp:-1:...` is still declined, because a
    verbatim `-1` in a nebula port stops the daemon starting.
    """
    parts = line.split(":", 2)
    span = _port_span(parts[1], parts[0]) if len(parts) == 3 else None
    return (parts[0], *span, parts[2]) if span else None


def sg_rule_port(from_port: str, to_port: str) -> str:
    """The inverse of `_port_span`: the PORT field an import writes for a
    `from_port`/`to_port` pair. Equal bounds collapse to the single-port
    spelling, so a group that never had a range imports to the same text it
    always did."""
    return from_port if from_port == to_port else f"{from_port}-{to_port}"


# IPv6, DECLINED WITH THE REAL REASON rather than made authorable.
#
# `fabric/nebula.py::sg_rules_to_firewall` reads `IpRanges` and
# `UserIdGroupPairs` and nothing else, so an `Ipv6Ranges` entry compiles to ZERO
# nebula rules. Emitting `ipv6_cidr_blocks` would therefore hand a user a
# firewall rule that is carried in Terraform, stored by the gateway, visible in
# `tofu plan` -- and enforced by nothing. That is a decorative permission, the
# same class of bug `tests/gateway/test_iam_vocabulary_is_enforceable.py` exists
# to prevent, so the ergonomic hole stays open and the message tells the truth
# about why. Making it real is a nebula change (teach the compiler IPv6), not a
# grammar one; `docs/limits.md` records it as such.
_NO_IPV6 = (
    "odin's security groups are IPv4 only, because the mesh firewall that enforces them "
    "(fabric/nebula.py) compiles IPv4 ranges and group identities and nothing else — an IPv6 rule "
    "would be carried by Terraform and enforced by nothing. Use an IPv4 CIDR, or another Security "
    "Group node's label to gate by identity"
)


def is_ipv6_cidr(peer: str) -> bool:
    """An IPv6 CIDR is the one peer form that is neither an IPv4 CIDR nor a
    label: it carries BOTH a `/` and a `:`, and no canvas label can (a label
    with a colon in it would already be unresolvable as a group).

    PUBLIC because `import_tf._readable_rule` needs it. That function asks "will
    the generator accept this line?", and parsing is only half the answer -- a
    line can parse cleanly here and still decline the whole security group for
    being IPv6. Exporting the real predicate is what keeps the import from
    writing such a line into a canvas."""
    return "/" in peer and ":" in peer


def _sg_peer(peer: str, res: ResourceDesired, refs: Refs) -> tuple[str, str] | None:
    """The third field of a rule (an ingress SOURCE or an egress DESTINATION),
    as an (argument name, HCL value) pair.

    The NAME is returned rather than a finished line because it decides the
    block's `=` alignment: `security_groups` is 15 characters against
    `cidr_blocks`' 11, and this module promises fmt-canonical output. Measured
    on the pre-v0.8.14 emitter, which pasted a finished line in and padded every
    other key to 11 regardless: `tofu fmt -check -diff` (OpenTofu 1.12.3)
    reported a real diff on any group with an identity-form ingress rule --
    exit 3, three lines re-padded per block. The docstring's "so `tofu fmt
    -check` accepts it unmodified" was simply not true for that shape, and no
    test covered it because every fmt test used a CIDR.

    A CIDR (anything with a `/`) stays `cidr_blocks`. ANYTHING ELSE is read as
    another SG NODE's canvas label and becomes `security_groups` -- the
    AWS-idiomatic "only the web tier may reach the database" rule (W2.6:
    `sg_rules_to_firewall` compiles a UserIdGroupPairs rule to a nebula
    `group:` rule, which nebula matches against the PEER's certificate groups,
    so this is the one source form that gates by IDENTITY rather than by
    address -- and overlay addresses are not VPC addresses, so a VPC-CIDR rule
    could never gate mesh traffic anyway). None = unresolvable, which
    `_sg` turns into a human reason."""
    if "/" in peer:
        return ("cidr_blocks", f"[{quote(peer)}]")
    if peer == res.id:
        return None  # a self-reference needs TF's `self = true`; not modeled yet
    kind, name = refs.get(peer, ("", ""))
    return ("security_groups", f"[aws_security_group.{name}.id]") if kind == "sg" else None


def _rule_reason(line: str, field: str, word: str) -> str:
    """The human reason ONE unparseable rule line declines its whole group.

    THREE sentences, because they are three different mistakes. A line whose
    port field carries a `-` was an attempt at a RANGE: the author is editing a
    firewall and needs to see WHICH line and what is wrong with it, not a
    generic format reminder that shows only the single-port example they
    already know. A `-1` port on a protocol that HAS ports is its own mistake
    and got the range message until v0.8.21, which was actively misleading --
    the author wrote a legal AWS spelling on the wrong protocol, and telling
    them "a range is two whole ports" sends them to fix the wrong thing.
    Anything else did not fit the grammar at all, and that message is
    deliberately byte-for-byte what it has always been (pinned by
    `test_hcl_sg_egress.py::test_the_ingress_messages_are_unchanged_word_for_word`)
    -- a grammar extension is no excuse for rewording text that was already
    correct.
    """
    parts = line.split(":", 2)
    if len(parts) == 3 and parts[1] == _ALL_PORTS:
        return (f"{field}: {line!r} uses the all-ports port {_ALL_PORTS!r}, which odin takes only "
                f"for {', '.join(sorted(_ALL_PORTS_PROTOCOLS))} — those are the protocols with no "
                f"ports to name. For {parts[0]!r}, give a real port or range, like "
                "tcp:443:0.0.0.0/0")
    if len(parts) == 3 and "-" in parts[1]:
        return (f"{field}: {line!r} has a malformed port range {parts[1]!r} — a range is two whole "
                "ports, low first, like tcp:8000-8100:0.0.0.0/0")
    return (f'{field}: expected one "protocol:port:{word}" rule per line, '
            "e.g. tcp:443:0.0.0.0/0")


def _sg_rule_blocks(res: ResourceDesired, refs: Refs, block: str, field: str, word: str) -> list[str] | str:
    """Every `ingress {}` / `egress {}` block one rule field produces, or -- the
    `_alb_ports` idiom -- the human reason a line can't be built."""
    lines = [stripped for line in _field(res, field, "").splitlines() if (stripped := line.strip())]
    rules = [parse_sg_rule(line) for line in lines]
    unreadable = [line for line, rule in zip(lines, rules, strict=True) if rule is None]
    if unreadable:
        return _rule_reason(unreadable[0], field, word)
    ipv6 = [peer for _protocol, _from, _to, peer in rules if is_ipv6_cidr(peer)]
    if ipv6:
        return f"{field}: {word} {ipv6[0]!r} is an IPv6 CIDR — {_NO_IPV6}"
    peers = [_sg_peer(peer, res, refs) for _protocol, _from, _to, peer in rules]
    if None in peers:
        bad = [r[3] for r, p in zip(rules, peers, strict=True) if p is None]
        return (
            f"{field}: {word} {bad[0]!r} is neither a CIDR (like 10.0.0.0/16) "
            "nor the label of another Security Group node on this canvas"
        )
    return [
        _sg_rule_block(block, protocol, from_port, to_port, *peer)
        for (protocol, from_port, to_port, _peer), peer in zip(rules, peers, strict=True)
    ]


def _default_egress_block() -> str:
    """AWS's own allow-all egress, through the authored-rule builder AND
    through the line parser — see `_DEFAULT_EGRESS_LINE`."""
    protocol, from_port, to_port, destination = parse_sg_rule(_DEFAULT_EGRESS_LINE)
    return _sg_rule_block(
        "egress", protocol, from_port, to_port, "cidr_blocks", f"[{quote(destination)}]",
    )


def _sg_rule_block(
    block: str, protocol: str, from_port: str, to_port: str, peer_key: str, peer_value: str,
) -> str:
    """One `ingress {}` / `egress {}` block, `=` aligned to its own widest key
    exactly the way `tofu fmt` aligns it (see `_sg_peer`).

    `from_port`/`to_port` arrive as a PAIR rather than as one port written
    twice. They were always two arguments in the emitted HCL; taking them
    separately is what lets `tcp:8000-8100:...` reach the file, and an equal
    pair produces the identical bytes it produced when there was only one."""
    args = {
        "from_port": from_port, "to_port": to_port, "protocol": quote(protocol),
        peer_key: peer_value,
    }
    width = max(len(key) for key in args)
    body = "\n".join(f"    {key.ljust(width)} = {value}" for key, value in args.items())
    return f"  {block} {{\n{body}\n  }}"


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
    built = [_sg_rule_blocks(res, refs, block, field, word) for block, field, word in _SG_RULE_FIELDS]
    declined = [reason for reason in built if isinstance(reason, str)]
    if declined:
        return declined[0]
    ingress, egress = built
    # v0.8.14: `egressRules` authors real outbound rules. The wide-open default
    # is emitted ONLY when the field is empty -- which is every canvas drawn
    # before the field existed, so their generated file is byte-identical to
    # what it was. The default is not a decoration either way: every
    # aws_security_group starts with AWS's seeded allow-all egress and the TF
    # provider REVOKES it when the config omits an egress block, so the empty
    # field has to keep saying "allow everything out" explicitly.
    return {"name": quote(res.id), "vpc_id": vpc_id}, "\n\n".join(
        [*ingress, *(egress or [_default_egress_block()])],
    )


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
    profile = refs.get(_instance_profile_key(res.id))
    if profile is not None:
        nested.append(f"  iam_instance_profile = aws_iam_instance_profile.{profile[1]}.name")
    grant_dep = _depends_on_block(res, refs, _grant_dependency(res, refs))
    if grant_dep:
        nested.append(grant_dep)
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
# WHERE the translate ran leaks into the archive.
#
# v0.8.14 makes the archive MULTI-MEMBER (a function may be a whole directory),
# which puts a second host-dependent input in reach: member ORDER. A directory
# walk's order is the filesystem's, so the members are written in sorted name
# order here and nowhere else -- `sorted()` is the whole of that guarantee, and
# `tests/agent/test_lambda_package.py` zips the same tree twice and compares
# bytes rather than trusting this comment.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644  # regular file, rw-r--r--
_ZIP_UNIX = 3  # ZipInfo.create_system


def _deterministic_zip(members: dict[str, bytes]) -> bytes:
    """`{member name: bytes}` -> a byte-deterministic zip. Same members, same
    bytes, on any host at any time -- see the note above for why every field
    is pinned and why the members are sorted."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = _ZIP_FILE_MODE << 16
            info.create_system = _ZIP_UNIX
            archive.writestr(info, members[name])
    return buf.getvalue()


def _lambda_entry(runtime: str) -> tuple[str, str]:
    return _LAMBDA_RUNTIME_ENTRY.get(runtime, _LAMBDA_RUNTIME_ENTRY[_DEFAULT_LAMBDA_RUNTIME])


# --- v0.8.14: a Lambda may be a whole DIRECTORY, not one pasted file --------
#
# THREE sources, ONE archive builder, and a precedence that is stated rather
# than inferred (`_lambda_package` is the only reader of any of them):
#
#   1. `sourceDir` -- a path to a directory ON THE MACHINE RUNNING ODIN. Its
#      whole tree is packaged, so a function can import its own modules. It is
#      also the DEPENDENCY story, and the only one odin offers: whatever you
#      have installed INTO that directory ships with it, because odin never
#      runs a package manager of its own and never fetches anything at apply
#      time (docs/limits.md says so in those words).
#   2. `files` -- an inline `{relative path: text}` map. Nothing authors this
#      by hand; `iac/import_tf.py` writes it when it recovers a MULTI-FILE
#      deployment zip, so a package that came from Terraform goes back to
#      Terraform byte-identically instead of collapsing to whichever member
#      happened to sort first.
#   3. `code` -- the single pasted file (the v1 shape), written under the
#      runtime's own entry filename. Unchanged, and still the default.
#
# The path is read by the SERVER at translate time, which is the honest place
# for it: `odin apply` posts the canvas and the server builds the zip, so the
# package cannot go missing between the two the way it does when a `.tf` file
# is handed over without the archive beside it (docs/limits.md, "a Lambda's
# CODE needs the whole directory").

# Names never packaged, and the reason is determinism, not tidiness: CPython
# writes `__pycache__/*.pyc` into a source directory the moment anything
# imports from it, and a `.pyc` embeds the source's mtime and size -- so a tree
# that had merely been imported once produced different archive bytes, a
# different `source_code_hash`, and the exact `Plan: 1 to change` churn the
# pinned ZipInfo above exists to prevent. `.venv` is excluded because a
# virtualenv is host-specific by construction (absolute paths in its scripts,
# a symlinked interpreter) and is never what belongs in a package; vendored
# dependencies go directly in the directory (`pip install -t .`), and
# `node_modules` is deliberately NOT excluded because that is exactly where a
# Node function's vendored dependencies live.
_ZIP_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})
_ZIP_SKIP_SUFFIXES = (".pyc", ".pyo")
_ZIP_SKIP_NAMES = frozenset({".DS_Store"})

# AWS's own quota for an UNZIPPED deployment package (250 MB). Checked from
# `stat` while walking, before a single byte is read, so pointing `sourceDir`
# at a home directory by accident is a fast refusal naming the measured size
# rather than an out-of-memory translate.
_MAX_PACKAGE_BYTES = 250 * 1024 * 1024

# The file extensions a HANDLER MODULE may have, keyed by the runtime's own
# entry filename suffix (from `_LAMBDA_RUNTIME_ENTRY`, so this cannot drift
# away from the runtime table).
_MODULE_SUFFIXES = {".py": (".py",), ".js": (".js", ".mjs", ".cjs")}


def _package_paths(root: Path) -> list[Path]:
    """Every file in `root`'s tree that a deployment package carries, sorted.

    Symlinks are skipped rather than followed: a link out of the tree would put
    a file the user never put in their source directory into the archive, and
    the RIE container mounts the EXTRACTED directory, where a link to a host
    path resolves to nothing anyway."""
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        and not (_ZIP_SKIP_DIRS & set(path.relative_to(root).parts))
        and path.suffix not in _ZIP_SKIP_SUFFIXES and path.name not in _ZIP_SKIP_NAMES
    )


def _dir_members(root: Path) -> dict[str, bytes] | str:
    if not root.is_dir():
        return (
            f"sourceDir {str(root)!r} is not a directory on the machine running odin -- "
            "the server reads it at translate time, so it must be a real path THERE"
        )
    paths = _package_paths(root)
    if not paths:
        return f"sourceDir {str(root)!r} holds no files to package"
    total = sum(path.stat().st_size for path in paths)
    if total > _MAX_PACKAGE_BYTES:
        return (
            f"sourceDir {str(root)!r} is {total / 1048576:.1f} MiB unzipped, over the "
            f"{_MAX_PACKAGE_BYTES // 1048576} MiB AWS allows an unzipped deployment package"
        )
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in paths}


def _safe_member(name: object) -> bool:
    """A `files` key that names a file INSIDE the package and nowhere else.
    `..` and a leading `/` both escape the extraction directory
    (`compute/functions.py::extract_code` calls `ZipFile.extractall`), and a
    backslash is a Windows separator zipfile would keep as part of the name."""
    parts = PurePosixPath(name).parts if isinstance(name, str) else ()
    return bool(name) and bool(parts) and ".." not in parts and "\\" not in str(name) and not str(name).startswith("/")


def _inline_members(mapping: dict) -> dict[str, bytes] | str:
    rejected = sorted(
        str(key) for key, value in mapping.items()
        if not _safe_member(key) or not isinstance(value, str)
    )
    if rejected:
        return (
            "files must map a RELATIVE path to that file's text; odin cannot package "
            f"{', '.join(repr(name) for name in rejected)}"
        )
    return {key: value.encode() for key, value in mapping.items()}


def _handler_checked(members: dict[str, bytes], handler: str, entry_filename: str) -> dict[str, bytes] | str:
    """The package, or the reason its own handler cannot possibly load.

    A multi-file package is the first shape where the entry file can simply be
    ABSENT -- the single-textarea path writes it by construction. Absent, the
    function still deploys, its RIE container still answers a TCP connect
    (`compute/functions.py` documents why readiness is a socket probe and not a
    warm-up invoke), and the failure surfaces only when somebody invokes it and
    gets `Runtime.ImportModuleError`. Reading the archive's own member list is
    a real signal available right here, at translate time, so odin declines the
    node and names the missing file instead of shipping that.
    """
    module = handler.rsplit(".", 1)[0]
    suffixes = _MODULE_SUFFIXES[PurePosixPath(entry_filename).suffix]
    if any(f"{module}{suffix}" in members for suffix in suffixes):
        return members
    listed = ", ".join(sorted(members)[:6])
    return (
        f"handler {handler!r} needs {module}{suffixes[0]} in the deployment package, and the "
        f"{len(members)} file(s) packaged do not include it: {listed}"
    )


def _lambda_package(res: ResourceDesired) -> dict[str, bytes] | str:
    """This function's zip members, or the human reason odin cannot build them
    -- routed into `unsupported` exactly like any other builder refusal. See
    the block comment above for the three sources and their precedence."""
    runtime = _field(res, "runtime", _DEFAULT_LAMBDA_RUNTIME)
    entry_filename, default_handler = _lambda_entry(runtime)
    source_dir = _field(res, "sourceDir", "").strip()
    declared = res.fields.get("files")
    inline = declared.value if declared is not None and isinstance(declared.value, dict) else {}
    if not source_dir and not inline:
        return {entry_filename: (_field(res, "code", "") or _DEFAULT_LAMBDA_CODE).encode()}
    members = _dir_members(Path(source_dir).expanduser()) if source_dir else _inline_members(inline)
    if isinstance(members, str):
        return members
    return _handler_checked(members, _field(res, "handler", default_handler), entry_filename)


# Workloads an IAM edge may start from (`ui/src/lib/iam.ts::computeTypes`). A
# lambda is excluded because it already has a role on every path, drawn or
# auto-generated.
_GRANTABLE_KINDS = ("ec2", "ecs")


def _grants_key(node_id: str) -> str:
    """`refs` key for the `aws_iam_role_policy` carrying this workload's grants.
    Reserved in pass 1 so the workload can `depends_on` it in pass 2."""
    return f"__grants__{node_id}"


def _instance_profile_key(node_id: str) -> str:
    """`refs` key for the instance profile that carries an ec2 node's role."""
    return f"__instance_profile__{node_id}"


def _workload_role_key(node_id: str) -> str:
    """`refs` key for the auto-role an ec2/ecs node gets when something is
    granted to it. Same reservation shape as `_lambda_role_key`."""
    return f"__workload_role__{node_id}"


def _granted_ids(stack: Stack) -> set[str]:
    """Every node an `iam` edge points AWAY from -- i.e. every workload that
    needs a role to carry the policy the edge compiles to."""
    return {edge.src for edge in stack.edges if edge.kind == "iam"}


# v0.8.14: the REAL ARN a drawn permission's `Resource` names, per target kind.
#
# Until now the `Resource` was odin's node LABEL, because that is what
# `gateway/classify.py` reports for a request and therefore what the evaluator
# matched. It enforced correctly inside odin and granted NOTHING on real AWS,
# which is the same "portable file that isn't" shape `not_in_terraform` exists
# to name.
#
# THE OTHER HALF OF THIS CHANGE LIVES IN `gateway/policy.py`. Emitting ARNs and
# changing nothing else would have silently broken every permission in the
# product: the gateway authorizes from the APPLIED IAM (v0.8.12) and asks
# `evaluate` to match these strings against a bare label. `policy.py::arn_label`
# reduces an ARN back to the label the classifier reports, and
# `tests/agent/test_hcl_iam_arns.py` pins THIS table against THAT reducer for
# every kind, so a shape added here without a reducer there fails the build
# rather than silently denying a granted call.
#
# Each string is what the gateway's own model builds for that resource
# (`gateway/models/*ctl.py`), so the ARN in `main.tf` is the ARN the local
# substrate reports back. Two kinds need TWO forms: an s3 grant is worthless
# without the object-level `bucket/*` (real IAM scopes GetObject to it and
# ListBucket to the bucket itself), and a log group's stream-level actions are
# scoped to `log-group:<name>:*`.
_ARN_FORMS: dict[str, tuple[str, ...]] = {
    "s3": ("arn:aws:s3:::{label}", "arn:aws:s3:::{label}/*"),
    "sqs": ("arn:aws:sqs:{region}:{account}:{label}",),
    "sns": ("arn:aws:sns:{region}:{account}:{label}",),
    "dynamodb": ("arn:aws:dynamodb:{region}:{account}:table/{label}",),
    "lambda": ("arn:aws:lambda:{region}:{account}:function:{label}",),
    "rds": ("arn:aws:rds:{region}:{account}:db:{label}",),
    "elasticache": ("arn:aws:elasticache:{region}:{account}:cluster:{label}",),
    "secret": ("arn:aws:secretsmanager:{region}:{account}:secret:{label}",),
    "ssm": ("arn:aws:ssm:{region}:{account}:parameter/{stripped}",),
    "logs": (
        "arn:aws:logs:{region}:{account}:log-group:{label}",
        "arn:aws:logs:{region}:{account}:log-group:{label}:*",
    ),
    "ecr": ("arn:aws:ecr:{region}:{account}:repository/{label}",),
    "ecs": ("arn:aws:ecs:{region}:{account}:service/{cluster}/{label}",),
    # W2.9. The label IS the KeyId (`kmsctl` deviation 1: real CreateKey carries
    # no name, so the canvas label rides in on the `odin:node` tag), which is
    # also what `classify._kms_resource` reports for a real request.
    #
    # THIS ROW WAS HELD BACK ONE STEP BEHIND ITS INVERSE, ON PURPOSE, and the
    # measurement is kept because it is the whole reason the two tables are
    # pinned together. With `policy.py::_ARN_RESOURCE_LABEL` still missing its
    # `kms` entry:
    #   arn_label("arn:aws:kms:...:key/app-key", "kms:Encrypt")  -> None
    #   classify(TrentService.Encrypt, {"KeyId": "app-key"})     -> ('kms:Encrypt', 'app-key')
    #   evaluate([Allow kms:Encrypt on the ARN], 'kms:Encrypt', 'app-key') -> False
    # i.e. emitting this line first would have denied every drawn kms grant
    # while `tofu plan` stayed clean and the apply stayed green -- canvas
    # correct, file correct, gateway refusing every call.
    #
    # CLOSED by `policy.py::_ARN_RESOURCE_LABEL`'s `"kms": re.compile(r"key/
    # (?P<label>.+)")`. With that line in, the same three calls give 'app-key' /
    # ('kms:Encrypt', 'app-key') / True, and removing it again makes an applied
    # kms grant evaluate False -- so the pairing is load-bearing, not decorative.
    #
    # ONE SHAPE ONLY, deliberately: no `alias/...` form. odin models no aliases
    # (`kmsctl` answers `InvalidAction` for every alias op), so a second shape
    # here would be an ARN no request can ever be made against -- and its
    # inverse would be a reducer entry with nothing behind it.
    "kms": ("arn:aws:kms:{region}:{account}:key/{label}",),
}


def _resource_arns(label: str, kind: str) -> tuple[str, ...]:
    """The real ARN(s) naming one grant target, or the bare LABEL when odin has
    no ARN shape for it.

    The fallback is not a formality: an edge can point at a node that is not on
    this canvas at all, and inventing `arn:aws::::<typo>` for it would be worse
    than saying what was drawn. `_not_in_terraform` reports every case that
    takes it, so the file never claims portability it does not have.
    """
    forms = _ARN_FORMS.get(kind)
    return tuple(
        form.format(label=label, region=_REGION, account=_ACCOUNT,
                    stripped=label.lstrip("/"), cluster=_ECS_CLUSTER_NAME)
        for form in forms
    ) if forms else (label,)


def _policy_document(edges, kind_by_id: dict[str, str], aws_names: dict[str, str]) -> str:
    """The IAM policy JSON for one workload's grants.

    `aws_names` maps a node id to the AWS name it is actually created under
    WHERE THAT DIFFERS FROM ITS LABEL -- today only a log group that some
    workload's substrate ships into (`_log_group_name`). It has to be applied
    here, not just in the builder: the gateway authorizes from the applied IAM
    and `classify.py` reports the group name a request NAMES, so a grant whose
    Resource kept the old label would deny the very PutLogEvents the edge was
    drawn to allow.
    """
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": list(edge.perms),
                "Resource": list(_resource_arns(
                    aws_names.get(edge.dst, edge.dst), kind_by_id.get(edge.dst, ""),
                )),
            }
            for edge in edges if edge.perms
        ],
    })


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
    blocks = [_depends_on_block(res, refs, _grant_dependency(res, refs))]
    # v0.8.19: an efs node edged to this function mounts its file system here.
    # The `arn` is an ACCESS POINT's, never the file system's: AWS's own pattern
    # for this argument ends `access-point/fsap-[a-f0-9]{17}` (botocore's
    # `FileSystemConfig.Arn`), so a file-system arn is a project real AWS
    # rejects. The mount pass reserved both halves under `_efs_mount_key`, and
    # reserving it at all is already gated on the efs node being buildable --
    # otherwise this reference would resolve to nothing and fail the plan for
    # every resource on the canvas.
    #
    # No extra `depends_on`: this reference IS the ordering, so tofu cannot
    # create the function before the access point exists.
    mount_path, access_point = refs.get(_efs_mount_key(res.id), ("", ""))
    if access_point:
        blocks.append(
            "  file_system_config {\n"
            f"    arn              = aws_efs_access_point.{access_point}.arn\n"
            f"    local_mount_path = {quote(mount_path)}\n"
            "  }"
        )
    return attrs, "\n\n".join(block for block in blocks if block)


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
# The one shared cluster's AWS NAME. Named rather than repeated as a literal
# because `_ARN_FORMS` has to build `service/<cluster>/<label>` out of the same
# value -- an ecs grant's ARN would name a cluster that does not exist if the
# two ever disagreed.
_ECS_CLUSTER_NAME = "odin"
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
    depends_on = _depends_on_block(res, refs, _placement_dependency(res, refs) + _grant_dependency(res, refs))
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


# v0.8.15: AN ECR EDGE AUTHORS THE IMAGE.
#
# `_ecs_container_definitions` read the node's hand-typed `image` field and
# NOTHING ELSE -- no edge was consulted anywhere -- so drawing an ecr node to a
# service granted `ecr:BatchGetImage` and left the service running whatever was
# typed (in practice the `nginx:alpine` default). The permission was the whole
# of the edge.
#
# The image is emitted as a real TERRAFORM INTERPOLATION of the repository's own
# attribute, not as an odin-only field and not as a `${{...}}` canvas ref:
#   * tofu resolves it at apply time, so the taskdef `ecsctl` stores carries the
#     REAL address (`127.0.0.1:{port}/{name}` -- `gateway/models/ecr.py`, whose
#     port is minted per env and cannot be typed by a user in advance), which is
#     what `compute/tasks.py` hands to `docker run`. The consumer therefore
#     already exists and needed no change;
#   * it is portable: applied against Amazon it names that account's real
#     repository URL;
#   * it creates the implicit dependency for free -- tofu orders the task
#     definition after the repository without any `depends_on`.
# A `${{repo.REPOSITORY_URI}}` ref would NOT have worked: `gateway/wiring.py`
# resolves refs into a container's ENVIRONMENT, and `compute/tasks.py` passes
# `container_def["image"]` to the driver verbatim, so the placeholder would
# have reached `docker run` unresolved.
#
# NOT TOUCHED, and deliberately: the ECR permission defaults themselves.
# `ecr:BatchGetImage` has no gateway handler and image-layer traffic never
# reaches the gateway (the registry is a real `registry:2` container a docker
# client dials directly -- `gateway/models/ecr.py`'s own docstring), so that
# grant can never bite. That is a real defect and it is a catalog change; it is
# reported rather than fixed here.
_DEFAULT_IMAGE_TAG = "latest"


def _ecr_image_key(node_id: str) -> str:
    """A synthetic `refs` key carrying the HCL name of the ecr repository a
    workload is edged to -- reserved by the image pass, read by `_ecs_image`."""
    return f"__ecr_image__{node_id}"


def _two_images(repos: list[str]) -> str:
    return (
        f"drawn to more than one ECR repository ({', '.join(repr(r) for r in repos)}) and odin "
        "cannot choose which one holds this service's image — draw one, or type the image address "
        "into the node's `image` field, which always wins over an edge"
    )


def _ecr_lambda_note(lambda_id: str, repos: list[str]) -> str:
    """NOT `unsupported`, which feeds a coverage gate: the lambda IS built and
    IS applied, so claiming odin cannot support it would be false. What is
    missing is a MEANING for the edge, which is exactly what `wiring_errors`
    is for (see `TfProject.wiring_errors`)."""
    return (
        f"{lambda_id} (lambda): the edge to {', '.join(repr(r) for r in repos)} (ecr) does NOT set "
        "this function's image — odin's Lambda substrate packages the node's code as a zip and runs "
        "it in an AWS RIE container (`compute/functions.py`), so container-image packaging "
        "(`package_type = \"Image\"`) is not modelled at all. Put the code in `code`/`sourceDir`; "
        "the repository is unused by this function"
    )


def _ecs_image(res: ResourceDesired, refs: Refs) -> str:
    """A HAND-TYPED `image` always wins -- `odin canvas set`, the README's JSON
    schema, `import-tf` and the translation agent all write the field directly,
    and an edge must never silently overwrite something a user typed. Unlike
    `securityGroups`, an image is single-valued, so `_merge_sg_edges`' "add to
    it" is not available as an answer here; this is `_merge_role_edges`' rule."""
    typed = _field(res, "image", "").strip()
    if typed:
        return typed
    repo_name = refs.get(_ecr_image_key(res.id), ("", ""))[1]
    if not repo_name:
        return _DEFAULT_ECS_IMAGE
    tag = _field(res, "imageTag", "").strip() or _DEFAULT_IMAGE_TAG
    return f"${{aws_ecr_repository.{repo_name}.repository_url}}:{tag}"


def _ecs_container_definitions(
    res: ResourceDesired, refs: Refs, mounts: tuple[tuple[str, str], ...] = (),
) -> list[dict]:
    """`mounts` is `[(efs node id, container path)]` for the file systems this
    service was edged to, resolved by `generate_tf`'s mount pass. It arrives as
    an argument rather than through `refs` because a service may mount SEVERAL
    file systems and a `refs` value is one pair of strings.

    ABSENT rather than `[]` when nothing is mounted, so every canvas without an
    efs node generates the byte-identical container definition it did before --
    `container_definitions` is stored verbatim by `ecsctl` and a changed string
    is a new task-definition revision, which redeploys the service."""
    port = _field(res, "port", _DEFAULT_ECS_PORT)
    port_int = int(port) if port.isdigit() else int(_DEFAULT_ECS_PORT)
    # `sourceVolume` must be the `volume` block's `name` (the taskdef pass emits
    # the efs node's id there) or ECS rejects the registration; `readOnly` is
    # false because odin's renderer has no `:ro` form at all (`ContainerSpec.
    # volumes`), and claiming read-only while mounting read-write would be worse
    # than not offering it.
    mounted = [
        {"sourceVolume": efs_id, "containerPath": path, "readOnly": False}
        for efs_id, path in mounts
    ]
    return [{
        "name": res.id,
        "image": _ecs_image(res, refs),
        "essential": True,
        "portMappings": [{"containerPort": port_int, "hostPort": 0, "protocol": "tcp"}],
        **({"mountPoints": mounted} if mounted else {}),
    }]


# W2.1: CloudWatch log groups (gateway/models/logsctl.py -- odin's one log
# SINK, control plane + data plane, no backing container). The canvas label IS
# the log group name, deliberately: `classify.py`'s `_classify_logs` reports
# the bare group name as the IAM resource, so a `logs:PutLogEvents` edge drawn
# to this node only enforces correctly while name == label (the same identity
# rule s3's bucket / sqs's queue name already carry).
_BAD_LOGS_RETENTION = "retentionInDays must be a whole number of days (e.g. 14)"

# v0.8.15: THE DRAWN GROUP IS THE ONE THAT RECEIVES.
#
# The two substrates that ship logs write to a name derived from the WORKLOAD's
# own id and read no destination from anywhere: `lambdactl._ship_logs` ->
# `/aws/lambda/{function}`, `ecsctl._ship_task_logs` -> `/ecs/{service}`. So
# drawing a log-group tile (default label `/odin/logs`) to a lambda `myfn` and
# applying created TWO groups -- the drawn one, which the policy granted
# `logs:PutLogEvents` on, and `/aws/lambda/myfn`, which got every line. The
# drawn one stayed empty forever, and the only canvas that appeared to work was
# one whose label happened to coincide.
#
# The fix runs in the direction that needs no new signal at all: the emitted
# group takes the name the substrate ALREADY writes to, so the canvas node
# backs the group that receives. Nothing had to learn to read a destination.
# (`aws_cloudwatch_log_group` would carry `logging_config`/`logConfiguration`
# in the other direction, but nothing in odin consumes either -- verified by
# grep, zero hits outside prose -- so emitting them would only move the
# decorative line from the canvas into the file.)
#
# WHAT THIS COSTS, stated because it breaks an invariant three other modules
# were written against ("a log group's identity IS its name, and odin's
# canonical resource id is the node label"):
#   * `reconcile/tf_status.py::_log_groups` already resolves through the
#     `odin:node` tag (`_label(tags, name)`), so /world keeps reporting the
#     node under its own label -- no change needed there, verified;
#   * `api/logs.py`'s `kind == "logs"` branch assumed name == label and is
#     changed in the same commit to resolve through that same tag;
#   * `iac/import_tf.py::_label` prefers the `name` literal, so importing
#     the generated file back gives the node the DESTINATION as its label.
#     The file then regenerates byte-identically (label == destination is the
#     coincidence case), and the drawn edge plus the policy ARN stay
#     self-consistent -- it is a visible label change, not a broken round
#     trip. `docs/limits.md` records it.
# An `ec2` node is deliberately NOT in this table: nothing ships a VM's output
# into CloudWatch Logs, so an ec2 -> logs edge is a grant for the code INSIDE
# the VM to call PutLogEvents itself, which works exactly as drawn today.
_LOG_DESTINATIONS = {"lambda": "/aws/lambda/{label}", "ecs": "/ecs/{label}"}
_LOG_SHIPPING_KINDS = tuple(sorted(_LOG_DESTINATIONS))


def _log_destination(workload: ResourceDesired) -> str:
    return _LOG_DESTINATIONS[workload.kind].format(label=workload.id)


def _log_sink_key(node_id: str) -> str:
    """A synthetic `refs` key (never a real canvas id) carrying the AWS NAME a
    logs node must take because it is some workload's sink. Reserved by the
    log-sink pass, read by `_logs` -- the `_alb_target_key` technique."""
    return f"__log_sink__{node_id}"


def _two_log_destinations(node_id: str, destinations: list[str]) -> str:
    return (
        f"drawn as the log sink for more than one workload, and odin's substrates ship to a group "
        f"named after the workload ({', '.join(destinations)}) — one group cannot be both. Draw one "
        f"Log Group node per workload (this one is {node_id!r})"
    )


def _log_group_name(res: ResourceDesired, refs: Refs) -> str:
    """The AWS name this group is created under: the workload's real
    destination when this node is a sink, else the node's own label (which is
    every canvas that draws no such edge -- byte-identical to before)."""
    return refs.get(_log_sink_key(res.id), ("", res.id))[1]


def _logs(res: ResourceDesired, refs: Refs) -> Built:
    # No retention field = no `retention_in_days` argument at all, which is
    # AWS's own "never expire" default -- emitting a made-up number instead
    # would silently expire a user's logs.
    retention = _field(res, "retentionInDays", "").strip()
    if retention and not retention.isdigit():
        return _BAD_LOGS_RETENTION
    attrs = {"name": quote(_log_group_name(res, refs))}
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


def _bad_kms_ref(field: str, label: str) -> str:
    return (
        f"{field} names {label!r}, which is not a kms node on this canvas — a key that does not "
        f"exist is a HARD error in odin (the gateway refuses to seal rather than quietly using "
        f"the default key), so clear the field or draw the kms node"
    )


def _kms_key_attr(res: ResourceDesired, refs: Refs, field: str, attr: str) -> dict[str, str] | str:
    """`{attr: aws_kms_key.<name>.key_id}` for the key this node is sealed under
    -- `{}` when it names none, or the decline reason when the name is not a kms
    node (the `_lambda`/`_security_group_refs` pattern: a `str` is routed into
    `unsupported`).

    An INTERPOLATED reference rather than the bare label, deliberately: it is
    what makes tofu create the key before the secret that names it. Without the
    ordering the apply is a coin flip, and the losing side is not a retryable
    error -- `kmsctl.seal` refuses a key that does not exist yet, so the secret
    fails to create at all.

    `.key_id` and not `.arn`: `gateway/classify.py::_kms_resource` reduces every
    KeyId form to the bare id, which for a canvas key IS its label, and that is
    the string an IAM edge's grant has to match.
    """
    label = _field(res, field, "").strip()
    if not label:
        return {}
    kind, name = refs.get(label, ("", ""))
    return {attr: f"aws_kms_key.{name}.key_id"} if kind == "kms" else _bad_kms_ref(field, label)


def _secret(res: ResourceDesired, refs: Refs) -> Built:
    key = _kms_key_attr(res, refs, "kmsKeyId", "kms_key_id")
    if isinstance(key, str):
        return key
    attrs = {"name": quote(res.id), "recovery_window_in_days": _SECRET_RECOVERY_WINDOW, **key}
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
    # PORTABILITY DIVERGENCE, stated rather than validated away: real AWS reads
    # `key_id` only for a `SecureString`, and rejects PutParameter if you send
    # one for a String. odin's `ssmctl._put_parameter` seals EVERY type under
    # the named key, so a String parameter here really is encrypted at rest by
    # the key the canvas drew. Declining the combination would therefore refuse
    # something odin does correctly; emitting it is honest about odin and only
    # divergent on Amazon, where the parameter would still be created and this
    # argument ignored for a non-SecureString.
    key = _kms_key_attr(res, refs, "keyId", "key_id")
    if isinstance(key, str):
        return key
    attrs = {"name": quote(res.id), "type": quote(param_type), "value": quote(value), **key}
    description = _field(res, "description", "")
    if description:
        attrs["description"] = quote(description)
    return attrs, ""


# W2.9: KMS keys (gateway/models/kmsctl.py + gateway/kms.py -- real AES-256-GCM
# material in a 0600 `.odin/{env}/kms.json`, sealing the secretsmanager and ssm
# sidecars). The builder that emits NO NAME, and that is the whole reason this
# kind is unusual: real `CreateKey` takes no name argument at all, so there is
# nowhere for the canvas label to ride except the `odin:node` tag every primary
# resource already carries (`_tags_block`, applied in generate_tf's pass 2). If
# that tag ever stopped being stamped on this type, `kmsctl._create_key` would
# mint a uuid instead and the key would be addressable from nothing -- not by
# `_kms_key_attr` below, not by an IAM edge, not by the World projection.
#
# `deletion_window_in_days` is PORTABILITY ONLY and the number is a lie odin is
# forced into: odin's ScheduleKeyDeletion destroys the material IMMEDIATELY
# (kmsctl's deviation 2), so the honest value would be 0, exactly as `_secret`
# emits `recovery_window_in_days = 0` for the same immediate-delete deviation.
# AWS's minimum is 7 and the provider validates it client-side, so 0 would fail
# `tofu plan` before the gateway is reached. 7 is emitted because it is the
# smallest value that parses -- odin does NOT honour it, and applied against
# real Amazon this key would survive 7 days where odin's is gone at once.
_KMS_DELETION_WINDOW = "7"
# `rotate` is a FLAG, not a rotation. `kmsctl` records `rotation_enabled` and
# `GetKeyRotationStatus` reports it back, so the field round-trips through a
# real EnableKeyRotation call -- but no key material is ever re-derived and no
# old ciphertext is re-wrapped. Offered anyway because the generated file is
# meant to be portable, where it does mean the real thing; never defaulted on,
# because a default asserts a protection odin has not got.
_KMS_ROTATE = ("true", "false")
_BAD_KMS_ROTATE = f"rotate must be one of {', '.join(_KMS_ROTATE)}"

# A label the gateway's `kmsctl.bare_key_id` would REWRITE, which is a key that
# is created successfully and can then never be addressed again.
#
# MEASURED against the real model, not reasoned about. `_create_key` keys its
# record by the RAW `odin:node` tag, while every other op keys by
# `bare_key_id(KeyId)` -- so the two disagree exactly when `bare_key_id` is not
# the identity. Driving the real handlers with a canvas label of
# `alias/prod-key`:
#
#   CreateKey   -> 200, KeyId 'alias/prod-key'
#   DescribeKey -> 400 NotFoundException "Key 'prod-key' does not exist"
#   Encrypt     -> 400 NotFoundException "Key 'prod-key' does not exist"
#
# ...and a secret naming it then fails `EncryptionFailure` quoting a key id the
# user never typed. A green create for a key that is dead on arrival is this
# repo's own "reports success it did not achieve", one layer down.
#
# Declined HERE, at generate time, on the `_rds` precedent: the node's name IS
# the identifier, so the reason names `data.label` -- the key a CLI reader can
# actually edit -- rather than a gesture. The rule is DUPLICATED from
# `bare_key_id` rather than imported, the same way `_SSM_TYPES` duplicates
# ssmctl's `VALID_TYPES`: the deterministic translator stays independent of the
# gateway. The real repair belongs in `_create_key` (create and lookup should
# key alike); this stops the canvas from reaching it either way.
_BAD_KMS_LABEL = (
    "a KMS key's name may not contain ':key/' or start with 'alias/' — the node's name IS the key "
    "id, and the gateway reduces both of those forms to something shorter, creating a key nothing "
    "can address afterwards. Change data.label"
)


def _bare_key_id(value: str) -> str:
    """`kmsctl.bare_key_id`, character for character. Mirrored rather than
    approximated: a looser `"alias/" in value` test would also decline
    `my-alias/key`, which the real function leaves alone (it strips a PREFIX),
    and declining a name that works is its own kind of wrong answer."""
    return (value.rpartition(":key/")[2] or value).removeprefix("alias/")


def _kms(res: ResourceDesired, refs: Refs) -> Built:
    if _bare_key_id(res.id) != res.id:
        return _BAD_KMS_LABEL
    attrs = {"deletion_window_in_days": _KMS_DELETION_WINDOW}
    description = _field(res, "description", "").strip()
    if description:
        attrs["description"] = quote(description)
    rotate = _field(res, "rotate", "").strip()
    if rotate and rotate not in _KMS_ROTATE:
        return _BAD_KMS_ROTATE
    if rotate:
        attrs["enable_key_rotation"] = rotate
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


# v0.8.18: EBS volumes -- one `aws_ebs_volume` per ebs node, plus an
# `aws_volume_attachment` companion per ebs<->ec2 edge (the attachment pass at
# the bottom of `generate_tf`). The substrate is a real `limactl disk` attached
# to the instance's Lima VM (`compute/`).
#
# `type = "gp3"` is FIXED and is NOT a canvas field. The tile authors exactly
# three (`label`, `az`, `size` -- ui/src/lib/catalog.ts), so a `type` argument
# read from the canvas would be a field nothing can set; and a `limactl disk`
# has no volume type at all, so any value but one would be a claim odin cannot
# back. It is registered in `import_tf._FIXED_VALUES` instead, which is what
# makes an imported `type = "io2"` a reported CHANGED argument rather than a
# silent substitution.
_DEFAULT_EBS_AZ = "us-east-1a"
_DEFAULT_EBS_SIZE = "10"
_EBS_VOLUME_TYPE = "gp3"
_BAD_EBS_SIZE = "size must be a whole number of GiB (e.g. 10)"


def _ebs(res: ResourceDesired, refs: Refs) -> Built:
    size = _field(res, "size", _DEFAULT_EBS_SIZE).strip()
    if not size.isdigit():
        return _BAD_EBS_SIZE
    return {
        "availability_zone": quote(_field(res, "az", _DEFAULT_EBS_AZ)),
        "size": size,
        "type": quote(_EBS_VOLUME_TYPE),
    }, ""


# v0.8.19: EFS file systems -- one `aws_efs_file_system` per efs node, and then
# the part that is actually the feature, which is the COMPANIONS: an
# `aws_efs_access_point` for a file system some lambda mounts, and a `volume` +
# `mountPoints` pair on the task definition of every ecs service that mounts it
# (the mount pass at the bottom of `generate_tf`). An `aws_efs_file_system` on
# its own is storage nothing can reach; the whole point of EFS is that many
# consumers share ONE of them, which is why the mount is many-to-many where
# `_VOLUME_HOST_KINDS`' attachment is strictly one-to-one.
#
# What this deliberately does NOT emit, each absence for its own reason:
#   * `encrypted` / `kms_key_id` -- odin encrypts nothing here. The substrate is
#     a real host directory; emitting the argument would claim a property the
#     substrate does not have, which is the mistake the kms work paid for once
#     already.
#   * `performance_mode` / `throughput_mode` -- the tile authors neither, so
#     reading them from the canvas would be a field nothing can set. Measured on
#     the wire (a real `tofu apply` of these two resources against a recording
#     HTTP endpoint, hashicorp/aws 6.57.1): the provider sends
#     `ThroughputMode: bursting` by itself.
#   * `name` -- MEASURED from the real provider schema (`tofu providers schema
#     -json`, OpenTofu 1.12.3, hashicorp/aws ~> 5.0): `aws_efs_file_system.name`
#     is COMPUTED, not an argument at all. A file system is named by its `Name`
#     tag and nothing else. odin carries the canvas label on `odin:node` for
#     every kind (`_tags_block`), which is the answer `kmsctl` reached for the
#     identical reason -- `CreateKey` has no name argument either.
_DEFAULT_EFS_PATH = "/mnt/efs"

# AWS's OWN pattern for a LAMBDA mount path, copied verbatim out of botocore's
# `FileSystemConfig.LocalMountPath` shape (botocore 1.43.30) rather than
# remembered -- `test_hcl_efs.py::test_the_mount_path_pattern_is_the_one_aws_publishes`
# reads it back out of botocore and fails the build if the two ever diverge.
#
# The character class holds no `/`, so a lambda mount path is exactly ONE segment
# under `/mnt`: `/mnt/efs` is legal and `/mnt/efs/data` is not. That is worth a
# guard rather than a comment, because the file odin would generate for the
# second is one real AWS rejects at CreateFunction -- an apply failure a long way
# from the canvas field that caused it.
#
# IT IS A LAMBDA CONSTRAINT AND ONLY A LAMBDA CONSTRAINT, which is the whole
# reason the check lives in the mount pass and not in `_efs`. ECS's own
# `MountPoint.containerPath` carries NO pattern and no length limit at all
# (botocore, printed: `metadata={}`), and odin's substrate is a bind mount that
# serves any path, so an ecs-only canvas mounting at `/data` is legal in AWS,
# legal here, and must not be refused. Applying lambda's rule to every efs node
# did exactly that, and it was caught by the agent building the IMPORTER against
# this file rather than by anything on this side.
#
# `fullmatch`, because that is how AWS applies a shape pattern and because
# `search` is measurably useless here: it accepts `/mnt/efs/data`, `/mnt/e s`
# and `/mnt/efs:x` alike (printed before this line was written).
_LOCAL_MOUNT_PATH_PATTERN = "/mnt/[a-zA-Z0-9-_.]+"
_MOUNT_PATH = re.compile(_LOCAL_MOUNT_PATH_PATTERN)

# `CreationToken` is `min 1, max 64` in botocore's own `CreateFileSystemRequest`.
# The canvas label IS the creation token (odin's canonical id everywhere else
# too), so a longer label has to be declined BY NAME: truncating it in silence
# would let two long labels name one file system.
_MAX_CREATION_TOKEN = 64


def _lambda_mount_path(efs_id: str, path: str) -> str:
    return (
        f"mounts {efs_id} at {path!r}, which is not a path a Lambda function can mount — AWS's own "
        f"pattern is {_LOCAL_MOUNT_PATH_PATTERN}, one segment under /mnt ('/mnt/efs' yes, "
        f"'/mnt/efs/data' no), and CreateFunction rejects anything else. An ecs service has no such "
        f"constraint (ECS's `containerPath` carries no pattern at all), so give {efs_id} a `path` "
        "under /mnt, or mount it on a service instead"
    )


def _long_creation_token(label: str) -> str:
    return (
        f"the label is {len(label)} characters and an EFS creation token is capped at "
        f"{_MAX_CREATION_TOKEN} — shorten the label rather than have odin truncate it, which would "
        "let two long labels name one file system"
    )


def _efs_fault(res: ResourceDesired) -> str:
    """The reason this efs node cannot be BUILT, or "" if it can.

    Only properties of the file system ITSELF live here -- the mount path does
    not, because whether a path is legal depends on WHO mounts it (see
    `_LOCAL_MOUNT_PATH_PATTERN`), which this cannot see.

    ONE function, shared by `_efs` (which declines the node) and by the mount
    pass, which runs BEFORE pass 2 -- `_lambda` reads its answer -- and so cannot
    consult `built_ids`. The two must not disagree: a mount pass that authored a
    `file_system_config` naming an access point the declined node never emitted
    leaves an unresolvable reference, and `tofu plan` fails for the WHOLE project
    on one of those, so every other resource on the canvas stops applying too.
    The same reason `_grant_role_ref` is shared by the pass that reserves a
    policy name and the pass that emits it.
    """
    return _long_creation_token(res.id) if len(res.id) > _MAX_CREATION_TOKEN else ""


def _efs(res: ResourceDesired, refs: Refs) -> Built:
    return _efs_fault(res) or ({"creation_token": quote(res.id)}, "")


# v0.8.19: DNS -- one `aws_route53_zone` per route53 node, plus an
# `aws_route53_record` companion per route53<->ec2 edge (the record pass at the
# bottom of `generate_tf`). The substrate is REAL NAME RESOLUTION: an
# `--add-host` entry on every container in the env and an `/etc/hosts` line on
# every Lima VM in it, both written from the applied records.
#
# THE TRAP, AND WHY THIS IS SCOPED TO ec2. A hosts entry is `<ip> <name>`: it
# carries no port and no scheme. So the only kinds a name can point AT are the
# ones whose World fact is a bare address, and that is exactly one.
#
# The SHAPES below were measured 2026-08-02 by running the real projectors in
# `reconcile/tf_status.py` over records in the shape the gateway models really
# write. The HOST halves are real constants read out of the source
# (`elbv2ctl._DNS_NAME`, `runtime/colima.py::CONTAINER_HOST`); the PORT DIGITS
# were the probe's own inputs, and are written here as `<dynamic port>` rather
# than as numbers, because inventing a plausible number and then citing it as a
# measurement is exactly the failure the rest of this comment is about:
#
#   ALB   projected facts  -> {"ALB_ENDPOINT": "http://127.0.0.1:<dynamic port>"}
#   RDS   projected facts  -> {"endpoint": "host.docker.internal:<dynamic port>", ...}
#   EC2   projected facts  -> {"PRIVATE_IP": <bare IPv4>, "MESH_IP": <bare IPv4>}
#
# An alb's port is DYNAMIC (`elbv2ctl._DNS_NAME`'s own note: two load balancers,
# or two envs, would collide on a fixed 80), so a name pointing at one would
# resolve to 127.0.0.1 and then fail to connect on :80 -- a green resource that
# does not work, which is the exact failure this repo's honesty rules exist to
# stop. rds is the same shape with a nonstandard port. So both are DECLINED BY
# NAME here rather than emitted (`_dns_target_unsupported`).
#
# WHICH ec2 ADDRESS THE TERRAFORM CARRIES, and why the SUBSTRATE does not always
# agree with it. The record's value is `aws_instance.<n>.private_ip` -- what
# DescribeInstances reports, what `tf_status._ec2_facts` publishes as
# `PRIVATE_IP`, and the only thing a project taken to Amazon could portably
# mean. What resolves the name LOCALLY is a hosts entry, and which address
# actually works there depends on who is reading it:
#
#   container -> VM private_ip : reachable
#   host      -> VM private_ip : reachable
#   VM        -> VM private_ip : 100% PACKET LOSS. Stock Lima `vz` NATs each VM
#                                into its OWN isolated address space, so there
#                                is no VM-to-VM underlay path at all, before
#                                nebula is even involved -- see the R5 note in
#                                `fabric/nebula.py` (~line 41 and ~line 465),
#                                which records it as confirmed live with two
#                                real VMs.
#
# So a VM's `/etc/hosts` may NOT be handed `private_ip`: that is a name which
# resolves and then hangs, which is this kind's own trap one layer down. A VM
# gets the Nebula OVERLAY address instead (relayed through the lighthouse, which
# is what makes VM-to-VM work at all), and gets NO entry when the env has no
# mesh -- `_ec2_facts` already withholds `MESH_IP` on exactly that condition,
# "so odin never hands out an address no peer can reach". The divergence between
# the emitted argument and the local substrate is deliberate, and is recorded in
# docs/limits.md in these measured terms -- the same shape as ebs's advisory
# `device_name`.
_DNS_TARGET_KINDS = ("ec2",)
_DNS_RECORD_TYPE = "A"
# 60s, and it is not arbitrary: odin's substrate is a hosts FILE, which has no
# TTL at all. The number exists so the generated project stays portable to
# Amazon, and it is deliberately the shortest value a human would write, because
# the local substrate re-reads on the next container launch / hosts push rather
# than on any expiry.
_DNS_RECORD_TTL = "60"
# A DNS label: letters, digits and hyphens, not starting or ending with one.
# `aws_route53_zone.name` must be a domain and a record's own name is
# `<ec2 label>.<zone>`, so BOTH halves are checked against this -- an
# unresolvable name is a record odin would write into /etc/hosts and no resolver
# would ever match.
_DNS_LABEL = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


def _bad_dns_name(name: str) -> str | None:
    """The reason `name` cannot be a DNS name, or None if it can."""
    parts = name.split(".")
    bad = [part for part in parts if not _DNS_LABEL.match(part)]
    if bad:
        return (
            f"{name!r} is not a valid DNS name: {', '.join(repr(p) for p in bad)} "
            "is not a label of letters, digits and hyphens (not leading or trailing)"
        )
    return None


def _route53(res: ResourceDesired, refs: Refs) -> Built:
    reason = _bad_dns_name(res.id)
    return reason if reason else ({"name": quote(res.id)}, "")


# kind -> terraform resource type; kept separate from _BUILDERS so pass 1 of
# generate_tf can assign HCL names (scoped per resource type) without running
# any builder.
# --- apigateway (v0.8.19) ---------------------------------------------------
#
# An HTTP API (`aws_apigatewayv2_api`) whose ROUTES come from its edges, exactly
# as an ALB's target attachments do. One canvas node becomes:
#
#     aws_apigatewayv2_api          the API itself (this builder)
#     aws_apigatewayv2_stage        one, always `$default` (companion pass)
#     aws_apigatewayv2_integration  one per target edge (companion pass)
#     aws_apigatewayv2_route x2     per target edge (companion pass)
#
# WHY TWO ROUTES PER TARGET. A route key `ANY /orders` matches ONLY `/orders`;
# `ANY /orders/{proxy+}` matches `/orders/a/b` and NOT `/orders`. Serving a whole
# path prefix therefore takes both -- that is AWS's own idiom, not odin's
# invention -- and they collapse back to ONE nginx `location` pair on the
# substrate side (`compute/apigw.py`) and to ONE canvas edge on the import side
# (`iac/import_tf.py` recovers the edge from the INTEGRATION, so the route
# count never reaches the canvas).
#
# The path segment is the TARGET's label, not a canvas field, for the reason
# `_EBS_DEVICE_NAMES` gives about positional device names: `generate_tf` is a
# pure function of the canvas, and a field the tile does not have cannot be read.
# A label is stable, unique on the canvas, and reads correctly
# (`https://<endpoint>/orders/...` -> the `orders` function).
_APIGW_STAGE = "$default"
_APIGW_TARGET_KINDS = ("lambda", "ecs")
# The ECS hostname suffix. Kept in lock-step with
# `gateway/models/apigwctl.py::ECS_HOST_SUFFIX`, which parses it back off the
# wire; `tests/agent/test_hcl_apigateway.py` asserts the two agree so a rename
# on one side fails the build rather than silently breaking routing.
_APIGW_ECS_HOST_SUFFIX = ".odin.internal"


def _apigateway(res: ResourceDesired, refs: Refs) -> Built:
    """The PRIMARY `aws_apigatewayv2_api` block.

    Deliberately NOT containment-gated, unlike `_alb`/`_ec2`: a real HTTP API is
    regional and has no subnet, so requiring one would be odin inventing a
    constraint AWS does not have. It also has no canvas-authored knob worth
    emitting -- `route_selection_expression` is fixed by the protocol and
    `disable_execute_api_endpoint` would be a claim about a domain odin has no
    model for."""
    return {
        "name": quote(res.id),
        "protocol_type": quote("HTTP"),
    }, ""


def _apigw_route_keys(target_id: str) -> tuple[str, str]:
    """The two route keys one target owns. One function so the generator and the
    tests cannot drift on the spelling."""
    return f"ANY /{target_id}", f"ANY /{target_id}/{{proxy+}}"


def _apigw_integration_attrs(target: ResourceDesired, target_name: str) -> dict[str, str]:
    """The integration block for one target, which is where the two kinds
    genuinely differ.

    lambda -> `AWS_PROXY` carrying the function's `invoke_arn`, which is real
    AWS's own wiring and what `apigwctl.function_of` reads the name back out of.

    ecs -> `HTTP_PROXY` at `http://<service name>.odin.internal`. An ECS service
    has no URL on real AWS either (an HTTP API reaches a private one through a
    VPC link, which odin does not model), and `integration_uri` must be a URI --
    so odin names the service through a hostname only odin resolves, and
    docs/limits.md says so rather than implying the file would work against
    Amazon. The alternative considered and rejected was requiring an ALB in
    between, which would make the simplest useful canvas a four-node one."""
    if target.kind == "lambda":
        return {
            "integration_type": quote("AWS_PROXY"),
            "integration_uri": f"aws_lambda_function.{target_name}.invoke_arn",
            "payload_format_version": quote("2.0"),
        }
    return {
        "integration_type": quote("HTTP_PROXY"),
        "integration_method": quote("ANY"),
        "integration_uri": f'"http://${{aws_ecs_service.{target_name}.name}}{_APIGW_ECS_HOST_SUFFIX}"',
    }


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
    "kms": "aws_kms_key",
    "ebs": "aws_ebs_volume",
    "route53": "aws_route53_zone",
    "efs": "aws_efs_file_system",
    # apigateway (v0.8.19). **v2, not v1**, and that is an importability
    # decision rather than a preference: an `aws_api_gateway_rest_api` needs
    # `aws_api_gateway_resource` + `_method` + `_integration` + `_deployment` +
    # `_stage` per path -- five companion types whose relationships the importer
    # would have to rebuild from `parent_id` chains -- against v2's flat
    # api/integration/route/stage. odin's own output MUST re-import
    # (ROADMAP: two companions already fail that bar and a third is not
    # acceptable), and a flat companion set is what makes that reachable.
    "apigateway": "aws_apigatewayv2_api",
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
    "kms": _kms,
    "ebs": _ebs,
    "route53": _route53,
    "efs": _efs,
    "apigateway": _apigateway,  # v0.8.19
}

# W2.5: which canvas kinds can actually BE an ALB target. An ECS service
# registers its OWN tasks (real ECS's scheduler behaviour, modeled in
# gateway/models/ecsctl.py), so it needs a `load_balancer` block on the service
# and no attachment at all; an ec2 instance is registered by tofu, through an
# `aws_lb_target_group_attachment` companion (v0.8.15 -- see the companion pass
# in generate_tf, and `_ALB_NO_LAMBDA` for the one target that is still
# declined).
_ALB_TARGET_KINDS = ("ecs", "ec2")

# DECLINED WITH THE REAL REASON, and it is a design decision rather than an
# unbuilt corner. odin's load-balancer substrate is a real nginx container
# (`compute/proxy.py`) whose upstreams are `host:port`; a lambda target needs an
# HTTP request TRANSLATED into the RIE's invoke envelope and the response
# translated back. That is the identical shim an `apigateway -> lambda` route
# needs, and building it twice is how odin ends up with two unrelated
# implementations of one thing -- so it is built once, there, and this says so.
_ALB_NO_LAMBDA = (
    "a lambda target needs an HTTP request translated into the RIE's invoke envelope, and odin's "
    "load-balancer substrate is an nginx reverse proxy that dials host:port upstreams. That "
    "translation is the same shim an apigateway → lambda route needs and is deliberately built "
    "once, there, rather than twice"
)


# v0.8.18: which canvas kinds a VOLUME can be attached to. `ec2` alone, because
# it is the only kind odin runs as a machine with a disk controller -- an ebs
# node edged to a lambda or an ecs service would author an attachment nothing
# could perform, the same rule `_SG_MEMBERS` and `_ROLE_HOLDERS` hold one file
# over. Mirrored by `ui/src/lib/iam.ts::volumeHostTypes`, and
# `tests/spec/test_edge_registry_matches_builders.py` fails the build if the two
# sides ever disagree -- the same cross-language ratchet `_ALB_TARGET_KINDS` has.
_VOLUME_HOST_KINDS = ("ec2",)

# The device names odin hands out, in order. Real AWS REJECTS two attachments
# claiming the same device on one instance, so the name is assigned by the
# volume's POSITION in the sorted list of that instance's attached volumes:
# deterministic, and stable when an unrelated volume on the same instance is
# declined. `/dev/sd[f-p]` is the conventional range for extra volumes on a
# Linux instance; a 12th volume is declined BY NAME rather than silently reusing
# a device that the provider (or Amazon) would then reject for the whole apply.
#
# `device_name` IS ADVISORY, and that is measured rather than assumed. On real
# Lima 2.1.3 (2026-08-02) the guest names an extra disk `/dev/vdb` (virtio),
# auto-partitions it `vdb1` ext4 and auto-mounts it at `/mnt/lima-<disk>`; adding
# that disk also SHIFTS the cloud-init `cidata` ISO from `vdb` to `vdc`. Device
# letters inside the guest are therefore positional and not a contract: odin does
# NOT honour `/dev/sdf`, and nothing here may claim it does. What the argument is
# for is Terraform itself -- `aws_volume_attachment.device_name` is required, and
# it must be unique per instance. It is also what the provider's own attachment
# WAITER filters DescribeVolumes on (`attachment.device`), so the string emitted
# here has to be the string `AttachVolume` was sent -- there is exactly one
# source for it (this tuple), and nothing normalises, lowercases or defaults it
# on the way out.
#
# THE RESIDUAL, MEASURED, because a positional scheme cannot be fully stable and
# pretending otherwise would be the caveat-that-outlives-its-fix bug in advance.
# `device_name` is ForceNew: measured against OpenTofu 1.12.3 with a real state
# file, changing it prints `~ device_name = "/dev/sdf" -> "/dev/sdg" # forces
# replacement` and `Plan: 1 to add, 0 to change, 1 to destroy` -- a detach and
# reattach of a LIVE disk. And the slot is positional, so adding a volume whose
# label sorts EARLIER renumbers every later volume on that instance: measured,
# adding `archive` to an instance holding `data` (/dev/sdf) and `logs`
# (/dev/sdg) moves them to /dev/sdg and /dev/sdh. Both existing disks are
# therefore detached and reattached by a change that had nothing to do with them.
#
# It is kept anyway, and the reasoning is worth writing down so nobody "fixes" it
# into something worse. `generate_tf` is a pure function of the canvas with no
# memory of the last apply, and the pool has 11 slots, so NO assignment rule can
# be insertion-stable -- hashing the label into a slot merely makes the
# renumbering rarer and unpredictable instead of rare and explainable. The real
# fixes are a canvas field the tile does not have, or reading the live
# attachment back, and both are larger than this pass. Until then the honest
# thing is that this is stated here, pinned by
# `test_hcl_ebs.py::test_adding_an_earlier_sorting_volume_renumbers_the_others`,
# and named in docs/limits.md rather than discovered during an apply.
_EBS_DEVICE_NAMES = tuple(f"/dev/sd{letter}" for letter in "fghijklmnop")


def _too_many_volumes(volume_id: str, instance_id: str) -> str:
    return (
        f"{volume_id} (ebs): {instance_id} already holds {len(_EBS_DEVICE_NAMES)} volumes and odin "
        f"assigns device names from {_EBS_DEVICE_NAMES[0]} to {_EBS_DEVICE_NAMES[-1]}, so there is "
        "none left — the aws_ebs_volume is emitted UNATTACHED and no aws_volume_attachment is "
        "generated for it. Move it to another instance, or detach one of the others"
    )


def _volume_already_attached(volume_id: str, instance_id: str, holder: str) -> str:
    return (
        f"{volume_id} (ebs): a second attachment edge, to {instance_id} — a gp3 volume attaches to "
        f"exactly one instance (and the substrate, a limactl disk, to exactly one VM), so only the "
        f"attachment to {holder} is emitted. Multi-attach would fail at apply, not here"
    )


# v0.8.19: which canvas kinds can MOUNT a file system. `ecs` and `lambda` -- the
# two kinds odin runs as CONTAINERS, because the substrate is a host directory
# bind-mounted into one, so a kind with no container has nowhere to put it.
#
# `ec2` is absent deliberately and the reason is measured, not stylistic: odin's
# Lima VMs are created with `"mounts": []` (`compute/lima_yaml.py`), so a host
# directory is not visible inside a VM at all. An `efs -> ec2` edge would author
# a mount that silently resolves to an EMPTY directory, which is the substrate
# hazard this repo has already been bitten by once under Colima's virtiofs.
#
# Mirrored by `ui/src/lib/iam.ts::efsMountTypes`, with
# `tests/spec/test_edge_registry_matches_builders.py` failing the build if the
# two sides ever disagree -- the same cross-language ratchet `_ALB_TARGET_KINDS`
# and `_VOLUME_HOST_KINDS` have.
_EFS_MOUNT_KINDS = ("ecs", "lambda")


def _efs_mount_key(node_id: str) -> str:
    """A synthetic `refs` key (never a real canvas id -- see the `Refs` type)
    carrying the EFS mount a workload was edged to, as `(local mount path,
    access point HCL name)`. Reserved by the mount pass in `generate_tf`, read by
    `_lambda`, exactly like `_alb_target_key` and the lambda auto-role."""
    return f"__efs_mount__{node_id}"


def _two_file_systems(efs_ids: list[str]) -> str:
    return (
        f"drawn to more than one EFS file system ({', '.join(repr(i) for i in efs_ids)}) and a "
        "Lambda function mounts at most ONE — `file_system_config` carries `max_items: 1` in the "
        "real provider schema and `FileSystemConfigs` `max: 1` in botocore's Lambda model (both "
        "measured), so emitting two blocks fails `tofu validate` for the whole project. Draw one"
    )


def _mount_path_taken(efs_id: str, consumer_id: str, holder: str, path: str) -> str:
    return (
        f"{efs_id} (efs): {consumer_id} already mounts {holder} at {path}, and one path inside a "
        f"container holds one file system — odin renders `-v <host dir>:{path}` per mounted file "
        f"system (`runtime/colima.py`), so only {holder} is mounted there. Give one of them a "
        "different `path`, or draw one of the edges elsewhere"
    )


_DNS_PORTED_FACTS = {
    "alb": "the `ALB_ENDPOINT` fact — `http://127.0.0.1:<a dynamic port>`. The proxy is published "
           "on a DYNAMIC host port because a fixed 80 would collide across load balancers and envs",
    "rds": "the `endpoint` fact — `host.docker.internal:<a dynamic port>`. A Postgres container "
           "is published on a dynamic host port for the same reason",
}


def _dns_target_unsupported(zone_id: str, target_id: str, kind: str) -> str:
    """Why this target cannot have a DNS record. NEVER a record that resolves to
    something unreachable: odin's substrate for a route53 record is a hosts entry
    (`--add-host` on containers, `/etc/hosts` on VMs), a hosts entry is
    `<ip> <name>` and carries NO PORT, so a name pointing at a ported endpoint
    would resolve and then fail to connect."""
    ported = _DNS_PORTED_FACTS.get(kind)
    reason = (
        f"its address is {ported}. A hosts entry is `<ip> <name>` and cannot carry a port, so "
        f"the name would resolve and then fail to connect"
        if ported else
        f"odin's substrate for a DNS record is a hosts entry (`--add-host` on containers, "
        f"`/etc/hosts` on VMs), and only {'/'.join(_DNS_TARGET_KINDS)} publishes a bare address for "
        f"one to point at — a {kind} node publishes no address a hosts file can express"
    )
    return (
        f"{zone_id} (route53): DNS record for {target_id} ({kind}) — {reason}. "
        f"No aws_route53_record is emitted; use the node's own endpoint fact instead"
    )


def _alb_target_unsupported(alb_id: str, target_id: str, kind: str) -> str:
    reason = _ALB_NO_LAMBDA if kind == "lambda" else (
        f"only {'/'.join(_ALB_TARGET_KINDS)} nodes can be load-balancer targets in Simulate v1"
    )
    return f"{alb_id} (alb): target edge to {target_id} ({kind}) — {reason}"


def _not_in_terraform(stack: Stack, emitted: set[str], kind_by_id: dict[str, str]) -> list[str]:
    """What the generated Terraform carries differently from odin itself.

    Two entries are gone from this list and it is worth saying which, because a
    caveat that outlives its fix is a bug in this repo. Until v0.8.11 a drawn
    permission reached the file not at all ("this file grants it nothing"); until
    v0.8.14 it reached the file naming odin's node LABEL, which enforced inside
    odin and granted nothing on Amazon. Both are closed: the policy is a real
    `aws_iam_role_policy` and its `Resource` is a real ARN (`_ARN_FORMS`).

    What is LEFT is only what is still true. A grant drawn FROM a kind that
    cannot hold a role emits nothing at all, and since v0.8.12 the gateway
    authorizes from the applied IAM, so a grant that is not in the file is a
    grant that does not exist. A grant drawn TO something odin has no ARN shape
    for — in practice a `dst` that is not a resource on this canvas — keeps the
    bare label, and says so.

    `emitted` is the set of nodes that really got a policy, and it is passed in
    rather than re-derived, for the reason pass 2 gives about its own companion
    blocks: re-deriving a condition instead of reading what actually happened is
    how the two drift.
    """
    return [
        message
        for edge in stack.edges if edge.kind == "iam" and edge.perms
        for message in [_grant_gap(edge, emitted, kind_by_id.get(edge.dst, ""))] if message
    ]


def _grant_gap(edge, emitted: set[str], dst_kind: str) -> str | None:
    """How this drawn permission differs from what the file carries, or None
    when the file carries it fully."""
    if edge.src not in emitted:
        return (
            f"{edge.src} -> {edge.dst}: NO policy is emitted and nothing enforces this — "
            f"{edge.src!r} is not a kind that can hold an IAM role (only lambda, ec2 and ecs can), "
            f"so this permission has no effect after an apply"
        )
    if dst_kind not in _ARN_FORMS:
        return (
            f"{edge.src} -> {edge.dst}: the policy is emitted, but its Resource is the node label "
            f"{edge.dst!r} rather than an ARN — odin has no ARN shape for "
            f"{f'a {dst_kind} target' if dst_kind else f'{edge.dst!r}, which is not a resource on this canvas'}"
        )
    return None


def generate_tf(stack: Stack) -> TfProject:
    by_id = {r.id: r for r in stack.resources}
    # An IAM edge's target kind, which is what decides the ARN its `Resource`
    # names. Built from the canvas rather than from `refs`, so a `dst` that no
    # builder ever reached still resolves to a kind and is reported honestly.
    kind_by_id = {r.id: r.kind for r in stack.resources}
    ordered = sorted(stack.resources, key=lambda r: (r.kind, r.id))
    used_names: dict[str, set[str]] = {}
    hcl_name_by_id: dict[str, str] = {}
    refs: Refs = {}
    blocks: list[tuple[tuple[str, str], str]] = []
    unsupported: list[str] = []
    wiring_errors: list[str] = []
    # Which workloads a drawn permission points away from -- read once, because
    # pass 1 reserves a role for each of them and the policy pass below emits it.
    granted_ids = _granted_ids(stack)

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
        # v0.8.11 generalises this from lambda to every workload an IAM edge can
        # start from. A drawn permission is emitted as a real
        # `aws_iam_role_policy`, and a policy needs a role to hang on -- so an
        # ec2 or ecs node that is granted something gets the same auto-role a
        # lambda has always had. A node nobody granted anything keeps emitting
        # exactly what it did before.
        if res.kind == "lambda" and not _field(res, "role", "").strip():
            role_name = unique_name(
                sanitize_name(f"{res.id}_role"), used_names.setdefault("aws_iam_role", set()),
            )
            refs[_lambda_role_key(res.id)] = ("iam_role", role_name)
        elif res.kind in _GRANTABLE_KINDS and res.id in granted_ids:
            role_name = unique_name(
                sanitize_name(f"{res.id}_role"), used_names.setdefault("aws_iam_role", set()),
            )
            refs[_workload_role_key(res.id)] = ("iam_role", role_name)
            # An ec2 instance reaches a role through an instance profile, and
            # `_ec2` runs in pass 2 -- so the profile's name is reserved here,
            # exactly like the role, or the builder has nothing to reference.
            if res.kind == "ec2":
                refs[_instance_profile_key(res.id)] = ("iam_instance_profile", unique_name(
                    sanitize_name(f"{res.id}_profile"),
                    used_names.setdefault("aws_iam_instance_profile", set()),
                ))
        # V5c: the FIRST ecs node seen reserves the one shared cluster's HCL
        # name -- every later ecs node's `_ecs` builder (pass 2) just reads
        # it back, same reservation technique as the lambda auto-role above.
        if res.kind == "ecs" and _ECS_CLUSTER_KEY not in refs:
            cluster_name = unique_name(
                sanitize_name(_ECS_CLUSTER_NAME), used_names.setdefault("aws_ecs_cluster", set()),
            )
            refs[_ECS_CLUSTER_KEY] = ("ecs_cluster", cluster_name)

    # Pass 1.4 (v0.8.15) — LOG SINK edges. A logs node edged to a workload takes
    # the AWS name that workload's substrate actually ships to, so the drawn
    # group is the one that receives (see `_LOG_DESTINATIONS`). `edge_declined`
    # is the `lambda_declined` shape: a reason pass 2 substitutes for the
    # builder, so nothing here has to reach into a builder's return value.
    #
    # `aws_names` is the same answer for the IAM half -- `_policy_document`
    # needs it or a granted PutLogEvents into the real group is denied.
    edge_declined: dict[str, str] = {}
    aws_names: dict[str, str] = {}
    for logs_id, workload_ids in _kind_pair_edges(stack, by_id, ("logs",), _LOG_SHIPPING_KINDS).items():
        if logs_id not in hcl_name_by_id:
            continue
        destinations = sorted({_log_destination(by_id[node_id]) for node_id in workload_ids})
        if len(destinations) > 1:
            edge_declined[logs_id] = _two_log_destinations(logs_id, destinations)
            continue
        refs[_log_sink_key(logs_id)] = ("log_group_name", destinations[0])
        aws_names[logs_id] = destinations[0]

    # Pass 1.45 (v0.8.15) — ECR IMAGE edges. An ecr node edged to an ecs service
    # authors that service's container image (`_ecs_image`); edged to a lambda
    # it authors nothing, and says so through `wiring_errors` rather than
    # `unsupported` -- the function itself is built and applied perfectly well.
    for workload_id, repo_ids in _kind_pair_edges(stack, by_id, _WIRED_KINDS, ("ecr",)).items():
        repos = sorted(repo_id for repo_id in repo_ids if repo_id in hcl_name_by_id)
        if workload_id not in hcl_name_by_id or not repos:
            continue
        if by_id[workload_id].kind == "lambda":
            wiring_errors.append(_ecr_lambda_note(workload_id, repos))
            continue
        if len(repos) > 1:
            edge_declined[workload_id] = _two_images(repos)
            continue
        refs[_ecr_image_key(workload_id)] = ("ecr_repository", hcl_name_by_id[repos[0]])

    # Pass 1.5 (W2.5) — resolve ALB TARGET EDGES, which pass 1 can't do (it
    # walks resources, and an edge's other end may not be named yet) and pass 2
    # can't either (a builder only sees `(res, refs)`, never the edge list). An
    # edge between an `alb` node and a compute node means "this load balancer
    # fronts that compute": accepted in EITHER drawn direction, since which end
    # the user started from carries no meaning. `alb` deliberately isn't an IAM
    # target on the canvas (see ui/src/lib/iam.ts), so an alb<->compute edge is
    # unambiguously this and nothing else.
    #
    # The two supported targets need OPPOSITE machinery, which is why one is a
    # synthetic `refs` entry and the other a list read by the companion pass:
    # an ECS service registers its own tasks, so it emits a `load_balancer`
    # block naming the target group (`_ecs`); an EC2 instance is registered by
    # tofu, through an `aws_lb_target_group_attachment` naming the instance id.
    alb_instance_targets: dict[str, list[str]] = {}
    for edge in sorted(stack.edges, key=lambda e: (e.src, e.dst)):
        for alb_id, target_id in ((edge.src, edge.dst), (edge.dst, edge.src)):
            alb_res, target_res = by_id.get(alb_id), by_id.get(target_id)
            if alb_res is None or target_res is None or alb_res.kind != "alb" or alb_id not in hcl_name_by_id:
                continue
            if target_res.kind == "ec2":
                targets = alb_instance_targets.setdefault(alb_id, [])
                targets += [target_id] if target_id not in targets else []
            elif target_res.kind in _ALB_TARGET_KINDS:
                refs[_alb_target_key(target_id)] = ("alb_target_group", f"{hcl_name_by_id[alb_id]}_tg")
            else:
                unsupported.append(_alb_target_unsupported(alb_id, target_id, target_res.kind))
            break  # one edge is one (alb, target) pair, whichever way it was drawn

    # Pass 1.55 (v0.8.19) — EFS MOUNT edges. An efs node edged to an ecs service
    # or a lambda means "that workload mounts this file system", at the efs
    # node's own `path`. The edge is the only thing that can say who mounts what,
    # and unlike every other pair in this file the relation is MANY-TO-MANY on
    # purpose: two workloads sharing one file system is the entire feature, so
    # nothing de-duplicates by file system the way the volume pass does.
    #
    # KEYED ON THE TWO NODE KINDS AND NEVER ON `edge.kind`, per the EDGE-AUTHORED
    # FIELDS note at the top of this module -- and here the hazard is LIVE, which
    # took two corrections to establish. The first draft of this comment claimed
    # no saved canvas could hold an efs node. Measured from git instead:
    #   * `ac796d6` (2026-06-20) shipped the tile draggable, sublabel
    #     'Elastic file system' -- NO `(placeholder)` marker at all;
    #   * `1b158fe` (2026-07-26) added the marker ("a sidebar tile now says
    #     whether Apply can actually build it");
    #   * `41d214b` (2026-07-27) hid placeholders, and its own message says the
    #     hiding is PALETTE-ONLY -- a canvas already holding one still renders.
    # So for five weeks it looked like an ordinary Storage tile, not a warned-off
    # one, which is what makes such saved canvases likely rather than merely
    # possible. Every edge from that node is typed `network` (the
    # unregistered-pair catch-all).
    #
    # It is milder than ebs's, and the difference is worth being exact about
    # rather than dramatising: `efs` was never in `translate.py::_KIND`, so Apply
    # skipped the node for that whole window and no file system was ever created
    # -- there is nothing for tofu to tear down. What a `edge.kind == "mount"`
    # gate WOULD do is silently ignore the old edge, so the user's first Apply
    # after this change creates a file system and mounts it NOWHERE, with no
    # error at all. A drawn line that does nothing is the exact bug the edge
    # registry exists to kill.
    #
    # It runs BEFORE pass 2 because `_lambda` reads its answer out of `refs`,
    # which is why the buildability gate here is `_efs_fault` and not `built_ids`
    # (see that function).
    efs_mounts: dict[str, list[tuple[str, str]]] = {}
    for consumer_id, efs_ids in sorted(
        _kind_pair_edges(stack, by_id, _EFS_MOUNT_KINDS, ("efs",)).items()
    ):
        mounts = [
            (efs_id, _field(by_id[efs_id], "path", _DEFAULT_EFS_PATH).strip())
            for efs_id in sorted(efs_ids)
            if efs_id in hcl_name_by_id and not _efs_fault(by_id[efs_id])
        ]
        if not mounts or consumer_id not in hcl_name_by_id:
            continue
        if by_id[consumer_id].kind == "lambda" and len(mounts) > 1:
            edge_declined[consumer_id] = _two_file_systems([efs_id for efs_id, _ in mounts])
            continue
        # The mount path is checked HERE and not in `_efs`, because only a lambda
        # has a pattern to break. The declined node is the FUNCTION rather than
        # the file system, for the smallest honest blast radius: the file system
        # is perfectly buildable, any ecs service mounting it still mounts it, and
        # what odin cannot do is exactly the one thing named -- mount it on that
        # function. Declining the efs node instead took the file system and every
        # other consumer's mount down with it.
        #
        # A lambda has exactly ONE mount by the time this line runs (an empty
        # list and a list of two both `continue` above), so `mounts[0]` is it.
        mounted_id, mount_path = mounts[0]
        if by_id[consumer_id].kind == "lambda" and not _MOUNT_PATH.fullmatch(mount_path):
            edge_declined[consumer_id] = _lambda_mount_path(mounted_id, mount_path)
            continue
        # One container path holds one file system, and TWO efs nodes on one
        # service is the default collision rather than an exotic one: the tile
        # defaults `path` to /mnt/efs, so drawing a second one and edging it to
        # the same service collides immediately. The first by sorted id keeps the
        # path and the second is declined by name.
        taken: dict[str, str] = {}
        for efs_id, path in mounts:
            holder = taken.setdefault(path, efs_id)
            if holder != efs_id:
                unsupported.append(_mount_path_taken(efs_id, consumer_id, holder, path))
                continue
            efs_mounts.setdefault(consumer_id, []).append((efs_id, path))
        # A lambda's single mount travels to `_lambda` through `refs`; an ecs
        # service's mounts travel to the task definition pass, which needs a list.
        one_mount = efs_mounts.get(consumer_id, [])[:1] if by_id[consumer_id].kind == "lambda" else []
        for efs_id, path in one_mount:
            refs[_efs_mount_key(consumer_id)] = (path, f"{hcl_name_by_id[efs_id]}_ap")

    # Which file systems need an `aws_efs_access_point` companion: the ones a
    # LAMBDA mounts, and no others (the `_ECS_CLUSTER_KEY` rule -- never a
    # dangling resource on a canvas that does not need one). An ecs service needs
    # none; its `efs_volume_configuration` names the file system directly.
    lambda_mounted = {
        efs_id
        for consumer_id, mounts in efs_mounts.items() if by_id[consumer_id].kind == "lambda"
        for efs_id, _ in mounts
    }

    # Pass 1.6 — reserve each granted workload's policy name, so pass 2 can
    # point that workload's `depends_on` at it. This runs as its own pass, after
    # every resource name exists, because the gate is `_grant_role_ref`: a
    # policy needs a role to hang on, and a lambda's DRAWN role is an ordinary
    # resource that pass 1 may not have reached yet when the lambda is visited.
    #
    # The gate is shared with the emission loop below for the reason pass 2's
    # comment gives: a reservation the emission loop then declines to fill
    # leaves a `depends_on` pointing at a block that does not exist, and an
    # unresolvable reference fails `tofu plan` for the WHOLE project.
    for res in ordered:
        if _grant_role_ref(res, stack, refs) is not None:
            refs[_grants_key(res.id)] = ("iam_role_policy", unique_name(
                sanitize_name(f"{res.id}_grants"),
                used_names.setdefault("aws_iam_role_policy", set()),
            ))

    # Pass 1.7 (v0.8.14) — resolve every lambda's DEPLOYMENT PACKAGE once,
    # before pass 2, because the answer is needed in two places: pass 2 must
    # DECLINE a function whose package odin cannot build (a `sourceDir` that
    # isn't a directory, a handler whose module the tree doesn't contain), and
    # the zip pass at the bottom needs the bytes. Resolving here rather than
    # inside `_lambda` keeps it to ONE directory walk, and — the part that
    # actually matters — keeps a declined function from emitting an
    # `aws_lambda_function` whose `filename` / `filebase64sha256()` name a zip
    # that was never written. That is not a bad node, it is a `tofu plan` that
    # fails for the WHOLE project, so every other resource on the canvas stops
    # applying too (the same failure the `built_ids` note below describes for
    # the alb companions).
    lambda_packages: dict[str, dict[str, bytes]] = {}
    lambda_declined: dict[str, str] = {}
    for res in ordered:
        if res.kind != "lambda" or res.id not in hcl_name_by_id:
            continue
        package = _lambda_package(res)
        if isinstance(package, str):
            lambda_declined[res.id] = package
            continue
        lambda_packages[res.id] = package

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
        built = lambda_declined.get(res.id) or edge_declined.get(res.id) or _BUILDERS[res.kind](res, refs)
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
        # AUTHORABLE since v0.8.21, and `true` is still what an unmarked edge
        # produces -- `Edge.raw_message_delivery` defaults to True and
        # `translate._edges` reads an absent field as True, so every canvas
        # drawn before the checkbox existed emits the identical bytes. Only an
        # edge explicitly turned off writes `false`, and then the IMPORT reads
        # it back (`import_tf._subscription_edge`) instead of substituting
        # odin's own value, which is what the round trip used to do.
        attrs = {
            "topic_arn": f"aws_sns_topic.{topic_name}.arn",
            "protocol": quote("sqs"),
            "endpoint": f"aws_sqs_queue.{queue_name}.arn",
            "raw_message_delivery": "true" if edge.raw_message_delivery else "false",
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

    # v0.8.11: every drawn IAM edge becomes REAL Terraform.
    #
    # Until now a permission you drew existed only in odin's Stack: the gateway
    # compiled it and enforced it, and `main.tf` contained nothing about it. That
    # made the generated project incomplete (taken to Amazon it granted nothing)
    # and made a canvas round trip through Terraform lose the whole security
    # posture in silence.
    #
    # One `aws_iam_role_policy` per granted workload, carrying every action that
    # workload was granted. The role it hangs on is the one pass 1 reserved: a
    # lambda's own (drawn or auto-generated), or the auto-role an ec2/ecs node
    # gets the moment something is granted to it.
    granted_with_a_policy: set[str] = set()
    for res in ordered:
        if res.id not in granted_ids:
            continue
        role_ref = _grant_role_ref(res, stack, refs)
        if role_ref is None:
            continue
        grants = [e for e in stack.edges if e.kind == "iam" and e.src == res.id and e.perms]
        _, role_name = role_ref
        name = refs[_grants_key(res.id)][1]
        attrs = {
            "name": quote(f"{res.id}-grants"),
            "role": f"aws_iam_role.{role_name}.name",
            "policy": quote(_policy_document(grants, kind_by_id, aws_names)),
        }
        blocks.append((("iam_role_policy", f"__grants__{res.id}"), _block("aws_iam_role_policy", name, attrs)))
        granted_with_a_policy.add(res.id)
        # The workload must not start before the policy that authorizes it. tofu
        # is free to order two resources that merely share a role either way, so
        # without this a container could come up, call S3 and get AccessDenied
        # for a permission that was drawn and applied -- a race that would look
        # exactly like a wrong grant. `_grant_dependency` feeds this into the
        # workload's own `depends_on`.


    # An ec2 node reaches its role through an INSTANCE PROFILE, which is how AWS
    # models it and what `iamctl` already implements (CreateInstanceProfile,
    # AddRoleToInstanceProfile). Emitted only for an instance that was granted
    # something, so an ungranted canvas is byte-identical to before.
    for res in ordered:
        role_ref = refs.get(_workload_role_key(res.id))
        if res.kind != "ec2" or role_ref is None:
            continue
        profile_ref = refs.get(_instance_profile_key(res.id))
        if profile_ref is None:
            continue
        blocks.append((
            ("aws_iam_instance_profile", res.id),
            _block("aws_iam_instance_profile", profile_ref[1], {
                "name": quote(f"{res.id}-profile"),
                "role": f"aws_iam_role.{role_ref[1]}.name",
            }),
        ))

    # ...and the auto-role itself for an ec2/ecs node that was granted something.
    # A lambda's auto-role is emitted by its own pass below; this is the same
    # block for the two kinds that never had one.
    for res in ordered:
        role_ref = refs.get(_workload_role_key(res.id))
        if role_ref is None:
            continue
        _, role_name = role_ref
        nested = f"  assume_role_policy = {_LAMBDA_TRUST_POLICY}"
        blocks.append((
            ("aws_iam_role", f"__workload__{res.id}"),
            _block("aws_iam_role", role_name, {"name": quote(f"{res.id}-role")}, nested),
        ))

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
        block = _block("aws_ecs_cluster", cluster_name, {"name": quote(_ECS_CLUSTER_NAME)})
        blocks.append((("aws_ecs_cluster", "__cluster__"), block))

    # v0.8.19: the `aws_efs_access_point` companion, for a file system some
    # LAMBDA mounts and for nothing else -- one canvas node to two tf resources,
    # the same shape as the ecs task definition and the lambda auto-role below.
    #
    # It exists because `aws_lambda_function.file_system_config.arn` is an ACCESS
    # POINT arn (AWS's own pattern for that argument ends
    # `access-point/fsap-[a-f0-9]{17}`), so a file-system arn there is a project
    # real AWS rejects. `built_ids` gates it for the reason pass 2 records: an
    # access point naming an `aws_efs_file_system` that pass 2 declined is an
    # unresolvable reference, and one of those fails `tofu plan` for the WHOLE
    # project. `root_directory { path = "/" }` because the file system's own root
    # IS the shared directory -- odin has no per-consumer sub-directory model,
    # and inventing one would be a claim the substrate does not back.
    for res in ordered:
        own_name = hcl_name_by_id.get(res.id)
        if res.id not in lambda_mounted or own_name is None or res.id not in built_ids:
            continue
        blocks.append((("aws_efs_access_point", res.id), _block(
            "aws_efs_access_point", f"{own_name}_ap",
            {"file_system_id": f"aws_efs_file_system.{own_name}.id"},
            '  root_directory {\n    path = "/"\n  }',
        )))

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
        # v0.8.19: the file systems this service mounts, in the TWO places ECS
        # needs them -- a `volume` block naming the file system, and a
        # `mountPoints` entry inside the container definition naming that volume
        # back. Either one alone mounts nothing at all: a volume no container
        # references is dead weight, and a mount point naming a volume that does
        # not exist is a RegisterTaskDefinition ECS rejects. `built_ids` is the
        # gate for the reason pass 2 records -- `aws_efs_file_system.<n>.id` for
        # a declined node is an unresolvable reference, which fails the plan for
        # the whole project.
        mounts = tuple((efs_id, path) for efs_id, path in efs_mounts.get(res.id, []) if efs_id in built_ids)
        container_json = quote(json.dumps(_ecs_container_definitions(res, refs, mounts)))
        volumes = "\n\n".join(
            "  volume {\n"
            f"    name = {quote(efs_id)}\n"
            "\n"
            "    efs_volume_configuration {\n"
            f"      file_system_id = aws_efs_file_system.{hcl_name_by_id[efs_id]}.id\n"
            '      root_directory = "/"\n'
            "    }\n"
            "  }"
            for efs_id, _ in mounts
        )
        nested = "\n\n".join(part for part in (f"  container_definitions = {container_json}", volumes) if part)
        # `cpu`/`memory` only when the canvas actually says so. odin ENFORCES
        # memory -- `compute/tasks.py::_memory_mib` turns the taskdef's value
        # into the container's hard cap -- and until now nothing emitted it, so
        # every task ran at the 512 MiB fallback and a container needing more
        # was OOM-killed with no field to say otherwise. The gateway already
        # carried both (`ecsctl` stores them from RegisterTaskDefinition and
        # passes them to `run_task`); this was the missing first link.
        #
        # ABSENT rather than an explicit 512 when unset: writing the default
        # into the HCL would freeze it into every canvas ever applied, so
        # changing the default later would silently not reach them.
        # `_memory_mib(None)` supplies it at the point that enforces it.
        sized = {
            key: quote(value)
            for key in ("cpu", "memory")
            if (value := _field(res, key, "").strip())
        }
        # `task_role_arn` when something was granted to this service: the policy
        # has to be reachable FROM the workload, or the gateway can only guess
        # which node a role belongs to by its name. With this the link is stated
        # in the file, which is what lets enforcement read the applied IaC.
        role_ref = refs.get(_workload_role_key(res.id))
        attached = {"task_role_arn": f"aws_iam_role.{role_ref[1]}.arn"} if role_ref else {}
        attrs = {
            "family": quote(res.id),
            **sized,
            **attached,
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
        # v0.8.15: each EC2 instance this load balancer fronts, registered by
        # tofu. The gateway half was already there and unreachable: elbv2ctl's
        # `_target_host` resolves an `i-...` target Id through
        # `stores.ec2compute` to the VM's real address, and nothing on the
        # canvas could ever produce such a target because `_ALB_TARGET_KINDS`
        # excluded ec2.
        #
        # `target_id` is the INSTANCE ID (`aws_instance.<n>.id`), which is what
        # makes `_target_host`'s branch fire; the port is the target group's own
        # (`_alb_ports`), so the instance is dialled where the group listens.
        # `built_ids` gates it for the reason pass 2 records -- an attachment
        # referencing an `aws_instance` that pass 2 declined is an unresolvable
        # reference, which fails `tofu plan` for the WHOLE project. That
        # instance's own decline (a node outside any Subnet, say) already names
        # the cause in `unsupported`, so nothing is lost silently here.
        for target_id in sorted(alb_instance_targets.get(res.id, [])):
            if target_id not in built_ids:
                continue
            instance_name = hcl_name_by_id[target_id]
            attachment = _block(
                "aws_lb_target_group_attachment", f"{own_name}_{instance_name}_attach",
                {
                    "target_group_arn": f"aws_lb_target_group.{own_name}_tg.arn",
                    "target_id": f"aws_instance.{instance_name}.id",
                    "port": target_port,
                },
            )
            blocks.append((("aws_lb_target_group_attachment", f"{res.id}.{target_id}"), attachment))

    # v0.8.18: VOLUME ATTACHMENTS. An ebs node and an ec2 node joined by an edge
    # is a volume attachment, and the edge is the ONLY thing that can say which
    # instance -- an `aws_ebs_volume` on its own attaches to nothing, so an ebs
    # node with no edge stays a real, free-standing `available` volume.
    #
    # KEYED ON THE TWO NODE KINDS AND NEVER ON `edge.kind`, for the reason the
    # EDGE-AUTHORED FIELDS note at the top of this module gives -- and here the
    # cost of getting that wrong is the worst in the file: every canvas saved
    # before the edge-type registry carries `kind: "network"`, so gating on the
    # type name would drop the attachment from the generated HCL and make the
    # next `tofu apply` DETACH a disk that has data on it. Direction is not
    # significant either (`_kind_pair_edges` reads both), because which end the
    # user started the drag from carries no meaning.
    #
    # `built_ids` gates the emission for the reason pass 2 records: an attachment
    # naming an `aws_instance` or an `aws_ebs_volume` that pass 2 declined is an
    # unresolvable reference, which fails `tofu plan` for the WHOLE project. The
    # declined node's own entry in `unsupported` already names the cause, so
    # nothing is lost silently here.
    attached_to: dict[str, str] = {}
    for instance_id, volume_ids in sorted(
        _kind_pair_edges(stack, by_id, _VOLUME_HOST_KINDS, ("ebs",)).items()
    ):
        # The SLOT is the volume's position among ALL of this instance's attached
        # volumes, built or not, so declining one (a non-numeric size) does not
        # renumber the devices of the others -- a renumbering tofu would apply as
        # a detach-and-reattach of live disks.
        for slot, volume_id in enumerate(sorted(volume_ids)):
            holder = attached_to.setdefault(volume_id, instance_id)
            if holder != instance_id:
                unsupported.append(_volume_already_attached(volume_id, instance_id, holder))
                continue
            if slot >= len(_EBS_DEVICE_NAMES):
                unsupported.append(_too_many_volumes(volume_id, instance_id))
                continue
            if instance_id not in built_ids or volume_id not in built_ids:
                continue
            volume_name, instance_name = hcl_name_by_id[volume_id], hcl_name_by_id[instance_id]
            # Its own name namespace: `a_b` + `c` and `a` + `b_c` both compose to
            # `a_b_c_attach`, and two attachments sharing an HCL name is a file
            # that does not parse.
            name = unique_name(
                f"{volume_name}_{instance_name}_attach",
                used_names.setdefault("aws_volume_attachment", set()),
            )
            attachment = _block("aws_volume_attachment", name, {
                "device_name": quote(_EBS_DEVICE_NAMES[slot]),
                "instance_id": f"aws_instance.{instance_name}.id",
                "volume_id": f"aws_ebs_volume.{volume_name}.id",
            })
            blocks.append((("aws_volume_attachment", f"{volume_id}.{instance_id}"), attachment))

    # apigateway (v0.8.19): each API's `$default` stage, plus one integration and
    # TWO routes per target edge. See the `_apigateway` note above for why two.
    #
    # KEYED ON THE TWO NODE KINDS AND NEVER ON `edge.kind`, the rule every
    # builder here holds: a canvas saved before the edge-type registry carries
    # `kind: "network"`, and gating on the type name would drop the routes and
    # make the next apply serve 404 for every path that worked yesterday.
    # Direction is not significant either -- which end the user dragged from
    # carries no meaning.
    #
    # `built_ids` gates each target for the reason pass 2 records: an integration
    # naming an `aws_lambda_function` or `aws_ecs_service` that pass 2 declined
    # is an unresolvable reference, which fails `tofu plan` for the WHOLE
    # project. That node's own `unsupported` entry already names the cause.
    apigw_targets = _kind_pair_edges(stack, by_id, ("apigateway",), _APIGW_TARGET_KINDS)
    for res in ordered:
        own_name = hcl_name_by_id.get(res.id)
        if res.kind != "apigateway" or own_name is None or res.id not in built_ids:
            continue
        blocks.append((("aws_apigatewayv2_stage", res.id), _block(
            "aws_apigatewayv2_stage", f"{own_name}_stage",
            {
                "api_id": f"aws_apigatewayv2_api.{own_name}.id",
                "name": quote(_APIGW_STAGE),
                # The stage odin serves is `$default`, whose invoke path has no
                # stage segment -- which is what makes the nginx prefix and the
                # route key agree about what `/orders` means. `auto_deploy` is
                # `$default`'s only sane setting: without it a route change needs
                # an explicit deployment that odin has no concept of.
                "auto_deploy": "true",
            },
        )))
        for target_id in sorted(apigw_targets.get(res.id, [])):
            if target_id not in built_ids:
                continue
            target = by_id[target_id]
            target_name = hcl_name_by_id[target_id]
            integration_name = unique_name(
                f"{own_name}_{target_name}",
                used_names.setdefault("aws_apigatewayv2_integration", set()),
            )
            blocks.append((("aws_apigatewayv2_integration", f"{res.id}.{target_id}"), _block(
                "aws_apigatewayv2_integration", integration_name,
                {
                    "api_id": f"aws_apigatewayv2_api.{own_name}.id",
                    **_apigw_integration_attrs(target, target_name),
                },
            )))
            for suffix, route_key in zip(("root", "proxy"), _apigw_route_keys(target_id), strict=True):
                blocks.append((("aws_apigatewayv2_route", f"{res.id}.{target_id}.{suffix}"), _block(
                    "aws_apigatewayv2_route", f"{integration_name}_{suffix}",
                    {
                        "api_id": f"aws_apigatewayv2_api.{own_name}.id",
                        "route_key": quote(route_key),
                        "target": f'"integrations/${{aws_apigatewayv2_integration.{integration_name}.id}}"',
                    },
                )))

    # v0.8.19: DNS RECORDS. A route53 node and a node it is edged to is an
    # `aws_route53_record`, and the edge is the ONLY thing that can say what a
    # name points at -- an `aws_route53_zone` on its own resolves nothing, so a
    # route53 node with no edge is a real, empty hosted zone and says so.
    #
    # KEYED ON THE TWO NODE KINDS AND NEVER ON `edge.kind`, the rule every
    # companion pass in this file holds (see the EDGE-AUTHORED FIELDS note at the
    # top). `route53` has been a drawable catalog tile since long before it had a
    # builder, so canvases carrying a route53 edge typed `network` already exist;
    # gating on the type name would silently emit no record for any of them.
    #
    # EVERY OTHER TARGET IS DECLINED BY NAME. That is the whole difficulty of
    # this kind, and it is measured rather than assumed -- see `_DNS_TARGET_KINDS`
    # for the four real fact shapes and `_dns_target_unsupported` for what the
    # user is told. Declining is the honest outcome: a record pointing at
    # `http://127.0.0.1:<dynamic port>` would resolve to 127.0.0.1 and fail to connect.
    for zone_id, target_ids in sorted(
        _kind_pair_edges(stack, by_id, ("route53",), tuple(_TF_TYPES)).items()
    ):
        for target_id in sorted(target_ids):
            kind = kind_by_id.get(target_id, "")
            if kind not in _DNS_TARGET_KINDS:
                unsupported.append(_dns_target_unsupported(zone_id, target_id, kind))
                continue
            fqdn = f"{target_id}.{zone_id}"
            bad = _bad_dns_name(fqdn)
            if bad:
                unsupported.append(
                    f"{zone_id} (route53): DNS record for {target_id} ({kind}) — {bad}"
                )
                continue
            # `built_ids` gates the emission for the reason pass 2 records: a
            # record naming an `aws_route53_zone` or an `aws_instance` that pass 2
            # declined is an unresolvable reference, which fails `tofu plan` for
            # the WHOLE project. The declined node's own entry in `unsupported`
            # already names the cause, so nothing is lost silently here.
            if zone_id not in built_ids or target_id not in built_ids:
                continue
            zone_name, instance_name = hcl_name_by_id[zone_id], hcl_name_by_id[target_id]
            name = unique_name(
                f"{zone_name}_{instance_name}", used_names.setdefault("aws_route53_record", set()),
            )
            record = _block("aws_route53_record", name, {
                "zone_id": f"aws_route53_zone.{zone_name}.zone_id",
                "name": quote(fqdn),
                "type": quote(_DNS_RECORD_TYPE),
                "ttl": _DNS_RECORD_TTL,
                # The instance's PRIVATE address, not its mesh one -- see the
                # `_DNS_TARGET_KINDS` note for why the overlay address would be
                # the same trap one layer down.
                "records": f"[aws_instance.{instance_name}.private_ip]",
            })
            blocks.append((("aws_route53_record", f"{zone_id}.{target_id}"), record))


    blocks.sort(key=lambda b: b[0])
    main_tf = "\n\n".join([HEADER, provider_block(), *(text for _, text in blocks)]) + "\n"

    # V4c: materialize each lambda's code into the zip its own HCL block
    # references by filename -- odin owns this pre-tofu, not a
    # `data archive_file` round-trip (module docstring). The entry filename
    # MUST match `_lambda_entry`'s choice for the SAME runtime, or the
    # deployed zip and the `handler` string would disagree. BYTE-DETERMINISTIC
    # (`_deterministic_zip`): identical members must produce an identical
    # archive, or `source_code_hash` churns on every translate.
    #
    # Gated on `built_ids` (v0.8.14): a function pass 2 declined has no
    # `aws_lambda_function` block, so a zip written for it would be a file
    # nothing references -- `simulate/workspace.py::_prune_stale` would delete
    # it on the next materialize anyway, and writing it in the first place
    # invites the reader to think the block exists.
    binary_files: dict[str, bytes] = {}
    for res in ordered:
        name = hcl_name_by_id.get(res.id)
        if res.kind != "lambda" or name is None or res.id not in built_ids:
            continue
        binary_files[f"{name}.zip"] = _deterministic_zip(lambda_packages[res.id])

    return TfProject(
        files={"main.tf": main_tf}, unsupported=unsupported,
        wiring_errors=wiring_errors, binary_files=binary_files,
        not_in_terraform=_not_in_terraform(stack, granted_with_a_policy, kind_by_id),
    )
