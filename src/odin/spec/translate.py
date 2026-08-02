"""Translate a canvas graph into a desired-state Stack.

A canvas node becomes a ResourceDesired: its `type` maps to a kind, its `data`
becomes provenance-tagged user fields, and any field whose value is a
`${{target.attr}}` reference becomes a typed Ref (and is lifted out of the
static env so the Fabric resolves it at reconcile time).
"""
from __future__ import annotations

import re

from odin.spec.models import Edge, FieldValue, Ref, ResourceDesired, Stack, is_sensitive_field_name

_REF = re.compile(r"^\$\{\{\s*([\w-]+)\.([\w-]+)\s*\}\}$")

# Canvas node type -> Stack kind. rds is a direct Postgres container; the rest
# are AWS-shaped resources provisioned in per-env backing containers.
# vpc/subnet/sg are the V1 network containers: their containment-stamped
# data.vpc/data.subnet fields flow through `_resource` like any other field.
# iam_role/ecr (V2c), ec2 (V3c), lambda (V4c), ecs (V5c), logs (W2.1) and
# secret/ssm (W2.4) are pure gateway-model kinds like vpc/subnet/sg -- no reconciler-driven
# provisioning at all (plan.py NoOps them; see reconcile/plan.py +
# aws/backings.py::ENSURE_KINDS), just fields flowing through generically for
# hcl.py's builders to read. ec2's REAL lifecycle (a Lima VM) is driven
# entirely by the gateway's RunInstances handler
# (gateway/models/ec2compute.py) once `tofu apply` reaches it; lambda's (a
# per-function RIE container) the same way via CreateFunction
# (gateway/models/lambdactl.py); ecs's (per-task Colima containers) via
# CreateService/UpdateService (gateway/models/ecsctl.py); logs' (a group +
# streams + events in a per-env JSON sidecar) via CreateLogGroup
# (gateway/models/logsctl.py); secret's (a record + versions in a 0600 JSON
# sidecar) via CreateSecret/PutSecretValue (gateway/models/secretsctl.py) and
# ssm's via PutParameter (gateway/models/ssmctl.py) -- the reconciler never
# touches any of them, same as vpc/subnet/sg. elasticache (W2.8) is the newest
# of these: its REAL lifecycle (a per-cluster redis:7-alpine container) is
# driven by CreateCacheCluster/DeleteCacheCluster in gateway/models/cachectl.py.
# alb (W2.5) is the same shape
# again: one canvas node expands to aws_lb + aws_lb_target_group +
# aws_lb_listener (agent/hcl.py), and its REAL substrate -- an nginx reverse
# proxy container whose upstreams are the target group's registered targets --
# is driven by the gateway's CreateLoadBalancer/CreateListener/RegisterTargets
# handlers (gateway/models/elbv2ctl.py + compute/proxy.py).
#
# kms is the same shape once more, and it is the kind whose LABEL matters most:
# real `CreateKey` takes no name at all, so `gateway/models/kmsctl.py` keys a key
# by the `odin:node` tag `agent/hcl.py::_tags_block` stamps. That tag is the ONLY
# carrier of the canvas label -- an untagged key gets a uuid and is addressable
# from nothing. Its REAL lifecycle (AES-256 material in a 0600 `.odin/{env}/
# kms.json`, sealing the secret/ssm sidecars) is driven by CreateKey /
# ScheduleKeyDeletion.

# (kind, field) pairs whose value is a CREDENTIAL BY CONSTRUCTION, whatever the
# field happens to be called (W2.4). `is_sensitive_field_name` catches names
# that LOOK secret-ish -- it would catch `secretString` by luck, and would miss
# an ssm parameter's `paramValue` entirely -- so the two kinds whose whole
# purpose is holding a secret say so explicitly here instead. The flag never
# changes how the value is USED (tofu needs the real thing); it's what keeps it
# out of the translation agent's prompt and out of every streamed `tofu` log
# line. See spec/models.py::FieldValue.sensitive.
_SENSITIVE_FIELDS = {
    "secret": frozenset({"secretString"}),
    "ssm": frozenset({"paramValue"}),
}

_KIND = {
    "rds": "rds",
    "s3": "s3",
    "sqs": "sqs",
    "sns": "sns",
    "dynamodb": "dynamodb",
    "vpc": "vpc",
    "subnet": "subnet",
    "sg": "sg",
    "iam_role": "iam_role",
    "ecr": "ecr",
    "ec2": "ec2",
    "lambda": "lambda",
    "ecs": "ecs",
    "logs": "logs",
    "secret": "secret",
    "ssm": "ssm",
    "elasticache": "elasticache",
    "alb": "alb",
    "kms": "kms",
}


def parse_ref(var: str, value: str) -> Ref | None:
    match = _REF.match(value.strip()) if isinstance(value, str) else None
    return Ref(var=var, target_id=match.group(1), target_attr=match.group(2)) if match else None


