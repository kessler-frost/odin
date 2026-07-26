"""Field test 5: the false-green window, reproduced at the cadence a user gets.

THE BUG. v0.7.4's post-apply lambda/rds verification is real, but it read a
RECORD that only the drift sweep refreshes, and the sweep runs every ~10 ticks.
Measured in the field at the default cadence: `docker rm -f` the RIE container,
then apply in a tight loop -> FOUR consecutive `applied` / exit-0 applies over
~8s with zero containers, none of which recreated the function; and `/world`
green for the same window. Same for rds under `docker kill` (three green applies
AND three green `/world` readings), and under `docker pause` the record stayed
`available` while a host probe of the published port timed out.

WHY THE ORIGINAL TESTS MISSED IT (.claude/CLAUDE.md honesty rule 1b, which was
written about exactly this): `test_lambda_noop_apply_outage_e2e.py` and
`test_rds_noop_apply_outage_e2e.py` both set `ODIN_DRIFT_SWEEP_TICKS=1` and then
WAIT for the record to flip before the failing apply -- measuring the guard only
after the signal it depends on has provably arrived, and stepping around the
entire residual.

SO THIS FILE SETS NO CADENCE KNOB AT ALL. `ODIN_DRIFT_SWEEP_TICKS` is left at
its default 10, nothing waits for a sweep, and the very first apply after the
container dies is the one under test -- the field's exact loop:

  1. a healthy resource                    -> applied, exit 0, and PROMPT
  2. kill/remove/pause the container, then
     apply IMMEDIATELY                     -> FAILS, naming the resource, and
                                              `/world` is not green for it
  3. apply again                           -> applied, exit 0, container back

Step 3 is as load-bearing as step 2: the fix must not trade a false green for a
stuck red, and "re-Apply to recreate" has to be true. Note the ORDER -- odin
reports the death before it recreates anything, because recreating an rds
container destroys its data and no operator should learn about that from a green
apply (see reconcile/drift.py's "WHY THE PROJECTION MAY NOT WRITE").

Step 1 also covers "a still-starting resource must not fail an apply": the fresh
apply that opens each test is exactly that case -- the record is `creating` /
`Pending` and the container does not exist yet for part of it, and the apply
succeeds.

Same substrate constraints as the other simulate e2es: the store root lives
under `$HOME` (Colima only mounts that tree), and container hygiene is by EXACT
name.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.aws.rds import container_name as db_container_name
from odin.compute.functions import container_name as function_container_name
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

LAMBDA_ENV = "false-green-lambda-e2e"
RDS_ENV = "false-green-rds-e2e"
FN = "worker"
DB = "orders-db"
DB_NAME = "orders"
USER = "svc"
PASSWORD = "false-green-pw-3z9"  # noqa: S105 -- a throwaway test credential
CODE = "def lambda_handler(event, context):\n    return event\n"

EMPTY_CANVAS: dict = {"nodes": [], "edges": []}
LAMBDA_CANVAS = {
    "nodes": [{"id": "n1", "type": "lambda", "data": {
        "label": FN, "runtime": "python3.12", "code": CODE,
    }}],
    "edges": [],
}
RDS_CANVAS = {
    "nodes": [{"id": "db", "type": "rds", "data": {
        "label": DB, "engine": "postgres", "instanceClass": "db.t3.micro",
        "allocatedStorage": "20", "dbName": DB_NAME, "username": USER, "password": PASSWORD,
    }}],
    "edges": [],
}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def _state(name: str) -> str:
    out = _docker("ps", "-a", "--format", "{{.State}}", "--filter", f"name=^{name}$").stdout.strip()
    return out or "absent"


def _record(root: Path, env: str, store: str, key: str) -> dict:
    path = root / env / "gateway" / f"{store}.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return state.get(key, {})


@pytest.fixture
def store_root(request):
    """Under the repo checkout, never pytest's `tmp_path` -- Colima mounts
    `$HOME` only, and a Lambda's /var/task is a real bind mount."""
    root = Path(__file__).resolve().parents[2] / f".odin-false-green-{request.node.name[:24]}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def containers():
    """Absolute hygiene, by EXACT name -- never a blanket `label=odin=1` sweep,
    which would remove containers this test did not create."""
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


