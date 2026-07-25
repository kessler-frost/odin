"""rds nodes as direct Postgres containers via the RuntimeDriver.

W2.7: this is now the SUBSTRATE of the gateway's RDS model
(`gateway/models/rdsctl.py`), not the reconciler's own provisioner -- the
`aws_db_instance` resource `tofu apply` creates is fulfilled by these exact
calls. Nothing about the container itself changed: the name is still
`odin-rds-{env}-{db_id}` (load-bearing for cleanup, labels and every test
that greps for it), the image is still `postgres:16-alpine`, and the host
port is still ephemeral (`ports={5432: 0}`).
"""
from __future__ import annotations

import psycopg2

from odin.runtime.colima import ContainerSpec

POSTGRES_IMAGE = "postgres:16-alpine"
# The engine version odin's substrate really is -- derived from the image tag
# above rather than hard-coded twice, so bumping the image can't leave the
# `EngineVersion` the RDS model reports on the wire lying about it.
POSTGRES_MAJOR = POSTGRES_IMAGE.split(":")[1].split("-")[0]


def container_name(env: str, db_id: str) -> str:
    """The EXACT container name every rds substrate call uses -- a module
    function (not just the method below) so a caller with no `PostgresRds`
    instance can build it too: `reconcile/drift.py`'s reality sweep needs the
    name to look for in one bulk `docker ps`, exactly like
    `compute/tasks.py::container_name` and `compute/functions.py`'s do for
    their kinds."""
    return f"odin-rds-{env}-{db_id}"


class PostgresRds:
    """The RDS model's substrate seam. `endpoint` returns as soon as a host
    port is published -- `rdsctl`'s create waiter is what gates `available`
    on a REAL `pg_ready` probe (formerly the Reconciler's job)."""

    def __init__(self, runtime, env: str = "default") -> None:
        self._rt = runtime
        self._env = env

    def container_name(self, db_id: str) -> str:
        return container_name(self._env, db_id)

    def create_db(self, db_id: str, user: str, password: str, db_name: str = "postgres") -> None:
        """`db_name` is REAL (W2.7): it becomes `POSTGRES_DB`, so the database
        an `aws_db_instance`'s `db_name` names actually exists and the
        DATABASE_URL fact can point at it. Defaults to `postgres` -- the
        database the image always creates anyway -- so an existing canvas with
        no `dbName` field gets byte-identical behavior and a byte-identical
        DATABASE_URL."""
        name = self.container_name(db_id)
        if self._rt.status(name) == "running":
            return  # idempotent: already up
        # A same-name remnant (an exited container after a `docker kill`, or a
        # `created`-but-never-started one) makes the bare `docker run` below
        # fail outright, so clear it unconditionally -- this is also exactly
        # what makes `rdsctl.converge_db_instances` able to recover a killed
        # container without a separate teardown step.
        self._rt.stop(name)
        self._rt.run_container(ContainerSpec(
            name=name,
            image=POSTGRES_IMAGE,
            env={"POSTGRES_USER": user, "POSTGRES_PASSWORD": password, "POSTGRES_DB": db_name},
            ports={5432: 0},
            labels={"odin-env": self._env},
        ))

    def delete_db(self, db_id: str) -> None:
        self._rt.stop(self.container_name(db_id))

    def endpoint(self, db_id: str) -> tuple[str, int] | None:
        port = self._rt.host_port(self.container_name(db_id), 5432)
        return ("127.0.0.1", port) if port else None

    def set_password(self, db_id: str, user: str, current_password: str, new_password: str) -> None:
        """A REAL `ALTER USER ... WITH PASSWORD` against the running Postgres
        -- what `ModifyDBInstance`'s `MasterUserPassword` has to mean if the
        DATABASE_URL fact odin publishes is to stay true (no mock-only modes).
        Raises if the DB can't be reached or the statement fails, so the
        caller can report a real RDS error instead of storing a password the
        container doesn't actually have."""
        endpoint = self.endpoint(db_id)
        if endpoint is None:
            raise RuntimeError(f"{self.container_name(db_id)} publishes no port")
        conn = psycopg2.connect(
            host=endpoint[0], port=endpoint[1], user=user,
            password=current_password, dbname="postgres", connect_timeout=3,
        )
        conn.autocommit = True
        # The role name is an IDENTIFIER (no parameter binding possible) --
        # quoted the way Postgres itself demands; the password IS bound.
        quoted = '"' + user.replace('"', '""') + '"'
        conn.cursor().execute(f"ALTER USER {quoted} WITH PASSWORD %s", (new_password,))
        conn.close()
