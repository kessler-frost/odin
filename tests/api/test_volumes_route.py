"""`GET /volumes` — what disk odin is holding, and which of it nothing owns.

## Why a listing route at all

An rds instance's data lives on a named Docker volume that deliberately OUTLIVES
its container (`aws/rds.py`), so `docker rm -f -v` never takes one. Until v0.8.15
nothing reclaimed one either, and the only thing on the machine that could find a
stranded one was a hand-run `docker volume ls`. Four orphans from two long-dead
environments accumulated that way.

`odin env rm` now reclaims an env's volumes as part of its teardown, which closes
the leak going forward. This route is the other half of the same honesty
requirement: an unreclaimable volume must be **NAMED**, because a reclaim that
reports nothing is indistinguishable from one that had nothing to do.

## The contract asserted here

* attribution is MEASURED off the `odin.env` label, never parsed from the name —
  a name cannot tell `conn2` from `conn2-app`;
* the `reclaim` command on each row is the one that really works for THAT volume,
  and a live env's row carries none at all;
* a docker that will not answer produces `null` and a reason, never `[]`.

Nothing on this route deletes anything, which is what makes it safe to run on a
machine with a second odin whose environments this one has never heard of.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import CANVAS, FakeRds
from tests.api.test_env_rm import CleanRuntime, FakeAws

LIVE = "vols-live"
DEAD = "vols-dead"


class NoVolumeSurface:
    """A runtime that does not implement the volume half of `RuntimeDriver` at
    all -- `tests/api/test_env_rm.py`'s `container_names` case, one method over.
    An `AttributeError` is still "odin could not ask"."""

    async def container_names(self, env=None):
        return []


def _app(tmp_path, runtime):
    return create_app(
        runtime=runtime, store=SpecStore(tmp_path), rds=FakeRds(), aws=FakeAws(),
        reap_ec2_vms=False,
    )


def _rows(body: dict) -> dict[str, dict]:
    return {row["name"]: row for row in body["volumes"]}


def test_a_live_envs_volume_is_reported_in_use_with_no_command_to_run(tmp_path):
    """The volume holds a database somebody is using. The only thing that may
    remove it is a real DELETE through a real apply, so the row deliberately
    offers no command — printing `odin env rm <live env>` next to a healthy
    database would be an invitation to destroy it."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{LIVE}-app-db-data", LIVE)
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        client.post(f"/apply?env={LIVE}", json=CANVAS)
        body = client.get("/volumes").json()

    row = _rows(body)[f"odin-rds-{LIVE}-app-db-data"]
    assert row["env"] == LIVE
    assert row["live"] is True
    assert "reclaim" not in row
    assert body["orphans"] == []


def test_a_dead_envs_volume_is_an_orphan_with_the_scoped_command_that_reclaims_it(tmp_path):
    """The measured bug, as a user meets it. The env is gone from every place
    odin looks; the label is still on the volume, so odin can name the env and
    hand back the one command that really works.

    Mutation-test: make `_labelled_env` return None unconditionally and this
    fails — the row falls back to `docker volume rm`, which would send a user to
    a manual step when odin could have done it."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DEAD}-app-db-data", DEAD)
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        body = client.get("/volumes").json()

    row = _rows(body)[f"odin-rds-{DEAD}-app-db-data"]
    assert row["env"] == DEAD
    assert row["live"] is False
    assert row["reclaim"] == f"odin env rm {DEAD}"
    assert body["orphans"] == [f"odin-rds-{DEAD}-app-db-data"]


def test_the_command_it_offers_really_reclaims_the_volume(tmp_path):
    """The route says `odin env rm <env>`, so this RUNS it. A suggestion nobody
    followed through is exactly how a guard ends up reading a signal that never
    arrives — and the failure mode here is quiet: `env rm` would answer
    `not_found` and leave the volume where it was."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DEAD}-app-db-data", DEAD)
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        offered = _rows(client.get("/volumes").json())[f"odin-rds-{DEAD}-app-db-data"]["reclaim"]
        assert offered.startswith("odin env rm ")
        env = offered.removeprefix("odin env rm ")

        resp = client.post("/envs/rm", params={"env": env})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "removed", resp.text
        assert resp.json()["reclaimed"]["volumes"] == [f"odin-rds-{DEAD}-app-db-data"]

        after = client.get("/volumes").json()

    assert runtime.volumes == set()
    assert after["volumes"] == [] and after["orphans"] == []


def test_an_ambiguous_name_is_attributed_by_its_label_not_by_the_shorter_reading(tmp_path):
    """`odin-rds-conn2-app-db-data` reads as env `conn2` database `app-db` AND as
    env `conn2-app` database `db`. Parsing would pick one; the label knows. Here
    the truth is the LONGER reading, which a shortest-first guess gets wrong."""
    runtime = CleanRuntime()
    runtime.seed_volume("odin-rds-conn2-app-db-data", "conn2-app")
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        row = _rows(client.get("/volumes").json())["odin-rds-conn2-app-db-data"]

    assert row["env"] == "conn2-app"
    assert row["reclaim"] == "odin env rm conn2-app"


