"""PostgresRds: rds nodes as direct Postgres containers (no emulator)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import asyncpg
import pytest

from odin.aws.rds import PGDATA, PostgresRds, volume_name
from odin.runtime.colima import ColimaRuntime, ContainerSpec


@dataclass
class FakeRuntime:
    runs: list[ContainerSpec] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    # `calls` records container AND volume operations in ONE list, because the
    # ORDER between them is a real contract that a per-kind list would hide:
    # docker refuses to remove a volume a live container still references, so a
    # `delete_db` that removed the volume first would fail every teardown
    # against real docker while passing a test that only checked membership.
    calls: list[tuple[str, str]] = field(default_factory=list)
    volumes: set[str] = field(default_factory=set)

    async def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.calls.append(("run", spec.name))
        self.statuses[spec.name] = "running"
        self.ports[spec.name] = 55432

    async def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.calls.append(("stop", name))
        self.statuses.pop(name, None)
        self.ports.pop(name, None)

    async def status(self, name: str) -> str:
        return self.statuses.get(name, "absent")

    async def host_port(self, name: str, container_port: int) -> int:
        return self.ports.get(name, 0)

    async def create_volume(self, name: str) -> None:
        self.calls.append(("create_volume", name))
        self.volumes.add(name)

    async def remove_volume(self, name: str) -> None:
        self.calls.append(("remove_volume", name))
        self.volumes.discard(name)


def test_container_name_is_env_scoped():
    rds = PostgresRds(FakeRuntime(), env="staging")
    assert rds.container_name("db") == "odin-rds-staging-db"


def test_volume_name_is_the_container_name_plus_data():
    assert volume_name("staging", "db") == "odin-rds-staging-db-data"
    assert PostgresRds(FakeRuntime(), env="staging").volume_name("db") == "odin-rds-staging-db-data"


async def test_create_db_runs_postgres_with_creds_and_dynamic_port():
    rt = FakeRuntime()
    await PostgresRds(rt, env="default").create_db("db", "app", "s3cret")
    spec = rt.runs[0]
    assert spec.name == "odin-rds-default-db"
    assert spec.image.startswith("postgres:16")
    assert spec.env["POSTGRES_USER"] == "app"
    assert spec.env["POSTGRES_PASSWORD"] == "s3cret"
    assert spec.ports == {5432: 0}


async def test_create_db_mounts_a_named_volume_at_pgdata():
    """THE fix. Without this mount the database lives on the container's own
    layer (strictly: on the image's ANONYMOUS volume, which `stop`'s
    `docker rm -f -v` deletes with it), so odin's own repair -- which removes
    the container and runs a new one -- hands back an empty database."""
    rt = FakeRuntime()
    await PostgresRds(rt, env="default").create_db("db", "app", "s3cret")
    assert rt.runs[0].volumes == {"odin-rds-default-db-data": PGDATA}
    assert PGDATA == "/var/lib/postgresql/data", "this is where postgres:16-alpine's PGDATA is"


async def test_the_volume_exists_before_the_container_that_mounts_it():
    """Order, not just membership: `docker run -v <name>:…` would auto-create
    the volume anyway, but UNLABELLED -- and an odin volume nothing can
    attribute to odin is one nothing can ever safely reclaim."""
    rt = FakeRuntime()
    await PostgresRds(rt, env="default").create_db("db", "app", "pw")
    assert rt.calls == [
        ("stop", "odin-rds-default-db"),
        ("create_volume", "odin-rds-default-db-data"),
        ("run", "odin-rds-default-db"),
    ]


async def test_create_db_is_idempotent_while_running():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    await rds.create_db("db", "app", "pw")
    await rds.create_db("db", "app", "pw")
    assert len(rt.runs) == 1


async def test_endpoint_none_until_running_then_host_port():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    assert await rds.endpoint("db") is None
    await rds.create_db("db", "app", "pw")
    assert await rds.endpoint("db") == ("127.0.0.1", 55432)


async def test_delete_db_stops_container():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    await rds.create_db("db", "app", "pw")  # stops once itself: clears any remnant pre-run
    await rds.delete_db("db")
    # W2.6: the database's mesh sidecar goes down WITH it (it lives in this
    # container's network namespace, so it would die anyway -- stopping it
    # explicitly is what keeps `docker ps` honest and leaves nothing behind).
    assert rt.stopped == ["odin-rds-default-db", "odin-rds-default-db-mesh", "odin-rds-default-db"]
    assert await rds.endpoint("db") is None


async def test_delete_db_removes_the_volume_after_the_container():
    """The other half of the fix, and the half that is a DISK leak if it is
    missing: surviving a container replacement is the point, surviving the
    instance that owns it is not. AFTER, because docker refuses to remove a
    volume a container still references."""
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    await rds.create_db("db", "app", "pw")
    assert rt.volumes == {"odin-rds-default-db-data"}
    await rds.delete_db("db")
    assert rt.volumes == set(), "a volume that outlives `odin destroy` is a data leak and a disk leak"
    container_gone = rt.calls.index(("stop", "odin-rds-default-db"), 1)
    assert rt.calls.index(("remove_volume", "odin-rds-default-db-data")) > container_gone


@pytest.mark.integration
async def test_create_db_boots_real_postgres_select_1():
    rt = ColimaRuntime()
    rds = PostgresRds(rt, env="itest")
    await rds.create_db("db", "app", "apppass123")
    try:
        deadline = time.monotonic() + 120  # first run may pull the image
        last = None
        while time.monotonic() < deadline:
            ep = await rds.endpoint("db")
            if ep is not None:
                try:
                    conn = await asyncpg.connect(
                        host=ep[0], port=ep[1], user="app",
                        password="apppass123", database="postgres",
                        timeout=2,
                    )
                    try:
                        assert await conn.fetchval("SELECT 1") == 1
                    finally:
                        await conn.close()
                    return
                # asyncpg raises OSError/TimeoutError while the container is
                # still coming up, where psycopg2 raised OperationalError --
                # a not-yet-listening Postgres is this loop's normal case.
                except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
                    last = exc
            await asyncio.sleep(2)
        raise AssertionError(f"postgres never became ready: {last}")
    finally:
        await rds.delete_db("db")
        assert await rt.status(rds.container_name("db")) == "absent"
        assert rds.volume_name("db") not in await rt.volume_names()


async def _connect_when_ready(rds: PostgresRds, db_id: str, user: str, password: str, timeout: float = 120):
    """Wait for a REAL Postgres behind `rds` and return an open connection.

    Same loop as the test above (asyncpg raises OSError/TimeoutError while the
    container is still coming up, which is this loop's normal case), factored
    out because the replacement test has to run it twice."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        ep = await rds.endpoint(db_id)
        if ep is not None:
            try:
                return await asyncpg.connect(
                    host=ep[0], port=ep[1], user=user, password=password,
                    database="postgres", timeout=2,
                )
            except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
                last = exc
        await asyncio.sleep(2)
    raise AssertionError(f"postgres never became ready: {last}")


@pytest.mark.integration
async def test_real_rows_survive_the_container_replacement_odins_repair_performs():
    """THE substrate-level proof, with a real container and real rows.

    odin's repair for a dead database is `create_db` on the same id, and
    `create_db`'s FIRST act is `stop` -- `docker rm -f -v`, which removes the
    container outright. Before the named volume that meant the rows went with
    it, and the apply that did it reported a green `applied`. So: write rows,
    kill the container the way the repair kills it, repair, read the rows back.

    Deliberately NOT a fabricated recovery. A unit test with a fake runtime can
    only prove the `-v` flag was passed; whether docker keeps a NAMED volume
    across `rm -f -v`, and whether postgres re-attaches to a populated PGDATA
    instead of re-running initdb, are facts about components this repo does not
    own -- exactly what honesty rule 1 says to probe rather than assume."""
    rt = ColimaRuntime()
    rds = PostgresRds(rt, env="rdsvol-survive")
    user, password = "app", "apppass123"
    await rds.create_db("db", user, password)
    try:
        conn = await _connect_when_ready(rds, "db", user, password)
        try:
            await conn.execute("CREATE TABLE orders (id int)")
            await conn.execute("INSERT INTO orders VALUES (42), (43)")
            before = await conn.fetchval("SELECT count(*) FROM orders")
        finally:
            await conn.close()
        assert before == 2

        # The repair, exactly as `rdsctl.converge_db_instances` performs it:
        # the container is destroyed and a new one is run under the same name.
        await rt.stop(rds.container_name("db"))
        assert await rt.status(rds.container_name("db")) == "absent", "the premise: it is really gone"
        assert rds.volume_name("db") in await rt.volume_names(), (
            "the data volume must OUTLIVE `docker rm -f -v` -- that is the whole fix"
        )
        await rds.create_db("db", user, password)

        conn = await _connect_when_ready(rds, "db", user, password)
        try:
            after = await conn.fetchval("SELECT count(*) FROM orders")
            rows = sorted(r["id"] for r in await conn.fetch("SELECT id FROM orders"))
        finally:
            await conn.close()
        assert (after, rows) == (2, [42, 43]), (
            f"odin's own repair returned {after} rows; the database was replaced, not repaired"
        )
        print(f"\n[rds-volume] rows before the replacement: {before}, after: {after} {rows}")
    finally:
        await rds.delete_db("db")
        assert await rt.status(rds.container_name("db")) == "absent"
        # ...and the volume goes WITH the instance. A volume that outlives a
        # delete is a data leak and a disk leak both.
        assert rds.volume_name("db") not in await rt.volume_names()
