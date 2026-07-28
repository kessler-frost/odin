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


def _edge(e: dict, labels: dict[str, str]) -> Edge:
    # The UI stores access metadata under `data` (Canvas.tsx): `permissions` +
    # `edgeType` ("iam" | "network"). Thread both through so the Brain's IAM
    # review sees real grants. (Data-flow ${{node.attr}} refs are NOT edges —
    # they're lifted into ResourceDesired.refs above.)
    # Edge endpoints are ReactFlow node IDs but Stack resources are keyed by
    # LABEL — translate through `labels` (fall back for edges naming labels).
    data = e.get("data") or {}
    perms = tuple(data.get("permissions") or ())
    kind = data.get("edgeType") or ("iam" if perms else "network")
    src, dst = e.get("source", ""), e.get("target", "")
    return Edge(src=labels.get(src, src), dst=labels.get(dst, dst), kind=kind, perms=perms)


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



# The edge kind that means "this security group gates this resource".
#
# Membership is a RELATIONSHIP, not ownership: containment already supplies an
# SG's own `vpc_id` (it belongs to exactly one VPC, immutably), but WHICH
# instances a group gates is a many-to-many fact between peers, and geometry
# cannot express it. Before this it could only be typed into an ec2/rds node's
# `securityGroups` text field, so the canvas could not show it at all.
SG_MEMBERSHIP = "sg"

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


def canvas_to_stack(canvas: dict, env: str = "default") -> Stack:
    nodes = canvas.get("nodes") or []
    labels = {n["id"]: _node_id(n) for n in nodes if n.get("id")}
    resources = tuple(r for n in nodes if (r := _resource(n)) is not None)
    edges = tuple(_edge(e, labels) for e in (canvas.get("edges") or []))
    return Stack(env=env, resources=_merge_sg_edges(resources, edges), edges=edges)
