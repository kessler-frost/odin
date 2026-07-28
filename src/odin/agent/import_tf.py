"""S4 — TF import: Terraform -> canvas, the reverse of S3b's `translate()`.

Two modes (research-verified, docs/superpowers/research/research-tofu-provider.md
§5 "Import direction"):

(a) **deterministic** (`parse_hcl*`): parse an existing project's HCL for every
    supported resource type (`_KIND` below, plus the two COMPANION types that
    fold into a node rather than becoming one: aws_sns_topic_subscription ->
    an edge, aws_secretsmanager_secret_version -> its secret node's value)
    into canvas nodes+edges. Unsupported types are LISTED, never dropped
    (northstar directive 5). W2.5 adds two more companions of the same shape:
    aws_lb_target_group + aws_lb_listener fold onto their aws_lb's `alb` node
    (one canvas node, three tf resources -- the inverse of hcl.py's own alb
    expansion, so generate -> import -> generate round-trips).

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
import io
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
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
    "aws_db_instance": "rds",
    "aws_lb": "alb",
    # v0.7.1: the two CONTAINER kinds. They were reported unsupported, which
    # was honest but left the import asymmetric with generate -- and an
    # `aws_lb` needs a subnet AND a vpc on the canvas, so an imported load
    # balancer could never be applied (field test U2). Importing them is what
    # makes containment reconstructible from the source's own `vpc_id`/
    # `subnets` references.
    "aws_vpc": "vpc",
    "aws_subnet": "subnet",
    # v0.8.4: the two that made a NETWORK canvas un-round-trippable. An
    # `aws_security_group` is where the interesting half lives -- its rules are
    # what the Nebula firewall actually compiles from, so losing them on import
    # loses the security posture, not a label. `aws_ecr_repository` is here
    # because it is a one-argument resource that was being reported unsupported
    # for no reason other than nobody having written the line.
    "aws_security_group": "sg",
    "aws_ecr_repository": "ecr",
    # v0.8.4: an `aws_instance` is a real Lima VM to odin, and it has NO `name`
    # argument -- its label comes from the `odin:node` tag `_label` already falls
    # back to. Its optional SSH key lives on a companion `aws_key_pair`, folded
    # back on below rather than becoming a node of its own.
    "aws_instance": "ec2",
    # v0.8.4: one canvas `ecs` node is THREE tf resources (service + task
    # definition + the shared cluster), so only the SERVICE becomes a node --
    # the other two fold on, the same shape as the alb trio.
    "aws_ecs_service": "ecs",
    # v0.8.4, the last kind. A function's CONFIG is all in the HCL; its CODE is
    # in a zip beside `main.tf`, so `parse_hcl_dir` recovers it and
    # `parse_hcl_text` cannot -- see `_stamp_lambda`, which says so rather than
    # letting odin's default payload pass for the user's own function.
    "aws_lambda_function": "lambda",
}
# Neither of these becomes a node. The task definition folds onto its service
# (image/port/memory/cpu live there, not on the service); the cluster is a
# singleton odin always emits exactly one of, named "odin", so importing it would
# invent a node the canvas has no kind for.
_ECS_COMPANION_TYPES = ("aws_ecs_task_definition", "aws_ecs_cluster")
# W2.5: the two OTHER types an `alb` canvas node expands to. Neither becomes a
# node of its own -- they fold ONTO the alb node the same way
# aws_secretsmanager_secret_version folds onto its secret, which is what makes
# generate -> import -> generate round-trip instead of multiplying resources.
_ALB_COMPANION_TYPES = ("aws_lb_target_group", "aws_lb_listener")
# The attribute each supported type's human-facing name lives in (mirrors
# hcl.py's builders: s3 uses `bucket`, elasticache uses `cluster_id`, rds uses
# `identifier`, everything else uses `name`).
_NAME_ATTR = {
    "aws_s3_bucket": "bucket", "aws_sqs_queue": "name", "aws_sns_topic": "name",
    "aws_dynamodb_table": "name", "aws_iam_role": "name",
    "aws_cloudwatch_log_group": "name",
    "aws_secretsmanager_secret": "name", "aws_ssm_parameter": "name",
    "aws_elasticache_cluster": "cluster_id",
    "aws_db_instance": "identifier",
    "aws_lb": "name",
    "aws_security_group": "name", "aws_ecr_repository": "name",
    "aws_ecs_service": "name",
    "aws_lambda_function": "function_name",
}
# canvas kind -> aws_* type, for mode (b) (the inverse of `_KIND`). iam_role,
# logs, secret and ssm have no backing to enumerate live resources from (all
# four are pure gateway models), so they stay out of the live path --
# elasticache likewise: its clusters exist only as gateway-model records plus a
# real container, and there's no `_import_id` shape to resolve one from outside
# a canvas Apply (mode (a), reading an existing HCL project, works fine).
# `rds` DOES stay in it: an `aws_db_instance`'s import id is its bare
# DBInstanceIdentifier (the `_import_id` default branch) and the gateway answers
# DescribeDBInstances for real -- the one thing `tofu plan
# -generate-config-out` cannot recover is the master `password` (no AWS API ever
# returns it), so a live-imported database comes back with hcl.py's default
# password rather than the original one. `alb` (W2.5) stays out of the live path
# too -- one canvas node is three aws_* resources, so there is no single live
# resource to import it from (mode (a), reading an existing HCL project, works).
# `sg` and `ecr` (v0.8.4) stay OUT of the live path deliberately, and for
# different reasons. A security group's live import id is its `sg-...` GroupId,
# which is minted by the gateway and appears nowhere on a canvas, so there is no
# id to resolve from outside an Apply. ECR has no `_import_id` shape either --
# its repositories exist as gateway-model records. Mode (a), reading an existing
# HCL project, works for both; claiming mode (b) without an id that resolves is
# how a live import would generate a bogus import block and fail at apply.
# `ec2` is out for the same reason as `sg`: an instance's live import id is its
# `i-...` InstanceId, minted at RunInstances and absent from any canvas.
# `ecs` is out for the alb reason rather than the id reason: one canvas node is
# three tf resources, so there is no single live resource to import it from.
# `lambda` is out of the live path because its CODE is not recoverable from any
# AWS API odin implements -- a live import would produce a function whose body is
# odin's default payload, which is the substitution this whole module refuses.
_NO_LIVE_IMPORT = {
    "iam_role", "logs", "secret", "ssm", "elasticache", "alb", "sg", "ecr", "ec2", "ecs", "lambda",
}
_TF_TYPE = {kind: rtype for rtype, kind in _KIND.items() if kind not in _NO_LIVE_IMPORT}

# The HCL arguments each kind CARRIES into the canvas -- so a round-trip through
# generate_tf reproduces them (finding #6). Any OTHER argument present on the
# resource is reported as a per-node warning rather than silently dropped
# (`__is_block__` is python-hcl2's internal block marker, never a real arg).
_IGNORED_ATTRS = {"__is_block__"}
_CARRIED_ATTRS = {
    # `force_destroy` is carried in the sense that odin always re-emits it --
    # as `true`, unconditionally (hcl.py's `_s3`). A source `force_destroy =
    # false` is therefore a CHANGED argument (`_FIXED_VALUES`), not a dropped
    # one: v0.7.5 reported it as "unmodeled", which reads as "odin ignored it"
    # when odin actually flips a bucket the user protected into one `tofu
    # destroy` will empty.
    "s3": {"bucket", "force_destroy", "tags"},
    "sqs": {"name"},
    "sns": {"name"},
    # `billing_mode` likewise: odin always emits PAY_PER_REQUEST, so a
    # PROVISIONED table with read/write capacity is a changed argument.
    "dynamodb": {"name", "billing_mode", "hash_key", "range_key", "attribute"},
    "iam_role": {"name"},  # assume_role_policy/inline policies are NOT carried -> warned
    "logs": {"name", "retention_in_days", "tags"},
    # W2.4: `recovery_window_in_days` is carried in the sense that odin always
    # emits its own value (0 -- see hcl.py's `_secret`). Until v0.7.6 that was
    # where the sentence stopped, and a source `recovery_window_in_days = 30`
    # became 0 in silence -- a 30-day undelete window turned into immediate,
    # irreversible deletion. It is a `_FIXED_VALUES` entry now, so odin's own
    # 0 still round-trips quietly while a DIFFERING one is reported.
    # The VALUE isn't here at all: it lives on the companion
    # aws_secretsmanager_secret_version resource, assembled separately below.
    "secret": {"name", "description", "recovery_window_in_days", "tags"},
    "ssm": {"name", "type", "value", "description", "tags"},
    # engine/num_cache_nodes are carried because hcl.py always re-emits them --
    # as `redis` and `1`, unconditionally. THIS COMMENT USED TO STOP THERE, and
    # that was the bug: neither value reaches the canvas node at all, so a
    # 3-node memcached cluster came back as a single-node redis one with no
    # warning of any kind (a different datastore, a different wire protocol,
    # a third of the nodes). Both are `_FIXED_VALUES` entries now.
    "elasticache": {"cluster_id", "engine", "node_type", "num_cache_nodes", "tags"},
    # `password` IS carried (unlike every other secret odin touches): dropping
    # it would make a round-trip through generate_tf silently substitute the
    # DEFAULT password, i.e. a real credential change on the next apply.
    "rds": {
        "identifier", "engine", "instance_class", "allocated_storage", "db_name",
        "username", "password", "skip_final_snapshot", "tags",
    },
    # W2.5: `internal`/`load_balancer_type` are values odin always emits itself
    # (hcl.py's `_alb`), so they are carried in the sense that odin re-emits
    # SOMETHING for them -- but a source value that DISAGREES with what odin
    # emits is a real semantic change and warns via `_FIXED_VALUES` below
    # (v0.7.0 dropped `internal = false` in silence, quietly turning an
    # internet-facing load balancer into an internal one). `subnets` is
    # CONTAINMENT on the canvas: carried as the `subnet`/`vpc` stamps when it
    # points at an imported subnet, warned about when it can't be resolved.
    "alb": {"name", "internal", "load_balancer_type", "subnets", "tags"},
    "vpc": {"cidr_block", "tags"},
    "subnet": {"cidr_block", "vpc_id", "tags"},
    # v0.8.4. `ingress` IS carried -- into the node's `ingressRules` text, one
    # `protocol:port:source` line per block, which is what `hcl.py::_sg` reads
    # back. `egress` is carried in the weaker sense the others in this map use:
    # odin always re-emits its own wide-open default (`_DEFAULT_EGRESS`), so a
    # source egress that DIFFERS is a changed argument, not a dropped one, and
    # says so via `_FIXED_VALUES` below. `vpc_id` is CONTAINMENT, stamped by
    # `_stamp_containment` exactly as a subnet's is.
    "sg": {"name", "vpc_id", "ingress", "egress", "tags"},
    "ecr": {"name", "tags"},
    # `subnet_id` is containment; `vpc_security_group_ids` becomes the node's
    # `securityGroups` label list; `key_name` is a reference to the companion
    # aws_key_pair whose `public_key` is the real value.
    "ec2": {"ami", "instance_type", "subnet_id", "vpc_security_group_ids",
            "key_name", "user_data", "tags"},
    # `depends_on` is carried in the sense that odin RE-DERIVES it from the
    # node's own `${{...}}` refs, so it is never a dropped argument -- but see
    # `_stamp_ecs_taskdef`: the refs themselves are not in the HCL at all and
    # cannot come back, which is reported rather than left to be discovered.
    "ecs": {
        "name", "cluster", "task_definition", "desired_count", "launch_type",
        "wait_for_steady_state", "deployment_minimum_healthy_percent",
        "deployment_maximum_percent", "timeouts", "placement_constraints",
        "depends_on", "load_balancer", "tags",
    },
    # `filename`/`source_code_hash` are carried in the sense that odin re-derives
    # both from the code it materializes itself; `role` is either a reference to a
    # drawn iam_role node or odin's own auto-generated one (`_stamp_lambda`).
    "lambda": {
        "function_name", "role", "handler", "runtime", "filename",
        "source_code_hash", "depends_on", "tags",
    },
}
# The companion resources' equivalent: which of THEIR arguments a round trip
# reproduces (hcl.py's alb companion pass emits exactly these). Until v0.7.1
# nothing computed dropped attributes for a companion at all, so a target
# group's `matcher`, `stickiness`, `deregistration_delay` -- and every
# health_check member except `path` -- vanished without a word. v0.7.6 adds the
# OTHER TWO companion types, which still had no honesty pass of any kind: an
# sns subscription's `filter_policy` (a routing rule -- without it the queue
# starts receiving every message on the topic) and `raw_message_delivery =
# false` (odin always emits true, which changes the envelope every consumer
# parses), plus a secret version's `version_stages`.
_CARRIED_COMPANION_ATTRS = {
    "aws_lb_target_group": {"name", "port", "protocol", "vpc_id", "target_type", "health_check"},
    "aws_lb_listener": {"load_balancer_arn", "port", "protocol", "default_action"},
    "aws_sns_topic_subscription": {"topic_arn", "protocol", "endpoint", "raw_message_delivery"},
    "aws_secretsmanager_secret_version": {"secret_id", "secret_string"},
}
_CARRIED_HEALTH_CHECK_ATTRS = {"path"}
# (owner, attribute) -> the value odin ALWAYS emits, lowercased. An imported
# value that differs is reported by name: the argument survives, its MEANING
# does not. Owner is the canvas kind for a primary resource, the aws_* type for
# a companion.
#
# EVERY ENTRY HERE IS A SUBSTITUTION ODIN MAKES BECAUSE THE LOCAL SUBSTRATE
# CANNOT DO THE OTHER THING (a real redis container is not memcached; odin's
# DeleteSecret has no recovery window; there is no snapshot surface to take a
# final snapshot into). The substitution is a legitimate limit -- the SILENCE
# was the bug, and this table is the whole cure: an entry is compared against
# the source's own value, so odin's own generated HCL round-trips without a
# word while anything else is named on the import itself.
_FIXED_VALUES = {
    ("s3", "force_destroy"): "true",
    ("dynamodb", "billing_mode"): "pay_per_request",
    ("secret", "recovery_window_in_days"): "0",
    ("elasticache", "engine"): "redis",
    ("elasticache", "num_cache_nodes"): "1",
    ("rds", "skip_final_snapshot"): "true",
    ("alb", "internal"): "true",
    ("alb", "load_balancer_type"): "application",
    ("aws_lb_target_group", "protocol"): "http",
    ("aws_lb_target_group", "target_type"): "instance",
    ("aws_lb_listener", "protocol"): "http",
    ("aws_sns_topic_subscription", "protocol"): "sqs",
    ("aws_sns_topic_subscription", "raw_message_delivery"): "true",
    # odin emits all four of these unconditionally (`hcl.py::_ecs`), and the
    # rolling-update pair is load-bearing: `_ECS_MIN_HEALTHY_PERCENT = 100` is
    # what keeps the previous revision serving while a new one comes up, so a
    # source that lowered it comes back with different rollout behaviour.
    ("ecs", "launch_type"): "ec2",
    ("ecs", "wait_for_steady_state"): "true",
    ("ecs", "deployment_minimum_healthy_percent"): "100",
    ("ecs", "deployment_maximum_percent"): "200",
}
# The default odin's `_node_data` falls back to when a NUMERIC argument's source
# value isn't a literal number odin can read (`allocated_storage = var.size`,
# `retention_in_days = local.days`) -> {kind: {attribute: what that costs the
# user}}. A quoted `"500"` IS readable (see `_int_text`); a computed one is not,
# and substituting odin's default for it in silence is the elasticache bug in
# another costume -- a 500 GiB database imported as 20 GiB, a 30-day log
# retention imported as never-expire.
_RDS_DEFAULT_STORAGE = "20"  # hcl.py::_DEFAULT_ALLOCATED_STORAGE
_UNREADABLE_NUMBERS = {
    "logs": {
        "retention_in_days": "the canvas gets no retention at all -- AWS's never-expire default",
    },
    "rds": {
        "allocated_storage": f"the canvas gets odin's default {_RDS_DEFAULT_STORAGE} GiB",
    },
}
# The kinds whose user `tags` map survives the round trip as node data (hcl.py's
# `_tags_block` merges a node's own `tags` field back in for EVERY primary
# builder, so this is purely about which imports bother to read them).
# `elasticache` was missing here while `tags` sat in its `_CARRIED_ATTRS` set,
# which is the worst possible combination: the tags were dropped AND the drop
# was suppressed. Reading them is the fix -- hcl.py already re-emits whatever a
# node carries -- not a warning about losing them.
# Every builder in hcl.py appends `_tags_block(res)`, so user tags round-trip
# for every kind -- these are the ones whose tags are read BACK into `data.tags`.
_TAGGED_KINDS = {"s3", "logs", "secret", "ssm", "rds", "alb", "vpc", "subnet", "elasticache", "sg", "ecr", "ec2", "ecs", "lambda"}
_CONTAINER_KINDS = ("vpc", "subnet")

# Canvas geometry for the layout pass. These ARE the UI's own container sizes
# (`defaultStyleForType` in ui/src/components/Canvas.tsx) and its 20px grid: an
# imported node has to be geometrically inside its container box, because the
# browser re-derives the `vpc`/`subnet` stamps from geometry whenever nodes are
# measured or dragged (ui/src/lib/containment.ts) and would strip a stamp whose
# node visually sits outside.
_LEAF_SIZE = (220, 120)
_MIN_VPC_SIZE = (560, 380)
_MIN_SUBNET_SIZE = (520, 280)
_PAD = 20
_HEADER = 40


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


def _plain_literal(value: object) -> str | None:
    """`value` as a plain string literal, or None when it is anything computed.

    Only a plain literal (no leftover `${...}`, whether it was a bare reference
    or a literal with an embedded interpolation) is a value odin can carry onto
    the canvas."""
    unquoted = hcl.unquote(value) if isinstance(value, str) else None
    return unquoted if isinstance(unquoted, str) and "${" not in unquoted else None


def _label(rtype: str, rname: str, attrs: dict) -> str:
    """The canvas label -- which for every named kind IS the name odin will
    create the resource under, so a fallback here RENAMES a real resource
    (reported by `_renamed_by_import`, never silent).

    A computed name falls back to odin's own management tag (which IS the canvas
    label, so odin's generated HCL round-trips even for the name-less kinds),
    then to the resource's own HCL name."""
    name_attr = _NAME_ATTR.get(rtype)
    literal = _plain_literal(attrs.get(name_attr)) if name_attr else None
    return literal or _tags(attrs, odin_tag=True).get("odin:node") or rname


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


