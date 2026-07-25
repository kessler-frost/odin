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
    fv = res.fields.get(key)
    return fv.value if fv is not None else default


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
    return attrs, ""


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
    }
    nested = (
        "  timeouts {\n"
        f'    create = {quote(_ECS_CONVERGE_TIMEOUT)}\n'
        f'    update = {quote(_ECS_CONVERGE_TIMEOUT)}\n'
        f'    delete = {quote(_ECS_CONVERGE_TIMEOUT)}\n'
        "  }"
    )
    return attrs, nested


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
_SSM_NEEDS_VALUE = "needs a Value (an SSM parameter can't exist without one)"
_BAD_SSM_TYPE = f"type must be one of {', '.join(_SSM_TYPES)}"


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
        nested = "\n\n".join(part for part in (nested, _tags_block(res)) if part)
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
        if res.kind != "secret":
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

    blocks.sort(key=lambda b: b[0])
    main_tf = "\n\n".join([HEADER, provider_block(), *(text for _, text in blocks)]) + "\n"

    # V4c: materialize each lambda's pasted code into the zip its own HCL
    # block references by filename -- odin owns this pre-tofu, not a
    # `data archive_file` round-trip (module docstring). The entry filename
    # MUST match `_lambda_entry`'s choice for the SAME runtime, or the
    # deployed zip and the `handler` string would disagree.
    binary_files: dict[str, bytes] = {}
    for res in ordered:
        name = hcl_name_by_id.get(res.id)
        if res.kind != "lambda" or name is None:
            continue
        runtime = _field(res, "runtime", _DEFAULT_LAMBDA_RUNTIME)
        entry_filename, _ = _lambda_entry(runtime)
        code = _field(res, "code", "") or _DEFAULT_LAMBDA_CODE
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(entry_filename, code)
        binary_files[f"{name}.zip"] = buf.getvalue()

    return TfProject(files={"main.tf": main_tf}, unsupported=unsupported, binary_files=binary_files)
