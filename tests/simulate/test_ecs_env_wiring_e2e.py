"""Field test 2, "THE PRODUCT HOLE" -- a canvas node's `env` actually wires it up.

Both field agents confirmed the gap: an ECS node whose `env` carried
`${{db.DATABASE_URL}}` / `${{cache.REDIS_URL}}` had that map SILENTLY DROPPED --
no `environment` in the task definition, no note, no warning -- so you could
provision the whole production stack and have no canvas-driven way to hand the
app its connection strings, while ROADMAP claimed "a container consumes
`${{cache.REDIS_URL}}`" and "a CONTAINER consumes `${{db.DATABASE_URL}}`".

This is the real-container proof. One canvas -- an `rds`, an `elasticache` and an
`ecs` node whose `env` references both -- through the real Apply route (real
tofu, real gateway, real Postgres/Redis/task containers), then FROM INSIDE the
task container:
  * the two variables are present, with real resolved values, and
  * a real protocol exchange against each: a Postgres SSLRequest answered `N`,
    and a Redis `PING` answered `+PONG`.

Plus the design invariant that made launch-time injection the right seam: the
resolved DATABASE_URL (which embeds the database password) appears NOWHERE in
the generated main.tf or in terraform.tfstate.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.agent import translate as translate_mod
from odin.compute.tasks import container_name
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.containers import reap_volumes

pytestmark = pytest.mark.integration

ENV = "ecs-env-wiring-e2e"
DB = "wiring-db"
CACHE = "wiring-cache"
SERVICE = "web"
DB_PASSWORD = "wiringCanary001"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "rds", "data": {
            "label": DB, "engine": "postgres", "dbName": "shop",
            "username": "app", "password": DB_PASSWORD}},
        {"id": "n2", "type": "elasticache", "data": {"label": CACHE}},
        {"id": "n3", "type": "ecs", "data": {
            "label": SERVICE, "image": "nginx:alpine", "count": "1", "port": "80",
            "env": {
                "DATABASE_URL": "${{" + f"{DB}.DATABASE_URL" + "}}",
                "REDIS_URL": "${{" + f"{CACHE}.REDIS_URL" + "}}",
                "APP_TIER": "web",
            },
        }},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}

# The 8-byte Postgres SSLRequest; a real server answers with a single 'N'
# (meaning "no SSL"), which is proof the wire reached Postgres itself rather
# than just an open port.
_SSL_REQUEST = r"\x00\x00\x00\x08\x04\xd2\x16\x2f"


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _task_container(root: Path) -> str:
    """The RUNNING task's real container name, discovered from the persisted
    ecsctl state -- the name embeds a task id minted at launch, so it cannot be
    predicted (test_ecs_drift_e2e.py's own technique)."""
    state = json.loads((root / ENV / "gateway" / "ecsctl.json").read_text())
    running = [
        task for key, task in state.items()
        if key.startswith("task:") and task["last_status"] == "RUNNING"
    ]
    assert len(running) == 1, f"expected exactly one running task, got {running}"
    task = running[0]
    return container_name(ENV, task["task_id"], task["container_name"])


def _in_container(name: str, script: str) -> str:
    result = _docker("exec", name, "sh", "-c", script)
    assert result.returncode == 0, f"{script!r} failed: {result.stdout}{result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def cleanup():
    """Container hygiene absolute: every container this env can create is
    force-removed by EXACT name-prefix on teardown, whatever the outcome."""
    yield
    ps = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name={ENV}")
    for container_id in (line for line in ps.stdout.splitlines() if line):
        _docker("rm", "-f", "-v", container_id)
    # The `-v` above does NOT take `odin-rds-{ENV}-{db}-data` with it: PGDATA is
    # a NAMED volume (`aws/rds.py`), and leaving named volumes alone is exactly
    # what `docker rm -f -v` promises. This canvas has an rds node and is never
    # destroyed through a real DELETE, so the volume leaked once per run.
    reap_volumes(ENV)


def test_an_ecs_service_consumes_its_canvas_env_refs_for_real(tmp_path, monkeypatch, cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        started = time.monotonic()
        resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        print(f"\n[wiring] apply-full took {time.monotonic() - started:.1f}s")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied", body
        assert body["tf"]["status"] == "ok", body
        assert body.get("unsupported") in (None, []), body

        # --- 1. ORDERING came from depends_on, not from an interpolated value.
        main_tf = (store.root / ENV / "tf" / "main.tf").read_text()
        depends_on = next(line for line in main_tf.splitlines() if "depends_on" in line)
        assert f"aws_db_instance.{DB.replace('-', '_')}" in depends_on, main_tf
        assert f"aws_elasticache_cluster.{CACHE.replace('-', '_')}" in depends_on, main_tf
        assert "environment" not in main_tf, "env values must never enter the HCL"

        # --- 2. NO SECRET IN TOFU STATE. The rds `password` argument is the one
        # legitimate copy; a resolved DATABASE_URL in the taskdef would be a
        # second one, in main.tf AND in the state file.
        assert main_tf.count(DB_PASSWORD) == 1, main_tf
        state = (store.root / ENV / "tf" / "terraform.tfstate").read_text()
        assert "postgresql://" not in state, "a resolved connection string reached tofu state"

        # --- 3. THE VARIABLES ARE REALLY THERE, with real values.
        name = _task_container(store.root)
        database_url = _in_container(name, "printenv DATABASE_URL")
        redis_url = _in_container(name, "printenv REDIS_URL")
        assert _in_container(name, "printenv APP_TIER") == "web", "static entries ride along too"
        assert database_url.startswith(f"postgresql://app:{DB_PASSWORD}@"), database_url
        assert redis_url.startswith("redis://"), redis_url

        # --- 4. THE CONTAINER ACTUALLY CONNECTS TO BOTH, from inside itself.
        db = urlparse(database_url)
        pg = _in_container(name, f"printf '{_SSL_REQUEST}' | nc -w 5 {db.hostname} {db.port}")
        assert pg == "N", f"Postgres did not answer the SSLRequest: {pg!r}"

        cache = urlparse(redis_url)
        pong = _in_container(name, f"printf 'PING\\r\\n' | nc -w 5 {cache.hostname} {cache.port}")
        assert "+PONG" in pong, f"Redis did not answer PING: {pong!r}"

        # --- 5. Teardown still works.
        torn = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
        assert torn.status_code == 200, torn.text
        assert torn.json()["tf"] == {"status": "ok", "exit_code": 0}, torn.json()

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"task containers survived: {leftover.stdout}"