def _tags(attrs: dict, odin_tag: bool = False) -> dict[str, str]:
    """The user `tags` map (odin's own `odin:node` management tag excluded, so
    a round-trip doesn't surface it as a user tag -- `odin_tag=True` keeps it,
    for the one caller that reads the label back out of it)."""
    raw = attrs.get("tags")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = hcl.unquote(key) or key
        val = hcl.unquote(value)
        if isinstance(name, str) and (odin_tag or name != "odin:node") and isinstance(val, str):
            out[name] = val
    return out


def _int_attr(value: object, default: int) -> int:
    """python-hcl2 parses an unquoted `port = 80` as a real int and a quoted
    `"80"` as a 4-character string (verified empirically -- see `unquote`), so
    both spellings have to reduce to the same number."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    unquoted = hcl.unquote(value)
    return int(unquoted) if isinstance(unquoted, str) and unquoted.isdigit() else default


def _forward_target_group(listener_attrs: dict) -> str | None:
    """`aws_lb_target_group.<name>` from a listener's `default_action {}` block
    (python-hcl2 parses a repeated block into a list of dicts). v1 reads the
    FIRST action carrying a `target_group_arn` -- the only shape hcl.py emits
    and the only one elbv2ctl models."""
    for block in listener_attrs.get("default_action") or []:
        target = _ref_target(block.get("target_group_arn"))
        if target:
            return f"aws_lb_target_group.{target}"
    return None


def _health_check_path(tg_attrs: dict) -> str:
    for block in tg_attrs.get("health_check") or []:
        path = hcl.unquote(block.get("path"))
        if isinstance(path, str) and path:
            return path
    return "/"


def _literal(value: object) -> str:
    """An HCL scalar reduced to a comparable lowercase string: python-hcl2 gives
    back a real bool for `internal = false`, an int for `port = 80`, and a
    quote-wrapped string for `"HTTP"`."""
    unquoted = hcl.unquote(value) if isinstance(value, str) else value
    return str(unquoted).lower()


def _int_text(value: object) -> str | None:
    """An HCL integer argument as digits, or None when the source value isn't a
    literal number at all. python-hcl2 parses `20` as a real int and a quoted
    `"20"` as a 4-character string (both are valid HCL for a number argument and
    both must survive), while `var.size` arrives as the string `${var.size}`,
    which nothing can turn into a number here -- and `isinstance(True, int)` is
    True in Python, so a bool has to be excluded explicitly."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    unquoted = hcl.unquote(value)
    return unquoted if isinstance(unquoted, str) and unquoted.isdigit() else None


