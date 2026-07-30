"""The connection edge: the most-drawn line in any architecture diagram, and
the one odin did nothing with.

`rds -> ecs` produced a cyan IAM edge whose default grant was `rds-db:connect`
-- an action `gateway/classify.py` can NEVER emit, because it builds
`rds:<Action>` from the query protocol's `Action` param and `rds-db:` is a
different service prefix entirely. For elasticache the default was
Describe-only, and no IAM policy can gate a Redis GET/SET at all. Meanwhile the
thing a user actually means by that line -- `DATABASE_URL` in the app's
environment -- still had to be typed by hand into the consumer's `env` field,
and no gesture authored it.

So the edge drawn most often was decorative. This is the gesture that authors
the address, and the address is the only one of "connection"'s three mechanisms
that was missing (reachability is the `sg` edge; permission is the `iam` edge).

EVERYTHING HERE GOES THROUGH THE PRODUCT'S OWN PATH -- `canvas_to_stack`, and
for the parts a builder must see, on into `generate_tf` -- for the reason
`test_edge_types.py` gives: the class of bug being fixed is a value that looks
right in the Stack and is read by nobody downstream.
"""
from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from odin.agent.hcl import generate_tf
from odin.gateway.policy import compile_policies
from odin.server import create_app
from odin.spec.models import Stack
from odin.spec.store import SpecStore
from odin.spec.translate import (
    CONNECTION,
    EDGE_KINDS,
    canvas_to_stack,
    connection_conflicts,
)
from tests.api.test_apply_full import FakeAws, FakeRds, FakeRuntime


def _edge(source: str, target: str, kind: str, **data) -> dict:
    return {"id": f"{source}-{target}", "source": source, "target": target,
            "data": {"edgeType": kind, **data}}


def _canvas(edges: list[dict], *, env: dict | None = None) -> dict:
    """One database, one cache, and one of each consumer kind -- plus the ec2
    node that deliberately gets nothing."""
    return {
        "nodes": [
            {"id": "db-1", "type": "rds", "position": {"x": 0, "y": 0},
             "data": {"label": "app-db", "engine": "postgres", "dbName": "postgres",
                      "username": "app", "password": "apppass123"}},
            {"id": "db-2", "type": "rds", "position": {"x": 0, "y": 40},
             "data": {"label": "other-db", "engine": "postgres", "dbName": "postgres",
                      "username": "app", "password": "apppass123"}},
            {"id": "cache-1", "type": "elasticache", "position": {"x": 0, "y": 80},
             "data": {"label": "cache", "nodeType": "cache.t3.micro"}},
            {"id": "ecs-1", "type": "ecs", "position": {"x": 40, "y": 0},
             "data": {"label": "web", "image": "nginx:alpine", **({"env": env} if env else {})}},
            {"id": "lam-1", "type": "lambda", "position": {"x": 40, "y": 40},
             "data": {"label": "worker", "code": "def lambda_handler(e, c):\n    return e",
                      **({"env": env} if env else {})}},
            {"id": "ec2-1", "type": "ec2", "position": {"x": 40, "y": 80},
             "data": {"label": "box"}},
        ],
        "edges": edges,
    }


def _refs(stack: Stack, resource_id: str) -> dict[str, str]:
    """`var -> ${{target.attr}}` for one resource, the shape a reader compares."""
    resource = next(r for r in stack.resources if r.id == resource_id)
    return {r.var: "${{" + f"{r.target_id}.{r.target_attr}" + "}}" for r in resource.refs}


# --- 1. the edge authors the ref ----------------------------------------------


def test_a_connection_edge_authors_the_database_url():
    """The whole point: draw the line, get the variable."""
    stack = canvas_to_stack(_canvas([_edge("db-1", "ecs-1", CONNECTION)]))
    assert _refs(stack, "web") == {"DATABASE_URL": "${{app-db.DATABASE_URL}}"}


def test_the_ref_survives_into_the_generated_terraform():
    """`refs` is not the finish line -- `agent/hcl.py` has to accept it and emit
    the `depends_on` that orders the database before the service, or the ref
    resolves against a database that does not exist yet and the task STOPS.

    Asserted through `generate_tf` rather than on the Stack for the same reason
    the role edge is: the bug class is a value nothing downstream reads."""
    project = generate_tf(canvas_to_stack(_canvas([_edge("db-1", "ecs-1", CONNECTION)])))
    assert project.wiring_errors == [], project.wiring_errors
    main = project.files["main.tf"]
    assert "aws_db_instance" in main
    # The service must depend on the database it was wired to.
    assert "depends_on" in main


