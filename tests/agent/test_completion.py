"""S2.4 — schema completion fills gaps, never overrides user values."""
from __future__ import annotations

from odin.agent.completion import ai_diff, complete, merge_completion, needs_completion
from odin.spec.models import FieldValue, ResourceDesired, Stack


def test_needs_completion_reports_gaps():
    stack = Stack(resources=(
        ResourceDesired(id="db", kind="rds",
                        fields={"engine": FieldValue(value="postgres")}),
    ))
    gaps = needs_completion(stack)
    assert set(gaps["db"]) == {"username", "password", "port"}  # engine already set


def test_merge_fills_only_missing_and_tags_ai():
    stack = Stack(resources=(
        ResourceDesired(id="db", kind="rds",
                        fields={"username": FieldValue(value="me", provenance="user")}),
    ))
    merged = merge_completion(stack, {"db": {"username": "ai-user", "port": 5432}})
    db = merged.resources[0]
    assert db.fields["username"].value == "me"             # user wins
    assert db.fields["username"].provenance == "user"
    assert db.fields["port"].value == 5432                 # ai filled the gap
    assert db.fields["port"].provenance == "ai"


def test_ai_diff_reports_only_ai_filled_fields():
    stack = Stack(resources=(
        ResourceDesired(id="db", kind="rds", fields={
            "engine": FieldValue(value="postgres", provenance="user"),
            "port": FieldValue(value=5432, provenance="ai"),
        }),
        ResourceDesired(id="api", kind="service", fields={
            "image": FieldValue(value="x", provenance="user"),
        }),
    ))
    assert ai_diff(stack) == {"db": {"port": 5432}}  # api has no AI fields -> omitted


def test_complete_orchestrates_fill():
    stack = Stack(resources=(ResourceDesired(id="db", kind="rds"),))

    def fake_fill(res, missing):
        return {f: "x" for f in missing}

    completed = complete(stack, fake_fill)
    db = completed.resources[0]
    assert all(db.fields[f].provenance == "ai" for f in ("engine", "username", "password", "port"))
