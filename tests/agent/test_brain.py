"""M1 — the real Brain (claude-agent-sdk) fills missing fields.

Marked `integration`: drives the Claude Code CLI. Values are non-deterministic,
so we assert the gaps got filled + tagged `ai` and the user value survived.
"""
from __future__ import annotations

import pytest

from odin.agent.brain import claude_complete, review_iam
from odin.spec.models import Edge, FieldValue, ResourceDesired, Stack

pytestmark = pytest.mark.integration


async def test_claude_fills_rds_gaps():
    stack = Stack(resources=(
        ResourceDesired(id="db", kind="rds",
                        fields={"engine": FieldValue(value="postgres", provenance="user")}),
    ))
    out = await claude_complete(stack)
    db = out.resources[0]

    assert db.fields["engine"].value == "postgres"        # user value survives
    assert db.fields["engine"].provenance == "user"
    for field in ("username", "password", "port"):
        assert field in db.fields                          # gap filled
        assert db.fields[field].provenance == "ai"         # tagged AI


async def test_review_iam_returns_list():
    assert await review_iam(Stack()) == []                 # no edges -> no call
    # ref (data-flow) edges are NOT access grants -> ignored, no LLM call
    refs_only = Stack(edges=(Edge(src="api", dst="db", kind="ref"),))
    assert await review_iam(refs_only) == []
    stack = Stack(edges=(Edge(src="api", dst="db", kind="iam", perms=("rds:*",)),))
    findings = await review_iam(stack)
    assert isinstance(findings, list)                      # LLM returns findings
