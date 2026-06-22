"""Translate a canvas graph into a desired-state Stack.

A canvas node becomes a ResourceDesired: its `type` maps to a kind, its `data`
becomes provenance-tagged user fields, and any field whose value is a
`${{target.attr}}` reference becomes a typed Ref (and is lifted out of the
static env so the Fabric resolves it at reconcile time).
"""
from __future__ import annotations

import re

from odin.spec.models import Edge, FieldValue, Ref, ResourceDesired, Stack

_REF = re.compile(r"^\$\{\{\s*([\w-]+)\.([\w-]+)\s*\}\}$")

# Canvas node type -> Stack kind.
_KIND = {"service": "service", "app": "service", "rds": "rds"}


def parse_ref(var: str, value: str) -> Ref | None:
    match = _REF.match(value.strip()) if isinstance(value, str) else None
    return Ref(var=var, target_id=match.group(1), target_attr=match.group(2)) if match else None


def _node_id(node: dict) -> str:
    data = node.get("data") or {}
    return data.get("label") or node.get("id") or ""


def _resource(node: dict) -> ResourceDesired | None:
    kind = _KIND.get(node.get("type", ""))
    if kind is None:
        return None
    data = dict(node.get("data") or {})
    label = data.pop("label", None)
    env_in = data.pop("env", {}) or {}

    refs: list[Ref] = []
    static_env: dict[str, str] = {}
    for key, value in env_in.items():
        ref = parse_ref(key, value)
        (refs.append(ref) if ref else static_env.update({key: value}))

    fields: dict[str, FieldValue] = {
        k: FieldValue(value=v, provenance="user") for k, v in data.items() if v is not None
    }
    if static_env:
        fields["env"] = FieldValue(value=static_env, provenance="user")

    return ResourceDesired(
        id=_node_id(node), kind=kind, fields=fields, refs=tuple(refs)
    )


def canvas_to_stack(canvas: dict, env: str = "default") -> Stack:
    nodes = canvas.get("nodes") or []
    resources = tuple(r for n in nodes if (r := _resource(n)) is not None)
    edges = tuple(
        Edge(src=e.get("source", ""), dst=e.get("target", ""))
        for e in (canvas.get("edges") or [])
    )
    return Stack(env=env, resources=resources, edges=edges)
