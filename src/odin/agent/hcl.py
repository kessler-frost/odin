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


def _s3(res: ResourceDesired) -> tuple[str, dict[str, str], str]:
    return "aws_s3_bucket", {"bucket": quote(res.id)}, ""


def _sqs(res: ResourceDesired) -> tuple[str, dict[str, str], str]:
    return "aws_sqs_queue", {"name": quote(res.id)}, ""


def _sns(res: ResourceDesired) -> tuple[str, dict[str, str], str]:
    return "aws_sns_topic", {"name": quote(res.id)}, ""


def _dynamodb(res: ResourceDesired) -> tuple[str, dict[str, str], str]:
    hash_key = _field(res, "hashKey", "id")
    attrs = {
        "name": quote(res.id),
        "billing_mode": quote("PAY_PER_REQUEST"),
        "hash_key": quote(hash_key),
    }
    nested = f'  attribute {{\n    name = {quote(hash_key)}\n    type = "S"\n  }}'
    return "aws_dynamodb_table", attrs, nested


_BUILDERS = {"s3": _s3, "sqs": _sqs, "sns": _sns, "dynamodb": _dynamodb}


def generate_tf(stack: Stack) -> TfProject:
    by_id = {r.id: r for r in stack.resources}
    used_names: dict[str, set[str]] = {}
    hcl_name_by_id: dict[str, str] = {}
    blocks: list[tuple[tuple[str, str], str]] = []
    unsupported: list[str] = []

    for res in sorted(stack.resources, key=lambda r: (r.kind, r.id)):
        builder = _BUILDERS.get(res.kind)
        if builder is None:
            reason = _UNSUPPORTED_REASONS.get(res.kind, f"{res.kind} — not supported in Simulate v1")
            unsupported.append(f"{res.id} ({res.kind}): {reason}")
            continue
        resource_type, attrs, nested = builder(res)
        name = unique_name(sanitize_name(res.id), used_names.setdefault(resource_type, set()))
        hcl_name_by_id[res.id] = name
        blocks.append(((res.kind, res.id), _block(resource_type, name, attrs, nested)))

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

    blocks.sort(key=lambda b: b[0])
    main_tf = "\n\n".join([HEADER, provider_block(), *(text for _, text in blocks)]) + "\n"
    return TfProject(files={"main.tf": main_tf}, unsupported=unsupported)
