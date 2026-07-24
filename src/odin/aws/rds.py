"""rds nodes as direct Postgres containers via the RuntimeDriver."""
from __future__ import annotations

from odin.runtime.colima import ContainerSpec

POSTGRES_IMAGE = "postgres:16-alpine"


class PostgresRds:
    """The Reconciler's `_rds` seam. `endpoint` returns as soon as a host port
    is published — the Reconciler's `pg_ready` probe gates healthy."""

    def __init__(self, runtime, env: str = "default") -> None:
        self._rt = runtime
        self._env = env

    def container_name(self, db_id: str) -> str:
        return f"odin-rds-{self._env}-{db_id}"

    def create_db(self, db_id: str, user: str, password: str) -> None:
        name = self.container_name(db_id)
        if self._rt.status(name) == "running":
            return  # idempotent: already up
        # A same-name exited remnant makes the bare `docker run` below fail
        # outright. The Reconciler's crash observer only clears one on the
        # "crashed" path (reconciler.py `_observe_rds`) — a World reset (World
        # has no observed record, e.g. `.odin/` wiped) goes through "pending"
        # instead and skips it entirely, so create_db must clear defensively.
        self._rt.stop(name)
        self._rt.run_container(ContainerSpec(
            name=name,
            image=POSTGRES_IMAGE,
            env={"POSTGRES_USER": user, "POSTGRES_PASSWORD": password},
            ports={5432: 0},
            labels={"odin-env": self._env},
        ))

    def delete_db(self, db_id: str) -> None:
        self._rt.stop(self.container_name(db_id))

    def endpoint(self, db_id: str) -> tuple[str, int] | None:
        port = self._rt.host_port(self.container_name(db_id), 5432)
        return ("127.0.0.1", port) if port else None
