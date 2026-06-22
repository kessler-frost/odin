"""Schema-native completion: the LLM fills a resource's missing fields.

The deterministic merge is the load-bearing part (and the only one that touches
the Stack): user-provided fields ALWAYS win; the model may only fill fields that
are absent, and every filled field is tagged ``provenance="ai"``. The LLM call
itself is injected (``llm_fill``) so this is testable without the agent, and so
the model can never write anything except through this merge — the structural
membrane the spec requires.
"""
from __future__ import annotations

from collections.abc import Callable

from odin.spec.models import FieldValue, ResourceDesired, Stack

# Fields the model may fill per kind when the user left them blank.
COMPLETABLE: dict[str, tuple[str, ...]] = {
    "rds": ("engine", "username", "password", "port"),
    "service": ("port",),
}

# (resource, missing_field_names) -> {field: value}
LlmFill = Callable[[ResourceDesired, list[str]], dict[str, object]]


def needs_completion(stack: Stack) -> dict[str, list[str]]:
    gaps: dict[str, list[str]] = {}
    for res in stack.resources:
        missing = [f for f in COMPLETABLE.get(res.kind, ()) if f not in res.fields]
        if missing:
            gaps[res.id] = missing
    return gaps


def merge_completion(stack: Stack, filled: dict[str, dict[str, object]]) -> Stack:
    """Apply AI values to MISSING fields only; user fields are immutable."""
    resources = []
    for res in stack.resources:
        new_fields = dict(res.fields)
        for field, value in filled.get(res.id, {}).items():
            if field not in new_fields:  # user always wins
                new_fields[field] = FieldValue(value=value, provenance="ai")
        resources.append(res.model_copy(update={"fields": new_fields}))
    return stack.model_copy(update={"resources": tuple(resources)})


def ai_diff(stack: Stack) -> dict[str, dict[str, object]]:
    """The fields the AI filled, per resource — the reviewable changeset.

    `{resource_id: {field: value}}` for every field whose provenance is "ai".
    The UI shows this as a staged diff before the user commits with Apply.
    """
    return {
        res.id: {k: fv.value for k, fv in res.fields.items() if fv.provenance == "ai"}
        for res in stack.resources
        if any(fv.provenance == "ai" for fv in res.fields.values())
    }


def complete(stack: Stack, llm_fill: LlmFill) -> Stack:
    """Fill every resource's gaps via the model, then merge (user wins)."""
    gaps = needs_completion(stack)
    if not gaps:
        return stack
    by_id = {r.id: r for r in stack.resources}
    filled = {rid: llm_fill(by_id[rid], missing) for rid, missing in gaps.items()}
    return merge_completion(stack, filled)
