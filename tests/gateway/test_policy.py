"""G1 -- policy: edge-compiled Statements + evaluate().

The evaluate() cases port the 14 edge-case categories validated by the
research prototype (.superpowers/sdd/research-iam-gateway.md §Q3: "Passes
all 14 edge-case tests: prefix wildcards, `*` crossing `/` in ARNs,
`bucket/*` NOT matching the bucket ARN itself, regex metachars treated
literally, narrow deny carve-outs inside broad allows, deny-all, list-valued
fields") plus the default-deny/case-sensitivity semantics the brief adds on
top. The prototype's own test source was in a prior session's ephemeral
scratchpad and is gone; these are reconstructed from the written categories
above rather than copied verbatim, and lean comprehensive rather than
literally-14.
"""
from __future__ import annotations

from odin.gateway.policy import Statement, compile_policies, evaluate
from odin.spec.models import Edge, ResourceDesired, Stack

# --- evaluate(): default-deny + exact match -------------------------------


def test_exact_action_exact_resource_allowed():
    statements = [Statement(actions=("s3:GetObject",), resources=("uploads",))]
    assert evaluate(statements, "s3:GetObject", "uploads") is True


def test_no_statements_denies_by_default():
    assert evaluate([], "s3:GetObject", "uploads") is False


def test_wrong_action_denies():
    statements = [Statement(actions=("s3:GetObject",), resources=("uploads",))]
    assert evaluate(statements, "s3:PutObject", "uploads") is False


def test_wrong_resource_denies():
    statements = [Statement(actions=("s3:GetObject",), resources=("uploads",))]
    assert evaluate(statements, "s3:GetObject", "other-bucket") is False


# --- action wildcards -------------------------------------------------------


def test_prefix_wildcard_action_matches():
    statements = [Statement(actions=("s3:Get*",), resources=("uploads",))]
    assert evaluate(statements, "s3:GetObject", "uploads") is True


def test_prefix_wildcard_action_does_not_match_other_verb():
    statements = [Statement(actions=("s3:Get*",), resources=("uploads",))]
    assert evaluate(statements, "s3:PutObject", "uploads") is False


def test_service_wildcard_action_matches_any_verb():
    statements = [Statement(actions=("s3:*",), resources=("uploads",))]
    assert evaluate(statements, "s3:DeleteObject", "uploads") is True


def test_global_wildcard_action_matches_anything():
    statements = [Statement(actions=("*",), resources=("uploads",))]
    assert evaluate(statements, "dynamodb:PutItem", "uploads") is True


# --- resource wildcards ------------------------------------------------------


def test_resource_wildcard_matches_any_resource():
    statements = [Statement(actions=("s3:GetObject",), resources=("*",))]
    assert evaluate(statements, "s3:GetObject", "anything-at-all") is True


def test_resource_prefix_wildcard_crosses_slash():
    statements = [Statement(actions=("s3:GetObject",), resources=("bucket/*",))]
    assert evaluate(statements, "s3:GetObject", "bucket/dir/nested/key") is True


def test_resource_prefix_wildcard_does_not_match_bare_resource():
    statements = [Statement(actions=("s3:GetObject",), resources=("bucket/*",))]
    assert evaluate(statements, "s3:GetObject", "bucket") is False


# --- regex metacharacters are literal, not regex -----------------------------


def test_regex_metacharacters_in_action_are_literal():
    statements = [Statement(actions=("s3:Get.Object",), resources=("uploads",))]
    assert evaluate(statements, "s3:GetXObject", "uploads") is False
    assert evaluate(statements, "s3:Get.Object", "uploads") is True


def test_regex_metacharacters_in_resource_are_literal():
    statements = [Statement(actions=("s3:GetObject",), resources=("my.bucket",))]
    assert evaluate(statements, "s3:GetObject", "myXbucket") is False
    assert evaluate(statements, "s3:GetObject", "my.bucket") is True


# --- explicit-deny-wins (compiler never emits Deny in v1, but the evaluator
# supports it for future edge-level deny authoring, per the brief) ----------


def test_explicit_deny_overrides_broad_allow():
    statements = [
        Statement(effect="Allow", actions=("s3:*",), resources=("bucket/*",)),
        Statement(effect="Deny", actions=("s3:DeleteObject",), resources=("bucket/*",)),
    ]
    assert evaluate(statements, "s3:GetObject", "bucket/key") is True
    assert evaluate(statements, "s3:DeleteObject", "bucket/key") is False


