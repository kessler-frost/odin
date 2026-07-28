"""rds nodes as direct Postgres containers via the RuntimeDriver.

W2.7: this is now the SUBSTRATE of the gateway's RDS model
(`gateway/models/rdsctl.py`), not the reconciler's own provisioner -- the
`aws_db_instance` resource `tofu apply` creates is fulfilled by these exact
calls. The name is still `odin-rds-{env}-{db_id}` (load-bearing for cleanup,
labels and every test that greps for it), the image is still
`postgres:16-alpine`, and the host port is still ephemeral (`ports={5432: 0}`).

ONE thing about the container did change, in v0.8.14: its PGDATA is a NAMED
volume (`volume_name`) instead of the image's anonymous one, so replacing the
container no longer replaces the database. Odin's own repair
(`rdsctl.converge_db_instances` -> `create_db`) is what made that urgent -- it
destroys and re-creates the container to fix a crash, and until the volume was
named that handed the user back an EMPTY database with a green apply. The
volume is created with the container and removed with `delete_db`, never in
between.
"""
from __future__ import annotations

from pathlib import Path

import asyncpg

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
# WHERE the database's bytes live, and therefore what has to sit on a named
# volume for a container replacement to be non-destructive. Probed off the real
# image rather than remembered:
#
#     $ docker image inspect postgres:16-alpine \
#         -f '{{json .Config.Volumes}} {{range .Config.Env}}{{.}} {{end}}'
#     {"/var/lib/postgresql/data":{}} ... PGDATA=/var/lib/postgresql/data ...
#
# Note what that first field says: the image ALREADY declares this path a
# volume, so every rds container odin has ever run had one. It was just an
# ANONYMOUS volume, which `RuntimeDriver.stop` (`docker rm -f -v`) deletes with
# the container -- which is exactly why odin's own repair used to hand back an
# empty database.
PGDATA = "/var/lib/postgresql/data"


def container_name(env: str, db_id: str) -> str:
    """The EXACT container name every rds substrate call uses -- a module
    function (not just the method below) so a caller with no `PostgresRds`
    instance can build it too: `reconcile/drift.py`'s reality sweep needs the
    name to look for in one bulk `docker ps`, exactly like
    `compute/tasks.py::container_name` and `compute/functions.py`'s do for
    their kinds."""
    return f"odin-rds-{env}-{db_id}"