def _derived_changes(triples: Iterable[tuple[str, object, str]]) -> dict[str, str]:
    """{attribute: why} for each `(attribute, source value, what odin emits)`
    whose source value disagrees with what odin emits -- the `_FIXED_VALUES`
    check for the values odin COMPUTES per resource instead of hardcoding: a
    node's own name, a target group's `<alb label>-tg`."""
    return {
        attr: f"odin always emits {expected}"
        for attr, value, expected in triples
        if value is not None and _literal(value) != _literal(expected)
    }


def _renamed_by_import(rtype: str, attrs: dict, label: str) -> dict[str, str]:
    """The name argument odin could not read (`name = "${var.env}-jobs"`).

    The canvas label falls back to the HCL resource name, and `generate_tf` then
    emits THAT as the real bucket/queue/table/cluster name -- so importing a
    project whose names are built from variables silently renames every resource
    in it. Compared against the label rather than tested for `${`, so a name odin
    CAN read (the round-trip case) never fires."""
    return _derived_changes((attr, attrs.get(attr), label) for attr in (_NAME_ATTR.get(rtype),) if attr)


def _unreadable_numbers(kind: str, attrs: dict) -> dict[str, str]:
    """The numeric arguments whose source value odin cannot read as a number, so
    `_node_data`'s own default lands on the canvas instead (`_UNREADABLE_NUMBERS`)."""
    return {
        attr: f"not a literal number, so {cost}"
        for attr, cost in _UNREADABLE_NUMBERS.get(kind, {}).items()
        if attr in attrs and _int_text(attrs[attr]) is None
    }


