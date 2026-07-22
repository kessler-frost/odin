# C1 — Real Backings (MiniStack removal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove MiniStack; back `rds`/`s3`/`sqs`/`sns`/`dynamodb` nodes with real OSS containers run by allfather's own reconciler.

**Architecture:** The Reconciler already talks to AWS-ish machinery only via three injected objects (`_rds`, `_aws`, `_aws_env`). We build direct-container replacements (`PostgresRds`, `BackingAws`), then flip `server.py` wiring, rename the action, extend supervision, and delete the emulator. Spec: `docs/superpowers/specs/2026-07-22-v030-real-backings-design.md`.

**Tech Stack:** Python 3.12+/uv, FastAPI, boto3 (provisioning client), Docker via Colima behind `RuntimeDriver`.

## Global Constraints

- `uv` never pip; `bun` never npm/npx; `python` not python3.
- Pathlib for paths; imports at top of file; keep if/else + try/except minimal.
- Permissive licenses only for backings (Apache-2.0/MIT/BSD/MPL — NO AGPL, no proprietary dev-only licenses).
- Unit suite must stay green after every task: `uv run pytest -q` (integration excluded by default via `addopts`).
- Integration tests marked `@pytest.mark.integration` / `pytestmark = pytest.mark.integration`; run explicitly `uv run pytest -m integration -q <file>`.
- Every container carries the `allfather=1` label (ColimaRuntime adds it) and is torn down by the test that made it; `docker ps -aq --filter label=allfather=1 | xargs -r docker rm -f` must show nothing left after a test session.
- Container naming: `allfather-rds-{env}-{id}` for rds, `allfather-aws-{svc}-{env}` for shared backings.
- Containers reach the host at `host.docker.internal` (the runtime adds the host-gateway mapping); published facts that containers consume must use it, host-side probes use `127.0.0.1`.
- Commit after every task (conventional commits); do NOT push until the controller says so.

---

### Task 1: PostgresRds — rds nodes as direct Postgres containers

**Files:**
- Modify: `src/odin/aws/rds.py` (append the new class; leave `MiniStackRds` untouched — it dies in the switchover task)
- Test: `tests/aws/test_rds_postgres.py` (new)

**Interfaces:**
- Consumes: `odin.runtime.colima.ContainerSpec`, the `RuntimeDriver` protocol (`run_container`, `stop`, `status`, `host_port`).
- Produces (the switchover task wires this into `server.py`): class `PostgresRds` with
  `__init__(self, runtime, env: str = "default")`,
  `create_db(self, db_id: str, user: str, password: str) -> None`,
  `delete_db(self, db_id: str) -> None`,
  `endpoint(self, db_id: str) -> tuple[str, int] | None`,
  `container_name(self, db_id: str) -> str` (= `f"allfather-rds-{self._env}-{db_id}"`).
  Exact drop-in for how the Reconciler already calls `_rds` (see `reconciler.py:139-161,197-209`).

- [ ] **Step 1: Read the two consumer/pattern files**

Read `src/odin/reconcile/reconciler.py` (how `_rds` is called: `create_db`, `delete_db`, `endpoint`, `container_name`; `pg_ready` gates healthy so `endpoint` may return as soon as a host port exists) and `tests/reconcile/test_reconciler.py` (the `FakeRuntime` test style to mirror).

- [ ] **Step 2: Write the failing unit tests**

`tests/aws/test_rds_postgres.py`:

```python
"""PostgresRds: rds nodes as direct Postgres containers (no emulator)."""
from __future__ import annotations

from dataclasses import dataclass, field

from odin.aws.rds import PostgresRds
from odin.runtime.colima import ContainerSpec


@dataclass
class FakeRuntime:
    runs: list[ContainerSpec] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)

    def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.statuses[spec.name] = "running"
        self.ports[spec.name] = 55432

    def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.statuses.pop(name, None)
        self.ports.pop(name, None)

    def status(self, name: str) -> str:
        return self.statuses.get(name, "absent")

    def host_port(self, name: str, container_port: int) -> int:
        return self.ports.get(name, 0)


def test_container_name_is_env_scoped():
    rds = PostgresRds(FakeRuntime(), env="staging")
    assert rds.container_name("db") == "allfather-rds-staging-db"


def test_create_db_runs_postgres_with_creds_and_dynamic_port():
    rt = FakeRuntime()
    PostgresRds(rt, env="default").create_db("db", "app", "s3cret")
    spec = rt.runs[0]
    assert spec.name == "allfather-rds-default-db"
    assert spec.image.startswith("postgres:16")
    assert spec.env["POSTGRES_USER"] == "app"
    assert spec.env["POSTGRES_PASSWORD"] == "s3cret"
    assert spec.ports == {5432: 0}


def test_create_db_is_idempotent_while_running():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    rds.create_db("db", "app", "pw")
    rds.create_db("db", "app", "pw")
    assert len(rt.runs) == 1


def test_endpoint_none_until_running_then_host_port():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    assert rds.endpoint("db") is None
    rds.create_db("db", "app", "pw")
    assert rds.endpoint("db") == ("127.0.0.1", 55432)


def test_delete_db_stops_container():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    rds.create_db("db", "app", "pw")
    rds.delete_db("db")
    assert rt.stopped == ["allfather-rds-default-db"]
    assert rds.endpoint("db") is None
```