# --- the structural contract `canvas_to_stack` needs before it can build a
# Stack at all (field test 4, P4-5) ---
#
# WHERE THE LINE IS, because getting it wrong would break something that
# already works. Odin deliberately ACCEPTS nodes it cannot build: an unknown
# `type` is a supported situation, not an error -- it is reported by
# `skipped_node_types` and the translator's `not_covered`, which is how a
# canvas stays honest about the AWS surface odin hasn't covered yet. So the
# checks below never look at WHICH type a node has. They only ask whether the
# handful of fields the translator READS are the type it reads them AS:
#   * `id` / `data.label` -- the resource's name. `ResourceDesired.id` and
#     `Edge.src`/`dst` are `str`, and `id` is additionally a dict key in
#     `canvas_to_stack`'s label map. A list-valued `data.label` is precisely
#     what made a malformed canvas 500: it flowed into `Edge(dst=[...])` and
#     surfaced as an unhandled pydantic error two layers below the request.
#   * `type` -- a dict lookup key in `_KIND` (and what `skipped_node_types`
#     reports); unhashable means no lookup at all.
#   * `data` / `data.env` -- iterated as mappings.
#   * an edge's `source`/`target`/`data`/`data.edgeType`/`data.permissions`.
# Everything else stays permissive on purpose: any other `data.*` key may hold
# any JSON value (it becomes a `FieldValue`, whose `value` is `Any`), and a
# missing `position` is a thing `odin canvas set` REPAIRS, never a rejection.
_A = {str: "a string", dict: "an object", list: "a list", bool: "a boolean",
      int: "a number", float: "a number", type(None): "null"}

_NODE_SHAPE = {"id": str, "type": str, "data": dict}
_NODE_DATA_SHAPE = {"label": str, "env": dict}
_EDGE_SHAPE = {"source": str, "target": str, "data": dict}
_EDGE_DATA_SHAPE = {"edgeType": str, "permissions": list}


def _got(value: object) -> str:
    return _A.get(type(value), type(value).__name__)


def _mistyped(where: str, holder: dict, shape: dict[str, type]) -> list[str]:
    """One line per present-but-wrong-typed field. Absent and null are both
    fine here -- `canvas_to_stack` treats them as "not given"."""
    return [
        f"{where}{key} must be {_A[expected]}, not {_got(holder[key])}"
        for key, expected in shape.items()
        if holder.get(key) is not None and not isinstance(holder[key], expected)
    ]


def _node_problems(index: int, node: object) -> list[str]:
    if not isinstance(node, dict):
        return [f"node[{index}] must be an object, not {_got(node)}"]
    ident = node.get("id") if isinstance(node.get("id"), str) else None
    where = f"node[{index}]" + (f" ({ident!r})" if ident else "") + ": "
    data = node.get("data")
    problems = _mistyped(where, node, _NODE_SHAPE)
    problems += _mistyped(f"{where}data.", data, _NODE_DATA_SHAPE) if isinstance(data, dict) else []
    if problems:  # `_node_id` below can only be trusted once the shape holds
        return problems
    return [
        f"{where}{why}" for ok, why in (
            (_node_id(node), 'no "id" and no "data.label" — a node needs one of them as its name'),
            (node.get("type"), 'no "type" — every node needs a kind (an UNKNOWN kind is fine, and is '
                               "reported as skipped; a missing one is nothing at all)"),
        ) if not ok
    ]


def _edge_problems(index: int, edge: object) -> list[str]:
    if not isinstance(edge, dict):
        return [f"edge[{index}] must be an object, not {_got(edge)}"]
    where = f"edge[{index}]: "
    data = edge.get("data")
    return (
        _mistyped(where, edge, _EDGE_SHAPE)
        + (_mistyped(f"{where}data.", data, _EDGE_DATA_SHAPE) if isinstance(data, dict) else [])
    )


def canvas_problems(canvas: object) -> list[str]:
    """Every structural reason this canvas cannot be translated, in the order
    a reader would fix them; empty means `canvas_to_stack` can consume it.

    Structural is the operative word -- see the note above `_NODE_SHAPE` for
    exactly where the line sits and why an unsupported node KIND is on the
    accepted side of it."""
    if not isinstance(canvas, dict):
        return [f'a canvas must be an object with "nodes" and "edges", not {_got(canvas)}']
    problems = [
        f"{key} must be a list, not {_got(canvas[key])}"
        for key in ("nodes", "edges")
        if canvas.get(key) is not None and not isinstance(canvas[key], list)
    ]
    if problems:
        return problems
    for index, node in enumerate(canvas.get("nodes") or []):
        problems += _node_problems(index, node)
    for index, edge in enumerate(canvas.get("edges") or []):
        problems += _edge_problems(index, edge)
    return problems


def _node_id(node: dict) -> str:
    data = node.get("data") or {}
    return data.get("label") or node.get("id") or ""