def _uncarried_attribute_blocks(attrs: dict, data: dict) -> list[str]:
    """A dynamodb `attribute {}` block for something that is neither the hash nor
    the range key. `generate_tf` emits an attribute block for exactly those two,
    so a secondary index's key attribute does not survive -- the index itself is
    already reported as an unmodeled argument, its attribute was not."""
    keys = {data.get("hashKey"), data.get("rangeKey")}
    return [f"attribute.{name}" for name in _attribute_types(attrs) if name not in keys]


def _attribute_notes(
    owner: str, attrs: dict, carried: set[str],
    also_dropped: Iterable[str], also_changed: dict[str, str],
) -> tuple[list[str], list[str]]:
    """`(dropped, changed)` -- the two ways an argument fails to survive a round
    trip through `generate_tf`, reported by name, never dropped in silence (the
    v0.5.4 attribute-honesty rule, which is meant to have no exceptions).

    Either odin doesn't model the argument at all (`dropped`), or odin emits its
    own value for it and the source's value differs (`changed`). The second kind
    is the sneakier one -- the argument is still THERE in the regenerated HCL,
    saying something else -- and it is returned SEPARATELY because through
    v0.7.5 both shared one line reading "imported without unmodeled
    attribute(s)", which says odin IGNORED the argument when odin in fact
    changed the resource. `also_changed` carries the cases a static table can't
    express: a value odin computes per resource, and a number odin can't read.
    """
    dropped = sorted({k for k in attrs if k not in carried and k not in _IGNORED_ATTRS} | set(also_dropped))
    fixed = {
        key: f"odin always emits {want}"
        for (fixed_owner, key), want in _FIXED_VALUES.items()
        if fixed_owner == owner and key in attrs and _literal(attrs[key]) != want
    }
    changed = sorted(
        f"{key}={_literal(attrs[key])} ({why})"
        for key, why in {**fixed, **also_changed}.items() if key in attrs
    )
    return dropped, changed


def _attribute_warnings(subject: str, what: str, dropped: list[str], changed: list[str]) -> list[str]:
    """The per-resource honesty lines a caller surfaces (`cli/translate.py`
    prints each as `warning: ...` on stderr, and they ride in the JSON body's
    `warnings` array for programmatic callers).

    Two lines, not one, and the second one says CHANGED: an argument odin
    substitutes its own value for is not a missing argument, and a user reading
    "imported without unmodeled attribute(s): engine=memcached" would reasonably
    conclude their cluster was imported unchanged minus a detail."""
    return [
        *([f"{subject}: imported without unmodeled {what}attribute(s): {', '.join(dropped)}"]
          if dropped else []),
        *([f"{subject}: imported with CHANGED {what}argument(s) -- odin substitutes its own "
           f"value: {', '.join(changed)}"] if changed else []),
    ]


