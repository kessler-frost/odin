"""RDS-via-MiniStack backend the Reconciler uses to create + observe databases.

Create boots a real Postgres (the `rds._docker` shim routes the spawn to
allfather's runtime); the container is named `ministack-rds-{id}`.
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from odin.aws.embed import ACCOUNT_ID, ministack_boto_client
from odin.runtime.colima import ContainerSpec


class MiniStackRds:
    def __init__(self, account_id: str = ACCOUNT_ID) -> None:
        self._account = account_id

    def create_db(self, db_id: str, user: str, password: str) -> None:
        rds = ministack_boto_client("rds", self._account)
        try:
            rds.create_db_instance(
                DBInstanceIdentifier=db_id,
                Engine="postgres",
                DBInstanceClass="db.t3.micro",
                AllocatedStorage=20,
                MasterUsername=user,
                MasterUserPassword=password,
            )
        except ClientError as exc:
            if "AlreadyExists" not in str(exc):
                raise

    def delete_db(self, db_id: str) -> None:
        """Remove MiniStack's RDS record so a later create boots a fresh DB."""
        rds = ministack_boto_client("rds", self._account)
        try:
            rds.delete_db_instance(DBInstanceIdentifier=db_id, SkipFinalSnapshot=True)
        except ClientError:
            pass

    def endpoint(self, db_id: str) -> tuple[str, int] | None:
        rds = ministack_boto_client("rds", self._account)
        try:
            inst = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
        except ClientError:
            return None
        if inst.get("DBInstanceStatus") != "available":
            return None
        endpoint = inst.get("Endpoint")
        return (endpoint["Address"], int(endpoint["Port"])) if endpoint else None

    def container_name(self, db_id: str) -> str:
        return f"ministack-rds-{db_id}"


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
        # Any exited remnant is already cleared by the Reconciler's crash
        # observer (which calls delete_db before the next create), so a
        # fresh boot never collides with a stale container of this name.
        name = self.container_name(db_id)
        if self._rt.status(name) == "running":
            return  # idempotent: already up
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
