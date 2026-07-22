"""Edge-compiled IAM policies for the odin gateway.

compile_policies turns a Stack's `kind == "iam"` edges (workload -> resource
node, carrying AWS verbs) into per-node Allow statements. evaluate is the
general matcher productionized from the research prototype
(.superpowers/sdd/research-iam-gateway.md §Q3): `*` wildcards (matching any
sequence, including across `/`) with every other character taken literally,
case-sensitive on both sides (odin controls both the compiler's casing and
the classifier's), explicit-deny-wins, default-deny. The compiler itself
never emits Deny in v1 -- Deny support is kept in the evaluator for future
edge-level deny authoring and is exercised by tests.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from odin.spec.models import Stack

Effect = Literal["Allow", "Deny"]


class Statement(BaseModel):
    model_config = {"frozen": True}
    effect: Effect = "Allow"
    actions: tuple[str, ...]
    resources: tuple[str, ...]


def _pattern(spec: str) -> re.Pattern[str]:
    """Compile an IAM-style wildcard spec: '*' matches any sequence
    (including across '/'); every other character is matched literally."""
    parts = spec.split("*")
    return re.compile("^" + ".*".join(re.escape(part) for part in parts) + "$")


def _matches_any(specs: tuple[str, ...], value: str) -> bool:
    return any(_pattern(spec).fullmatch(value) for spec in specs)


def compile_policies(stack: Stack) -> dict[str, list[Statement]]:
    """Compile each workload's `kind == "iam"` edges into Allow statements."""
    policies: dict[str, list[Statement]] = {}
    for edge in stack.edges:
        if edge.kind != "iam":
            continue
        statement = Statement(actions=edge.perms, resources=(edge.dst,))
        policies.setdefault(edge.src, []).append(statement)
    return policies


def evaluate(statements: list[Statement], action: str, resource: str) -> bool:
    """default-deny; an explicit Deny beats any Allow regardless of order."""
    allowed = False
    for statement in statements:
        if not (_matches_any(statement.actions, action) and _matches_any(statement.resources, resource)):
            continue
        if statement.effect == "Deny":
            return False
        allowed = True
    return allowed