def _dropped_health_check_attrs(tg_attrs: dict) -> list[str]:
    """Health-check members other than `path`: odin emits only the canvas-
    authored `path` and leaves the rest for the provider to read back, so a
    source `matcher`/`interval`/`healthy_threshold` does not survive."""
    return sorted(
        f"health_check.{key}"
        for block in (tg_attrs.get("health_check") or [])
        for key in block
        if key not in _CARRIED_HEALTH_CHECK_ATTRS and key not in _IGNORED_ATTRS
    )


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
        # `_int_text` because a quoted `retention_in_days = "30"` is valid HCL for
        # a number argument and used to fall straight through an `isinstance(int)`
        # test into "no retention" -- i.e. never expire, for a group the user
        # asked to expire. A genuinely computed value still can't be carried, and
        # `_unreadable_numbers` reports THAT rather than substituting in silence.
        retention = _int_text(attrs.get("retention_in_days"))
        if retention is not None:
            data["retentionInDays"] = retention
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
    if kind == "rds":
        # Same reading as logs' retention above, and the same reason: a quoted
        # `allocated_storage = "500"` silently became odin's default 20 GiB.
        storage = _int_text(attrs.get("allocated_storage"))
        data["allocatedStorage"] = storage or _RDS_DEFAULT_STORAGE
        for attr, field in (("engine", "engine"), ("instance_class", "instanceClass"),
                            ("db_name", "dbName"), ("username", "username"), ("password", "password")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    if kind in _CONTAINER_KINDS:
        cidr = hcl.unquote(attrs.get("cidr_block"))
        if isinstance(cidr, str):
            data["cidr"] = cidr
    if kind in _TAGGED_KINDS:
        tags = _tags(attrs)
        if tags:
            data["tags"] = tags
    if kind == "elasticache":
        node_type = hcl.unquote(attrs.get("node_type"))
        if isinstance(node_type, str):
            data["nodeType"] = node_type
    if kind == "lambda":
        for attr, field in (("runtime", "runtime"), ("handler", "handler")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    if kind == "ecs":
        count = _int_text(attrs.get("desired_count"))
        if count is not None:
            data["count"] = count
    if kind == "ec2":
        # `userData` is carried verbatim, and that is a deliberate trust
        # decision, not an oversight: it is a shell script odin will run on a
        # real VM. SECURITY.md's rule for the whole import path applies -- treat
        # an imported .tf like a shell script, because for this field it IS one.
        for attr, field in (("ami", "ami"), ("instance_type", "instanceType"),
                            ("user_data", "userData")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    return data


def _referenced_label(value: object, rtype: str, by_hcl_name: dict[str, str]) -> str | None:
    """The canvas label of the imported `rtype` resource an interpolation points
    at (`vpc_id = aws_vpc.net.id` -> the vpc node's label), or None."""
    target = _ref_target(value)
    return by_hcl_name.get(f"{rtype}.{target}") if target else None


# hcl.py::_DEFAULT_EGRESS, as the parsed block it becomes. odin re-emits exactly
# this for every security group and offers no way to author anything else, so an
# imported group whose egress DIFFERS is a changed argument -- and in the one
# direction that matters: a source that restricted outbound traffic comes back
# wide open. That is a real posture change, and v0.8.4 is the first release in
# which it could happen at all, so it is reported from the start rather than
# discovered later.
_ODIN_EGRESS = {"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}


def _same_literal(value: object, want: object) -> bool:
    """`_literal` equality that also works for a LIST argument.

    `_literal` only unquotes a `str`, so a list falls through to `str(...)` and
    `['"0.0.0.0/0"']` (what python-hcl2 hands back -- quotes retained on the
    MEMBERS) never equals `['0.0.0.0/0']`. Measured: that mismatch made odin's
    own generated egress report itself as a changed argument, i.e. a warning on
    every single security-group import, which is exactly how a real warning gets
    trained out of people.
    """
    if isinstance(want, list):
        return isinstance(value, list) and [_literal(v) for v in value] == [_literal(w) for w in want]
    return _literal(value) == _literal(want)


def _egress_changes(kind: str, attrs: dict) -> dict[str, str]:
    """`{"egress": why}` when a security group's outbound rules are not the wide
    -open default odin always emits. Empty for every other kind, and for a group
    that already matches -- which is every group odin generated itself, so its
    own round trip stays quiet."""
    if kind != "sg":
        return {}
    blocks = attrs.get("egress") or []
    same = len(blocks) == 1 and all(
        _same_literal(blocks[0].get(key), want) for key, want in _ODIN_EGRESS.items()
    )
    return {} if same else {
        "egress": "odin always emits its own wide-open egress (0-65535 to 0.0.0.0/0) and has no "
                  "field for outbound rules, so a restricted one comes back UNRESTRICTED"
    }


def _ingress_rule_line(block: dict, by_hcl_name: dict[str, str]) -> str | None:
    """One `protocol:port:source` line from an `ingress {}` block, or None when
    the block cannot be expressed as one.

    The exact inverse of `hcl.py::_ingress_source`: `cidr_blocks` is a literal
    CIDR, and `security_groups` is another SG NODE'S LABEL -- the
    identity-based "only the web tier may reach me" rule, which is the form the
    Nebula firewall compiles to a `group:` rule. Reading it back as the referenced
    group's label (not its `sg-` id) is what lets the rule survive a round trip
    at all, since the canvas has no ids in it.

    A port RANGE cannot be expressed: odin's field is one port, and `_sg` emits
    `from_port == to_port`. Returning None sends it to the dropped list rather
    than silently narrowing a range to its lower bound.
    """
    from_port, to_port = _int_text(block.get("from_port")), _int_text(block.get("to_port"))
    protocol = hcl.unquote(block.get("protocol"))
    if from_port is None or from_port != to_port or not isinstance(protocol, str):
        return None
    cidrs = block.get("cidr_blocks") or []
    groups = block.get("security_groups") or []
    source = next(
        (text for value in cidrs if isinstance(text := hcl.unquote(value), str) and "/" in text),
        None,
    ) or next(
        (label for value in groups
         if (label := _referenced_label(value, "aws_security_group", by_hcl_name))),
        None,
    )
    return f"{protocol}:{from_port}:{source}" if source else None


_ODIN_PLACEMENT_PREFIX = "attribute:odin.instance == "


def _placement_host(attrs: dict) -> str | None:
    """The EC2 node a service's tasks were pinned to, out of its real
    `placement_constraints { type = "memberOf" }`.

    This is the owner's "an ecs box inside an ec2 box means ecs ON ec2" gesture
    as it survives Terraform, so losing it on import would silently move a
    workload back onto the shared host -- a different machine, with different
    memory, reported as a clean import.
    """
    for block in attrs.get("placement_constraints") or []:
        expression = hcl.unquote(block.get("expression"))
        if isinstance(expression, str) and expression.startswith(_ODIN_PLACEMENT_PREFIX):
            return expression[len(_ODIN_PLACEMENT_PREFIX):].strip() or None
    return None


def _container_definition(taskdef: dict) -> dict:
    """The single container out of a task definition's `container_definitions`.

    It is a JSON STRING literal in the HCL, so this needs `unquote` to be a REAL
    inverse of `quote` -- which it only became in this same release. With the old
    quote-stripping version the escaped inner quotes survived and `json.loads`
    could not read it at all.
    """
    raw = hcl.unquote(taskdef.get("container_definitions"))
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed[0] if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) else {}


def _lambda_code(archives: dict[str, bytes], filename: str) -> str | None:
    """The function body out of its deployment zip, or None.

    odin materializes a single-entry zip beside `main.tf` and references it by
    filename (`hcl.py::_lambda`), so the code is recoverable in DIRECTORY mode and
    simply absent in text mode. `_stamp_lambda` reports the difference rather than
    letting odin's `_DEFAULT_LAMBDA_CODE` pass for the user's own function.
    """
    raw = archives.get(filename)
    if raw is None:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            return archive.read(names[0]).decode() if names else None
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError):
        return None


def _stamp_lambda(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str],
    role_names: dict[str, str], archives: dict[str, bytes],
) -> tuple[list[str], set[str]]:
    """Resolve each function's role and code. Returns (warnings, labels to DROP).

    ## The phantom role, which was a defect before lambda import existed

    A lambda drawn with no `role` gets an AUTO-GENERATED `aws_iam_role`
    (`hcl.py` pass 3, `name = "<function>-role"`). Because `_KIND` maps
    `aws_iam_role` to a real canvas kind, importing odin's own generated project
    produced an `iam_role` NODE THE USER NEVER DREW -- measured before this
    function existed: a one-lambda canvas round-tripped into a canvas containing
    one `iam_role` called `thumbnailer-role` and no function at all. So the role
    is dropped here when it is odin's own, and the node's `role` field is left
    empty, which is exactly what the canvas said in the first place.

    Detecting it reconstructs hcl.py's `<function>-role` NAMING CONVENTION, and
    that is a compromise worth naming: the generated HCL carries no marker
    distinguishing an auto-role from a drawn one (both get the same
    `assume_role_policy` -- `_iam_role` and the auto pass emit the identical
    default Lambda trust policy), so the name is the only signal available. A
    user who draws a role and happens to call it `<function>-role` gets it folded
    in; the effect is that their `role` field comes back empty and odin
    regenerates the same role, so the Terraform is unchanged.
    """
    warnings: list[str] = []
    drop: set[str] = set()
    for label, node in node_by_label.items():
        if node["type"] != "lambda":
            continue
        attrs = attrs_by_label[label]

        role_label = _referenced_label(attrs.get("role"), "aws_iam_role", by_hcl_name)
        auto = role_label is not None and role_names.get(role_label) == f"{label}-role"
        if role_label and not auto:
            node["data"]["role"] = role_label
        if auto:
            drop.add(role_label)
        warnings += [] if role_label or attrs.get("role") is None else [
            f"{label} (lambda): its `role` names no imported aws_iam_role, so odin will "
            "auto-generate an execution role for it on the next Apply"
        ]

        filename = hcl.unquote(attrs.get("filename"))
        code = _lambda_code(archives, filename) if isinstance(filename, str) else None
        if code is not None:
            node["data"]["code"] = code
        warnings += [] if code is not None else [
            f"{label} (lambda): its CODE could not be imported -- a function's body lives in "
            f"{filename or 'a zip'} beside main.tf, not in the HCL. Reading a directory "
            "(`odin translate import <dir>`) recovers it; from HCL text alone the node comes back "
            "with odin's DEFAULT placeholder payload, which is NOT your function."
        ]
    return warnings, drop


def _stamp_ecs_taskdef(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str],
    taskdefs: dict[str, dict],
) -> list[str]:
    """Fold each service's task definition back onto its node, and say what a
    round trip cannot bring with it.

    `image`, `port`, `memory` and `cpu` are all on the TASK DEFINITION, not the
    service, so without this the node comes back with a default image and no
    resources -- an nginx placeholder where the user's own container was.

    THE WIRING IS THE HONEST GAP, and it is not this importer's doing: a
    workload's `${{db.DATABASE_URL}}` refs are deliberately NEVER interpolated
    into the HCL (`hcl.py`'s `_WIRED_KINDS` note -- a resolved DATABASE_URL
    carries the database password, so writing it into the generated config would
    put a credential in `terraform.tfstate` in plaintext AND drift on every
    plan). Only `depends_on` survives, which names WHICH producers the service
    consumed but not which variable or attribute. So the ordering round-trips and
    the values cannot, and an imported service will start with no configuration
    it did not have on the canvas. `depends_on` is used to name the producers in
    the warning, which is the most an import can honestly offer here.
    """
    warnings: list[str] = []
    for label, node in node_by_label.items():
        if node["type"] != "ecs":
            continue
        attrs = attrs_by_label[label]

        target = _ref_target(attrs.get("task_definition"))
        taskdef = taskdefs.get(target or "", {})
        if not taskdef:
            warnings.append(
                f"{label} (ecs): its `task_definition` names no imported aws_ecs_task_definition, so "
                "the service comes back with odin's DEFAULT image and no port -- not the container "
                "it was running"
            )
        container = _container_definition(taskdef)
        image = container.get("image")
        if isinstance(image, str):
            node["data"]["image"] = image
        ports = container.get("portMappings") or []
        port = ports[0].get("containerPort") if ports and isinstance(ports[0], dict) else None
        if port is not None:
            node["data"]["port"] = str(port)
        for attr, field in (("memory", "memory"), ("cpu", "cpu")):
            value = _int_text(taskdef.get(attr))
            if value is not None:
                node["data"][field] = value

        host = _placement_host(attrs)
        if host:
            node["data"]["host"] = host

        # A `depends_on` entry parses as `'${aws_db_instance.app_db}'` -- an
        # interpolation wrapper around the `<type>.<name>` key `by_hcl_name` is
        # keyed by, and with NO attribute suffix, unlike every other reference in
        # this file. So neither `_ref_target` (which pulls the NAME out of
        # `${aws_x.y.arn}`) nor the raw string resolves it: the first draft used
        # each in turn and the warning named "resources this file does not
        # define" for a database sitting right there in the same file. Printed the
        # real parsed value rather than reasoning about it a third time.
        producers = [
            found for value in (attrs.get("depends_on") or [])
            if (found := by_hcl_name.get(str(value).strip().removeprefix("${").removesuffix("}")))
            and found != host  # see below
        ]
        # Only warn about producers that are NOT the placement host. `depends_on`
        # has TWO sources in hcl.py -- the node's env refs AND
        # `_placement_dependency` (the instance a placed service must not start
        # before) -- and a service placed inside an ec2 box with no env refs at
        # all has a `depends_on` naming only that instance. Measured end to end
        # through the real CLI: such a service was told to "re-add the env
        # references it consumed" when it had never had any. A warning that fires
        # on a correct import is worse than no warning, because it is the reason
        # people stop reading them.
        warnings += [] if not producers else [
            f"{label} (ecs): its canvas wiring cannot be imported -- a `${{{{producer.ATTR}}}}` env "
            "reference is deliberately never written into the generated Terraform (it would put a "
            "resolved password into tfstate). NEITHER the values NOR the ordering survive: odin "
            "re-derives `depends_on` FROM those refs, so a re-generated project has no ordering "
            f"either. This service depended on "
            f"{', '.join(producers) or 'resources this file does not define'}; re-add the env "
            "references it consumed."
        ]
    return warnings