- [ ] **Step 3: Run tests, confirm they fail**

Run: `uv run pytest tests/aws/test_rds_postgres.py -q`
Expected: ImportError — `PostgresRds` not defined.

- [ ] **Step 4: Implement PostgresRds**

Append to `src/odin/aws/rds.py`:

```python
POSTGRES_IMAGE = "postgres:16-alpine"


class PostgresRds:
    """rds nodes as direct Postgres containers via the RuntimeDriver.

    Drop-in for the Reconciler's `_rds` seam. `endpoint` returns as soon as a
    host port is published — the Reconciler's `pg_ready` probe gates healthy.
    """

    def __init__(self, runtime, env: str = "default") -> None:
        self._rt = runtime
        self._env = env

    def container_name(self, db_id: str) -> str:
        return f"allfather-rds-{self._env}-{db_id}"

    def create_db(self, db_id: str, user: str, password: str) -> None:
        name = self.container_name(db_id)
        if self._rt.status(name) == "running":
            return  # idempotent: already up
        self._rt.stop(name)  # clear any exited remnant so the boot is fresh
        self._rt.run_container(ContainerSpec(
            name=name,
            image=POSTGRES_IMAGE,
            env={"POSTGRES_USER": user, "POSTGRES_PASSWORD": password},
            ports={5432: 0},
            labels={"allfather-env": self._env},
        ))

    def delete_db(self, db_id: str) -> None:
        self._rt.stop(self.container_name(db_id))

    def endpoint(self, db_id: str) -> tuple[str, int] | None:
        port = self._rt.host_port(self.container_name(db_id), 5432)
        return ("127.0.0.1", port) if port else None
```

(Import `ContainerSpec` at top of the file: `from odin.runtime.colima import ContainerSpec`.)

NOTE: the Fake in the tests treats `stop` on an absent name as fine — verify `ColimaRuntime.stop` is likewise idempotent (it is: `rm -f`, check=False); do not add defensive branches. But DO update the FakeRuntime `stop` in the test to tolerate absent names if the code calls `stop` before first run.

- [ ] **Step 5: Run unit tests, confirm green; run the whole unit suite**

Run: `uv run pytest tests/aws/test_rds_postgres.py -q` → all pass.
Run: `uv run pytest -q` → everything else still green (102 tests were green before this task).

- [ ] **Step 6: Add the make-or-break integration test**

Append to `tests/aws/test_rds_postgres.py`:

```python
import time

import psycopg2
import pytest

from odin.runtime.colima import ColimaRuntime


@pytest.mark.integration
def test_create_db_boots_real_postgres_select_1():
    rt = ColimaRuntime()
    rds = PostgresRds(rt, env="itest")
    rds.create_db("db", "app", "apppass123")
    try:
        deadline = time.monotonic() + 120  # first run may pull the image
        last = None
        while time.monotonic() < deadline:
            ep = rds.endpoint("db")
            if ep is not None:
                try:
                    conn = psycopg2.connect(
                        host=ep[0], port=ep[1], user="app",
                        password="apppass123", dbname="postgres",
                        connect_timeout=2,
                    )
                    with conn, conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        assert cur.fetchone()[0] == 1
                    conn.close()
                    return
                except psycopg2.OperationalError as exc:
                    last = exc
            time.sleep(2)
        raise AssertionError(f"postgres never became ready: {last}")
    finally:
        rds.delete_db("db")
        assert rt.status(rds.container_name("db")) in ("absent", "unknown", "")
```

Adjust the final status assertion to whatever `ColimaRuntime.status` actually returns for a removed container (read `colima.py` first; do not guess).

- [ ] **Step 7: Run the integration test for real (Colima is running)**

Run: `uv run pytest -m integration tests/aws/test_rds_postgres.py -q`
Expected: 1 passed (~10-60s). Then verify no leftovers: `docker ps -a --filter label=allfather=1 --format '{{.Names}}'` prints nothing from this test.

- [ ] **Step 8: Commit**

```bash
git add src/odin/aws/rds.py tests/aws/test_rds_postgres.py
git commit -m "feat(aws): PostgresRds — rds nodes as direct Postgres containers"
```

---

*(Tasks 2+ — BackingAws, the switchover, new sqs/sns/dynamodb coverage, UI shrink — are appended by the controller once the backing-research report locks the s3/sqs/sns/dynamodb image choices. Do not invent them from this file.)*
