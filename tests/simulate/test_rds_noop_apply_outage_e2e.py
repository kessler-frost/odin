"""Field test 3's hole, for RDS -- the other kind the fix never reached.

`test_rds_tf_e2e.py` already proves the RECOVERABLE case: kill the Postgres
container, the reality sweep says `crashed`, and the same Apply button brings it
back. This file proves the case that used to lie: an Apply whose recovery does
NOT work.

`converge_db_instances` starts the re-create on a thread and returns, so
/apply-full answered `applied` -- and `odin apply` exited 0 -- the instant it
was spawned, even when Postgres never accepted a connection afterwards. tofu
cannot catch it: `status` is read-only Computed in the provider's schema, so the
plan is empty and the create waiter (the one thing that DOES catch a bad fresh
create) never runs again.

The trigger is the task's own words -- "an RDS whose Postgres never accepts
connections" -- built out of real containers: the database's container name is
taken over by a container that publishes 5432 with nothing listening behind it.
`PostgresRds.create_db` is idempotent on a RUNNING container of that name (it
must be -- that is what makes the recoverable case above idempotent), so the
converge finds it up, and `_wait_available`'s real `pg_ready` probe is what
tells the truth about it.

  1. a healthy database                      -> applied, exit 0
  2. an identical no-op re-apply             -> applied, exit 0, PROMPT (the
                                                verification may not tax the
                                                happy path)
  3. the container is replaced by one that
     never answers; re-apply                 -> FAILS, names the instance and
                                                the real connection error
  4. remove it, re-apply                     -> applied, exit 0, recovered

`_CREATE_TIMEOUT` is shortened from its 180s default (the documented knob
`_finish_create` reads at call time) so step 3's honest failure lands in ~20s
instead of three minutes; `ODIN_RDS_AVAILABLE_TIMEOUT` is set to outlast it,
which is exactly the relationship the two defaults have.
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
from odin.aws.rds import container_name
from odin.gateway.models import rdsctl
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "rds-noop-outage-e2e"
NODE = "outage-db"
DB_NAME = "orders"
USER = "svc"
PASSWORD = "noop-pw-7k1"
# A container that publishes 5432 and never listens on it -- an image already
# on the machine for the other integration tests, so this pulls nothing.
SQUATTER_IMAGE = "nginx:alpine"
CREATE_TIMEOUT = 20.0
DRIFT_WINDOW = 60.0

CANVAS = {
    "nodes": [{
        "id": "db", "type": "rds",
        "data": {
            "label": NODE, "engine": "postgres", "instanceClass": "db.t3.micro",
            "allocatedStorage": "20", "dbName": DB_NAME,
            "username": USER, "password": PASSWORD,
        },
    }],
    "edges": [],
}
EMPTY_CANVAS: dict = {"nodes": [], "edges": []}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def _db_record(root: Path) -> dict:
    path = root / ENV / "gateway" / "rdsctl.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return state.get(f"db:{NODE}", {})


@pytest.fixture
def db_cleanup():
    """Container hygiene ABSOLUTE, by EXACT name -- both the real database
    container and the stand-in that takes its name, whatever the outcome.

    The data VOLUME needs its own removal: `rm -f -v` drops a container's
    anonymous volumes and deliberately leaves NAMED ones (that is what makes
    odin's rds repair non-destructive), so removing only the container would
    leak a Postgres volume on every failed run."""
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)
        _docker("volume", "rm", "-f", f"{name}-data")


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-rds-noop-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _apply(client, canvas: dict) -> tuple[dict, float]:
    started = time.monotonic()
    resp = client.post("/apply-full", params={"env": ENV}, json=canvas)
    assert resp.status_code == 200, resp.text
    return resp.json(), time.monotonic() - started


def _await_status(root: Path, want: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        record = _db_record(root)
        if record.get("status") == want:
            return record
        assert time.monotonic() < deadline, f"never reached {want}: {record.get('status')}"
        time.sleep(0.5)


def test_a_noop_apply_cannot_report_success_on_a_database_that_never_answers(
    store_root, db_cleanup, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    victim = container_name(ENV, NODE)
    db_cleanup.append(victim)

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    # Sweep every tick so step 3 reaches its PRECONDITION (a `failed` record,
    # the state an Apply is supposed to be the recovery for) in seconds rather
    # than on the ~10-tick cadence. Legitimate here for the reason
    # test_lambda_noop_apply_outage_e2e.py spells out: the thing under test is
    # an apply whose recovery cannot work, not how quickly odin notices a dead
    # container -- that is measured at the full default cadence in
    # test_false_green_window_e2e.py (honesty rule 1b).
    monkeypatch.setenv("ODIN_DRIFT_SWEEP_TICKS", "1")
    monkeypatch.setattr(rdsctl, "_CREATE_TIMEOUT", CREATE_TIMEOUT)
    monkeypatch.setenv("ODIN_RDS_AVAILABLE_TIMEOUT", str(CREATE_TIMEOUT + 40))

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        # --- 1. a genuinely healthy database --------------------------------
        body, elapsed = _apply(client, CANVAS)
        print(f"\n[FT3-rds] fresh apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert body["tf"]["status"] == "ok", body
        assert "unhealthy_resources" not in body, body
        assert _db_record(store_root)["status"] == "available"

        # --- 2. an identical no-op re-apply on a HEALTHY database -----------
        body, elapsed = _apply(client, CANVAS)
        print(f"[FT3-rds] no-op apply on a healthy database took {elapsed:.1f}s")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body
        assert "unhealthy_resources" not in body, body
        assert elapsed < 60, f"a healthy no-op apply must stay prompt, took {elapsed:.1f}s"

        # --- 3. the database dies and CANNOT come back, then THE BUG --------
        # The container is destroyed out of band, which the reality sweep
        # confirms and records as `failed` -- the state an Apply is supposed to
        # be the recovery for. Then the name is taken over by a container that
        # publishes 5432 and never listens behind it, so the recovery cannot
        # work: `create_db` is idempotent on a RUNNING container of that name
        # (exactly what makes the recoverable case in test_rds_tf_e2e.py work),
        # so the converge finds it "up" and only `_wait_available`'s REAL
        # `pg_ready` connection can tell the difference.
        _docker("rm", "-f", "-v", victim)
        _await_status(store_root, "failed", DRIFT_WINDOW)
        squat = _docker(
            "run", "-d", "--name", victim, "--label", "odin=1",
            "-p", "0:5432", SQUATTER_IMAGE,
        )
        assert squat.returncode == 0, squat.stderr

        body, elapsed = _apply(client, CANVAS)
        print(f"[FT3-rds] no-op apply on a BROKEN database took {elapsed:.1f}s -> {body['status']}")
        # tofu genuinely had nothing to do -- exactly why it could never have
        # caught this, and why odin has to.
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        # THE regression: this used to be `applied`, exit 0, on a dead database.
        assert body["status"] == "applied_resources_unhealthy", body
        (fault,) = body["unhealthy_resources"]
        assert fault["kind"] == "rds", fault
        assert fault["node"] == NODE, fault
        assert fault["observed"] == "failed", fault
        # ...and the real underlying reason: the LAST REAL probe failure, in the
        # apply's own output rather than only in /world.
        assert "never became ready" in (fault["reason"] or ""), fault
        assert NODE in body["note"], body["note"]
        assert elapsed < 180, f"the failure must be bounded, took {elapsed:.1f}s"

        # --- 4. clear it: the Apply is the recovery -------------------------
        _docker("rm", "-f", "-v", victim)
        body, elapsed = _apply(client, CANVAS)
        print(f"[FT3-rds] recovery apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert "unhealthy_resources" not in body, body
        assert _db_record(store_root)["status"] == "available"

        # --- 5. teardown still completes promptly ---------------------------
        body, elapsed = _apply(client, EMPTY_CANVAS)
        print(f"[FT3-rds] teardown apply took {elapsed:.1f}s")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body

    leftover = _docker("ps", "-aq", "--filter", f"name={victim}")
    assert leftover.stdout.strip() == "", f"the database container survived: {leftover.stdout}"
    # ...and so does its data volume: a teardown that leaves one behind is a
    # data leak and a disk leak, and nothing else in odin would ever reclaim it.
    vols = _docker("volume", "ls", "--filter", f"name={victim}", "--format", "{{.Name}}")
    assert vols.stdout.strip() == "", f"the database's data volume survived: {vols.stdout}"