def _stamp_ec2_wiring(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str],
    key_pairs: dict[str, dict],
) -> list[str]:
    """Rebuild an instance's containment, security groups and SSH key.

    Three references, each of which decides whether the node can be applied at
    all or what it is protected by, so each has its own warning rather than one
    vague line:

    * `subnet_id` -> the `subnet`/`vpc` stamps. `hcl.py::_ec2` REFUSES to build
      an instance that is not inside a subnet, so a missing stamp is a node Apply
      silently skips.
    * `vpc_security_group_ids` -> the `securityGroups` field, one LABEL per line
      (`_security_group_refs` reads exactly that). An id odin cannot resolve to
      an imported group is dropped and counted -- the regenerated instance is
      then in FEWER groups than the source, which for an inbound-deny default
      means less reachable, not more exposed, but is still a posture change.
    * `key_name` -> the companion `aws_key_pair`'s `public_key`, followed by
      REFERENCE rather than by reconstructing the `<name>_key` naming convention:
      the convention is hcl.py's private business, and a hand-authored project
      names its key pairs however it likes.

    A post-pass for `_stamp_sg_rules`' reason -- a group or key pair may be
    defined after the instance that points at it.
    """
    warnings: list[str] = []
    for label, node in node_by_label.items():
        if node["type"] != "ec2":
            continue
        attrs = attrs_by_label[label]

        subnet = _referenced_label(attrs.get("subnet_id"), "aws_subnet", by_hcl_name)
        vpc = node_by_label[subnet]["data"].get("vpc") if subnet in node_by_label else None
        node["data"].update({"subnet": subnet, "vpc": vpc} if subnet and vpc else {})
        warnings += [] if subnet and vpc else [
            f"{label} (ec2): imported without containment -- its `subnet_id` names no imported "
            "aws_subnet inside an imported aws_vpc, so Apply will skip it (\"not contained inside "
            "a Subnet on the canvas\") until you draw a VPC + Subnet and drop it inside"
        ]

        wanted = attrs.get("vpc_security_group_ids") or []
        groups = [
            found for value in wanted
            if (found := _referenced_label(value, "aws_security_group", by_hcl_name))
        ]
        if groups:
            node["data"]["securityGroups"] = "\n".join(groups)
        lost = len(wanted) - len(groups)
        warnings += [] if not lost else [
            f"{label} (ec2): {lost} of {len(wanted)} security group(s) could not be imported -- "
            "the id names no imported aws_security_group, so the regenerated instance is in FEWER "
            "groups than the source and the rules those groups carried do not apply to it"
        ]

        target = _ref_target(attrs.get("key_name"))
        public_key = hcl.unquote((key_pairs.get(target) or {}).get("public_key")) if target else None
        if isinstance(public_key, str):
            node["data"]["key"] = public_key
        warnings += [] if target is None or isinstance(public_key, str) else [
            f"{label} (ec2): its `key_name` names no imported aws_key_pair, so the instance comes "
            "back with NO SSH key and you will not be able to log in to it"
        ]
    return warnings


def _stamp_sg_rules(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str]
) -> list[str]:
    """Rebuild each security group's `ingressRules` text from its own blocks.

    A POST-pass, like `_stamp_containment`, and for the same reason: a rule may
    reference a group defined LATER in the file, which is not yet in
    `by_hcl_name` while the nodes are still being built.

    An unexpressible block is named rather than dropped in silence. Losing an
    ingress rule quietly is the worst import defect available here -- the
    regenerated group would be MORE restrictive than the source (traffic that
    used to be allowed stops), and nothing on the canvas would show the rule had
    ever existed.
    """
    warnings: list[str] = []
    for label, node in node_by_label.items():
        if node["type"] != "sg":
            continue
        blocks = attrs_by_label[label].get("ingress") or []
        lines = [_ingress_rule_line(block, by_hcl_name) for block in blocks]
        kept = [line for line in lines if line]
        if kept:
            node["data"]["ingressRules"] = "\n".join(kept)
        lost = len(lines) - len(kept)
        warnings += [] if not lost else [
            f"{label} (sg): {lost} of {len(lines)} ingress rule(s) could not be imported -- odin's "
            "rule is one `protocol:port:source` with a single port and a CIDR or an imported "
            "security group, so a port RANGE or a source odin cannot resolve is left out. The "
            "regenerated group allows LESS than the source did."
        ]
    return warnings