def _resource(node: dict) -> ResourceDesired | None:
    kind = _KIND.get(node.get("type", ""))
    if kind is None:
        return None
    data = dict(node.get("data") or {})
    data.pop("label", None)
    data.pop("status", None)  # UI-only fields, not desired state
    data.pop("error", None)
    env_in = data.pop("env", {}) or {}

    refs: list[Ref] = []
    static_env: dict[str, str] = {}
    for key, value in env_in.items():
        ref = parse_ref(key, value)
        (refs.append(ref) if ref else static_env.update({key: value}))

    fields: dict[str, FieldValue] = {}
    for key, value in data.items():
        if value is None or value == "":
            continue
        ref = parse_ref(key, value) if isinstance(value, str) else None
        if ref is not None:  # a top-level ${{node.attr}} field becomes a Ref
            refs.append(ref)
        else:
            sensitive = is_sensitive_field_name(key) or key in _SENSITIVE_FIELDS.get(kind, ())
            fields[key] = FieldValue(value=value, provenance="user", sensitive=sensitive)
    if static_env:
        # Security finding #3: the whole `env` field is flagged sensitive if
        # ANY entry looks like a secret (coarse -- fine for the LLM-prompt
        # redaction that reads this flag; `ResourceDesired.sensitive_values`
        # re-inspects the dict key-by-key for the finer-grained tofu-log scrub).
        env_sensitive = any(is_sensitive_field_name(k) for k in static_env)
        fields["env"] = FieldValue(value=static_env, provenance="user", sensitive=env_sensitive)

    return ResourceDesired(
        id=_node_id(node), kind=kind, fields=fields, refs=tuple(refs)
    )


def _edges(e: dict, labels: dict[str, str]) -> tuple[Edge, ...]:
    # The UI stores access metadata under `data` (Canvas.tsx): `permissions` +
    # `edgeType`, one of `EDGE_KINDS` above. Thread both through so the Brain's
    # IAM review sees real grants. (Data-flow ${{node.attr}} refs are NOT edges —
    # they're lifted into ResourceDesired.refs above.)
    # Edge endpoints are ReactFlow node IDs but Stack resources are keyed by
    # LABEL — translate through `labels` (fall back for edges naming labels).
    #
    # An edge with no `edgeType` at all (a hand-authored canvas) falls to
    # `UNMODELLED`, matching what the UI now stores for the same pair. The kind
    # written here is NOT read back by any builder -- `agent/hcl.py`'s ALB and
    # subscription passes key on the two NODE kinds -- so changing the word
    # cannot change what gets built, only what the canvas says it is.
    #
    # ONE CANVAS EDGE CAN CARRY MORE THAN ONE MEANING (v0.8.15). A pair like
    # rds/ecs means both `connection` and `iam`, and in AWS both readings are
    # simultaneously true, so the picker in `ConfigPanel.tsx` is multi-select and
    # stores its answer as a `+`-joined set in the same `edgeType` string.
    # It is split back into one `Edge` per meaning HERE, and that is the point:
    # every Python consumer downstream matches a SINGLE kind
    # (`gateway/policy.py::compile_policies` and `agent/hcl.py::_granted_ids`
    # both gate on `kind == "iam"`), so a joined string reaching them would have
    # dropped the grant silently. Splitting at the boundary means none of them
    # changed at all.
    #
    # A one-meaning edge -- every canvas ever saved -- takes the identical path
    # it always did and produces the identical `Edge`, because a string with no
    # separator in it splits into itself.
    data = e.get("data") or {}
    perms = tuple(data.get("permissions") or ())
    stored = str(data.get("edgeType") or "") or ("iam" if perms else UNMODELLED)
    kinds = [k for k in (part.strip() for part in stored.split(EDGE_KIND_SEPARATOR)) if k] or [UNMODELLED]
    src, dst = e.get("source", ""), e.get("target", "")
    src, dst = labels.get(src, src), labels.get(dst, dst)
    # `perms` rides on EVERY part rather than only the `iam` one, deliberately:
    # for the single-meaning case that keeps the produced `Edge` byte-identical
    # to what this function returned before, which is what makes a stored Stack
    # revision's content hash stable across this change. Only the `iam` edge's
    # `perms` is ever read.
    return tuple(Edge(src=src, dst=dst, kind=kind, perms=perms) for kind in dict.fromkeys(kinds))


def skipped_node_types(canvas: dict) -> list[str]:
    """Distinct canvas node types that aren't runnable workloads/resources, so
    Apply/Preview can tell the user instead of silently dropping them.

    `or "?"`, not `get("type", "?")` (field test 6, F4's class): a node whose
    `type` is literally `null` used to put the Python object `None` in this list,
    which `cli/apply.py` prints as `skipped: None` and which lands in
    `not_covered` -- the ONE array the README tells CI to gate on. A `null` and a
    missing key are the same absence and now read the same. It also stops a
    canvas mixing `null` with real types from raising `TypeError` inside
    `sorted`. `/apply` and `/apply-full` reject a falsy type at the schema, but
    `/tf/plan` reads `.odin/canvas.json` from disk, which is deliberately never
    re-validated."""
    types = [n.get("type") or "?" for n in (canvas.get("nodes") or []) if n.get("type") not in _KIND]
    return sorted(set(types))


