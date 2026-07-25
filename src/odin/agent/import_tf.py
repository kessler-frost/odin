"""S4 — TF import: Terraform -> canvas, the reverse of S3b's `translate()`.

Two modes (research-verified, docs/superpowers/research/research-tofu-provider.md
§5 "Import direction"):

(a) **deterministic** (`parse_hcl*`): parse an existing project's HCL for every
    supported resource type (`_KIND` below, plus the two COMPANION types that
    fold into a node rather than becoming one: aws_sns_topic_subscription ->
    an edge, aws_secretsmanager_secret_version -> its secret node's value)
    into canvas nodes+edges. Unsupported types are LISTED, never dropped
    (northstar directive 5).

(b) **live-state import** (`import_live`): resources already exist in the
    env's backings (created out-of-band, or by a prior tofu apply) but were
    never authored as canvas nodes. Generates `import {}` blocks and runs
    `tofu plan -generate-config-out` in a throwaway scratch project against
    the SAME gateway + env-var injection every tofu invocation in Simulate
    uses, then parses the generated HCL with mode (a). The plan command's own
    exit code is not trusted as a success signal — research verified
    OpenTofu's generate-config-out reports a "Conflicting configuration
    arguments" error (a known quirk: it emits mutually-exclusive
    bucket/bucket_prefix and tags/tags_all) even though the file it wrote is
    complete and parses fine; since this file is only ever read back in here
    (never re-applied), that quirk doesn't matter for canvas hydration.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from odin.agent import hcl
from odin.aws.backings import ACCOUNT, REGION
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR

_GRID_STEP = 220

# aws_* resource type -> canvas node kind. Anything else is unsupported.
_KIND = {
    "aws_s3_bucket": "s3",
    "aws_sqs_queue": "sqs",
    "aws_sns_topic": "sns",
    "aws_dynamodb_table": "dynamodb",
    "aws_iam_role": "iam_role",
    "aws_cloudwatch_log_group": "logs",
    "aws_secretsmanager_secret": "secret",
    "aws_ssm_parameter": "ssm",
    "aws_elasticache_cluster": "elasticache",
}
# The attribute each supported type's human-facing name lives in (mirrors
# hcl.py's builders: s3 uses `bucket`, elasticache uses `cluster_id`,
# everything else uses `name`).
_NAME_ATTR = {
    "aws_s3_bucket": "bucket", "aws_sqs_queue": "name", "aws_sns_topic": "name",
    "aws_dynamodb_table": "name", "aws_iam_role": "name",
    "aws_cloudwatch_log_group": "name",
    "aws_secretsmanager_secret": "name", "aws_ssm_parameter": "name",
    "aws_elasticache_cluster": "cluster_id",
}
# canvas kind -> aws_* type, for mode (b) (the inverse of `_KIND`). iam_role,
# logs, secret and ssm have no backing to enumerate live resources from (all
# four are pure gateway models), so they stay out of the live path --
# elasticache likewise: its clusters exist only as gateway-model records plus a
# real container, and there's no `_import_id` shape to resolve one from outside
# a canvas Apply (mode (a), reading an existing HCL project, works fine).
_NO_LIVE_IMPORT = {"iam_role", "logs", "secret", "ssm", "elasticache"}
_TF_TYPE = {kind: rtype for rtype, kind in _KIND.items() if kind not in _NO_LIVE_IMPORT}

# The HCL arguments each kind CARRIES into the canvas -- so a round-trip through
# generate_tf reproduces them (finding #6). Any OTHER argument present on the
# resource is reported as a per-node warning rather than silently dropped
# (`__is_block__` is python-hcl2's internal block marker, never a real arg).
_IGNORED_ATTRS = {"__is_block__"}
_CARRIED_ATTRS = {
    "s3": {"bucket", "tags"},
    "sqs": {"name"},
    "sns": {"name"},
    "dynamodb": {"name", "hash_key", "range_key", "attribute"},
    "iam_role": {"name"},  # assume_role_policy/inline policies are NOT carried -> warned
    "logs": {"name", "retention_in_days", "tags"},
    # W2.4: `recovery_window_in_days` is carried in the sense that odin always
    # emits its own value (0 -- see hcl.py's `_secret`), so a differing imported
    # one is deliberately NOT surfaced as a dropped attribute the user must act
    # on. The VALUE isn't here at all: it lives on the companion
    # aws_secretsmanager_secret_version resource, assembled separately below.
    "secret": {"name", "description", "recovery_window_in_days", "tags"},
    "ssm": {"name", "type", "value", "description", "tags"},
    # engine/num_cache_nodes are carried because hcl.py always re-emits them
    # (redis, 1) -- so a round-trip reproduces the resource without warning
    # about arguments odin does model, just doesn't need on the node.
    "elasticache": {"cluster_id", "engine", "node_type", "num_cache_nodes", "tags"},
}
# The kinds whose user `tags` map survives the round trip as node data (hcl.py's
# `_tags_block` merges a node's own `tags` field back in for EVERY primary
# builder, so this is purely about which imports bother to read them).
_TAGGED_KINDS = {"s3", "logs", "secret", "ssm"}


class Unsupported(BaseModel):
    model_config = {"frozen": True}
    type: str
    name: str
    reason: str


class ImportResult(BaseModel):
    model_config = {"frozen": True}
    nodes: list[dict] = []
    edges: list[dict] = []
    unsupported: list[Unsupported] = []
    # A per-node note that a resource WAS imported but some of its in-resource
    # arguments couldn't be carried (finding #6) -- honest at attribute
    # granularity, not just resource-type granularity.
    warnings: list[str] = []
    # Set ONLY when the input itself failed to PARSE (finding #7) -- distinct
    # from a well-formed file that merely contains unsupported resources (which
    # stays a success with an `unsupported` list). The CLI treats a non-None
    # value as a hard error and exits non-zero, so a CI job's exit-code check
    # catches a broken import.
    parse_error: str | None = None


@dataclass(frozen=True)
class LiveResource:
    """A resource the caller asserts already exists in the env's backings —
    mode (b)'s input. `type` is the canvas kind (s3/sqs/sns/dynamodb), `id`
    the resource's AWS-facing name (bucket/queue/topic/table name)."""

    type: str
    id: str