def test_direction_does_not_matter():
    """An edge drawn db->service and one drawn service->db express the same
    intent, exactly as `_merge_sg_edges` and `_merge_role_edges` already hold."""
    forward = canvas_to_stack(_canvas([_edge("db-1", "ecs-1", CONNECTION)]))
    backward = canvas_to_stack(_canvas([_edge("ecs-1", "db-1", CONNECTION)]))
    assert _refs(forward, "web") == _refs(backward, "web")
    assert _refs(forward, "web") != {}


def test_a_cache_authors_redis_url():
    stack = canvas_to_stack(_canvas([_edge("cache-1", "lam-1", CONNECTION)]))
    assert _refs(stack, "worker") == {"REDIS_URL": "${{cache.REDIS_URL}}"}


def test_both_producers_can_wire_one_consumer():
    """A service with a database AND a cache is the ordinary case."""
    stack = canvas_to_stack(_canvas([
        _edge("db-1", "ecs-1", CONNECTION), _edge("cache-1", "ecs-1", CONNECTION),
    ]))
    assert _refs(stack, "web") == {
        "DATABASE_URL": "${{app-db.DATABASE_URL}}", "REDIS_URL": "${{cache.REDIS_URL}}",
    }


# --- 2. the kinds it deliberately does NOT cover -------------------------------


def test_an_ec2_consumer_gets_NOTHING_because_it_never_receives_the_env_map():
    """MEASURED, not assumed. `gateway/wiring.py::node_env` has exactly two
    callers -- `gateway/models/ecsctl.py` and `gateway/models/lambdactl.py`.
    `gateway/models/ec2compute.py` imports `workload_env` (the issued gateway
    credentials) and never `node_env`, so a ref authored onto an ec2 node would
    reach nothing at all.

    Authoring it anyway is the drawn-line-that-does-nothing bug this edge type
    exists to fix, so ec2 is excluded on the same rule `_SG_MEMBERS` and
    `_ROLE_HOLDERS` hold, and `docs/limits.md` says so. The canvas does not
    register the pair either (`ui/src/lib/iam.ts`), so this is what happens to a
    hand-authored canvas that names the kind directly."""
    stack = canvas_to_stack(_canvas([_edge("db-1", "ec2-1", CONNECTION)]))
    assert _refs(stack, "box") == {}


def test_a_producer_odin_has_no_variable_name_for_authors_nothing():
    """s3 publishes real observed facts and is not a wiring producer; alb and
    ecr ARE referenceable but have no single obvious variable name, and guessing
    one authors a field the app does not read."""
    canvas = _canvas([_edge("cache-1", "ecs-1", CONNECTION)])
    canvas["nodes"].append(
        {"id": "b-1", "type": "s3", "position": {"x": 80, "y": 0}, "data": {"label": "bucket"}},
    )
    canvas["edges"].append(_edge("b-1", "ecs-1", CONNECTION))
    stack = canvas_to_stack(canvas)
    assert _refs(stack, "web") == {"REDIS_URL": "${{cache.REDIS_URL}}"}


# --- 3. a typed value wins, and the disagreement is REPORTED -------------------


def test_a_hand_typed_static_value_wins():
    """`odin canvas set`, the README's JSON schema and the translation agent all
    write `env` directly. A field is a legitimate authoring surface and an edge
    must not become a second source of truth beside it."""
    stack = canvas_to_stack(_canvas(
        [_edge("db-1", "ecs-1", CONNECTION)], env={"DATABASE_URL": "postgresql://elsewhere/db"},
    ))
    assert _refs(stack, "web") == {}


def test_a_hand_typed_ref_to_a_DIFFERENT_producer_wins():
    stack = canvas_to_stack(_canvas(
        [_edge("db-1", "ecs-1", CONNECTION)], env={"DATABASE_URL": "${{other-db.DATABASE_URL}}"},
    ))
    assert _refs(stack, "web") == {"DATABASE_URL": "${{other-db.DATABASE_URL}}"}


def test_a_disagreement_is_reported_naming_BOTH_answers():
    """Never resolved silently. The typed value wins deterministically so every
    non-refusing path stays sane, and `/apply-full` refuses on this list
    (`server.py::_wiring_rejection`) so the user is told which line odin
    ignored instead of finding out never."""
    stack = canvas_to_stack(_canvas(
        [_edge("db-1", "ecs-1", CONNECTION)], env={"DATABASE_URL": "postgresql://elsewhere/db"},
    ))
    (note,) = connection_conflicts(stack)
    assert "web" in note and "app-db" in note
    assert "DATABASE_URL" in note
    assert "postgresql://elsewhere/db" in note        # what the canvas says
    assert "${{app-db.DATABASE_URL}}" in note         # what the edge asked for


def test_AGREEMENT_is_not_a_conflict():
    """Typing the ref the edge would have written is a user saying the same
    thing twice, not a contradiction. Reporting it would train people to ignore
    the list."""
    stack = canvas_to_stack(_canvas(
        [_edge("db-1", "ecs-1", CONNECTION)], env={"DATABASE_URL": "${{app-db.DATABASE_URL}}"},
    ))
    assert connection_conflicts(stack) == []