def _stamp_containment(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str]
) -> list[str]:
    """Rebuild the canvas's containment stamps from the source's own references.

    Containment on the canvas is not geometry to the backend: it is the
    `data.vpc`/`data.subnet` fields the UI stamps onto a node drawn inside a
    container box (`ui/src/lib/containment.ts`), which `spec/translate.py`
    carries through like any other field and `agent/hcl.py` turns into
    `vpc_id`/`subnets`. So the inverse is exact: a subnet's `vpc_id` and a load
    balancer's `subnets` name the containers it belongs to.

    Returns a warning for any node that needed containment and couldn't get it
    -- reported HERE, at import time, instead of surfacing much later as
    Apply's "not contained inside a Subnet on the canvas" for a defect created
    at import (field test U2).
    """
    warnings: list[str] = []
    # v0.8.4: an sg's `vpc_id` is containment too, and EXACTLY as load-bearing --
    # `hcl.py::_sg` refuses to build a group that is not inside a VPC, so an
    # imported group without this stamp is a node Apply will skip.
    for label, node in node_by_label.items():
        if node["type"] != "sg":
            continue
        vpc = _referenced_label(attrs_by_label[label].get("vpc_id"), "aws_vpc", by_hcl_name)
        node["data"].update({"vpc": vpc} if vpc else {})
        warnings += [] if vpc else [
            f"{label} (sg): imported without containment -- its `vpc_id` names no imported "
            "aws_vpc, so Apply will skip it until you draw a VPC on the canvas and drop it inside"
        ]
    subnets = [(label, node) for label, node in node_by_label.items() if node["type"] == "subnet"]
    for label, node in subnets:
        vpc = _referenced_label(attrs_by_label[label].get("vpc_id"), "aws_vpc", by_hcl_name)
        node["data"].update({"vpc": vpc} if vpc else {})
        warnings += [] if vpc else [
            f"{label} (subnet): imported without containment -- its `vpc_id` names no imported "
            "aws_vpc, so Apply will skip it until you draw a VPC on the canvas and drop it inside"
        ]
    for label, node in node_by_label.items():
        if node["type"] != "alb":
            continue
        wanted = attrs_by_label[label].get("subnets") or []
        subnet = next(
            (found for value in wanted
             if (found := _referenced_label(value, "aws_subnet", by_hcl_name))),
            None,
        )
        vpc = node_by_label[subnet]["data"].get("vpc") if subnet in node_by_label else None
        node["data"].update({"subnet": subnet, "vpc": vpc} if subnet and vpc else {})
        warnings += [] if subnet and vpc else [
            f"{label} (alb): imported without containment -- its `subnets` name no imported "
            "aws_subnet inside an imported aws_vpc, so Apply will skip it (\"not contained inside "
            "a Subnet on the canvas\") until you draw a VPC + Subnet and drop it inside"
        ]
        # A canvas node sits in exactly ONE Subnet box, so only the first
        # resolvable subnet becomes containment. Real load balancers are
        # multi-AZ by requirement (the API rejects a single subnet for an ALB),
        # so this is the common case, not the exotic one -- and it changed the
        # regenerated `subnets` list in silence through v0.7.5.
        warnings += [
            f"{label} (alb): imported into ONE subnet ({subnet}) of the {len(wanted)} its `subnets` "
            "names -- a canvas node lives inside a single Subnet box, so the rest are dropped and "
            "the regenerated aws_lb spans one subnet"
        ] if subnet and vpc and len(wanted) > 1 else []
    return warnings


def _place(node: dict, x: int, y: int, size: tuple[int, int] | None = None) -> None:
    node["position"] = {"x": x, "y": y}
    node.update({"size": {"width": size[0], "height": size[1]}} if size else {})


def _subnet_size(children: list[dict]) -> tuple[int, int]:
    width = max(_MIN_SUBNET_SIZE[0], 2 * _PAD + len(children) * (_LEAF_SIZE[0] + _PAD))
    return width, max(_MIN_SUBNET_SIZE[1], _HEADER + _LEAF_SIZE[1] + _PAD)


def _layout(nodes: list[dict]) -> None:
    """Nest imported nodes GEOMETRICALLY, so the canvas agrees with the stamps.

    The stamps alone aren't enough: the browser re-derives containment from
    geometry every time nodes are measured or dragged, and strips a stamp whose
    node isn't visually inside its box -- so an imported load balancer parked on
    a flat row at y=0 would lose its containment on the first render. Sizes and
    the 20px grid come from the UI's own defaults.

    A project with no containers keeps the flat 220px row exactly as before.
    """
    vpcs = [n for n in nodes if n["type"] == "vpc"]
    subnets = [n for n in nodes if n["type"] == "subnet"]
    if not (vpcs or subnets):
        return
    leaves = [n for n in nodes if n["type"] not in _CONTAINER_KINDS]
    nested: list[int] = []
    x = bottom = 0
    for vpc in vpcs:
        own_subnets = [s for s in subnets if s["data"].get("vpc") == vpc["data"]["label"]]
        children = [[c for c in leaves if c["data"].get("subnet") == s["data"]["label"]]
                    for s in own_subnets]
        sizes = [_subnet_size(kids) for kids in children]
        width = max(_MIN_VPC_SIZE[0], 2 * _PAD + max((w for w, _ in sizes), default=0))
        height = max(_MIN_VPC_SIZE[1], _HEADER + sum(h + _PAD for _, h in sizes) + _PAD)
        _place(vpc, x, 0, (width, height))
        y = _HEADER
        for subnet, kids, (sub_w, sub_h) in zip(own_subnets, children, sizes, strict=True):
            _place(subnet, x + _PAD, y, (sub_w, sub_h))
            for index, child in enumerate(kids):
                _place(child, x + 2 * _PAD + index * (_LEAF_SIZE[0] + _PAD), y + _HEADER)
            nested += [id(subnet), *(id(kid) for kid in kids)]
            y += sub_h + _PAD
        x += width + 2 * _PAD
        bottom = max(bottom, height)
    loose = [n for n in nodes if n["type"] != "vpc" and id(n) not in nested]
    for index, node in enumerate(loose):
        _place(node, index * _GRID_STEP, bottom + 3 * _PAD,
               _MIN_SUBNET_SIZE if node["type"] == "subnet" else None)


