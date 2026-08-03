"""W2.7 -- the ONE integration test for RDS-on-Terraform: a real canvas with an
`rds` node through a real `tofu apply`, into a real `postgres:16-alpine`
container, with a real `asyncpg` connection made over the DATABASE_URL fact
odin published.

What this proves that no unit test can:
  1. `aws_db_instance` is REAL through the gateway: tofu creates it, the
     provider's own create waiter absorbs the container boot, and the instance
     only reports `available` once a genuine `pg_ready` connection succeeds.
  2. Apply -> `tofu plan -detailed-exitcode` is ZERO DRIFT (the research bar,
     and the whole reason the DB-instance shape models every
     Optional-without-Computed provider attribute).
  3. The `${{db.VAR}}` fact contract SURVIVED the move off the reconciler: both
     `DATABASE_URL` (host.docker.internal) and `DATABASE_URL_VM`
     (host.lima.internal) are published, both name the container's real
     published port, and connecting with those credentials really works.
  4. Scenario 2's crash/recovery behavior survived it too: `docker kill` the
     container -> the reality sweep reports `crashed` with a verdict naming the
     container and its real exit code -> the SAME Apply button brings a working
     database back (`rdsctl.converge_db_instances`).
  4b. ...and that recovery is NON-DESTRUCTIVE (v0.8.14). Real rows are written
     over the published DATABASE_URL before the kill and read back after the
     recovery apply. Until each instance got a named data volume this was the
     single worst thing odin did quietly: the repair replaced the container, the
     database came back EMPTY, and the apply reported a green `applied` with a
     one-line warning. The warning is now a footnote, and this is what pays for
     it.
  5. Empty canvas + Apply destroys it for real -- no leftover container, AND no
     leftover volume. A volume that outlives `destroy` is a data leak and a disk
     leak both, so the fix for (4b) is only half a fix without it.

THE CADENCE -- why this file sets NO drift knob (and used to). It set
`ODIN_DRIFT_SWEEP_TICKS=1`, which .claude/CLAUDE.md honesty rule 1b names as a
test fabricating a guard's promptness. That was load-bearing for exactly one
assertion -- that `rdsctl.json` reads `failed` -- and that assertion was FLAKY
for a precise reason: the two facts it paired come from different code paths
with different cadences.

  * `/world` reading `crashed` is CADENCE-FREE. `tf_status.project()` applies
    `drift.live_verdicts` on every projection, so the phase and the verdict flip
    on the next reconciler tick whatever the sweep is doing.
  * the RECORD being corrected to `failed` was, here, the BACKGROUND sweep's
    write. `Reconciler._project_tf_owned` runs the sweep and the projection in
    one tick but as TWO INDEPENDENT `_dead` calls, each taking its own
    `docker ps`/`inspect` sample -- and `drift.py` deliberately makes the
    correcting one skip a pass rather than act on an ambiguous reading
    (`_listing`'s "unknown is not gone", and `_not_serving` refusing to treat
    `inspect`'s `absent` as a death).

So the OBSERVED failure was `/world` already saying `crashed` while the record
still said `available`, and the test read the record with no retry at all. Which
of the two samples disagreed is NOT claimed here, because it was not caught in
the act: the kill landing between the sweep's read and the projection's read, and
the sweep hitting an ambiguous reading and skipping, both produce exactly this
and are both narrow enough to explain why it is rare. Naming one would be a
guess dressed as a diagnosis. The ROOT SHAPE is what the fix acts on and is not
in doubt -- two independent samples per tick, and an assertion that read the
product of only one of them without waiting for it.

The fix is not a longer wait; it is to stop asking a background loop at all. The
record correction that odin's CONTRACT depends on is the one /apply-full makes
for itself (`server.py` calls `drift.sweep_compute` BEFORE
`rdsctl.converge_db_instances`, which is what made recovery take one apply
instead of two), and that write is witnessed synchronously in the apply's own
response by `recovered_resources` -- `_recovering_resources` filters on
`status == rdsctl.FAILED` and is called in the single instant the mark exists.
So the record assertion moved onto that signal, the knob went away, and this
test now runs at the cadence the user gets. It also got STRONGER: it pins the
record's reason to the verdict `/world` showed, the one-wording invariant
`ecsctl.container_gone_reason` exists for.

Shape/hygiene modeled on tests/simulate/test_logs_tf_e2e.py, including its
LOAD-BEARING store-root discovery (Colima only mounts `$HOME`, so the SpecStore
must live under the repo checkout, never pytest's `tmp_path`) and its absolute
container-hygiene fixture (the exact container name is force-removed on
teardown even if the test fails before `tofu destroy`). The translation agent's
refine pass is stubbed to the deterministic skeleton: this test is about the
gateway model, the substrate and the facts, not the SDK pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.agent import translate as translate_mod
from odin.aws.rds import container_name, volume_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "rds-tf-e2e"
NODE = "w27-db"          # also the DBInstanceIdentifier (iac/hcl.py's `_rds`)
DB_NAME = "orders"
USER = "svc"
PASSWORD = "w27-pw-9x2"
STORAGE = "20"
INSTANCE_CLASS = "db.t3.micro"

# How long a real Postgres container has to boot AND accept a connection before
# the apply is considered broken (rdsctl's own `_CREATE_TIMEOUT` is 180s; tofu's
# create waiter sits on top of it).
BOOT_WINDOW = 240.0
# How long `/world` gets to report a killed database. This is ONE reconciler
# tick's worth of work, not a sweep cadence: `tf_status.project()` applies
# `live_verdicts` on every projection, so the phase flips on the next tick
# whatever the drift sweep is doing. The test PRINTS the time it really took, so
# the number is measured on every run rather than asserted here. The generous
# bound is for a stalled reconciler, which is the only way this can genuinely
# fail -- it is not absorbing a cadence.
DRIFT_WINDOW = 45.0

CANVAS = {
    "nodes": [{
        "id": "db", "type": "rds",
        "data": {
            "label": NODE, "engine": "postgres", "instanceClass": INSTANCE_CLASS,
            "allocatedStorage": STORAGE, "dbName": DB_NAME,
            "username": USER, "password": PASSWORD,
        },
    }],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _running(name: str) -> bool:
    out = _docker("inspect", "-f", "{{.State.Running}}", name)
    return out.stdout.strip() == "true"


def _tofu(args: list[str], workspace: Path, env_vars: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


def _rdsctl_state(root: Path, env: str) -> dict:
    path = root / env / "gateway" / "rdsctl.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _observed(client, timeout: float, want_phase: str) -> dict | None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        world = client.get("/world", params={"env": ENV}).json()
        last = next((r for r in world["resources"] if r["id"] == NODE), None)
        if last is not None and last["phase"] == want_phase:
            return last
        time.sleep(1)
    return last


async def _query(facts: dict, sql: str):
    """Connect with the credentials odin PUBLISHED and run a real statement.

    The host in `DATABASE_URL` is `host.docker.internal` on purpose (that's what
    a container consumer needs, and the port is the same either way), so this
    host-side connection swaps in loopback and reuses the published port --
    proving the port and credentials in the fact are the real ones."""
    port = int(facts["endpoint"].rsplit(":", 1)[1])
    conn = await asyncpg.connect(
        host="127.0.0.1", port=port, user=USER, password=PASSWORD,
        database=DB_NAME, timeout=10,
    )
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


async def _select_one(facts: dict) -> int:
    return (await _query(facts, "SELECT 1 AS one"))[0]["one"]


async def _write_rows(facts: dict) -> list[int]:
    """Real rows, through the fact odin published -- the thing a recovery must
    not destroy."""
    await _query(facts, "CREATE TABLE IF NOT EXISTS orders (id int)")
    await _query(facts, "INSERT INTO orders VALUES (42), (43)")
    return await _read_rows(facts)


async def _read_rows(facts: dict) -> list[int]:
    """The rows, or `[]` when the TABLE ITSELF is gone.

    The `to_regclass` probe first, and it is not decoration: FOUND BY
    MUTATION-TESTING the non-destructive claim below. With the repair broken so
    the replacement container mounts no data volume, `SELECT id FROM orders`
    raises `asyncpg.exceptions.UndefinedTableError` at plan time -- so the test
    failed with a driver traceback and the carefully-worded "odin's own repair
    emptied the database" message it exists to print never appeared. An emptied
    database is exactly the failure this assertion is for; it should read as one."""
    (present,) = await _query(facts, "SELECT to_regclass('orders') IS NOT NULL AS ok")
    return sorted(r["id"] for r in await _query(facts, "SELECT id FROM orders")) if present["ok"] else []


@pytest.fixture
def db_cleanup():
    """Container hygiene ABSOLUTE: force-removed by EXACT name on teardown
    regardless of outcome -- the guarantee `tofu destroy` alone can't give if
    the test fails before it runs.

    The VOLUME is force-removed the same way, and it needs its own line: `rm -f
    -v` drops a container's anonymous volumes and deliberately leaves NAMED ones
    (that is what makes odin's rds repair non-destructive), so a test that only
    removed the container would leak a Postgres data volume on every failed
    run."""
    names: list[str] = []
    volumes: list[str] = []
    yield names, volumes
    for name in names:
        _docker("rm", "-f", "-v", name)
    for volume in volumes:
        _docker("volume", "rm", "-f", volume)


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-w27-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def skeleton_translate(monkeypatch):
    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)


async def test_an_rds_node_is_a_real_tf_managed_postgres_that_survives_being_killed(
    store_root, db_cleanup, skeleton_translate, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    victim = container_name(ENV, NODE)
    data_volume = volume_name(ENV, NODE)
    cleanup_names, cleanup_volumes = db_cleanup
    cleanup_names.append(victim)
    cleanup_volumes.append(data_volume)
    # NO CADENCE KNOB -- see "THE CADENCE" in the module docstring. This used to
    # set `ODIN_DRIFT_SWEEP_TICKS=1`; every signal it waits on below is
    # cadence-free, so the default (10 ticks) is what runs here.
    monkeypatch.delenv("ODIN_DRIFT_SWEEP_TICKS", raising=False)

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        resp = client.post("/apply-full", json=CANVAS, params={"env": ENV})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["unsupported"] == [], body["unsupported"]
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body["tf"]

        # (1) The instance is REAL and `available` -- which, per rdsctl's create
        # waiter, means a genuine `pg_ready` connection already succeeded before
        # tofu's own waiter was allowed to finish.
        record = _rdsctl_state(store.root, ENV)[f"db:{NODE}"]
        assert record["status"] == "available", record
        assert record["engine"] == "postgres"
        assert record["db_instance_class"] == INSTANCE_CLASS
        assert record["allocated_storage"] == int(STORAGE)
        assert record["db_name"] == DB_NAME
        assert record["master_username"] == USER
        port = record["endpoint_port"]
        assert port, "an available instance must publish its real host port"
        assert _running(victim), "the apply must have left a REAL Postgres container running"

        gateway_port = client.get("/health").json()["gateway"]["port"]
        operator = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)

        # (2) Zero drift: apply -> plan changes NOTHING. This is what every
        # explicitly-emitted provider default in the DB-instance shape is for.
        workspace = workspace_mod.tf_dir(store.root, ENV)
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, _tf_env(gateway_port, *operator))
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # (3) THE fact contract: both endpoint forms, both naming the real port,
        # and a REAL connection with the published credentials.
        healthy = _observed(client, BOOT_WINDOW, "healthy")
        assert healthy is not None and healthy["phase"] == "healthy", f"never healthy: {healthy}"
        facts = healthy["facts"]
        assert facts["endpoint"] == f"host.docker.internal:{port}"
        assert facts["endpoint_vm"] == f"host.lima.internal:{port}"
        assert facts["DATABASE_URL"] == f"postgresql://{USER}:{PASSWORD}@host.docker.internal:{port}/{DB_NAME}"
        assert facts["DATABASE_URL_VM"] == f"postgresql://{USER}:{PASSWORD}@host.lima.internal:{port}/{DB_NAME}"
        assert await _select_one(facts) == 1
        print(f"\n[W2.7] SELECT 1 over the published DATABASE_URL on :{port} (db {DB_NAME!r})")

        # (3b) REAL ROWS, written through that same published fact. Everything
        # below is about whether odin's own repair gives them back.
        before = await _write_rows(facts)
        assert before == [42, 43]
        volumes = _docker("volume", "ls", "--format", "{{.Name}}").stdout.split()
        assert data_volume in volumes, (
            f"the database's data must be on a NAMED volume ({data_volume}), or the repair "
            f"below cannot be non-destructive. Volumes present: {volumes}"
        )

        # (4) Scenario 2, preserved: kill the container out of band.
        assert _docker("kill", victim).returncode == 0
        assert not _running(victim), "the premise: the database is really down"

        killed_at = time.monotonic()
        drifted = _observed(client, DRIFT_WINDOW, "crashed")
        assert drifted is not None and drifted["phase"] == "crashed", (
            f"a killed database must not keep reading healthy (last seen: {drifted})"
        )
        assert drifted["verdict"], "drift without a verdict is just a red badge with no answer"
        assert victim in drifted["verdict"], "the verdict must name the container that died"
        assert "re-Apply" in drifted["verdict"], drifted["verdict"]
        # A `docker kill` is SIGKILL -> exit 137; the verdict carries the REAL code.
        assert "exit 137" in drifted["verdict"], drifted["verdict"]
        # And no stale DATABASE_URL left advertising a database that's gone.
        assert drifted["facts"] == {}, drifted["facts"]
        print(f"[W2.7] killed -> /world crashed in {time.monotonic() - killed_at:.2f}s")

        # ...and the SAME Apply button brings it back -- with NOTHING having
        # waited for the background sweep first, which is the whole point of the
        # cadence note in this module's docstring. tofu's own plan here is empty
        # (an aws_db_instance's config never changed), so this recovery is
        # `converge_db_instances` -- the lambda/ecs pattern, for rds.
        reapply = client.post("/apply-full", json=CANVAS, params={"env": ENV})
        assert reapply.status_code == 200, reapply.text
        assert reapply.json()["tf"] == {"status": "ok", "exit_code": 0}, reapply.json()

        # ...and the apply DISCLOSES the repair, with what it cost -- read from
        # the real volume listing, not asserted (`server.py::_RECOVERY_COST`).
        #
        # This is ALSO the proof that the RECORD was corrected to `failed`, and
        # it is why the test no longer reads `rdsctl.json` for that itself.
        # `_recovering_resources` filters on `status == rdsctl.FAILED` and
        # server.py calls it in the one instant the mark exists -- after
        # `drift.sweep_compute`, before `converge_db_instances` clears it back to
        # `creating`. So a non-empty entry here cannot be produced without a
        # record that really said `failed`, and it arrives IN THIS RESPONSE
        # rather than whenever a background loop got round to it.
        recovery_body = reapply.json()
        (disclosed,) = recovery_body["recovered_resources"]
        assert (disclosed["kind"], disclosed["node"]) == ("rds", NODE), disclosed
        # ...carrying the SAME sentence the canvas showed. Both come from
        # `drift.py::_dead_verdict`, and that one-wording rule is exactly what
        # `ecsctl.container_gone_reason` was extracted for after two paths
        # writing different strings for one event made a test flake.
        assert disclosed["reason"] == drifted["verdict"], (
            f"the record's reason and the canvas verdict must be one sentence: "
            f"{disclosed['reason']!r} vs {drifted['verdict']!r}"
        )
        assert disclosed["data_kept"] is True, disclosed
        assert "its data survived" in recovery_body["note"], recovery_body["note"]

        recovered = _observed(client, BOOT_WINDOW, "healthy")
        assert recovered is not None and recovered["phase"] == "healthy", f"never recovered: {recovered}"
        assert recovered["verdict"] is None
        assert _running(victim), "recovery means a really-running container, not a green badge"
        assert await _select_one(recovered["facts"]) == 1, "the recovered database must really answer"

        # (4b) THE PROOF the disclosure above is now allowed to be a footnote:
        # the rows written before the kill are still there. The container is a
        # NEW one -- `create_db` removed the old one before running it -- so
        # this is the volume, not a survivor.
        after = await _read_rows(recovered["facts"])
        assert after == before, (
            f"odin's own repair emptied the database: {before} before the kill, {after} after "
            f"the recovery apply. The named data volume ({data_volume}) did not survive the "
            f"container replacement."
        )
        print(f"[W2.7] killed -> crashed -> re-Apply -> rows {before} -> {after} (non-destructive)")

        # Still zero drift after the recovery apply.
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, _tf_env(gateway_port, *operator))
        assert plan.returncode == 0, f"drift after recovery (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # (5) Teardown through the ONLY human surface (empty canvas + Apply).
        resp = client.post("/apply-full", json=EMPTY_CANVAS, params={"env": ENV})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tf"] == {"status": "ok", "exit_code": 0}
        assert _rdsctl_state(store.root, ENV).get(f"db:{NODE}") is None

    ps_after = _docker("ps", "-a", "--filter", f"name={victim}", "--format", "{{.Names}}")
    assert ps_after.stdout.strip() == "", f"the Postgres container survived teardown: {ps_after.stdout}"
    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name={ENV}")
    assert leftover.stdout.strip() == ""
    # (5, the other half) The DATA VOLUME goes with the instance. Surviving a
    # container replacement is the point; surviving `destroy` would be a data
    # leak and, on a machine with little disk headroom, a growing one -- every
    # Postgres volume is tens of MiB and nothing else would ever reclaim it.
    vols_after = _docker("volume", "ls", "--filter", f"name={ENV}", "--format", "{{.Name}}")
    assert vols_after.stdout.strip() == "", f"a data volume survived teardown: {vols_after.stdout}"