def test_the_report_is_IDEMPOTENT_over_the_merged_stack():
    """`connection_conflicts` reads the stack the merge already ran on, and
    `/apply-full` re-derives that stack from the canvas on every call. A ref
    this translator authored must therefore agree with the edge that asked for
    it, or every apply would report a conflict with itself."""
    stack = canvas_to_stack(_canvas([_edge("db-1", "ecs-1", CONNECTION)]))
    assert connection_conflicts(stack) == []
    assert connection_conflicts(canvas_to_stack(_canvas([_edge("db-1", "ecs-1", CONNECTION)]))) == []


def test_two_databases_on_one_service_is_a_conflict_and_the_result_is_STABLE():
    """Both want `DATABASE_URL`. The FIRST DRAWN one is authored, so the
    generated file never depends on the order the merge happened to see them,
    and the second is reported rather than silently dropped."""
    stack = canvas_to_stack(_canvas([
        _edge("db-1", "ecs-1", CONNECTION), _edge("db-2", "ecs-1", CONNECTION),
    ]))
    assert _refs(stack, "web") == {"DATABASE_URL": "${{app-db.DATABASE_URL}}"}
    (note,) = connection_conflicts(stack)
    assert "other-db" in note and "app-db" in note


# --- 4. one line, more than one meaning ---------------------------------------


def test_a_joined_edge_type_becomes_one_edge_PER_meaning():
    """`rds/ecs` means both `connection` and `iam`, and in AWS both readings are
    simultaneously true -- odin's first genuinely ambiguous pair. The UI stores
    the multi-select as a `+`-joined set, and it is split HERE so every Python
    consumer keeps matching a single kind."""
    stack = canvas_to_stack(_canvas(
        [_edge("ecs-1", "db-1", "connection+iam", permissions=["rds:DescribeDBInstances"])],
    ))
    kinds = sorted(e.kind for e in stack.edges)
    assert kinds == ["connection", "iam"]
    assert _refs(stack, "web") == {"DATABASE_URL": "${{app-db.DATABASE_URL}}"}


def test_the_GRANT_survives_a_joined_edge_type():
    """The failure this split exists to prevent. `gateway/policy.py::
    compile_policies` and `agent/hcl.py::_granted_ids` both gate on
    `kind == "iam"`, so a joined string reaching them intact would have compiled
    NO policy -- the user would see permissions ticked in the panel and get
    none, which is this repo's most-repeated bug shape."""
    stack = canvas_to_stack(_canvas(
        [_edge("ecs-1", "db-1", "connection+iam", permissions=["rds:DescribeDBInstances"])],
    ))
    statements = compile_policies(stack)["web"]
    assert [a for s in statements for a in s.actions] == ["rds:DescribeDBInstances"]


def test_a_single_meaning_round_trips_UNCHANGED():
    """Why this needed no migration: a string with no separator in it splits
    into itself, so every canvas ever saved produces exactly the `Edge` it
    always did."""
    for kind in sorted(EDGE_KINDS):
        stack = canvas_to_stack(_canvas([_edge("ecs-1", "db-1", kind)]))
        assert [e.kind for e in stack.edges] == [kind], kind


def test_an_edge_with_no_type_at_all_is_unchanged():
    """A hand-authored canvas that names no `edgeType`."""
    stack = canvas_to_stack({
        "nodes": _canvas([])["nodes"],
        "edges": [{"id": "e1", "source": "db-1", "target": "ecs-1"}],
    })
    assert [e.kind for e in stack.edges] == ["unmodelled"]
    assert _refs(stack, "web") == {}


# --- 5. the migration hazard, held open ---------------------------------------


def test_a_LEGACY_rds_to_ecs_edge_authors_NOTHING():
    """Every canvas saved before v0.8.15 types this pair `iam`, and this asserts
    that such a canvas is completely unaffected -- it gets no new environment
    variable, no new `depends_on`, and no conflict.

    That is the safe direction of the same-commit-migration rule. The rule
    exists because gating a builder on `edge.kind` can DESTROY something already
    being built (an sns->sqs subscription typed `network`); a NEW meaning gated
    on a NEW kind only withholds a new feature from an old canvas, which is the
    behaviour a user upgrading is entitled to. Ticking the box turns it on."""
    stack = canvas_to_stack(_canvas([_edge("db-1", "ecs-1", "iam", permissions=["rds:*"])]))
    assert _refs(stack, "web") == {}
    assert connection_conflicts(stack) == []
    assert [e.kind for e in stack.edges] == ["iam"]


