"""W2.9 / M8 -- `POST /agent/debug`: the wiring, the honesty guarantees, and
the CSRF posture. The agent itself is faked here (the real pass has its own
tests in tests/agent/test_debugger.py); what's under test is that the route
assembles REAL state -- desired Stack, observed World, the env's event log, and
logs through the wave-1 `/logs` resolver -- and never fails for agent reasons.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odin.agent import debugger
from odin.api.debug import ScrubSetUnreadable, build_context, create_debug_router, issued_credentials
from odin.api.ws import ConnectionManager
from odin.gateway.keys import KeyStore
from odin.gateway.stores import SynthStores
from odin.runtime.colima import ContainerFacts, HostFacts
from odin.server import _unhandled_failure, create_app
from odin.spec.models import FieldValue, ResourceDesired, Stack, WorldDelta
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime

ENV = "dbg"
CANVAS = {"nodes": [{"type": "rds", "data": {"label": "db", "password": "hunter2-secret-value"}}], "edges": []}


class LoggingRuntime(FakeRuntime):
    """A FakeRuntime that also answers the two calls `api/logs.py::fetch_logs`
    makes -- so the route's log resolution is exercised for real, not stubbed
    out at the assembler's callable."""

    def status(self, name):
        return "exited"

    def logs(self, name, tail=100):
        return f"==> {name}\nFATAL: config missing\n"

    def facts(self, name, container_port=0):
        return ContainerFacts(phase="crashed")

    def exit_code(self, name):
        return 1

    def ensure_host(self):
        return HostFacts()


def _fake_diagnose(seen: list[dict], answer: str = "the db never started", suspects=None):
    async def diagnose(context: dict, question: str) -> dict:
        seen.append({"context": context, "question": question})
        return {"answer": answer, "suspects": suspects if suspects is not None else [
            {"node_id": "db", "reason": "exited with FATAL: config missing"},
        ]}

    return diagnose


DB = ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")})


@pytest.fixture
def wired(tmp_path):
    """The router alone, over real store/stores/ws state -- no lifespan, no
    reconciler, no gateway. `db` is in the applied stack: a node the route has
    never heard of is a different case, tested on its own below."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV, resources=(DB,)))
    seen: list[dict] = []
    app = FastAPI()
    app.include_router(create_debug_router(
        store, SynthStores(tmp_path), LoggingRuntime(), ConnectionManager(tmp_path),
        diagnose=_fake_diagnose(seen),
    ))
    with TestClient(app) as client:
        yield client, store, seen


def test_the_route_answers_with_the_diagnosis_and_suspects(wired):
    client, _store, _seen = wired
    resp = client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"], "question": "what's wrong here?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "env": ENV, "answer": "the db never started",
        "suspects": [{"node_id": "db", "reason": "exited with FATAL: config missing"}],
    }


def test_the_question_and_the_assembled_context_reach_the_agent(wired):
    client, store, seen = wired
    store.apply(Stack(env=ENV, resources=(
        ResourceDesired(id="db", kind="rds", fields={"engine": FieldValue(value="postgres")}),
    )))
    client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"], "question": "why is db red?"})
    assert seen[-1]["question"] == "why is db red?"
    node = seen[-1]["context"]["nodes"]["db"]
    assert node["desired"]["kind"] == "rds"
    # The wave-1 /logs resolver really ran: an EXITED container's last logs are
    # exactly the diagnostic a crash needs.
    assert "FATAL: config missing" in node["logs"]


def test_the_observed_world_and_the_event_log_reach_the_agent(tmp_path):
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV))
    store.apply_delta(WorldDelta(env=ENV, resource_id="db", kind="rds", phase="crashed", verdict="exit 1"))
    ws = ConnectionManager(tmp_path)
    seen: list[dict] = []
    app = FastAPI()
    app.include_router(create_debug_router(store, SynthStores(tmp_path), LoggingRuntime(), ws, diagnose=_fake_diagnose(seen)))
    with TestClient(app) as client:
        # A real broadcast is what writes the durable per-env event log.
        client.portal.call(ws.broadcast, {"type": "log", "env": ENV, "source": "db", "text": "boom"})
        client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})
    node = seen[-1]["context"]["nodes"]["db"]
    assert node["observed"] == {"phase": "crashed", "facts": {}, "verdict": "exit 1", "restarts": 0}
    assert [e["text"] for e in node["events"]] == ["boom"]


def test_a_failed_tofu_apply_reaches_the_agent_env_wide(tmp_path):
    """The gap `resource_id`-keyed events can't close: a `{type:"tf"}` event
    belongs to the ENV, so "your apply failed with this error" reached no node.
    Driven through a REAL `broadcast` -> `events.jsonl` -> `get_events`
    round-trip, which is the only path the route ever reads."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV, resources=(DB,)))
    ws = ConnectionManager(tmp_path)
    seen: list[dict] = []
    app = FastAPI()
    app.include_router(create_debug_router(store, SynthStores(tmp_path), LoggingRuntime(), ws, diagnose=_fake_diagnose(seen)))
    error = "Error: creating S3 Bucket (assets): BucketAlreadyOwnedByYou"
    with TestClient(app) as client:
        client.portal.call(ws.broadcast, {"type": "tf", "env": ENV, "phase": "apply", "line": error})
        client.portal.call(ws.broadcast, {
            "type": "tf", "env": ENV, "phase": "apply", "status": "failed", "exit_code": 1, "tail": [error],
        })
        client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})
    assert seen[-1]["context"]["recent_tf"] == [f"apply: {error}", "tofu apply failed (exit 1)"]