def volume_name(env: str, db_id: str) -> str:
    """The named volume holding THIS database's data, derived from the
    container name so the pair is legible in `docker volume ls` next to
    `docker ps` -- `odin-rds-{env}-{db_id}-data`.

    A module function for `container_name`'s reason: callers that hold no
    `PostgresRds` need it too (`server.py`'s recovery disclosure asks the
    runtime whether this exact volume is still there before it tells a user
    their data survived)."""
    return f"{container_name(env, db_id)}-data"


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

    def volume_name(self, db_id: str) -> str:
        return volume_name(self._env, db_id)

    def mesh_member(self, db_id: str) -> str:
        """This database's mesh identity == its container name: unique per
        (env, node) already, and it makes an `overlay.json` assignment
        legible next to the container it belongs to."""
        return self.container_name(db_id)

    async def join_mesh(self, db_id: str, firewall: FirewallRules | None = None, revision: str = "") -> str | None:
        """Put this database on the env's overlay behind `firewall` (its drawn
        SG's compiled rules). No-op returning None when the env has no Nebula
        network — i.e. no VPC on the canvas.

        `revision` is the env's security-group membership digest (field test
        4): a database is the ADMITTING member in the common case, so it is
        the one that has to re-check the sessions it already granted when a
        client loses the group that let it in. See `fabric/sidecar.py::ensure`."""
        return await self._mesh.ensure(
            self.container_name(db_id), self.mesh_member(db_id), firewall=firewall, revision=revision,
        )

    def overlay_endpoint(self, db_id: str) -> tuple[str, int] | None:
        """The SG-gated (overlay_ip, 5432) address, or None when this database
        never joined a mesh. Read-only."""
        ip = self._mesh.overlay_ip(self.mesh_member(db_id))
        return (ip, POSTGRES_PORT) if ip else None

    async def create_db(self, db_id: str, user: str, password: str, db_name: str = "postgres") -> None:
        """`db_name` is REAL (W2.7): it becomes `POSTGRES_DB`, so the database
        an `aws_db_instance`'s `db_name` names actually exists and the
        DATABASE_URL fact can point at it. Defaults to `postgres` -- the
        database the image always creates anyway -- so an existing canvas with
        no `dbName` field gets byte-identical behavior and a byte-identical
        DATABASE_URL."""
        name = self.container_name(db_id)
        if await self._rt.status(name) == "running":
            return  # idempotent: already up
        # A same-name remnant (an exited container after a `docker kill`, or a
        # `created`-but-never-started one) makes the bare `docker run` below
        # fail outright, so clear it unconditionally -- this is also exactly
        # what makes `rdsctl.converge_db_instances` able to recover a killed
        # container without a separate teardown step.
        await self._rt.stop(name)
        # ...and THIS is what makes that recovery non-destructive. The volume
        # is created before the container and outlives it, so the re-created
        # container mounts the SAME PGDATA the dead one was using and the
        # database comes back with its rows. Measured on a real container:
        # 2 rows written, `docker rm -f -v`, fresh container on this volume,
        # 2 rows read back. It is also why `POSTGRES_USER`/`POSTGRES_DB` below
        # are honoured on the FIRST boot only -- the entrypoint skips initdb
        # when PGDATA is already populated, which is correct: after a
        # `ModifyDBInstance` password change the volume's own credentials are
        # the current ones, and `set_password` put them there with a real
        # `ALTER USER`.
        await self._rt.create_volume(self.volume_name(db_id))
        await self._rt.run_container(ContainerSpec(
            name=name,
            image=POSTGRES_IMAGE,
            env={"POSTGRES_USER": user, "POSTGRES_PASSWORD": password, "POSTGRES_DB": db_name},
            ports={5432: 0},
            labels={"odin-env": self._env},
            volumes={self.volume_name(db_id): PGDATA},
        ))

    async def delete_db(self, db_id: str) -> None:
        # The mesh sidecar lives in this container's network namespace, so it
        # dies with it either way -- stopping it explicitly keeps `docker ps`
        # honest and makes the "no leftover containers" rule hold.
        await self._mesh.stop(self.container_name(db_id))
        await self._rt.stop(self.container_name(db_id))
        # The volume goes LAST, and that ordering is load-bearing rather than
        # tidy: docker refuses to remove a volume a container still references
        # (probed -- `rc 1: remove <vol>: volume is in use - [<container id>]`),
        # so removing it first would fail every delete. A DELETE is the one
        # moment the data is genuinely meant to go: surviving a container
        # replacement is the point, surviving the resource that owns it would
        # be a data leak and a disk leak both, on a machine with little
        # headroom. `remove_volume` raises rather than shrugging, so a volume
        # that could not be removed fails the delete instead of being reported
        # as a clean teardown (honesty rule 2) -- `_finish_delete` keeps the
        # record in `deleting` with the reason, and the next Apply retries.
        await self._rt.remove_volume(self.volume_name(db_id))

    async def endpoint(self, db_id: str) -> tuple[str, int] | None:
        port = await self._rt.host_port(self.container_name(db_id), 5432)
        return ("127.0.0.1", port) if port else None

    async def set_password(self, db_id: str, user: str, current_password: str, new_password: str) -> None:
        """A REAL `ALTER USER ... WITH PASSWORD` against the running Postgres
        -- what `ModifyDBInstance`'s `MasterUserPassword` has to mean if the
        DATABASE_URL fact odin publishes is to stay true (no mock-only modes).
        Raises if the DB can't be reached or the statement fails, so the
        caller can report a real RDS error instead of storing a password the
        container doesn't actually have."""
        endpoint = await self.endpoint(db_id)
        if endpoint is None:
            raise RuntimeError(f"{self.container_name(db_id)} publishes no port")
        conn = await asyncpg.connect(
            host=endpoint[0], port=endpoint[1], user=user,
            password=current_password, database="postgres", timeout=3,
        )
        try:
            # ALTER USER takes NO bound parameters -- not the role (an
            # identifier) and not the password (Postgres rejects a placeholder
            # there outright: `syntax error at or near "$1"`). So the statement
            # is built by POSTGRES ITSELF via `format('%I','%L')`, which quotes
            # an identifier and a literal by its own rules. That retires the
            # hand-rolled quote-doubling this used to do, and it is the whole
            # reason the swap is not a rename: psycopg2 bound client-side and
            # hid the restriction.
            statement = await conn.fetchval(
                "SELECT format('ALTER USER %I WITH PASSWORD %L', $1::text, $2::text)",
                user, new_password,
            )
            await conn.execute(statement)
        finally:
            await conn.close()