def test_connection_is_a_registered_kind_so_the_agent_can_draw_it():
    """`agent/chat.py` validates an agent-proposed edge against `EDGE_KINDS` and
    refuses anything else, naming what odin models."""
    assert CONNECTION in EDGE_KINDS


# --- 6. through the PRODUCT'S OWN ROUTE ----------------------------------------


def test_apply_full_REFUSES_a_conflicting_connection_edge(tmp_path):
    """`connection_conflicts` returning a list proves a function; this proves the
    PRODUCT. `/apply-full` is where the list has to arrive, and the wiring
    rejection it feeds is assembled from `skeleton.wiring_errors` -- a field
    `agent/hcl.py` builds and this one is not in. A conflict that never reached
    the route would be a guard reading a signal nobody sends, which is the
    failure mode odin's honesty rule 1 exists for.

    Refusing is the honest verdict, not a harsh one: odin cannot tell which of
    the two answers the user meant, and applying would hand the workload one of
    them while the canvas showed both. Nothing is touched -- the refusal lands
    beside the uncovered-destroy and capacity guards, before tofu."""
    canvas = _canvas(
        [_edge("db-1", "ecs-1", CONNECTION)], env={"DATABASE_URL": "postgresql://elsewhere/db"},
    )
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path),
                     rds=FakeRds(), aws=FakeAws(), backings=False)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": "conn"}, json=canvas)
        _torn_down(client, "conn")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    (note,) = [n for n in body["wiring_errors"] if "DATABASE_URL" in n]
    assert "app-db" in note and "postgresql://elsewhere/db" in note
    # NOT `unsupported`/`not_covered`: those are the COVERAGE fields a CI gate
    # reads, and this is a user error on nodes odin builds perfectly well
    # (field test 5, F5-8).
    assert body.get("not_covered", []) == [], body.get("not_covered")


# --- the two /apply-full tests really boot containers -------------------------
#
# MEASURED 2026-07-29, after these leaked four containers into every unit run and
# cost a release-gate diagnosis: `create_app(runtime=FakeRuntime(), rds=FakeRds(),
# aws=FakeAws(), backings=False)` does NOT make `/apply-full` hermetic. It runs a
# real `tofu apply`, and the gateway's own models default their substrate --
# `lambdactl` to `FunctionRuntime(ColimaRuntime(), ...)`, and rdsctl/cachectl
# likewise -- so the injected fakes are bypassed and real Postgres, Redis and RIE
# containers start. Before this teardown: `conn2` left
# odin-rds-conn2-app-db, odin-rds-conn2-other-db, odin-lambda-conn2-worker and
# odin-cache-conn2-cache standing, on EVERY run of the unit suite.
#
# `_torn_down` is the narrow fix: destroy the env before the client closes. The
# wider one -- that a fake runtime does not actually isolate `/apply-full` -- is
# a real defect in the test seam rather than in these tests, and is recorded in
# ROADMAP rather than papered over here.


def _torn_down(client, env: str):
    """Destroy `env` on the way out, then reap what `/destroy` provably cannot.

    `/destroy` alone is not enough here, and the reason is the defect: creation
    is REAL and teardown is FAKE. The gateway's rdsctl bypassed the injected
    `FakeRds` and started a real Postgres through `ColimaRuntime`, while
    `/destroy` runs through the reconciler, which DOES honour `FakeRds` -- so it
    tears down a fake and the real container stands. Measured: `/destroy` cleared
    the lambda and cache containers and left `odin-rds-conn2-app-db` and
    `odin-rds-conn2-other-db` running.

    So the reap is scoped to this env's own container names, never a label or a
    machine-wide filter -- another agent's containers must not be reachable from
    here. It asserts the end state rather than trusting either step, because a
    teardown that quietly does nothing is exactly how this leaked for a week."""
    resp = client.post("/destroy", params={"env": env})
    assert resp.status_code == 200, resp.text
    subprocess.run(
        f"docker ps -aq --filter name=-{env}- --filter name=-{env}$ | xargs -r docker rm -f",
        shell=True, capture_output=True, check=False,
    )
    survivors = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name=-{env}-"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    assert survivors == [], f"{env} left {len(survivors)} containers standing"
    return resp.json()


def test_apply_full_ACCEPTS_the_ordinary_connection_edge(tmp_path):
    """The other half, and the one that catches an over-eager guard: a canvas
    whose only connection edge agrees with everything must sail through. A
    refusal here would block the feature's own happy path."""
    canvas = _canvas([_edge("db-1", "ecs-1", CONNECTION)])
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path),
                     rds=FakeRds(), aws=FakeAws(), backings=False)
    with TestClient(app) as client:
        resp = client.post("/apply-full", params={"env": "conn2"}, json=canvas)
        _torn_down(client, "conn2")

    assert resp.status_code != 409, resp.text
    assert resp.json().get("wiring_errors", []) == []
