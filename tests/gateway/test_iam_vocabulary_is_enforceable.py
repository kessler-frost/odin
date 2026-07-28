"""Every permission the UI offers must be one the gateway can actually enforce.

The README's first claim is "the IAM permissions you draw are enforced". That
holds only while the action strings the UI writes onto an edge are the same
strings `gateway/classify.py` produces for a real request -- and nothing checked
that, across a TypeScript/Python boundary where nothing type-checks either side.

The failure it prevents, measured against the real evaluator before this test
existed:

    evaluate([Statement(actions=("lambda:InvokeFunction",))], "lambda:Invoke")  -> False
    evaluate([Statement(actions=("lambda:Invoke",))],         "lambda:Invoke")  -> True

AWS spells it `lambda:InvokeFunction`; odin's classifier emits `lambda:Invoke`
(`_LAMBDA_ROUTES`). Offering the AWS spelling in the UI would produce an edge
that draws, applies, reports success -- and grants nothing. A DECORATIVE
permission, which is the exact class of bug this repo's honesty rules exist for,
and one no amount of UI testing would catch.

## How a service's actions are decided

Two shapes in `classify.py`, and they need different checks:

  * FIXED-op services parse a request into one of a known set. `lambda` is the
    only one today (`_LAMBDA_ROUTES`), so every `lambda:*` action the UI offers
    must name an op in that table.
  * TARGET-derived services build `f"{service}:{op}"` straight from the
    `x-amz-target` header (ecr, ecs, logs, secretsmanager, ssm, dynamodb, ...),
    so ANY op a real SDK sends is emittable and only the SERVICE prefix can be
    checked here.

Both checks are worth having: the first catches a wrong op name, the second
catches a whole service the gateway does not classify at all -- an edge to a
kind whose calls can never be matched.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from odin.gateway.classify import classify
from odin.gateway.policy import Statement, evaluate

REPO = Path(__file__).resolve().parents[2]
CLASSIFY = (REPO / "src" / "odin" / "gateway" / "classify.py").read_text()
IAM_TS = (REPO / "ui" / "src" / "lib" / "iam.ts").read_text()
CATALOG_TS = (REPO / "ui" / "src" / "lib" / "catalog.ts").read_text()

# Services `classify.py` dispatches on (`if service == "..."`). An action whose
# prefix is missing here can never be produced, so an edge granting it is inert.
# Both dispatch spellings: `service == "x"` AND `service in ("x", "y")`.
# Matching only the first form reported sqs as unclassified, which it is not --
# it shares `_classify_target` with dynamodb. A checker that cries wolf is worse
# than none, so this reads the real dispatch rather than one shape of it.
CLASSIFIED_SERVICES = set(re.findall(r'service == "([a-z0-9-]+)"', CLASSIFY)) | {
    name
    for group in re.findall(r'service in \(([^)]*)\)', CLASSIFY)
    for name in re.findall(r'"([a-z0-9-]+)"', group)
}

# The ops `_LAMBDA_ROUTES` can return, i.e. the only valid `lambda:*` actions.
LAMBDA_OPS = set(re.findall(r'\),\s*"([A-Za-z]+)"\),', CLASSIFY))

# `rds-db:connect` is the IAM-auth action for connecting to a database. It is
# not produced by `classify.py` because it is not an API call at all -- it names
# the data-plane connection, which the mesh firewall gates. Kept because AWS
# spells it exactly this way and the canvas should too.
NOT_API_ACTIONS = {"rds-db:connect"}


def _ui_actions() -> set[str]:
    """Every action string the UI can put on an edge, from both files."""
    # An IAM action is `service:Op` where Op is UpperCamel or `*`. Requiring
    # that leading capital is what separates a permission from a docker image
    # tag: a first pass matched `nginx:alpine` out of a node's defaultData and
    # reported it as an unenforceable grant.
    return {
        action
        for text in (IAM_TS, CATALOG_TS)
        for action in re.findall(r"'([a-z0-9-]+:(?:[A-Z][A-Za-z]*|\*))'", text)
    }


def test_the_ui_offers_actions_at_all():
    """A regex that silently matches nothing would make every test below pass."""
    actions = _ui_actions()
    assert len(actions) > 20, f"only found {len(actions)} — the extraction is broken, not the vocabulary"
    assert "s3:GetObject" in actions
    assert "lambda:Invoke" in actions


@pytest.mark.parametrize("action", sorted(_ui_actions()))
def test_every_offered_action_belongs_to_a_service_the_gateway_classifies(action: str):
    if action in NOT_API_ACTIONS:
        return
    service = action.split(":", 1)[0]
    assert service in CLASSIFIED_SERVICES, (
        f"the UI offers {action!r}, but `classify.py` never dispatches on service {service!r} — "
        "a request could not be matched to it, so the grant would be decorative"
    )


@pytest.mark.parametrize("action", sorted(a for a in _ui_actions() if a.startswith("lambda:")))
def test_every_lambda_action_names_a_real_route_op(action: str):
    """The one fixed-op service, and the one that already bit."""
    op = action.split(":", 1)[1]
    if op == "*":
        return
    assert op in LAMBDA_OPS, (
        f"the UI offers {action!r}, but `_LAMBDA_ROUTES` can only ever emit {sorted(LAMBDA_OPS)}. "
        "AWS's own spelling is not necessarily odin's: `lambda:InvokeFunction` grants nothing "
        "because the classifier emits `lambda:Invoke`."
    )


def test_the_aws_spelling_of_invoke_really_would_not_work():
    """The measurement this file exists for, kept executable rather than quoted.

    If a future change makes `lambda:InvokeFunction` work, this fails and the
    warning above should be rewritten rather than left to mislead.
    """
    aws_spelling = [Statement(actions=("lambda:InvokeFunction",), resources=("thumbnailer",))]
    odins_spelling = [Statement(actions=("lambda:Invoke",), resources=("thumbnailer",))]
    assert evaluate(aws_spelling, "lambda:Invoke", "thumbnailer") is False
    assert evaluate(odins_spelling, "lambda:Invoke", "thumbnailer") is True


def test_a_wildcard_grant_covers_its_service():
    """`<service>:*` is offered for every target, so it has to actually work."""
    for action in ("lambda:Invoke", "ecr:BatchGetImage", "ecs:RunTask", "s3:GetObject"):
        service = action.split(":", 1)[0]
        statements = [Statement(actions=(f"{service}:*",), resources=("thing",))]
        assert evaluate(statements, action, "thing") is True, action


# --- EventBridge, checked FORWARDS as well as backwards -----------------------
#
# Every test above starts from the UI and asks whether the gateway can keep up.
# That direction cannot protect a service the UI has not reached yet: an
# `events` edge is being drawn in the canvas in parallel with the classifier
# landing here, and until both are in the tree `_ui_actions()` yields no
# `events:*` at all, so every parametrized test above would pass vacuously
# while the grant was inert.
#
# So this pair checks the OTHER direction for the newest service: given the
# actions an `events` edge will offer, can the classifier emit them, and does
# an edge granting one really allow the matching call? Written against the real
# `classify`/`evaluate`, not against a list of strings -- a string list would
# agree with itself.

# What an `events` iam edge is for, and the only actions worth offering: the
# rule's own read/describe surface plus `PutEvents`, which is the one a
# workload actually calls. Kept small deliberately -- the create verbs belong to
# the operator (tofu), never to a workload edge.
EVENTS_EDGE_ACTIONS = ("events:PutEvents", "events:DescribeRule", "events:ListTargetsByRule", "events:*")


def test_the_gateway_classifies_eventbridge_at_all():
    assert "events" in CLASSIFIED_SERVICES, (
        "`classify.py` no longer dispatches on `events`, so every `events:*` grant is decorative "
        "and an `aws_cloudwatch_event_rule` in generated Terraform fails `tofu apply` with "
        "AccessDenied/unmappable-action"
    )


@pytest.mark.parametrize("action", EVENTS_EDGE_ACTIONS)
def test_an_events_grant_allows_the_call_the_classifier_really_emits(action: str):
    """The falsification: build the statement an edge compiles to, then feed
    `evaluate` the exact `(action, resource)` pair `classify` produces for a
    real EventBridge request -- so a rename on either side fails here."""
    body = json.dumps({"Entries": [{"Source": "odin", "DetailType": "t", "Detail": "{}"}]}).encode()
    emitted = classify(
        "events", "POST", "/", {}, {"X-Amz-Target": "AWSEvents.PutEvents"}, body,
    )
    assert emitted == ("events:PutEvents", "default"), "classify no longer names the bus PutEvents targets"
    statements = [Statement(actions=(action,), resources=("default",))]
    expected = action in ("events:PutEvents", "events:*")
    assert evaluate(statements, *emitted) is expected, action