def test_a_volume_with_no_env_label_says_so_and_offers_the_manual_step(tmp_path):
    """A volume created before v0.8.15 carries no `odin.env`, so `odin env rm`
    cannot reach it — its label filter matches nothing. Offering that command
    anyway would print `removed`… having reclaimed nothing. `docker volume rm` is
    the honest answer, and this is the residual `docs/limits.md` states."""
    runtime = CleanRuntime()
    runtime.seed_volume("odin-rds-legacy-app-db-data", None)
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        body = client.get("/volumes").json()
        row = _rows(body)["odin-rds-legacy-app-db-data"]

        assert row["env"] is None
        assert row["live"] is False
        assert row["reclaim"] == "docker volume rm odin-rds-legacy-app-db-data"
        assert body["orphans"] == ["odin-rds-legacy-app-db-data"]

        # ...and the claim is TRUE: `odin env rm` really cannot take it.
        resp = client.post("/envs/rm", params={"env": "legacy"})
        assert resp.json()["status"] == "not_found"

    assert runtime.volumes == {"odin-rds-legacy-app-db-data"}


def test_live_and_orphaned_volumes_are_separated_in_one_listing(tmp_path):
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{LIVE}-db-data", LIVE)
    runtime.seed_volume(f"odin-rds-{DEAD}-db-data", DEAD)
    runtime.seed_volume("odin-rds-legacy-db-data", None)
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        client.post(f"/apply?env={LIVE}", json=CANVAS)
        body = client.get("/volumes").json()

    assert LIVE in body["envs"]
    # Rows are sorted by volume NAME, so `legacy` comes before `vols-*`.
    assert body["orphans"] == ["odin-rds-legacy-db-data", f"odin-rds-{DEAD}-db-data"]
    assert [(r["name"], r["live"]) for r in body["volumes"]] == [
        ("odin-rds-legacy-db-data", False),
        (f"odin-rds-{DEAD}-db-data", False),
        (f"odin-rds-{LIVE}-db-data", True),
    ]


def test_a_machine_odin_cannot_ask_answers_null_and_a_reason_not_an_empty_list(tmp_path):
    """Field test 6's F4. `{"volumes": []}` from a docker that would not answer
    reads as "odin is holding no disk" — the single most misleading thing this
    route could say, given it exists to answer that question.

    Mutation-test: return `{"volumes": [], "orphans": []}` from the `except` and
    this fails."""
    class BlindRuntime(CleanRuntime):
        async def volume_names(self, env=None):
            raise RuntimeError("Cannot connect to the Docker daemon")

    app = _app(tmp_path, BlindRuntime())
    with TestClient(app) as client:
        resp = client.get("/volumes")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["volumes"] is None
    assert body["orphans"] is None
    assert "could not list this machine's Docker volumes" in body["error"]
    assert "Cannot connect to the Docker daemon" in body["error"]


def test_a_machine_with_no_odin_volumes_says_so_without_an_error(tmp_path):
    """The other half of the pair above: genuinely empty is a real answer, and it
    must NOT carry the `error` a failed read does."""
    app = _app(tmp_path, CleanRuntime())
    with TestClient(app) as client:
        body = client.get("/volumes").json()

    assert body["volumes"] == []
    assert body["orphans"] == []
    assert "error" not in body


def test_the_route_never_removes_anything(tmp_path):
    """It is a READ. Asserted rather than assumed, because this route is the one
    place that computes "nothing owns this volume" — and that judgement is made
    against THIS server's store root only, so acting on it would delete a second
    odin's databases."""
    runtime = CleanRuntime()
    runtime.seed_volume(f"odin-rds-{DEAD}-db-data", DEAD)
    runtime.seed_volume("odin-rds-legacy-db-data", None)
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/volumes").json()["orphans"]

    assert runtime.volumes == {f"odin-rds-{DEAD}-db-data", "odin-rds-legacy-db-data"}


def test_it_mints_no_env_for_a_volume_it_cannot_attribute(tmp_path):
    """A read must never create an env as a side effect (`_loop_health`'s rule).

    The risk is specific here: `_labelled_env` puts GUESSED env names to docker,
    and `GET /envs`'s own env list is what liveness is judged against. Neither may
    turn a guess into a registered environment."""
    runtime = CleanRuntime()
    runtime.seed_volume("odin-rds-vols-guess-app-db-data", None)
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        before = set(app.state.reconcilers)
        assert client.get("/volumes").json()["orphans"]

        assert set(app.state.reconcilers) == before
        assert "vols-guess" not in client.get("/envs").json()["envs"]
    assert not (tmp_path / "vols-guess").exists()
    assert not (tmp_path / "vols-guess-app").exists()


def test_a_runtime_without_the_volume_surface_is_reported_not_crashed(tmp_path):
    """A runtime with no `volume_names` at all — the same case
    `tests/api/test_env_rm.py` uses for containers. An AttributeError is still
    "odin could not ask", and it must not reach the client as a 500 with a
    non-JSON body (the last line of defence in `server.py`)."""
    app = _app(tmp_path, NoVolumeSurface())
    with TestClient(app) as client:
        body = client.get("/volumes").json()

    assert body["volumes"] is None
    assert "could not list this machine's Docker volumes" in body["error"]
