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
    Apply/Preview can tell the user instead of silently dropping them."""
    types = [n.get("type", "?") for n in (canvas.get("nodes") or []) if n.get("type") not in _KIND]
    return sorted(set(types))


def canvas_to_stack(canvas: dict, env: str = "default") -> Stack:
    nodes = canvas.get("nodes") or []
    labels = {n["id"]: _node_id(n) for n in nodes if n.get("id")}
    resources = tuple(r for n in nodes if (r := _resource(n)) is not None)
    edges = tuple(_edge(e, labels) for e in (canvas.get("edges") or []))
    return Stack(env=env, resources=resources, edges=edges)
