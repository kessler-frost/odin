"""An UNSCOPED list/describe is denied even with the edge drawn -- pinned.

ROADMAP states this under **Service coverage expansion** ("An UNSCOPED
list/describe call is denied even with an edge drawn"), and after the v0.7.1
doc pass it also states the MECHANISM, which is not the intuitive one. Until
this file nothing tested any of it: the behaviour is an emergent property of
three parts that live in different modules (`classify.py`'s per-service
resource extraction, `policy.py`'s one-sided wildcard expansion, `app.py`'s
default-deny), so any of them could have moved and only a field tester would
have noticed.

The three claims, each pinned below:

1. A call that NAMES NO RESOURCE classifies to the LITERAL string `"*"`
   (`_rds_resource` / `_classify_elasticache`'s fallback), while an IAM edge
   compiles to a statement naming one literal node label
   (`policy.compile_policies`). Wildcards are expanded on the STATEMENT side
   ONLY (`policy._pattern` is applied to the spec, never to the value), so the
   statement resource `"db"` does not match the request resource `"*"` and
   default-deny applies. The edge is real and the action is allowed -- it is
   the RESOURCE that fails to match.

2. That same literal-`"*"` fallback is why `tofu` is never blocked: the
   operator principal's statement really is `*`/`*`, and a statement-side `*`
   matches anything INCLUDING the string `"*"`. If the fallback were instead
   "unclassifiable", every operator describe would fail.

3. The denial names the ACTION and never the RESOURCE
   (`errors.access_denied`) -- which is precisely why the outcome reads as a
   contradiction to whoever drew the edge, and cost a field tester a
   confusing hour. Pinned as-is: this test asserts the message odin sends
   TODAY, so a deliberate future change to name the resource has to come here
   and say so, rather than happening by accident.

Requests are REAL boto3-signed captures through the shared `sink` fixture, so
the bare-versus-scoped distinction is the one botocore actually puts on the
wire, not a hand-built body.
"""
from __future__ import annotations

import pytest

from odin.gateway.app import GatewayState
from odin.gateway.classify import classify
from odin.gateway.errors import access_denied
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.gateway.policy import Statement, compile_policies, evaluate
from odin.spec.models import Edge, ResourceDesired, Stack

ENV = "default"
DB = "db"
CACHE = "cache"
WORKLOAD = "web"

_RDS_PERMS = ("rds:DescribeDBInstances", "rds:ModifyDBInstance")
_CACHE_PERMS = ("elasticache:DescribeCacheClusters",)


def _stack() -> Stack:
    """The canvas a field tester actually drew: a workload with an `rds` edge
    and an `elasticache` edge, each carrying the describe verb."""
    return Stack(
        env=ENV,
        resources=(
            ResourceDesired(id=WORKLOAD, kind="ecs"),
            ResourceDesired(id=DB, kind="rds"),
            ResourceDesired(id=CACHE, kind="elasticache"),
        ),
        edges=(
            Edge(src=WORKLOAD, dst=DB, kind="iam", perms=_RDS_PERMS),
            Edge(src=WORKLOAD, dst=CACHE, kind="iam", perms=_CACHE_PERMS),
        ),
    )


def _statements():
    return compile_policies(_stack())[WORKLOAD]


def _classified(sink, service: str, call) -> tuple[str, str]:
    request = sink.call(call)
    return classify(service, request.method, "/", {}, request.headers, request.body)


# --- claim 1: the literal "*" resource, and the one-sided wildcard ----------


def test_a_bare_describe_classifies_to_the_literal_star_resource(sink, rds, elasticache):
    """Not "unclassifiable", and not the node label -- the LITERAL string
    `"*"`. Everything else in this file follows from that one value."""
    assert _classified(sink, "rds", lambda: rds.describe_db_instances()) == (
        "rds:DescribeDBInstances", "*",
    )
    assert _classified(sink, "elasticache", lambda: elasticache.describe_cache_clusters()) == (
        "elasticache:DescribeCacheClusters", "*",
    )


def test_a_scoped_describe_classifies_to_the_node_label(sink, rds, elasticache):
    """The identifier IS the canvas label (`agent/hcl.py` emits
    `identifier = <label>`), which is what the edge compiled its statement
    against."""
    assert _classified(sink, "rds", lambda: rds.describe_db_instances(DBInstanceIdentifier=DB)) == (
        "rds:DescribeDBInstances", DB,
    )
    assert _classified(
        sink, "elasticache", lambda: elasticache.describe_cache_clusters(CacheClusterId=CACHE),
    ) == ("elasticache:DescribeCacheClusters", CACHE)


def test_the_drawn_edge_compiles_to_one_literal_resource_never_a_wildcard():
    statements = _statements()
    assert {statement.resources for statement in statements} == {(DB,), (CACHE,)}


@pytest.mark.parametrize(("action", "resource"), [
    ("rds:DescribeDBInstances", DB),
    ("elasticache:DescribeCacheClusters", CACHE),
])
def test_the_scoped_call_is_allowed_by_the_drawn_edge(action, resource):
    assert evaluate(_statements(), action, resource) is True


@pytest.mark.parametrize("action", ["rds:DescribeDBInstances", "elasticache:DescribeCacheClusters"])
def test_the_unscoped_call_is_denied_by_the_very_same_edge(action):
    """The heart of it: same principal, same statements, same ALLOWED action
    -- denied purely because the request's resource is the literal `"*"` and
    a statement resource of `"db"` cannot match it. `policy._pattern` is only
    ever applied to the statement's spec, so wildcards never expand on the
    REQUEST side."""
    assert evaluate(_statements(), action, "*") is False


def test_a_statement_side_wildcard_is_what_expands_not_the_request_side():
    """Stated as its own case because it is the whole asymmetry: `db*` (a
    statement) matches the request resource `db`, while `db` (a statement)
    does not match the request resource `*`."""
    assert evaluate([Statement(actions=("rds:*",), resources=("db*",))], "rds:DescribeDBInstances", DB) is True
    assert evaluate([Statement(actions=("rds:*",), resources=("db",))], "rds:DescribeDBInstances", "*") is False


# --- claim 2: which is exactly why tofu is not blocked ----------------------


@pytest.mark.parametrize("action", ["rds:DescribeDBInstances", "elasticache:DescribeCacheClusters"])
def test_the_operator_is_not_blocked_by_the_same_fallback(action):
    """`tofu` runs as the operator principal, whose statement really is
    `*`/`*` -- and a statement-side `*` matches anything, including the
    literal `"*"` a bare describe classifies to. This is the reason the
    fallback is `"*"` rather than an unmappable-action denial."""
    operator = GatewayState().statements_for(ENV, OPERATOR_NODE_ID)
    assert evaluate(operator, action, "*") is True
    assert evaluate(operator, action, DB) is True


# --- claim 3: the denial names the action, never the resource --------------


@pytest.mark.parametrize(("service", "action"), [
    ("rds", "rds:DescribeDBInstances"),
    ("elasticache", "elasticache:DescribeCacheClusters"),
])
def test_the_denial_names_the_action_and_never_the_resource(service, action):
    """Pinned as the CURRENT wording, not endorsed as the best one: the
    response tells you the action you hold an edge for was denied and says
    nothing about the resource resolving to `"*"`, which is what makes it
    read as a contradiction. Changing it is a deliberate decision that has to
    edit this assertion."""
    body = access_denied(service, action).body.decode()
    assert f"User is not authorized to perform: {action}" in body
    assert "*" not in body
    assert "unscoped" not in body.lower()