def _label(rtype: str, rname: str, attrs: dict) -> str:
    name_attr = _NAME_ATTR.get(rtype)
    value = attrs.get(name_attr) if name_attr else None
    unquoted = hcl.unquote(value) if isinstance(value, str) else None
    # Only a plain literal (no leftover `${...}`, whether it was a bare
    # reference or a literal with an embedded interpolation) is trustworthy
    # as a human label; anything computed falls back to the resource's own
    # HCL name.
    if isinstance(unquoted, str) and "${" not in unquoted:
        return unquoted
    return rname


def _ref_target(value: object) -> str | None:
    """The resource NAME an interpolation like `${aws_sns_topic.alerts.arn}`
    points at (`alerts`), or None if `value` isn't an interpolation."""
    if not isinstance(value, str) or not (value.startswith("${") and value.endswith("}")):
        return None
    parts = value[2:-1].split(".")
    return parts[1] if len(parts) >= 2 else None


def _attribute_types(attrs: dict) -> dict[str, str]:
    """{attribute name -> type} from a dynamodb table's `attribute {}` blocks
    (python-hcl2 parses repeated blocks into a list of dicts)."""
    types: dict[str, str] = {}
    for block in attrs.get("attribute") or []:
        name = hcl.unquote(block.get("name"))
        atype = hcl.unquote(block.get("type"))
        if isinstance(name, str) and isinstance(atype, str):
            types[name] = atype
    return types


def _tags(attrs: dict) -> dict[str, str]:
    """The user `tags` map (odin's own `odin:node` management tag excluded, so
    a round-trip doesn't surface it as a user tag)."""
    raw = attrs.get("tags")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = hcl.unquote(key) or key
        val = hcl.unquote(value)
        if isinstance(name, str) and name != "odin:node" and isinstance(val, str):
            out[name] = val
    return out


def _dropped_attrs(kind: str, attrs: dict) -> list[str]:
    carried = _CARRIED_ATTRS.get(kind, set())
    return sorted(k for k in attrs if k not in carried and k not in _IGNORED_ATTRS)


