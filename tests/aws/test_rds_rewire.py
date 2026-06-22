"""S0.3 — the make-or-break: RDS CreateDBInstance boots a REAL Postgres via
allfather's runner (not MiniStack's docker), reachable + auth-able.

Marked `integration`: needs Colima/Docker. Run with `-m integration`.
"""
from __future__ import annotations

import time

import psycopg2
import pytest

from odin.aws.embed import (
    install_rds_spawn_rewire,
    ministack_boto_client,
    start_ministack,
    stop_ministack,
)
from odin.runtime.colima import ColimaRuntime

pytestmark = pytest.mark.integration

DB_ID = "appdb"
CONTAINER = f"ministack-rds-{DB_ID}"
USER, PASSWORD = "app", "apppass123"


@pytest.fixture
def embedded():
    runtime = ColimaRuntime()
    runtime.stop(CONTAINER)
    start_ministack()
    install_rds_spawn_rewire(runtime)
    yield runtime
    runtime.stop(CONTAINER)
    stop_ministack()


def _wait_available(rds, db_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        desc = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
        if desc["DBInstanceStatus"] == "available":
            return desc["Endpoint"]
        if desc["DBInstanceStatus"] == "failed":
            raise AssertionError("RDS instance reported failed")
        time.sleep(2)
    raise AssertionError(f"RDS instance not available within {timeout}s")


def test_rds_create_boots_real_postgres(embedded):
    runtime = embedded
    rds = ministack_boto_client("rds")
    rds.create_db_instance(
        DBInstanceIdentifier=DB_ID,
        Engine="postgres",
        DBInstanceClass="db.t3.micro",
        AllocatedStorage=20,
        MasterUsername=USER,
        MasterUserPassword=PASSWORD,
    )

    endpoint = _wait_available(rds, DB_ID)
    assert endpoint["Address"] and endpoint["Port"]

    # The container is real and allfather-managed (not double-spawned).
    assert runtime.status(CONTAINER) == "running"

    # A real psycopg2 connection to the booted Postgres succeeds.
    conn = psycopg2.connect(
        host=endpoint["Address"],
        port=endpoint["Port"],
        user=USER,
        password=PASSWORD,
        dbname="postgres",
        connect_timeout=10,
    )
    cur = conn.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1
    conn.close()
