"""PostgresRds: rds nodes as direct Postgres containers (no emulator)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import psycopg2
import pytest

from odin.aws.rds import PostgresRds
from odin.runtime.colima import ColimaRuntime, ContainerSpec


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
    assert rds.container_name("db") == "odin-rds-staging-db"


def test_create_db_runs_postgres_with_creds_and_dynamic_port():
    rt = FakeRuntime()
    PostgresRds(rt, env="default").create_db("db", "app", "s3cret")
    spec = rt.runs[0]
    assert spec.name == "odin-rds-default-db"
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
    rds.create_db("db", "app", "pw")  # stops once itself: clears any remnant pre-run
    rds.delete_db("db")
    # W2.6: the database's mesh sidecar goes down WITH it (it lives in this
    # container's network namespace, so it would die anyway -- stopping it
    # explicitly is what keeps `docker ps` honest and leaves nothing behind).
    assert rt.stopped == ["odin-rds-default-db", "odin-rds-default-db-mesh", "odin-rds-default-db"]
    assert rds.endpoint("db") is None


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
        assert rt.status(rds.container_name("db")) == "absent"