# Every node type `_KIND` maps -- i.e. every type that can become a Stack
# resource at all. Public so an apply guard can say WHY a drawn node produced
# nothing ("its type is not a kind odin models") without importing `_KIND`.
MODELLED_NODE_TYPES = frozenset(_KIND)


def drawn_node_types(canvas: dict) -> dict[str, str]:
    """Every name a canvas node claims -> that node's `type` EXACTLY as it is
    written on the canvas.

    The deliberate difference from `canvas_to_stack`: NOTHING is dropped. A
    node whose type odin doesn't model (`"s3 "` with a trailing space, field
    test 5) never becomes a Stack resource -- but it is still a node the user
    has drawn and is still asking for, and that is precisely the fact an apply
    needs in order to tell "the user deleted this node, destroy it" apart from
    "the user typo'd this node's type, do NOT destroy it".

    The type is returned verbatim (never normalised) so the caller can `!r` it:
    a one-character typo is only visible with the quotes on."""
    return {
        name: str(node.get("type") or "")
        for node in (canvas.get("nodes") or [])
        if isinstance(node, dict) and (name := _node_id(node))
    }



# --- edge kinds --------------------------------------------------------------
#
# `Edge.kind` is a free `str` (spec/models.py), which is what lets every canvas
# ever saved keep parsing -- including the ones carrying the pre-rename catch-all
# `"network"`. The cost of a free string is that a typo round-trips through the
# store and through Apply looking real, so the set below is the ONE place Python
# knows which kinds exist, and `agent/chat.py` validates against it.
#
# Being in this set does NOT mean a builder gates on it. Two consumers -- the
# subscription pass in `agent/hcl.py` and `reconcile/reconciler.py::
# _desired_subs` -- key on the two NODE kinds and never read `edge.kind`, and
# they must keep doing so: every saved canvas types an sns->sqs edge `network`,
# so requiring `kind == "subscription"` without a migration in the same commit
# would drop the subscription from the generated HCL for all of them and `tofu`
# would DESTROY the live subscription on the next apply. The reconciler would
# not catch it either -- `_desired_subs` only ever ADDS a missing subscription.

# The edge kind that means "this security group gates this resource".
#
# Membership is a RELATIONSHIP, not ownership: containment already supplies an
# SG's own `vpc_id` (it belongs to exactly one VPC, immutably), but WHICH
# instances a group gates is a many-to-many fact between peers, and geometry
# cannot express it. Before this it could only be typed into an ec2/rds node's
# `securityGroups` text field, so the canvas could not show it at all.
SG_MEMBERSHIP = "sg"

# "This workload assumes this role" -- folded into the `role` FIELD below.
ROLE_ASSUMPTION = "role"

# "This load balancer fronts that service" and "this topic fans out to that
# queue". Both are PRESENTATIONAL today (see the note above): they name what the
# user drew, and the passes that build them read the node kinds instead.
ALB_TARGET = "target"
SNS_SUBSCRIPTION = "subscription"

# "This workload's environment is wired to that producer's endpoint" -- folded
# into the consumer's `refs` by `_merge_connection_edges` below.
CONNECTION = "connection"

# "This key encrypts that sidecar at rest" -- folded into the target's own
# key-naming FIELD by `_merge_encryption_edges` below.
ENCRYPTION = "encryption"

# One canvas edge, more than one meaning. `data.edgeType` holds a `+`-joined SET
# in registry order (`ui/src/lib/iam.ts::serializeEdgeTypes`) and `_edges` splits
# it into one `Edge` per meaning, so `Edge.kind` is always exactly one kind and
# every consumer keeps matching on one. A single meaning has no separator in it
# and round-trips unchanged, which is why this needed no migration.
EDGE_KIND_SEPARATOR = "+"

# The catch-all: odin has no model for this pair of kinds, so the edge is stored
# and nothing reads it. Measured before the rename, `"network"` was the answer
# for 341 of the 378 unordered kind pairs and meant exactly this for 340 of them.
UNMODELLED = "unmodelled"

# The pre-rename catch-all. Every canvas saved before `UNMODELLED` existed
# carries it, and `Edge.kind`'s own default is `"ref"`, so both stay valid.
LEGACY_UNMODELLED = "network"

EDGE_KINDS = frozenset({
    "iam", SG_MEMBERSHIP, ROLE_ASSUMPTION, ALB_TARGET, SNS_SUBSCRIPTION,
    CONNECTION, ENCRYPTION, UNMODELLED, LEGACY_UNMODELLED, "ref",
})