def test_deny_all_overrides_everything():
    statements = [
        Statement(effect="Allow", actions=("*",), resources=("*",)),
        Statement(effect="Deny", actions=("*",), resources=("*",)),
    ]
    assert evaluate(statements, "s3:GetObject", "anything") is False


def test_deny_order_independent():
    """Explicit deny wins regardless of statement order."""
    statements = [
        Statement(effect="Deny", actions=("s3:DeleteObject",), resources=("bucket/*",)),
        Statement(effect="Allow", actions=("s3:*",), resources=("bucket/*",)),
    ]
    assert evaluate(statements, "s3:DeleteObject", "bucket/key") is False
    assert evaluate(statements, "s3:GetObject", "bucket/key") is True


# --- list-valued fields -------------------------------------------------------


def test_list_valued_statement_matches_any_combination():
    statements = [
        Statement(
            actions=("s3:GetObject", "s3:PutObject"),
            resources=("uploads", "downloads"),
        )
    ]
    assert evaluate(statements, "s3:PutObject", "uploads") is True
    assert evaluate(statements, "s3:GetObject", "downloads") is True
    assert evaluate(statements, "s3:DeleteObject", "uploads") is False
    assert evaluate(statements, "s3:GetObject", "other") is False


# --- case sensitivity (v1: case-sensitive, "we control both sides") --------


def test_case_sensitive_action_does_not_match_different_case():
    statements = [Statement(actions=("s3:GetObject",), resources=("uploads",))]
    assert evaluate(statements, "s3:getobject", "uploads") is False


def test_case_sensitive_resource_does_not_match_different_case():
    statements = [Statement(actions=("s3:GetObject",), resources=("Uploads",))]
    assert evaluate(statements, "s3:GetObject", "uploads") is False


# --- compile_policies(): Stack edges -> per-node statements -----------------


def _stack_with_edges() -> Stack:
    return Stack(
        env="default",
        resources=(
            ResourceDesired(id="api", kind="service"),
            ResourceDesired(id="worker", kind="batch"),
            ResourceDesired(id="uploads", kind="s3"),
            ResourceDesired(id="queue", kind="sqs"),
        ),
        edges=(
            Edge(src="api", dst="uploads", kind="iam", perms=("s3:GetObject", "s3:PutObject")),
            Edge(src="worker", dst="queue", kind="iam", perms=("sqs:SendMessage",)),
            Edge(src="api", dst="worker", kind="ref"),
        ),
    )


def test_compile_ignores_non_iam_edges():
    policies = compile_policies(_stack_with_edges())
    api_resources = {r for stmt in policies["api"] for r in stmt.resources}
    assert "worker" not in api_resources


def test_compile_produces_allow_statement_per_iam_edge():
    policies = compile_policies(_stack_with_edges())
    assert "api" in policies
    assert "worker" in policies
    api_stmt = policies["api"][0]
    assert api_stmt.effect == "Allow"
    assert api_stmt.actions == ("s3:GetObject", "s3:PutObject")
    assert api_stmt.resources == ("uploads",)


def test_compile_groups_multiple_edges_by_src_node():
    stack = Stack(
        env="default",
        resources=(
            ResourceDesired(id="api", kind="service"),
            ResourceDesired(id="uploads", kind="s3"),
            ResourceDesired(id="queue", kind="sqs"),
        ),
        edges=(
            Edge(src="api", dst="uploads", kind="iam", perms=("s3:GetObject",)),
            Edge(src="api", dst="queue", kind="iam", perms=("sqs:SendMessage",)),
        ),
    )
    policies = compile_policies(stack)
    assert len(policies["api"]) == 2
    resources = {r for stmt in policies["api"] for r in stmt.resources}
    assert resources == {"uploads", "queue"}


def test_compile_produces_statements_usable_directly_by_evaluate():
    policies = compile_policies(_stack_with_edges())
    assert evaluate(policies["api"], "s3:GetObject", "uploads") is True
    assert evaluate(policies["api"], "s3:DeleteObject", "uploads") is False
    assert evaluate(policies["worker"], "sqs:SendMessage", "queue") is True