def parse_hcl(files: dict[str, str], archives: dict[str, bytes] | None = None) -> ImportResult:
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
    alb_companions: list[tuple[str, str, dict]] = []
    key_pairs: dict[str, dict] = {}
    taskdefs: dict[str, dict] = {}
    node_by_label: dict[str, dict] = {}
    attrs_by_label: dict[str, dict] = {}
    index = 0

    for rtype, rname, attrs in triples:
        if rtype == "aws_sns_topic_subscription":
            subscriptions.append((rname, attrs))
            continue
        if rtype == "aws_secretsmanager_secret_version":
            secret_versions.append((rname, attrs))
            continue
        if rtype in _ALB_COMPANION_TYPES:
            alb_companions.append((rtype, rname, attrs))
            continue
        if rtype in _ECS_COMPANION_TYPES:
            # The task definition folds onto its service; the cluster is a
            # singleton odin always emits and the canvas has no kind for.
            if rtype == "aws_ecs_task_definition":
                taskdefs[rname] = attrs
            continue
        if rtype == "aws_key_pair":
            # A COMPANION, like a secret version: it folds onto the instance that
            # references it and never becomes a node. Keyed by HCL resource name
            # because that is what the instance's `key_name` interpolation names.
            key_pairs[rname] = attrs
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
        attrs_by_label[label] = attrs
        dropped, changed = _attribute_notes(
            kind, attrs, _CARRIED_ATTRS.get(kind, set()),
            _uncarried_attribute_blocks(attrs, node["data"]),
            {**_renamed_by_import(rtype, attrs, label), **_unreadable_numbers(kind, attrs),
             **_egress_changes(kind, attrs)},
        )
        warnings += _attribute_warnings(f"{label} ({kind})", "", dropped, changed)
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
            version_type = "aws_secretsmanager_secret_version"
            dropped, changed = _attribute_notes(
                version_type, attrs, _CARRIED_COMPANION_ATTRS[version_type], (), {},
            )
            warnings += _attribute_warnings(f"{label} (secret)", f"{version_type} ", dropped, changed)
            continue
        unsupported.append(Unsupported(
            type="aws_secretsmanager_secret_version", name=rname,
            reason="secret value not carried -- it references a secret outside the supported set, or isn't a literal",
        ))

    # W2.5: fold the alb's two companion resources onto its node. A LISTENER
    # names its load balancer directly (`load_balancer_arn`) and, through its
    # forward action's `target_group_arn`, the target group -- so the listener
    # is what ties the trio together and is walked first. A target group with no
    # listener pointing at it can't be attributed to any load balancer, so it's
    # reported rather than guessed at (the subscription pass's rule).
    target_groups = {f"aws_lb_target_group.{rname}": attrs for rtype, rname, attrs in alb_companions if rtype == "aws_lb_target_group"}
    claimed_target_groups: set[str] = set()
    for rtype, rname, attrs in alb_companions:
        if rtype != "aws_lb_listener":
            continue
        alb_target = _ref_target(attrs.get("load_balancer_arn"))
        node = node_by_label.get(by_hcl_name.get(f"aws_lb.{alb_target}", "")) if alb_target else None
        if node is None:
            unsupported.append(Unsupported(
                type=rtype, name=rname,
                reason="listener references a load balancer outside the supported set",
            ))
            continue
        node["data"]["listenerPort"] = str(_int_attr(attrs.get("port"), 80))
        tg_key = _forward_target_group(attrs)
        tg_attrs = target_groups.get(tg_key) if tg_key else None
        if tg_attrs is None:
            unsupported.append(Unsupported(
                type=rtype, name=rname,
                reason="listener's forward action names no importable target group -- port/health check not carried",
            ))
            continue
        claimed_target_groups.add(tg_key)
        node["data"]["port"] = str(_int_attr(tg_attrs.get("port"), 80))
        node["data"]["healthCheckPath"] = _health_check_path(tg_attrs)
        # The companions' own arguments are held to the same honesty rule as a
        # primary resource's: v0.7.0 folded them on and said nothing about what
        # it left behind (a target group's `matcher = "200-299"`, every
        # health_check member but `path`).
        label = node["data"]["label"]
        for companion_type, companion_attrs in (
            ("aws_lb_listener", attrs), ("aws_lb_target_group", tg_attrs),
        ):
            dropped, changed = _attribute_notes(
                companion_type, companion_attrs, _CARRIED_COMPANION_ATTRS[companion_type],
                # A listener carries no health_check block and no `name`, so both
                # of these are no-ops for it -- no per-type branch needed.
                _dropped_health_check_attrs(companion_attrs),
                # hcl.py names the target group `<the alb node's label>-tg`, so a
                # target group the user named anything else is renamed by the
                # round trip (silent through v0.7.5).
                _derived_changes([("name", companion_attrs.get("name"), f"{label}-tg")]),
            )
            warnings += _attribute_warnings(f"{label} (alb)", f"{companion_type} ", dropped, changed)
    for rtype, rname, attrs in alb_companions:
        if rtype == "aws_lb_target_group" and f"aws_lb_target_group.{rname}" not in claimed_target_groups:
            unsupported.append(Unsupported(
                type=rtype, name=rname,
                reason="target group is not the forward target of any imported listener -- not folded onto a load balancer",
            ))

    warnings += _stamp_containment(node_by_label, attrs_by_label, by_hcl_name)
    warnings += _stamp_sg_rules(node_by_label, attrs_by_label, by_hcl_name)
    warnings += _stamp_ec2_wiring(node_by_label, attrs_by_label, by_hcl_name, key_pairs)
    warnings += _stamp_ecs_taskdef(node_by_label, attrs_by_label, by_hcl_name, taskdefs)
    role_names = {
        label: str(hcl.unquote(attrs_by_label[label].get("name")) or "")
        for label, node in node_by_label.items() if node["type"] == "iam_role"
    }
    lambda_warnings, folded_roles = _stamp_lambda(
        node_by_label, attrs_by_label, by_hcl_name, role_names, archives or {},
    )
    warnings += lambda_warnings
    # A folded auto-role must leave BOTH lists: the node list the canvas is built
    # from, and `node_by_label`, which later passes read.
    nodes = [node for node in nodes if node["id"] not in folded_roles]
    for label in folded_roles:
        node_by_label.pop(label, None)
    # ...and its WARNINGS go with it. The per-resource honesty pass ran while the
    # role was still a node, so a folded auto-role left behind
    # "thumbnailer-role (iam_role): imported without unmodeled attribute(s):
    # assume_role_policy" -- about a node that no longer exists, on every single
    # lambda import. Warning noise is not harmless here: this module's whole value
    # is that its warnings are worth reading.
    warnings = [
        warning for warning in warnings
        if not any(warning.startswith(f"{label} (iam_role)") for label in folded_roles)
    ]
    _layout(nodes)

    edges: list[dict] = []
    for rname, attrs in subscriptions:
        topic_target = _ref_target(attrs.get("topic_arn"))
        queue_target = _ref_target(attrs.get("endpoint"))
        topic_label = by_hcl_name.get(f"aws_sns_topic.{topic_target}") if topic_target else None
        queue_label = by_hcl_name.get(f"aws_sqs_queue.{queue_target}") if queue_target else None
        if topic_label and queue_label:
            edges.append({"source": topic_label, "target": queue_label})
            # The subscription becomes an EDGE, and an edge carries no arguments
            # -- so everything the source put ON the subscription has to be
            # accounted for here or it vanishes. `filter_policy` is the one that
            # matters most: drop it and the queue starts receiving every message
            # published to the topic, which no warning ever mentioned.
            sub_type = "aws_sns_topic_subscription"
            dropped, changed = _attribute_notes(
                sub_type, attrs, _CARRIED_COMPANION_ATTRS[sub_type], (), {},
            )
            warnings += _attribute_warnings(
                f"{topic_label} -> {queue_label} (sns subscription)", "", dropped, changed,
            )
        else:
            unsupported.append(Unsupported(
                type="aws_sns_topic_subscription", name=rname,
                reason="subscription references a resource outside the supported set -- edge dropped",
            ))

    return ImportResult(nodes=nodes, edges=edges, unsupported=unsupported, warnings=warnings)


def parse_hcl_text(text: str, archives: dict[str, bytes] | None = None) -> ImportResult:
    return parse_hcl({"main.tf": text}, archives)


def parse_hcl_dir(directory: Path) -> ImportResult:
    """Directory mode reads the ZIPS as well as the `.tf` files -- a lambda's body
    lives in one, so text mode can only ever report it missing (`_stamp_lambda`).
    Synchronous on purpose: `.read_bytes()` on a page-cached few-KB archive is
    sub-millisecond, and an async file API in Python is a thread pool in a costume
    (the concurrency note in .claude/CLAUDE.md)."""
    files = {p.name: p.read_text() for p in sorted(directory.glob("*.tf"))}
    archives = {p.name: p.read_bytes() for p in sorted(directory.glob("*.zip"))}
    return parse_hcl(files, archives)


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