def test_a_secret_in_a_tofu_line_never_reaches_the_agent(app_client, monkeypatch):
    """`simulate/runner.py` already scrubs tofu's stream before it reaches
    events.jsonl -- this pins the assembler's own second pass, which is what
    covers a line that was logged with a different (or empty) secret set."""
    seen: list[dict] = []
    monkeypatch.setattr(debugger, "diagnose", _fake_diagnose(seen))
    app_client.post("/apply", params={"env": ENV}, json=CANVAS)
    ws = app_client.app.state.ws_manager  # the same durable log the context is built from
    app_client.portal.call(ws.broadcast, {
        "type": "tf", "env": ENV, "phase": "apply", "line": '  + password = "hunter2-secret-value"',
    })
    app_client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})
    context = seen[-1]["context"]
    assert context["recent_tf"], "the tf line must have reached the context for this to mean anything"
    assert "hunter2-secret-value" not in json.dumps(context)


def test_a_node_id_odin_has_no_record_of_is_named_and_costs_no_model_call(wired):
    """Field test 2 finding #8: canvas node ids (`d1`) instead of labels bought
    52 seconds, a real model call and a confident four-paragraph analysis of
    nodes that do not exist -- with three of them listed as suspects."""
    client, _store, seen = wired
    resp = client.post("/agent/debug", json={"env": ENV, "node_ids": ["d1", "e1"]})

    assert resp.status_code == 200
    body = resp.json()
    assert seen == [], "no evidence means no model call"
    assert "'d1'" in body["answer"] and "'e1'" in body["answer"]
    assert "no such node" in body["answer"]
    assert "db" in body["answer"]  # …and it names the labels that DO exist
    assert body["suspects"] == []


