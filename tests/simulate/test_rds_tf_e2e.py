"""W2.7 -- the ONE integration test for RDS-on-Terraform: a real canvas with an
`rds` node through a real `tofu apply`, into a real `postgres:16-alpine`
container, with a real `psycopg2` connection made over the DATABASE_URL fact
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
  5. Empty canvas + Apply destroys it for real -- no leftover container.

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

import psycopg2
import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.aws.rds import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "rds-tf-e2e"
NODE = "w27-db"          # also the DBInstanceIdentifier (agent/hcl.py's `_rds`)
DB_NAME = "orders"
USER = "svc"
PASSWORD = "w27-pw-9x2"
STORAGE = "20"
INSTANCE_CLASS = "db.t3.micro"

# How long a real Postgres container has to boot AND accept a connection before
# the apply is considered broken (rdsctl's own `_CREATE_TIMEOUT` is 180s; tofu's
# create waiter sits on top of it).
BOOT_WINDOW = 240.0
# The reality sweep runs every tick here (ODIN_DRIFT_SWEEP_TICKS=1, ~1s), so a
# killed container should surface well inside this.
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


def _select_one(facts: dict) -> int:
    """Connect with the credentials odin PUBLISHED and run a real query.

    The host in `DATABASE_URL` is `host.docker.internal` on purpose (that's what
    a container consumer needs, and the port is the same either way), so this
    host-side connection swaps in loopback and reuses the published port --
    proving the port and credentials in the fact are the real ones."""
    port = int(facts["endpoint"].rsplit(":", 1)[1])
    conn = psycopg2.connect(
        host="127.0.0.1", port=port, user=USER, password=PASSWORD,
        dbname=DB_NAME, connect_timeout=10,
    )
    cur = conn.cursor()
    cur.execute("SELECT 1")
    value = cur.fetchone()[0]
    conn.close()
    return value


@pytest.fixture
def db_cleanup():
    """Container hygiene ABSOLUTE: force-removed by EXACT name on teardown
    regardless of outcome -- the guarantee `tofu destroy` alone can't give if
    the test fails before it runs."""
    names: list[str] = []
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


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


def test_an_rds_node_is_a_real_tf_managed_postgres_that_survives_being_killed(
    store_root, db_cleanup, skeleton_translate, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    victim = container_name(ENV, NODE)
    db_cleanup.append(victim)
    # Sweep every tick (~1s) so the drift half isn't 10 ticks of waiting; the
    # default cadence itself is covered by tests/reconcile/test_drift.py.
    monkeypatch.setenv("ODIN_DRIFT_SWEEP_TICKS", "1")

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
        assert _select_one(facts) == 1
        print(f"\n[W2.7] SELECT 1 over the published DATABASE_URL on :{port} (db {DB_NAME!r})")

        # (4) Scenario 2, preserved: kill the container out of band.
        assert _docker("kill", victim).returncode == 0
        assert not _running(victim), "the premise: the database is really down"

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
        # The RECORD is corrected too, which is what survives a restart.
        assert _rdsctl_state(store.root, ENV)[f"db:{NODE}"]["status"] == "failed"

        # ...and the SAME Apply button brings it back. tofu's own plan here is
        # empty (an aws_db_instance's config never changed), so this recovery is
        # `converge_db_instances` -- the lambda/ecs pattern, for rds.
        reapply = client.post("/apply-full", json=CANVAS, params={"env": ENV})
        assert reapply.status_code == 200, reapply.text
        assert reapply.json()["tf"] == {"status": "ok", "exit_code": 0}, reapply.json()

        recovered = _observed(client, BOOT_WINDOW, "healthy")
        assert recovered is not None and recovered["phase"] == "healthy", f"never recovered: {recovered}"
        assert recovered["verdict"] is None
        assert _running(victim), "recovery means a really-running container, not a green badge"
        assert _select_one(recovered["facts"]) == 1, "the recovered database must really answer"
        print("[W2.7] killed -> crashed with a real verdict -> re-Apply -> SELECT 1 again")

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