def _node_data(kind: str, label: str, attrs: dict) -> dict:
    data: dict = {"label": label}
    if kind == "dynamodb":
        hash_key = hcl.unquote(attrs.get("hash_key"))
        data["hashKey"] = hash_key if isinstance(hash_key, str) else "id"
        types = _attribute_types(attrs)
        if data["hashKey"] in types:
            data["hashKeyType"] = types[data["hashKey"]]
        range_key = hcl.unquote(attrs.get("range_key"))
        if isinstance(range_key, str):
            data["rangeKey"] = range_key
            if range_key in types:
                data["rangeKeyType"] = types[range_key]
    if kind == "logs":
        # python-hcl2 parses an unquoted `retention_in_days = 14` as a real int
        # (verified empirically) -- the canvas field is text, so stringify it.
        retention = attrs.get("retention_in_days")
        if isinstance(retention, int):
            data["retentionInDays"] = str(retention)
    if kind == "ssm":
        # W2.4: the parameter's VALUE comes across as canvas data -- the same
        # trust model as any other import (SECURITY.md: treat an imported .tf
        # like a shell script), and it's the only way a round trip through
        # `generate_tf` can reproduce the parameter at all.
        for attr, field in (("type", "paramType"), ("value", "paramValue")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    if kind in ("secret", "ssm"):
        description = hcl.unquote(attrs.get("description"))
        if isinstance(description, str):
            data["description"] = description
    if kind in _TAGGED_KINDS:
        tags = _tags(attrs)
        if tags:
            data["tags"] = tags
    if kind == "elasticache":
        node_type = hcl.unquote(attrs.get("node_type"))
        if isinstance(node_type, str):
            data["nodeType"] = node_type
    return data


def parse_hcl(files: dict[str, str]) -> ImportResult:
    """Mode (a) core: `files` maps filename -> HCL text (a single-string
    caller passes `{"main.tf": text}`)."""
    try:
        triples = hcl.parse_tf(files)
    except Exception as exc:
        # A genuine PARSE failure -- a hard error (finding #7), distinct from a
        # well-formed file with only unsupported resources.
        return ImportResult(parse_error=f"HCL failed to parse: {exc}")

    by_hcl_name: dict[str, str] = {}  # "aws_sns_topic.alerts" -> canvas label
    nodes: list[dict] = []
    unsupported: list[Unsupported] = []
    warnings: list[str] = []
    subscriptions: list[tuple[str, dict]] = []
    secret_versions: list[tuple[str, dict]] = []
    node_by_label: dict[str, dict] = {}
    index = 0

    for rtype, rname, attrs in triples:
        if rtype == "aws_sns_topic_subscription":
            subscriptions.append((rname, attrs))
            continue
        if rtype == "aws_secretsmanager_secret_version":
            secret_versions.append((rname, attrs))
            continue
        kind = _KIND.get(rtype)
        if kind is None:
            unsupported.append(Unsupported(type=rtype, name=rname, reason=f"{rtype} -- not supported by odin's import (yet)"))
            continue
        label = _label(rtype, rname, attrs)
        by_hcl_name[f"{rtype}.{rname}"] = label
        node = {
            "id": label, "type": kind,
            "position": {"x": index * _GRID_STEP, "y": 0},
            "data": _node_data(kind, label, attrs),
        }
        nodes.append(node)
        node_by_label[label] = node
        dropped = _dropped_attrs(kind, attrs)
        if dropped:
            warnings.append(f"{label} ({kind}): imported without unmodeled attribute(s): {', '.join(dropped)}")
        index += 1

    # W2.4: a companion `aws_secretsmanager_secret_version` carries the VALUE,
    # which on the canvas is a field of the secret node itself -- so it's
    # assembled ONTO that node rather than imported as a node of its own (the
    # exact inverse of hcl.py's own companion pass, so generate -> import ->
    # generate round-trips). Mirrors the subscription pass below: an
    # unresolvable reference is reported, never silently dropped.
    for rname, attrs in secret_versions:
        target = _ref_target(attrs.get("secret_id"))
        label = by_hcl_name.get(f"aws_secretsmanager_secret.{target}") if target else None
        node = node_by_label.get(label) if label else None
        value = hcl.unquote(attrs.get("secret_string"))
        # Only a plain literal is a real value; a computed one ("${...}", e.g.
        # `secret_string = jsonencode(...)` or a var reference) can't be carried.
        if node is not None and isinstance(value, str) and "${" not in value:
            node["data"]["secretString"] = value
            continue
        unsupported.append(Unsupported(
            type="aws_secretsmanager_secret_version", name=rname,
            reason="secret value not carried -- it references a secret outside the supported set, or isn't a literal",
        ))

    edges: list[dict] = []
    for rname, attrs in subscriptions:
        topic_target = _ref_target(attrs.get("topic_arn"))
        queue_target = _ref_target(attrs.get("endpoint"))
        topic_label = by_hcl_name.get(f"aws_sns_topic.{topic_target}") if topic_target else None
        queue_label = by_hcl_name.get(f"aws_sqs_queue.{queue_target}") if queue_target else None
        if topic_label and queue_label:
            edges.append({"source": topic_label, "target": queue_label})
        else:
            unsupported.append(Unsupported(
                type="aws_sns_topic_subscription", name=rname,
                reason="subscription references a resource outside the supported set -- edge dropped",
            ))

    return ImportResult(nodes=nodes, edges=edges, unsupported=unsupported, warnings=warnings)


def parse_hcl_text(text: str) -> ImportResult:
    return parse_hcl({"main.tf": text})


def parse_hcl_dir(directory: Path) -> ImportResult:
    files = {p.name: p.read_text() for p in sorted(directory.glob("*.tf"))}
    return parse_hcl(files)


def _import_id(resource: LiveResource, gateway_port: int) -> str:
    """Terraform's import ID format differs per resource type — mirrors the
    identifiers `aws/backings.py` itself hands back to workloads."""
    if resource.type == "sqs":
        return f"http://127.0.0.1:{gateway_port}/{ACCOUNT}/{resource.id}"
    if resource.type == "sns":
        return f"arn:aws:sns:{REGION}:{ACCOUNT}:{resource.id}"
    return resource.id  # s3 bucket name / dynamodb table name


def _import_blocks(resources: list[LiveResource], gateway_port: int) -> str:
    used: dict[str, set[str]] = {}
    blocks = []
    for resource in resources:
        tf_type = _TF_TYPE[resource.type]
        name = hcl.unique_name(hcl.sanitize_name(resource.id), used.setdefault(tf_type, set()))
        blocks.append(
            f'import {{\n  to = {tf_type}.{name}\n  id = {hcl.quote(_import_id(resource, gateway_port))}\n}}'
        )
    return "\n\n".join(blocks) + "\n"


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": REGION,
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


async def _run(tofu: str, args: tuple[str, ...], cwd: Path, env: dict[str, str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        tofu, *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    await proc.communicate()  # best-effort: `generated.tf`'s existence is the real success signal (module docstring)


async def import_live(
    resources: list[LiveResource], gateway_port: int, access_key: str, secret_key: str,
) -> ImportResult:
    """Mode (b): generate `import {}` blocks for `resources`, resolve them
    against the real backings through the gateway, and parse whatever HCL
    `tofu plan -generate-config-out` produces with mode (a)."""
    supported = [r for r in resources if r.type in _TF_TYPE]
    unsupported = [
        Unsupported(type=r.type, name=r.id, reason=f"{r.type} -- not supported by odin's import (yet)")
        for r in resources if r.type not in _TF_TYPE
    ]
    if not supported:
        return ImportResult(unsupported=unsupported)

    tofu = shutil.which("tofu")
    if tofu is None:
        return ImportResult(unsupported=[
            *unsupported, Unsupported(type="*", name="*", reason="tofu not on PATH -- cannot live-import"),
        ])

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        main_tf = f"{hcl.HEADER}\n\n{hcl.provider_block(REGION)}\n\n{_import_blocks(supported, gateway_port)}"
        (scratch / "main.tf").write_text(main_tf)
        (scratch / "override.tf").write_text(workspace_mod.OVERRIDE_TF)
        env = _tf_env(gateway_port, access_key, secret_key)
        await _run(tofu, ("init", "-input=false"), scratch, env)
        await _run(tofu, ("plan", "-input=false", "-no-color", "-generate-config-out=generated.tf"), scratch, env)
        generated = scratch / "generated.tf"
        if not generated.exists():
            return ImportResult(unsupported=[
                *unsupported,
                Unsupported(type="*", name="*", reason="tofu could not generate config for the requested resources"),
            ])
        result = parse_hcl({"generated.tf": generated.read_text()})

    return ImportResult(
        nodes=result.nodes, edges=result.edges,
        unsupported=[*unsupported, *result.unsupported], warnings=result.warnings,
        parse_error=result.parse_error,
    )
