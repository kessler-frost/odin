"""Field test 5 (HIGHEST, silent data loss): an apply may not DESTROY a
resource because one character of its node changed.

Two field-proven reproductions, both with real data:

* `count: "2"` -> `"two"` on a live ECS node destroyed the service and both
  task containers and removed `aws_ecs_service` from tofu's state.
* `type: "s3"` -> `"s3 "` (a trailing space) destroyed a real bucket, the
  object written into it through the gateway, and the rustfs backing.

Both answered `status: applied`, `tf: ok`, exit 0, in under four seconds. The
only signal was a line in `not_covered`, a field the README describes as "a
node odin didn't act on".

The distinction these tests pin down, because getting it wrong breaks
something that works:

* a node REMOVED from the canvas must still be destroyed -- "empty canvas =
  full destroy" is odin's documented teardown story;
* a node still DRAWN that merely became uncovered, and that really exists, is
  the bug -- refuse, name it, exit nonzero;
* a node uncovered that was never successfully applied has nothing to lose --
  today's behavior (skip it, report it in `not_covered`) stays.

Unit-level (fakes throughout); the real-substrate proof -- a real bucket with
a real object in it, surviving a typo'd apply -- is
tests/simulate/test_uncovered_destroy_guard_e2e.py.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from odin.agent.hcl import generate_tf
from odin.agent.translate import TranslateResult
from odin.server import _covered_nodes, create_app
from odin.simulate.runner import TfResult
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack
from tests.api.test_apply_full import FakeAws, FakeRds, FakeRuntime, _patch_translate

ENV = "default"

S3 = {"nodes": [{"id": "n1", "type": "s3", "data": {"label": "uploads"}}], "edges": []}
S3_TYPO = {"nodes": [{"id": "n1", "type": "s3 ", "data": {"label": "uploads"}}], "edges": []}
EMPTY = {"nodes": [], "edges": []}

ECS = {
    "nodes": [
        {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
        {"id": "s", "type": "subnet", "data": {"label": "sub", "vpc": "net", "cidr": "10.0.1.0/24"}},
        {"id": "n", "type": "ecs", "data": {"label": "api", "image": "nginx",
                                            "count": "2", "vpc": "net", "subnet": "sub"}},
    ],
    "edges": [],
}


def _ecs_canvas(count: str) -> dict:
    canvas = json.loads(json.dumps(ECS))
    canvas["nodes"][2]["data"]["count"] = count
    return canvas


def _app(tmp_path):
    return create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path),
                      rds=FakeRds(), aws=FakeAws(), backings=False)


def _really_apply(client, canvas: dict = S3) -> list[str]:
    """The FIRST existence witness, produced the way the field test produced
    it: a real apply through the real route, after which the reconciler has
    really observed the resource into World. Seeding `world.json` by hand does
    not work and should not -- the env's background reconciler prunes an
    observed resource nothing desires, which is exactly the behavior that makes
    World a witness worth reading."""
    assert client.post("/apply", json=canvas).status_code == 200
    observed = [r["id"] for r in client.get("/world").json()["resources"]]
    assert observed, "the seed apply put nothing in World"
    return observed


def _in_tf_state(tmp_path, *resources: tuple[str, str], env: str = ENV) -> None:
    """The SECOND existence witness: tofu's own state, carrying the same
    `odin:node` tag hcl.py stamps on every canvas-node-backed block."""
    workspace = tmp_path / env / "tf"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "terraform.tfstate").write_text(json.dumps({
        "version": 4,
        "resources": [
            {"mode": "managed", "type": tf_type, "name": node,
             "instances": [{"attributes": {"tags": {"odin:node": node}}}]}
            for node, tf_type in resources
        ],
    }))


# --- the signal the guard reads, probed against the REAL generator ----------


def test_generate_tf_really_tags_every_node_backed_block(tmp_path):
    """Honesty rule 1: the guard keys on `odin:node`, so this asserts the real
    `generate_tf` actually emits it -- if hcl.py ever stopped stamping the tag,
    or renamed it, this fails here rather than the guard silently never firing.
    The ECS canvas is deliberate: it also emits an `aws_ecs_cluster` and an
    `aws_ecs_task_definition`, which are COMPANIONS, not canvas nodes, and must
    not appear."""
    project = generate_tf(canvas_to_stack(ECS, env=ENV))
    assert _covered_nodes(project.files) == {"net", "sub", "api"}


def test_a_declined_resource_drops_out_of_the_covered_set(tmp_path):
    """...and the other half of the same signal: `count: "two"` is a perfectly
    well-SHAPED canvas (v0.7.4's structural validation passes it) whose ECS
    service simply never gets built. The task definition still is, which is
    exactly why coverage is read off the `odin:node` tag rather than off the
    presence of any block at all."""
    project = generate_tf(canvas_to_stack(_ecs_canvas("two"), env=ENV))
    assert _covered_nodes(project.files) == {"net", "sub"}
    assert project.unsupported == ["api (ecs): count must be a whole number (e.g. 2)"]


# --- /apply-full: the route the UI and `odin apply` use ---------------------


def test_a_typoed_type_over_a_live_resource_refuses_the_apply(tmp_path, monkeypatch):
    """The `type: "s3 "` reproduction. The node is still drawn, the bucket
    really exists, and this apply covers nothing -- so applying would delete
    it."""
    calls = _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = _app(tmp_path)
    with TestClient(app) as client:
        _really_apply(client)
        applied = app.state.store.get_stack(ENV)
        resp = client.post("/apply-full", json=S3_TYPO)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["would_destroy"] == [
        {"node": "uploads", "requested_as": "s3 ", "kind": None,
         "reason": "its type 's3 ' is not a kind odin models (a typo?) -- it is not in the desired state at all"},
    ]
    # The trailing space is only visible with the quotes on -- that IS the bug.
    assert "'s3 '" in body["error"]
    assert "uploads" in body["error"] and "DESTROY" in body["error"]
    assert calls == []  # refused before translate, before tofu, before anything
    assert app.state.store.get_stack(ENV) == applied  # and the desired state never moved


def test_a_declining_field_value_over_a_live_resource_refuses_the_apply(tmp_path, monkeypatch):
    """The `count: "two"` reproduction, and the boundary v0.7.4's canvas
    validation cannot cover: the canvas is structurally perfect, so validation
    passes it; the failure is semantic and it deletes a running service."""
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    _in_tf_state(tmp_path, ("api", "aws_ecs_service"), ("net", "aws_vpc"), ("sub", "aws_subnet"))
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=_ecs_canvas("two"))

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert [item["node"] for item in body["would_destroy"]] == ["api"]
    # hcl.py's OWN reason, reproduced verbatim -- the user is told what to fix.
    assert body["would_destroy"][0]["reason"] == "count must be a whole number (e.g. 2)"
    assert body["would_destroy"][0]["kind"] == "ecs"


def test_removing_the_node_from_the_canvas_still_destroys_it(tmp_path, monkeypatch):
    """The other side of the line, and the one that must not break: a node the
    user DELETED is a node the user asked to have destroyed."""
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = _app(tmp_path)
    with TestClient(app) as client:
        _really_apply(client)
        resp = client.post("/apply-full", json={"nodes": [], "edges": []})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"


def test_an_empty_canvas_is_still_a_full_teardown(tmp_path, monkeypatch):
    """"empty canvas = full destroy", with BOTH existence witnesses loaded --
    the shape most at risk from a guard that over-reaches, since an empty
    canvas covers nothing at all."""
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    _in_tf_state(tmp_path, ("api", "aws_ecs_service"))
    app = _app(tmp_path)

    async def _tf_ok(*args, **kwargs):  # the workspace above makes tofu run for real otherwise
        return TfResult(ok=True, exit_code=0)

    app.state.tf_runner.apply = _tf_ok
    with TestClient(app) as client:
        _really_apply(client)
        resp = client.post("/apply-full", json=EMPTY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    assert app.state.store.get_stack(ENV).resources == ()


def test_an_uncovered_node_that_never_existed_is_still_only_skipped(tmp_path, monkeypatch):
    """Nothing to lose, so nothing to refuse: today's behavior stays exactly as
    it was. This is also what stops the guard from wedging a canvas that has a
    permanently-unsupported node drawn on it -- `/apply-full` commits a Stack
    whenever TOFU succeeds, and an unbuildable resource does not fail an apply,
    so the last-applied Stack would name a node that has never existed for a
    single second."""
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=S3_TYPO)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied"
    assert body["not_covered"] == ["s3 "]


def test_a_second_apply_of_a_permanently_unsupported_node_is_not_refused(tmp_path, monkeypatch):
    """The same guarantee stated as the regression it prevents: apply a canvas
    whose ECS node has never been buildable, twice. The second apply must not
    start refusing just because the first one committed a Stack naming it."""
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/apply-full", json=_ecs_canvas("two")).status_code == 200
        assert "api" in [r.id for r in app.state.store.get_stack(ENV).resources]
        resp = client.post("/apply-full", json=_ecs_canvas("two"))
    assert resp.status_code == 200, resp.text


def test_the_override_applies_anyway_and_is_named_in_the_refusal(tmp_path, monkeypatch):
    """For the operator who genuinely means it. Nothing in the UI or the CLI
    sets it, so it cannot be reached by accident -- the refusal itself is the
    only place it is documented."""
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = _app(tmp_path)
    with TestClient(app) as client:
        _really_apply(client)
        refused = client.post("/apply-full", json=S3_TYPO)
        allowed = client.post("/apply-full", params={"allow_destroying_uncovered": "true"}, json=S3_TYPO)

    assert "allow_destroying_uncovered=true" in refused.json()["error"]
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "applied"


def test_a_refused_apply_does_not_supersede_an_in_flight_one(tmp_path, monkeypatch):
    """A canvas whose every node is uncovered produces an EMPTY Stack, which is
    the shape `_bump_epoch` treats as a teardown. The refusal changes nothing,
    so it must not invalidate somebody else's in-flight apply on its way out --
    which is why the guard runs ahead of the epoch bump."""
    _patch_translate(monkeypatch, TranslateResult(files={}, refined=False))
    app = _app(tmp_path)
    with TestClient(app) as client:
        _really_apply(client)
        before = dict(app.state.env_epoch)
        assert client.post("/apply-full", json=S3_TYPO).status_code == 409
    assert app.state.env_epoch == before


# --- the same refusal on the two sibling apply surfaces --------------------


def test_apply_refuses_too(tmp_path):
    """`/apply` never runs tofu, but it commits the desired state and its
    trailing tick is what gc'd the rustfs backing -- and the object inside it --
    in the field test. Same refusal, same reason."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        _really_apply(client)
        applied = app.state.store.get_stack(ENV)
        resp = client.post("/apply", json=S3_TYPO)
    assert resp.status_code == 409, resp.text
    assert "uploads" in resp.json()["error"]
    assert app.state.store.get_stack(ENV) == applied


def test_apply_still_applies_a_covered_canvas(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _really_apply(client)
        resp = client.post("/apply", json=S3)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"


def test_tf_apply_refuses_too(tmp_path):
    """`/tf/apply` has no canvas -- it applies the STORED Stack, so the Stack
    itself is what the user is still asking for. A resource in it that builds no
    Terraform is one `tofu apply` deletes out of its own state."""
    store = SpecStore(tmp_path)
    store.apply(canvas_to_stack(_ecs_canvas("two"), env=ENV))
    _in_tf_state(tmp_path, ("api", "aws_ecs_service"))
    app = create_app(runtime=FakeRuntime(), store=store, rds=FakeRds(), aws=FakeAws(), backings=False)
    with TestClient(app) as client:
        resp = client.post("/tf/apply")
    assert resp.status_code == 409, resp.text
    assert [item["node"] for item in resp.json()["would_destroy"]] == ["api"]