# Kinds whose HCL reads `securityGroups` (`agent/hcl.py::_security_group_refs`,
# used by `_ec2` and `_rds`). An SG edge to anything else is left alone rather
# than invented into a field nothing consumes.
_SG_MEMBERS = frozenset({"ec2", "rds"})


def _merge_sg_edges(
    resources: tuple[ResourceDesired, ...], edges: tuple[Edge, ...],
) -> tuple[ResourceDesired, ...]:
    """Fold SG-membership edges into each member's `securityGroups` field.

    The field is NOT replaced. A hand-authored canvas (`odin canvas set`, the
    README's own JSON schema, the translation agent next) writes it directly and
    must keep working, so an edge ADDS to whatever is typed there -- the edge is
    another way to author the same fact, not a second source of truth competing
    with it. `agent/hcl.py` therefore needs no change at all: it still reads one
    field, and cannot tell how a line got there.

    Order is preserved and duplicates are dropped, so drawing an edge that
    duplicates a typed line is a no-op rather than a doubled `security_groups`
    entry -- which real AWS rejects.

    Direction is not significant. An edge drawn sg->instance and one drawn
    instance->sg express the same intent, exactly as an IAM edge does
    (`edgeDataForConnection` in the UI takes the same view), so both are read
    the same way rather than one silently doing nothing.
    """
    by_id = {r.id: r for r in resources}
    extra: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind != SG_MEMBERSHIP:
            continue
        for group, member in ((edge.src, edge.dst), (edge.dst, edge.src)):
            if (by_id.get(group) or None) is None or (by_id.get(member) or None) is None:
                continue
            if by_id[group].kind == "sg" and by_id[member].kind in _SG_MEMBERS:
                extra.setdefault(member, []).append(group)

    if not extra:
        return resources
    merged = []
    for res in resources:
        groups = extra.get(res.id)
        if not groups:
            merged.append(res)
            continue
        typed = [ln.strip() for ln in str(res.fields.get("securityGroups", FieldValue(value="")).value).splitlines() if ln.strip()]
        combined = list(dict.fromkeys([*typed, *groups]))
        fields = {**res.fields, "securityGroups": FieldValue(value="\n".join(combined), provenance="user")}
        merged.append(res.model_copy(update={"fields": fields}))
    return tuple(merged)


# Kinds whose HCL reads a `role` field (`agent/hcl.py::_lambda`). ec2 and ecs
# reach a role through an auto-generated role plus an instance profile /
# `task_role_arn` and read no `role` field at all, so a role edge drawn to them
# is deliberately NOT registered on the canvas (`ui/src/lib/iam.ts`) and never
# authored here -- the same rule `_SG_MEMBERS` holds. Writing a field nothing
# reads is the drawn-line-that-does-nothing bug this whole edge type exists to
# fix. See docs/limits.md for what it would take to honour those two.
_ROLE_HOLDERS = frozenset({"lambda"})


def _merge_role_edges(
    resources: tuple[ResourceDesired, ...], edges: tuple[Edge, ...],
) -> tuple[ResourceDesired, ...]:
    """Fold role edges into each holder's `role` field.

    Before this, `iam_role` was not registered as an edge target at all (it
    declares no `iamActions`, correctly -- a role is not an IAM data-plane
    target), so an `iam_role -> lambda` edge fell through to the catch-all,
    was stored in the Stack, survived every revision, and was read by NOTHING.
    Draw `admin-role -> my-lambda` while the lambda's `role` field says
    `other-role` and you got a dead edge, `other-role` in the generated file,
    and `other-role`'s statements enforced by the gateway: the canvas saying one
    thing and the gateway doing another, silently and permanently.

    Folding into the field the builder ALREADY reads is what makes the edge take
    effect with no change to `agent/hcl.py` at all -- `_lambda` still reads one
    `role` field and cannot tell how a name got there. Same technique as
    `_merge_sg_edges`, and direction is not significant for the same reason.

    A HAND-TYPED value wins. `odin canvas set`, the README's JSON schema and the
    translation agent all write the field directly, and an edge must not
    silently overwrite something a user typed -- unlike `securityGroups`, a role
    is single-valued, so "add to it" is not available as an answer here.

    Two DIFFERENT roles edged to one lambda is a contradiction the canvas can
    express and odin cannot resolve; the lowest name wins, deterministically, so
    the generated file never depends on edge ordering. That is better than the
    old behaviour (both edges did nothing) and still not right -- it is recorded
    as an open limit in docs/limits.md rather than presented as a decision.
    """
    by_id = {r.id: r for r in resources}
    edged: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind != ROLE_ASSUMPTION:
            continue
        for role, holder in ((edge.src, edge.dst), (edge.dst, edge.src)):
            role_res, holder_res = by_id.get(role), by_id.get(holder)
            if role_res is None or holder_res is None:
                continue
            if role_res.kind == "iam_role" and holder_res.kind in _ROLE_HOLDERS:
                edged.setdefault(holder, []).append(role)

    if not edged:
        return resources
    merged = []
    for res in resources:
        roles = edged.get(res.id)
        typed = str(res.fields.get("role", FieldValue(value="")).value).strip()
        if not roles or typed:
            merged.append(res)
            continue
        fields = {**res.fields, "role": FieldValue(value=sorted(roles)[0], provenance="user")}
        merged.append(res.model_copy(update={"fields": fields}))
    return tuple(merged)