@pytest.fixture
def offline_translate(monkeypatch):
    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported,
            binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)


def _apply(client, env: str, canvas: dict) -> tuple[dict, float]:
    started = time.monotonic()
    resp = client.post("/apply-full", params={"env": env}, json=canvas)
    assert resp.status_code == 200, resp.text
    return resp.json(), time.monotonic() - started


def _world(client, env: str, label: str) -> dict:
    resp = client.get("/world", params={"env": env})
    assert resp.status_code == 200, resp.text
    return next((r for r in resp.json()["resources"] if r["id"] == label), {})


def _assert_default_cadence(monkeypatch) -> None:
    """The whole point of this file. Rule 1b: a guard that depends on a signal
    produced on a cadence must be measured at the cadence the user gets."""
    monkeypatch.delenv("ODIN_DRIFT_SWEEP_TICKS", raising=False)


def test_a_lambda_whose_container_was_removed_fails_the_very_next_apply(
    store_root, containers, offline_translate, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    _assert_default_cadence(monkeypatch)
    rie = function_container_name(LAMBDA_ENV, FN)
    containers.append(rie)

    with TestClient(create_app(store=SpecStore(store_root))) as client:
        # --- 1. a genuinely healthy function --------------------------------
        body, elapsed = _apply(client, LAMBDA_ENV, LAMBDA_CANVAS)
        print(f"\n[FT5-lambda] fresh apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert _state(rie) == "running"

        body, healthy_noop = _apply(client, LAMBDA_ENV, LAMBDA_CANVAS)
        print(f"[FT5-lambda] healthy no-op apply took {healthy_noop:.1f}s -> {body['status']}")
        assert body["status"] == "applied", body
        assert "unhealthy_resources" not in body, body
        assert healthy_noop < 30, f"the happy path must stay prompt, took {healthy_noop:.1f}s"

        # --- 2. THE BUG: remove the container and apply IMMEDIATELY ---------
        # No sweep is waited for, no cadence is shortened. In the field this
        # loop returned `applied` / exit 0 four times over ~8 seconds.
        _docker("rm", "-f", "-v", rie)
        assert _state(rie) == "absent"

        body, elapsed = _apply(client, LAMBDA_ENV, LAMBDA_CANVAS)
        print(f"[FT5-lambda] FIRST apply after the removal took {elapsed:.1f}s -> {body['status']}")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body  # tofu had nothing to do
        assert body["status"] == "applied_resources_unhealthy", body
        (fault,) = body["unhealthy_resources"]
        assert (fault["kind"], fault["node"], fault["observed"]) == ("lambda", FN, "Failed"), fault
        assert "removed outside odin" in fault["reason"], fault
        assert FN in body["note"], body["note"]
        # ...and `/world` is not green for it either, in the same breath.
        observed = _world(client, LAMBDA_ENV, FN)
        assert observed.get("phase") == "crashed", observed
        assert "removed outside odin" in (observed.get("verdict") or ""), observed

        # --- 3. ...and the next apply really does recreate it ---------------
        body, elapsed = _apply(client, LAMBDA_ENV, LAMBDA_CANVAS)
        print(f"[FT5-lambda] recovery apply took {elapsed:.1f}s -> {body['status']}")
        assert body["status"] == "applied", body
        assert "unhealthy_resources" not in body, body
        assert _state(rie) == "running", "the recovery apply must actually bring the container back"
        assert _record(store_root, LAMBDA_ENV, "lambdactl", f"fn:{FN}")["state"] == "Active"
        assert _world(client, LAMBDA_ENV, FN).get("phase") == "healthy"

        _apply(client, LAMBDA_ENV, EMPTY_CANVAS)  # teardown through the product's own path

    assert _state(rie) == "absent", "the RIE container survived the teardown"


def test_a_killed_or_paused_database_fails_the_very_next_apply(
    store_root, containers, offline_translate, monkeypatch,
):
    """`docker kill` and `docker pause`, in one env because a fresh rds apply is
    ~90s of real Postgres boot and the second case needs a live database to
    start from.

    PAUSE is the case a record-trusting check cannot catch and a NAME listing
    cannot either: the container is present and `docker ps` lists it, while
    nothing inside answers a connection. In the field the record stayed
    `available`, `wait_for_available_instances` passed, and three more applies
    returned `applied` / exit 0."""
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    _assert_default_cadence(monkeypatch)
    db = db_container_name(RDS_ENV, DB)
    containers.append(db)

    with TestClient(create_app(store=SpecStore(store_root))) as client:
        # --- 1. a genuinely healthy database --------------------------------
        body, elapsed = _apply(client, RDS_ENV, RDS_CANVAS)
        print(f"\n[FT5-rds] fresh apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert _state(db) == "running"

        body, healthy_noop = _apply(client, RDS_ENV, RDS_CANVAS)
        print(f"[FT5-rds] healthy no-op apply took {healthy_noop:.1f}s -> {body['status']}")
        assert body["status"] == "applied", body
        assert "unhealthy_resources" not in body, body
        assert healthy_noop < 30, f"the happy path must stay prompt, took {healthy_noop:.1f}s"

        # --- 2. `docker kill`: the canonical way a database dies -------------
        # The container is still PRESENT (exited), which is why a name listing
        # could never see this -- and no sweep is waited for.
        _docker("kill", db)
        assert _state(db) == "exited"

        body, elapsed = _apply(client, RDS_ENV, RDS_CANVAS)
        print(f"[FT5-rds] FIRST apply after the kill took {elapsed:.1f}s -> {body['status']}")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied_resources_unhealthy", body
        (fault,) = body["unhealthy_resources"]
        assert (fault["kind"], fault["node"], fault["observed"]) == ("rds", DB, "failed"), fault
        assert "is not running (exit 137)" in fault["reason"], fault
        assert DB in body["note"], body["note"]
        observed = _world(client, RDS_ENV, DB)
        assert observed.get("phase") == "crashed", observed
        # A dead database must also stop advertising a DATABASE_URL nothing can
        # connect to -- the stale fact the Fabric would hand a consumer.
        assert not observed.get("facts"), observed

        # --- 3. the next apply recreates it ---------------------------------
        body, elapsed = _apply(client, RDS_ENV, RDS_CANVAS)
        print(f"[FT5-rds] recovery apply took {elapsed:.1f}s -> {body['status']}")
        assert body["status"] == "applied", body
        assert _state(db) == "running"
        assert _record(store_root, RDS_ENV, "rdsctl", f"db:{DB}")["status"] == "available"
        assert _world(client, RDS_ENV, DB).get("phase") == "healthy"

        # --- 4. `docker pause`: present, listed, and serving nothing ---------
        _docker("pause", db)
        assert _state(db) == "paused"

        body, elapsed = _apply(client, RDS_ENV, RDS_CANVAS)
        print(f"[FT5-rds] FIRST apply after the pause took {elapsed:.1f}s -> {body['status']}")
        assert body["status"] == "applied_resources_unhealthy", body
        (fault,) = body["unhealthy_resources"]
        assert (fault["kind"], fault["node"], fault["observed"]) == ("rds", DB, "failed"), fault
        assert "is paused" in fault["reason"], fault
        assert _world(client, RDS_ENV, DB).get("phase") == "crashed"

        # ...and it recovers from a pause too (`create_db` clears a same-name
        # remnant whatever state it is in).
        body, elapsed = _apply(client, RDS_ENV, RDS_CANVAS)
        print(f"[FT5-rds] recovery apply after the pause took {elapsed:.1f}s -> {body['status']}")
        assert body["status"] == "applied", body
        assert _state(db) == "running"

        _apply(client, RDS_ENV, EMPTY_CANVAS)  # teardown through the product's own path

    assert _state(db) == "absent", "the database container survived the teardown"