def test_a_selected_node_that_exists_but_has_no_observations_is_still_diagnosed(tmp_path):
    """The deliberate `desired: None` case must survive: the canvas can select a
    stale tile whose node was removed from the stack but is still observed, and
    its last state is exactly what explains where it went."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV))  # no desired resources at all
    store.apply_delta(WorldDelta(env=ENV, resource_id="gone", kind="ecs", phase="crashed", verdict="removed"))
    seen: list[dict] = []
    app = FastAPI()
    app.include_router(create_debug_router(
        store, SynthStores(tmp_path), LoggingRuntime(), ConnectionManager(tmp_path),
        diagnose=_fake_diagnose(seen),
    ))
    with TestClient(app) as client:
        resp = client.post("/agent/debug", json={"env": ENV, "node_ids": ["gone"]})

    assert resp.status_code == 200
    assert seen, "an observed-but-undesired node has real evidence: the agent must run"
    assert seen[-1]["context"]["nodes"]["gone"]["desired"] is None
    assert seen[-1]["context"]["unknown_nodes"] == []


def test_a_mixed_request_still_runs_but_the_model_is_told_which_ids_are_unknown(wired):
    client, _store, seen = wired
    resp = client.post("/agent/debug", json={"env": ENV, "node_ids": ["db", "d1"]})
    assert resp.status_code == 200
    assert seen, "one id has real evidence, so the diagnosis is worth making"
    assert seen[-1]["context"]["unknown_nodes"] == ["d1"]


def test_an_empty_question_falls_back_to_the_canned_one(wired):
    client, _store, seen = wired
    client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"], "question": ""})
    assert seen[-1]["question"] == "what's wrong here?"


def test_extra_keys_from_the_agent_cannot_500_the_route(tmp_path):
    seen: list[dict] = []
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV, resources=(DB,)))
    app = FastAPI()
    app.include_router(create_debug_router(
        store, SynthStores(tmp_path), LoggingRuntime(), ConnectionManager(tmp_path),
        diagnose=_fake_diagnose(seen, suspects=[{"node_id": "db", "reason": "r", "confidence": "high"}]),
    ))
    with TestClient(app) as client:
        resp = client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})
    assert resp.status_code == 200
    assert resp.json()["suspects"] == [{"node_id": "db", "reason": "r"}]


# --- through the real app: wiring, the real diagnose, and CSRF ---------------


@pytest.fixture
def app_client(tmp_path):
    app = create_app(runtime=LoggingRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app) as client:
        yield client


def test_the_route_is_wired_into_the_real_app_and_never_500s_when_the_agent_is_off(app_client, monkeypatch):
    # ODIN_DEBUG_AGENT=0 exercises the REAL `debugger.diagnose` (no SDK
    # spawned, no monkeypatched seam) and proves the route's contract: an
    # unavailable agent is still an honest 200.
    monkeypatch.setenv("ODIN_DEBUG_AGENT", "0")
    app_client.post("/apply", params={"env": ENV}, json=CANVAS)
    resp = app_client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["env"] == ENV and body["suspects"] == [] and "off" in body["answer"]


def test_odin_ai_0_answers_honestly_through_the_real_route_without_a_model_call(app_client, monkeypatch):
    """The one switch for every model call, at the route. Same real
    `debugger.diagnose`, same honest 200 -- the answer just names `ODIN_AI`,
    the switch that actually stopped it, rather than this feature's own flag."""
    monkeypatch.setenv("ODIN_AI", "0")
    monkeypatch.setenv("ODIN_DEBUG_AGENT", "1")  # the feature is opted IN and still makes no call
    app_client.post("/apply", params={"env": ENV}, json=CANVAS)
    resp = app_client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["suspects"] == [] and "ODIN_AI" in body["answer"]


def test_a_cross_origin_post_is_rejected_like_every_other_state_changing_route(app_client):
    # Deliberate (see api/debug.py's own note): the route mutates nothing, but
    # it spends a model call and hands back the env's config/logs/verdicts, so
    # a page odin didn't serve must not be able to trigger it.
    resp = app_client.post(
        "/agent/debug", json={"env": ENV, "node_ids": ["db"]}, headers={"origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_a_secret_typed_on_the_canvas_never_reaches_the_agent(app_client, monkeypatch):
    """The leak test for the ROUTE path: the canvas's own `password` field
    (auto-flagged sensitive by `is_sensitive_field_name`) and its value must be
    absent from the context the agent is handed, however it got there. Patched
    at `debugger.diagnose` -- the module attribute the route resolves per
    request -- so this runs through the app's OWN router, no second one."""
    seen: list[dict] = []
    monkeypatch.setattr(debugger, "diagnose", _fake_diagnose(seen))
    app_client.post("/apply", params={"env": ENV}, json=CANVAS)
    app_client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})
    assert "hunter2-secret-value" not in json.dumps(seen[-1]["context"])
    assert seen[-1]["context"]["nodes"]["db"]["desired"]["fields"]["password"]["value"] == "[REDACTED]"