# --- the encryption edge -----------------------------------------------------
#
# TARGET kind -> the canvas field naming the key it is sealed under. The two
# names differ because the two AWS APIs differ, and `agent/hcl.py` reads each
# one into the argument its own resource type takes: a secret's `kmsKeyId` ->
# `aws_secretsmanager_secret.kms_key_id`, a parameter's `keyId` ->
# `aws_ssm_parameter.key_id`. Naming them alike here would only move the
# translation somewhere less visible.
#
# EXACTLY the kinds whose HCL reads such a field, which is the same rule
# `_SG_MEMBERS`, `_ROLE_HOLDERS` and `_CONNECTION_CONSUMERS` hold, and for the
# same reason: an encryption edge to anything else would author a field no
# builder consumes -- the drawn-line-that-does-nothing bug. It is also the
# HONEST boundary here rather than an arbitrary one. `gateway/models/kmsctl.py`
# seals exactly two things, the secretsmanager and ssm sidecars; nothing
# encrypts an s3 object, an rds volume or a dynamodb item, because those live in
# real RustFS/Postgres/dynalite containers odin does not hold the keys for. So a
# `kms -> s3` edge stays `unmodelled`, whose label says on the canvas that odin
# does nothing with the line.
_ENCRYPTION_FIELDS = {"secret": "kmsKeyId", "ssm": "keyId"}
_ENCRYPTION_TARGETS = frozenset(_ENCRYPTION_FIELDS)


def _merge_encryption_edges(
    resources: tuple[ResourceDesired, ...], edges: tuple[Edge, ...],
) -> tuple[ResourceDesired, ...]:
    """Fold encryption edges into each target's own key-naming field.

    The third instance of the technique `_merge_sg_edges` and `_merge_role_edges`
    already use, and it earns its place the same way: `agent/hcl.py::_secret` and
    `::_ssm` read ONE field and cannot tell whether a key's name was typed there
    or drawn, so the edge takes effect with no builder change at all.

    A HAND-TYPED value wins, exactly as it does for `role` and for the same
    reason: `odin canvas set`, the README's JSON schema and the translation agent
    all write the field directly, and a key is single-valued, so "add to it" is
    not an available answer. Direction is not significant either -- an edge drawn
    key->secret and one drawn secret->key express the same intent.

    Two DIFFERENT keys edged to one secret is a contradiction the canvas can
    express and odin cannot resolve; the lowest name wins, deterministically, so
    the generated file never depends on edge ordering. Recorded as an open limit
    rather than presented as a decision, the same as `_merge_role_edges`'.

    WHAT IS AT STAKE if this quietly picks wrong, which is why it declines to
    guess: naming a key that does not exist is a HARD error in the gateway
    (`kmsctl.seal` raises rather than falling back to the default key), and
    deleting the key a secret was sealed under destroys that secret's value --
    `ScheduleKeyDeletion` is immediate in odin. So this authors a name only when
    the canvas already agrees on one.
    """
    by_id = {r.id: r for r in resources}
    edged: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind != ENCRYPTION:
            continue
        for key, target in ((edge.src, edge.dst), (edge.dst, edge.src)):
            key_res, target_res = by_id.get(key), by_id.get(target)
            if key_res is None or target_res is None:
                continue
            if key_res.kind == "kms" and target_res.kind in _ENCRYPTION_TARGETS:
                edged.setdefault(target, []).append(key)

    if not edged:
        return resources
    merged = []
    for res in resources:
        keys = edged.get(res.id)
        field = _ENCRYPTION_FIELDS.get(res.kind, "")
        typed = str(res.fields.get(field, FieldValue(value="")).value).strip()
        if not keys or typed:
            merged.append(res)
            continue
        fields = {**res.fields, field: FieldValue(value=sorted(keys)[0], provenance="user")}
        merged.append(res.model_copy(update={"fields": fields}))
    return tuple(merged)


# --- the connection edge -----------------------------------------------------
#
# PRODUCER kind -> (env var, the fact to read off it). Both kinds are in
# `spec/models.py::REFERENCEABLE_KINDS`, so `agent/hcl.py::_ref_fault` already
# accepts a ref to them and emits the `depends_on` that orders the producer
# before the consumer -- this authors an ordinary ref and nothing else.
#
# The PLAIN attribute, never the `_VM` one: the two consumers below are both
# containers (`CONTAINER_HOST`), and `DATABASE_URL_VM`/`REDIS_URL_VM` name the
# same port under `host.lima.internal`, which is the address an EC2 Lima VM
# needs. See docs/limits.md for `DATABASE_URL_MESH`, the only SG-gated form,
# which is deliberately not the default because it exists only when a VPC is
# drawn.
_CONNECTION_REFS: dict[str, tuple[str, str]] = {
    "rds": ("DATABASE_URL", "DATABASE_URL"),
    "elasticache": ("REDIS_URL", "REDIS_URL"),
}

