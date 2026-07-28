"""An emitted IAM policy names a real ARN -- and the gateway still enforces it.

The limit this closes read: "an emitted IAM policy names resources by label, not
ARN, so `main.tf` taken to real AWS grants that workload nothing."

THIS IS THE HIGHEST-RISK CHANGE IN ITS BATCH, and the risk is worth stating
before the tests. Since v0.8.12 the gateway authorizes from the APPLIED IAM, and
`gateway/classify.py` reports odin's node LABEL for every request -- `uploads`,
never `arn:aws:s3:::uploads`. So emitting ARNs and changing nothing else would
have left every policy in the product matching nothing: every drawn permission
silently denied, an apply still green, `main.tf` looking more correct than
before. That failure mode is invisible to any test that only reads the generated
file.

So this file tests the JOINT behaviour, in three layers:

  1. the two tables are INVERSES -- `hcl.py::_ARN_FORMS` builds an ARN,
     `policy.py::arn_label` reduces it back, for every kind either knows;
  2. the reduced label is the one the REAL classifier reports for a REAL
     request, not one this test decided was plausible. `classify()` is called
     with the wire shape each service actually uses (an `X-Amz-Target` header, a
     query-protocol form body, a REST path), so a table entry that disagrees
     with the classifier fails here rather than in production;
  3. `evaluate` still honours a BARE LABEL, because `compile_policies` (the edge
     compiler, used when a Reconciler has no gateway stores) still emits one and
     an imported hand-written policy may too.

Layers 2 and 3 are the two mutation targets. Breaking ARN matching (`arn_label`
returning None) must fail layer 2; breaking label matching (`_resource_specs`
dropping the specs themselves) must fail layer 3.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from odin.agent import hcl
from odin.agent.hcl import generate_tf
from odin.aws.backings import ACCOUNT, REGION
from odin.gateway.classify import classify
from odin.gateway.policy import Statement, arn_label, compile_policies, evaluate
from odin.spec.translate import canvas_to_stack

REPO = Path(__file__).resolve().parents[2]

# One row per kind an IAM edge can point AT: the canvas node, and the REAL
# request whose classification that grant has to match. The request shapes are
# the four wire protocols `classify.py` dispatches on -- a JSON `X-Amz-Target`
# header, the query protocol's form body, S3's (method, path), and Lambda's REST
# route -- so nothing here is a shape odin invented for the test.
#
# `resource` is what the classifier is EXPECTED to report; it is asserted rather
# than assumed, because that expectation is exactly what an ARN has to reduce to.
TARGETS: dict[str, dict] = {
    "s3": {
        "node": {"label": "uploads"},
        "action": "s3:GetObject", "resource": "uploads",
        "request": ("s3", "GET", "/uploads/key.txt", {}, {}, b""),
    },
    "sqs": {
        "node": {"label": "jobs"},
        "action": "sqs:SendMessage", "resource": "jobs",
        "request": ("sqs", "POST", "/", {}, {"X-Amz-Target": "AmazonSQS.SendMessage"},
                    b'{"QueueUrl": "http://gw/000000000000/jobs"}'),
    },
    "sns": {
        "node": {"label": "alerts"},
        "action": "sns:Publish", "resource": "alerts",
        "request": ("sns", "POST", "/", {}, {},
                    f"Action=Publish&TopicArn=arn%3Aaws%3Asns%3A{REGION}%3A{ACCOUNT}%3Aalerts".encode()),
    },
    "dynamodb": {
        "node": {"label": "items", "hashKey": "pk"},
        "action": "dynamodb:GetItem", "resource": "items",
        "request": ("dynamodb", "POST", "/", {}, {"X-Amz-Target": "DynamoDB_20120810.GetItem"},
                    b'{"TableName": "items"}'),
    },
    "lambda": {
        "node": {"label": "resizer", "runtime": "python3.12"},
        "action": "lambda:Invoke", "resource": "resizer",
        "request": ("lambda", "POST", "/2015-03-31/functions/resizer/invocations", {}, {}, b""),
    },
    "rds": {
        "node": {"label": "app-db"},
        "action": "rds:DescribeDBInstances", "resource": "app-db",
        "request": ("rds", "POST", "/", {}, {},
                    b"Action=DescribeDBInstances&DBInstanceIdentifier=app-db"),
    },
    "elasticache": {
        "node": {"label": "hot"},
        "action": "elasticache:DescribeCacheClusters", "resource": "hot",
        "request": ("elasticache", "POST", "/", {}, {},
                    b"Action=DescribeCacheClusters&CacheClusterId=hot"),
    },
    "secret": {
        "node": {"label": "app-secret", "secretString": "s"},
        "action": "secretsmanager:GetSecretValue", "resource": "app-secret",
        # The ARN form, which is what terraform passes for a `SecretId`
        "request": ("secretsmanager", "POST", "/", {},
                    {"X-Amz-Target": "secretsmanager.GetSecretValue"},
                    json.dumps({
                        "SecretId": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:app-secret",
                    }).encode()),
    },
    "ssm": {
        # HIERARCHICAL on purpose: `/odin/db` is the one label whose canonical
        # form is not simply the ARN's last path segment, and getting it wrong
        # would deny a real grant while every flat-named parameter kept working.
        "node": {"label": "/odin/db", "paramValue": "x"},
        "action": "ssm:GetParameter", "resource": "/odin/db",
        "request": ("ssm", "POST", "/", {}, {"X-Amz-Target": "AmazonSSM.GetParameter"},
                    b'{"Name": "/odin/db"}'),
    },
    "logs": {
        "node": {"label": "/aws/ecs/api"},
        "action": "logs:PutLogEvents", "resource": "/aws/ecs/api",
        "request": ("logs", "POST", "/", {}, {"X-Amz-Target": "Logs_20140328.PutLogEvents"},
                    b'{"logGroupName": "/aws/ecs/api"}'),
    },
    "ecr": {
        "node": {"label": "images"},
        "action": "ecr:BatchGetImage", "resource": "images",
        "request": ("ecr", "POST", "/", {},
                    {"X-Amz-Target": "AmazonEC2ContainerRegistry_V20150921.BatchGetImage"},
                    b'{"repositoryName": "images"}'),
    },
    "ecs": {
        "node": {"label": "worker", "image": "nginx:alpine"},
        "action": "ecs:ListTasks", "resource": "worker",
        "request": ("ecs", "POST", "/", {},
                    {"X-Amz-Target": "AmazonEC2ContainerServiceV20141113.ListTasks"},
                    b'{"serviceName": "worker"}'),
    },
}

# The workload every grant is drawn FROM. A lambda always has a role, drawn or
# auto-generated, so it needs no containment -- which keeps each canvas to the
# two nodes the grant is actually about.
_CALLER = {"id": "w", "type": "lambda", "position": {"x": 0, "y": 0},
           "data": {"label": "caller", "runtime": "python3.12"}}


def _canvas(kind: str) -> dict:
    target = TARGETS[kind]
    return {
        "nodes": [
            _CALLER,
            {"id": "t", "type": kind, "position": {"x": 0, "y": 0}, "data": target["node"]},
        ],
        "edges": [{"id": "e", "source": "w", "target": "t",
                   "data": {"edgeType": "iam", "permissions": [target["action"]]}}],
    }


def _statements(project) -> list[Statement]:
    """The Statements the gateway would authorize from, read out of the emitted
    Terraform the way `policy._statements_for_role` reads them out of `iamctl`
    -- one JSON document, `Action`/`Resource` in either singular or list form."""
    policies = [
        attrs for (rtype, _name), attrs in hcl.resource_attrs(project.files).items()
        if rtype == "aws_iam_role_policy"
    ]
    (policy,) = policies
    document = json.loads(hcl.unquote(policy["policy"]))
    return [
        Statement(actions=tuple(s["Action"]), resources=tuple(s["Resource"]))
        for s in document["Statement"]
    ]


def _applied_statements(kind: str) -> list[Statement]:
    return _statements(generate_tf(canvas_to_stack(_canvas(kind))))


# --- layer 1: the two tables are inverses -------------------------------------


@pytest.mark.parametrize("kind", sorted(hcl._ARN_FORMS))
def test_every_emitted_arn_reduces_back_to_the_node_label(kind: str):
    label = TARGETS[kind]["node"]["label"]
    action = TARGETS[kind]["action"]
    arns = hcl._resource_arns(label, kind)
    assert arns, f"no ARN shape for {kind}"
    for arn in arns:
        assert arn.startswith("arn:aws:"), arn
        assert arn_label(arn, action) == label, arn


def test_the_arn_constants_match_the_gateways():
    """`hcl.py` deliberately does not import the gateway (the deterministic
    translator stays independent of it), so the region/account are duplicated.
    Prose lock-step is what goes stale; this fails the build instead."""
    assert hcl._REGION == REGION
    assert hcl._ACCOUNT == ACCOUNT


def test_an_arn_shape_exists_for_every_kind_the_ui_offers_as_an_iam_target():
    """A new IAM target kind added to the catalog without an ARN shape here
    would fall back to the bare label -- which still ENFORCES, so nothing would
    break; it would just quietly stop being portable, and `not_in_terraform`
    would grow an entry nobody reads. Caught at build time instead."""
    offered = _ui_iam_target_kinds()
    assert len(offered) >= 11, f"the extraction found only {sorted(offered)} -- it is broken"
    assert offered <= set(hcl._ARN_FORMS), f"no ARN shape for {sorted(offered - set(hcl._ARN_FORMS))}"


def _ui_iam_target_kinds() -> set[str]:
    """Every canvas kind the UI will let a permission edge point at: the three
    built into `iam.ts` plus every catalog entry declaring `iamActions`.

    COMMENTS ARE STRIPPED FIRST, and that is not a nicety. Both files argue at
    length about which kinds are NOT IAM targets -- `alb` has a paragraph on it
    -- so a chunk-and-search over the raw text reported `alb` as a target
    purely because the prose next to it discusses `iamActions`. That is the
    "regex silently matches the wrong thing" failure this file's own
    length-check guards the other direction of.
    """
    def code(path: Path) -> str:
        return re.sub(r"//[^\n]*", "", path.read_text())

    iam_ts = code(REPO / "ui" / "src" / "lib" / "iam.ts")
    catalog_ts = code(REPO / "ui" / "src" / "lib" / "catalog.ts")
    builtin = re.search(r"iamActionsForTarget[^{]*\{(.*?)\n\};", iam_ts, re.S)
    kinds = set(re.findall(r"^\s{2}(\w+): \[", builtin.group(1), re.M)) if builtin else set()
    # A catalog entry runs from its own `type:` to the next one; it is a target
    # iff `iamActions` appears inside it.
    entries = re.split(r"\n\s*(?:\{\s*)?type: '", catalog_ts)
    return kinds | {entry.split("'", 1)[0] for entry in entries[1:] if "iamActions:" in entry}


# --- layer 2: the reduced label is what the real classifier reports ------------


@pytest.mark.parametrize("kind", sorted(TARGETS))
def test_a_real_request_is_allowed_by_the_arn_the_generator_emitted(kind: str):
    """THE MUTATION TARGET for ARN matching. Break `arn_label` and every row
    here denies -- which is exactly what would have shipped had the emitter
    changed alone."""
    target = TARGETS[kind]
    classified = classify(*target["request"])
    assert classified == (target["action"], target["resource"]), (
        f"the classifier reports {classified}, not {(target['action'], target['resource'])} -- "
        "the request shape in TARGETS no longer matches classify.py"
    )
    action, resource = classified
    assert evaluate(_applied_statements(kind), action, resource) is True


@pytest.mark.parametrize("kind", sorted(TARGETS))
def test_the_emitted_resource_really_is_an_arn_and_not_the_label(kind: str):
    """Layer 2 would still pass if the emitter had quietly kept emitting labels,
    since a label matches a label. This is the half that pins the CHANGE."""
    (statement,) = _applied_statements(kind)
    assert all(r.startswith("arn:aws:") for r in statement.resources), statement.resources
    assert TARGETS[kind]["node"]["label"] not in statement.resources


def test_a_grant_no_longer_reports_itself_as_unportable():
    """The caveat this change removes. `not_in_terraform` said "its Resource is
    the node label -- Amazon expects an ARN" for every drawn grant; a caveat that
    outlives its fix is a bug in this repo."""
    assert generate_tf(canvas_to_stack(_canvas("s3"))).not_in_terraform == []


def test_an_edge_to_a_node_that_is_not_on_the_canvas_keeps_the_label_and_says_so():
    """The one case that still falls back. Inventing an ARN for a target odin
    cannot identify would be worse than naming what was drawn."""
    canvas = _canvas("s3")
    canvas["edges"].append({"id": "e2", "source": "w", "target": "ghost",
                            "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}})
    canvas["nodes"].append({"id": "ghost", "type": "route53", "position": {"x": 0, "y": 0},
                            "data": {"label": "ghost"}})
    project = generate_tf(canvas_to_stack(canvas))
    (gap,) = project.not_in_terraform
    assert "'ghost'" in gap and "ARN" in gap
    # The grant is still emitted, naming what was drawn -- alongside the s3
    # grant, which keeps its real ARN.
    resources = [r for statement in _statements(project) for r in statement.resources]
    assert "ghost" in resources
    assert "arn:aws:s3:::uploads" in resources


# --- layer 3: a bare label still matches --------------------------------------


def test_the_edge_compiler_still_produces_statements_that_enforce():
    """THE MUTATION TARGET for label matching. `compile_policies` emits the bare
    label, and a Reconciler with no gateway stores still uses it; so does any
    hand-written policy that names a plain string. Drop the literal specs from
    `_resource_specs` and this denies while every ARN row above still passes."""
    stack = canvas_to_stack(_canvas("s3"))
    statements = compile_policies(stack)["caller"]
    assert statements[0].resources == ("uploads",)
    assert evaluate(statements, "s3:GetObject", "uploads") is True


def test_an_arn_for_another_service_does_not_match():
    """The service guard. Without it `arn:aws:s3:::*` would reduce to a bare `*`
    and match every resource of every service -- an over-grant no test of the
    happy path would notice."""
    wildcard = [Statement(actions=("*",), resources=("arn:aws:s3:::*",))]
    assert evaluate(wildcard, "s3:GetObject", "anything") is True
    assert evaluate(wildcard, "sqs:SendMessage", "anything") is False


def test_a_deny_on_the_arn_form_still_wins():
    """Explicit-deny-wins has to survive the reduction, or a Deny written as an
    ARN would be silently ignored while the Allow beside it applied."""
    statements = [
        Statement(actions=("s3:*",), resources=("uploads",)),
        Statement(effect="Deny", actions=("s3:*",), resources=("arn:aws:s3:::uploads",)),
    ]
    assert evaluate(statements, "s3:GetObject", "uploads") is False
