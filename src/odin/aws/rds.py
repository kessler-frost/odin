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

from pathlib import Path

import psycopg2

from odin.fabric.models import FirewallRules
from odin.fabric.sidecar import MeshSidecar
from odin.runtime.colima import ContainerSpec

POSTGRES_IMAGE = "postgres:16-alpine"
# The engine version odin's substrate really is -- derived from the image tag
# above rather than hard-coded twice, so bumping the image can't leave the
# `EngineVersion` the RDS model reports on the wire lying about it.
POSTGRES_MAJOR = POSTGRES_IMAGE.split(":")[1].split("-")[0]
# The port Postgres listens on INSIDE the container -- which is what an
# overlay peer dials, because the mesh sidecar shares this container's network
# namespace (the host-published port is a separate, ephemeral one).
POSTGRES_PORT = 5432


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
    on a REAL `pg_ready` probe (formerly the Reconciler's job).

    W2.6: an rds node also joins the env's Nebula mesh (`fabric/sidecar.py`),
    so a drawn `db-sg` compiles into a firewall that really gates who may
    reach the database — `overlay_endpoint` is that gated address. Both are
    live at once: the published host port stays exactly as it was (the create
    waiter's `pg_ready` probe and every `${{db.DATABASE_URL}}` consumer ride
    it), the overlay is an ADDITIONAL, gated path. Who calls `join_mesh`
    moved with rds itself: the gateway's RDS model owns the container's
    lifecycle now, so `rdsctl.py::_finish_create` joins (and
    `ensure_db_mesh` re-ensures on every Apply) instead of a reconciler
    tick."""

    def __init__(self, runtime, env: str = "default", root: Path = Path(".odin"), mesh: MeshSidecar | None = None) -> None:
        self._rt = runtime
        self._env = env
        self._mesh = mesh or MeshSidecar(runtime, env, root)

    def container_name(self, db_id: str) -> str:
        return container_name(self._env, db_id)

    def mesh_member(self, db_id: str) -> str:
        """This database's mesh identity == its container name: unique per
        (env, node) already, and it makes an `overlay.json` assignment
        legible next to the container it belongs to."""
        return self.container_name(db_id)

    def join_mesh(self, db_id: str, firewall: FirewallRules | None = None, revision: str = "") -> str | None:
        """Put this database on the env's overlay behind `firewall` (its drawn
        SG's compiled rules). No-op returning None when the env has no Nebula
        network — i.e. no VPC on the canvas.

        `revision` is the env's security-group membership digest (field test
        4): a database is the ADMITTING member in the common case, so it is
        the one that has to re-check the sessions it already granted when a
        client loses the group that let it in. See `fabric/sidecar.py::ensure`."""
        return self._mesh.ensure(
            self.container_name(db_id), self.mesh_member(db_id), firewall=firewall, revision=revision,
        )

    def overlay_endpoint(self, db_id: str) -> tuple[str, int] | None:
        """The SG-gated (overlay_ip, 5432) address, or None when this database
        never joined a mesh. Read-only."""
        ip = self._mesh.overlay_ip(self.mesh_member(db_id))
        return (ip, POSTGRES_PORT) if ip else None

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
        # The mesh sidecar lives in this container's network namespace, so it
        # dies with it either way -- stopping it explicitly keeps `docker ps`
        # honest and makes the "no leftover containers" rule hold.
        self._mesh.stop(self.container_name(db_id))
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