# CONSUMER kinds: exactly the kinds whose real container is launched with the
# node's `env` map, which is `gateway/wiring.py::node_env`'s caller list --
# `gateway/models/ecsctl.py` and `gateway/models/lambdactl.py`, and nothing else.
# `gateway/models/ec2compute.py` imports `workload_env` (the issued gateway
# credentials) and never `node_env`, so a ref authored onto an ec2 node would
# reach NOTHING. That is the drawn-line-that-does-nothing bug this edge exists to
# fix, so ec2 is left out on the same rule `_SG_MEMBERS` and `_ROLE_HOLDERS`
# hold, and recorded in docs/limits.md rather than quietly half-supported.
_CONNECTION_CONSUMERS = frozenset({"ecs", "lambda"})


def _ref_text(target_id: str, target_attr: str) -> str:
    return "${{" + f"{target_id}.{target_attr}" + "}}"


def _authored_vars(res: ResourceDesired) -> dict[str, str]:
    """Every environment variable this resource ALREADY claims, and what it says
    each one is: static `env` entries as their literal value, refs as the
    `${{target.attr}}` text they were written as.

    Both halves matter, because `_resource` has already split one authored `env`
    map across two places by the time any merge runs."""
    env = res.fields.get("env")
    static = env.value if env is not None and isinstance(env.value, dict) else {}
    return {
        **{str(key): str(value) for key, value in static.items()},
        **{ref.var: _ref_text(ref.target_id, ref.target_attr) for ref in res.refs},
    }


def _connection_wires(
    resources: tuple[ResourceDesired, ...], edges: tuple[Edge, ...],
) -> list[tuple[str, str, Ref]]:
    """`(consumer id, producer id, the ref that edge asks for)`, in drawn order.

    Direction is not significant, exactly as it isn't for `_merge_sg_edges` and
    `_merge_role_edges`: an edge drawn db->service and one drawn service->db
    express the same intent, and only one of the two orientations can ever match
    (no producer kind is a consumer kind and vice versa)."""
    by_id = {r.id: r for r in resources}
    wires: list[tuple[str, str, Ref]] = []
    for edge in edges:
        if edge.kind != CONNECTION:
            continue
        for producer, consumer in ((edge.src, edge.dst), (edge.dst, edge.src)):
            producer_res, consumer_res = by_id.get(producer), by_id.get(consumer)
            if producer_res is None or consumer_res is None:
                continue
            wire = _CONNECTION_REFS.get(producer_res.kind)
            if wire is None or consumer_res.kind not in _CONNECTION_CONSUMERS:
                continue
            var, attr = wire
            wires.append((consumer, producer, Ref(var=var, target_id=producer, target_attr=attr)))
    return wires


def _merge_connection_edges(
    resources: tuple[ResourceDesired, ...], edges: tuple[Edge, ...],
) -> tuple[ResourceDesired, ...]:
    """Fold connection edges into each consumer's `refs`.

    The one genuine job of the most-drawn line in any architecture diagram.
    Before this, `rds -> ecs` produced a cyan IAM edge whose default grant was
    `rds-db:connect` -- an action `classify.py` can never emit, so the gateway
    never evaluated it -- while the thing the user meant, `DATABASE_URL`, still
    had to be typed by hand into the consumer's env field. Three mechanisms wear
    the word "connection": reachability (the `sg` edge, real), permission (the
    `iam` edge, real where the data plane is AWS-signed) and the ADDRESS. Only
    the address had no gesture, and this is it.

    Folding into `refs` -- the thing `gateway/wiring.py::node_env` already
    resolves and injects at container launch -- is what makes the edge take
    effect with no change to any builder, the same technique `_merge_sg_edges`
    and `_merge_role_edges` use for `securityGroups` and `role`.

    A HAND-TYPED value wins, and silently doing nothing is NOT how that is
    reported: `connection_conflicts` names every var where the two disagree, and
    `/apply-full` refuses on it. A field is a legitimate authoring surface
    (`odin canvas set`, the README's JSON schema, the translation agent), so an
    edge must not become a second source of truth beside it -- but a drawn line
    that quietly does nothing is the exact bug this edge type was created to fix,
    so it cannot be the answer to the disagreement either.

    Two producers of the same kind edged to one consumer is the other
    disagreement (both want `DATABASE_URL`). The first drawn one is authored, so
    the result never depends on which order the merge happened to see them, and
    the second is reported the same way.
    """
    wires = _connection_wires(resources, edges)
    if not wires:
        return resources
    authored: dict[str, dict[str, Ref]] = {}
    for consumer, _producer, ref in wires:
        authored.setdefault(consumer, {}).setdefault(ref.var, ref)
    return tuple(
        res.model_copy(update={"refs": (*res.refs, *added)})
        if (added := [
            ref for var, ref in authored.get(res.id, {}).items()
            if var not in _authored_vars(res)
        ]) else res
        for res in resources
    )