def test_a_gateway_issued_credential_never_reaches_the_agent(tmp_path):
    """Field test 2 finding #6, end to end through the route's own assembly: a
    REAL key pair minted by the REAL KeyStore, planted in the crash verdict the
    way a failed `docker run` used to put it there, must be redacted -- by name,
    not by the 200-char clip that was covering it with 35 characters to spare."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV))
    access_key, secret_key = KeyStore(store.root).issue(ENV, "web-svc")
    assert len(secret_key) == 40  # a real issued pair, not a stand-in
    store.apply_delta(WorldDelta(
        env=ENV, resource_id="web-svc", kind="ecs", phase="crashed",
        verdict=f"docker run odin-ecs-{ENV}-web-svc failed: -e AWS_SECRET_ACCESS_KEY={secret_key}",
        facts={"logtail": f"AWS_ACCESS_KEY_ID={access_key}"},
    ))

    context = build_context(
        store, SynthStores(tmp_path), LoggingRuntime(), ConnectionManager(tmp_path), ENV, ["web-svc"],
    )

    dumped = json.dumps(context)
    assert secret_key not in dumped and access_key not in dumped
    assert "docker run odin-ecs-dbg-web-svc failed" in dumped  # the diagnostic itself survives


def test_the_issued_credential_scrub_set_is_empty_for_an_env_that_issued_nothing(tmp_path):
    assert issued_credentials(SpecStore(tmp_path).root, ENV) == frozenset()


# --- the scrub set is the ONE thing here that may not degrade quietly --------
#
# `issued_credentials`' docstring said it was "best-effort BY DESIGN"; the code
# could raise five different ways, and in a sixth it returned a WRONG answer in
# silence. Probed against the real file, all eight cases -- see that docstring.
# An empty scrub set is the input that lets odin's own credentials into a model
# prompt, so "there are none" and "I could not tell" must not be the same value.


@pytest.mark.parametrize("label,content", [
    ("truncated write", '{"db": ["AKIAodin1234", "supersecr'),
    ("empty file", ""),
    ("a list, not an object", "[]"),
    ("a null value", '{"db": null}'),
    # THE dangerous one. This raised nothing: the comprehension iterated the
    # STRING, so the scrub set became {'A','K','I','4','o','d','i','n','1','2','3'}
    # -- every one of those letters then redacted out of the whole prompt.
    ("values are strings, not pairs", '{"db": "AKIAodin1234"}'),
])
def test_an_unreadable_scrub_set_is_never_silently_an_empty_one(tmp_path, label, content):
    keys = tmp_path / ENV / "keys.json"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(content)
    with pytest.raises(ScrubSetUnreadable) as caught:
        issued_credentials(tmp_path, ENV)
    assert str(keys) in str(caught.value), f"{label}: the failure must name the file"


def test_an_unreadable_scrub_set_stops_the_model_call_entirely(tmp_path):
    """The point of failing at all: no prompt is built, so nothing can leak
    into one. A route that answered 200 with a diagnosis here would have sent
    real access/secret pairs to a model unscrubbed."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV, resources=(DB,)))
    keys = tmp_path / ENV / "keys.json"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text('{"db": "AKIAodin1234"}')

    called: list[dict] = []

    async def diagnose(context, question):
        called.append(context)
        return {"answer": "should never happen", "suspects": []}

    app = FastAPI()
    app.add_exception_handler(Exception, _unhandled_failure)
    app.include_router(create_debug_router(
        store, SynthStores(tmp_path), LoggingRuntime(), ConnectionManager(tmp_path), diagnose=diagnose,
    ))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/agent/debug", json={"env": ENV, "node_ids": ["db"]})

    assert called == [], "the model must not be called with an unknown scrub set"
    assert resp.status_code == 500, "documented: a file odin wrote and cannot parse is a named failure"
    body = resp.json()
    assert str(keys) in body["error"]
    assert body["store"]["path"] == str(keys.absolute()), "the file rides structurally, not scraped from prose"


def test_a_well_formed_scrub_set_still_reaches_the_prompt(tmp_path):
    """The guard must not have broken the thing it protects: a real issued pair
    is still collected and still scrubbed (field test 2 finding #6)."""
    keystore = KeyStore(tmp_path)
    access, secret = keystore.issue(ENV, "db")
    assert issued_credentials(tmp_path, ENV) == frozenset({access, secret})


def test_build_context_is_usable_without_the_route(tmp_path):
    """The seam the integration test leans on: the real assembled context, no
    HTTP and no SDK involved."""
    store = SpecStore(tmp_path)
    store.apply(Stack(env=ENV))
    context = build_context(store, SynthStores(tmp_path), LoggingRuntime(), ConnectionManager(tmp_path), ENV, ["db"])
    assert context["env"] == ENV and "db" in context["nodes"]