def connection_conflicts(stack: Stack) -> list[str]:
    """Every connection edge whose variable the consumer already claims as
    something else -- one human sentence each, for `wiring_errors`.

    Computed on the MERGED Stack and therefore idempotent: a ref this translator
    authored now agrees with the edge that asked for it, so it reports nothing,
    while a genuine disagreement survives the merge and keeps reporting. That
    matters because `/apply-full` re-derives the Stack from the canvas on every
    call.

    Reported through `wiring_errors` rather than `unsupported` for the reason
    `agent/hcl.py::_ref_fault` spells out at length: `unsupported` is the
    COVERAGE field a CI gate reads, and a canvas that wires two things to one
    variable is a user error on nodes odin supports perfectly well.

    It is FATAL at `/apply-full` (`server.py::_wiring_rejection` refuses the
    whole apply), which is the honest consequence rather than a harsher one:
    odin cannot tell which of the two answers the user meant, and applying the
    canvas would hand the workload one of them while the screen showed both.
    """
    conflicts = []
    for consumer, producer, ref in _connection_wires(stack.resources, stack.edges):
        res = next(r for r in stack.resources if r.id == consumer)
        claimed = _authored_vars(res).get(ref.var)
        wanted = _ref_text(ref.target_id, ref.target_attr)
        if claimed is None or claimed == wanted:
            continue
        conflicts.append(
            f"{consumer} ({res.kind}): the connection edge from {producer!r} would set "
            f"{ref.var}={wanted}, but {consumer!r} already sets {ref.var}={claimed}. odin cannot "
            f"tell which one you meant, so it changed nothing: delete the edge, or clear "
            f"{ref.var} on {consumer!r} and let the edge author it"
        )
    return conflicts


def _orient_subscription_edges(
    resources: tuple[ResourceDesired, ...], edges: tuple[Edge, ...],
) -> tuple[Edge, ...]:
    """Point every sns/sqs edge topic -> queue, whichever way it was drawn.

    A subscription has exactly one possible direction: a topic fans out to a
    queue, never the reverse. Both consumers nevertheless key on the DRAWN
    direction -- `agent/hcl.py`'s subscription pass reads `edge.src` as the
    topic, and `reconcile/reconciler.py::_desired_subs` filters on
    `e.src == sns_id` -- so drawing the edge queue -> topic gave a grey line, a
    green Apply, no subscription, and no entry in `unsupported` or
    `wiring_errors`. A silent no-op, with no test in either direction. Three
    hundred lines earlier hcl.py's ALB pass already accepts both orderings
    "since which end the user started from carries no meaning"; the same
    reasoning had never been applied here.

    Normalising in the SPEC rather than teaching either consumer to look at
    `edge.kind` is the deliberate choice. Every canvas saved before edge types
    were named carries `kind="network"` on this edge and works anyway, precisely
    because both consumers ignore the kind; a builder that started requiring
    `kind == "subscription"` would drop the subscription from the generated HCL
    for all of them, and `tofu` would DESTROY the live subscription on the next
    apply while the reconciler stayed quiet (`_desired_subs` only ever ADDS).
    Flipping src/dst leaves that path exactly as it is and fixes both consumers
    at once.

    `iam` edges are left alone: `hcl.py::_granted_ids` and
    `gateway/policy.py::compile_policies` both read `edge.src` as the PRINCIPAL,
    so flipping one would move a grant to a different node.
    """
    by_id = {r.id: r for r in resources}
    reversed_ids = {
        (edge.src, edge.dst) for edge in edges
        if edge.kind != "iam"
        and getattr(by_id.get(edge.src), "kind", None) == "sqs"
        and getattr(by_id.get(edge.dst), "kind", None) == "sns"
    }
    return tuple(
        edge.model_copy(update={"src": edge.dst, "dst": edge.src})
        if (edge.src, edge.dst) in reversed_ids else edge
        for edge in edges
    )


def canvas_to_stack(canvas: dict, env: str = "default") -> Stack:
    nodes = canvas.get("nodes") or []
    labels = {n["id"]: _node_id(n) for n in nodes if n.get("id")}
    resources = tuple(r for n in nodes if (r := _resource(n)) is not None)
    edges = _orient_subscription_edges(
        resources, tuple(edge for e in (canvas.get("edges") or []) for edge in _edges(e, labels)),
    )
    resources = _merge_connection_edges(
        _merge_encryption_edges(
            _merge_role_edges(_merge_sg_edges(resources, edges), edges), edges,
        ), edges,
    )
    return Stack(env=env, resources=resources, edges=edges)
