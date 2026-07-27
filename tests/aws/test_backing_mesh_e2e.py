"""W2.6 pieces 2+3, container level: an UNMODIFIED upstream `postgres:16-alpine`
becomes a real Nebula mesh member, and the drawn `db-sg` compiled onto it
really gates a REAL Postgres connection over the overlay.

What runs for real here: a real host `nebula` lighthouse (unprivileged), a
real `nebula` daemon in a companion container sharing the Postgres container's
network namespace, real `nebula-cert` PKI, and real `psql` queries from two
other mesh members -- one whose certificate carries the `sg-web` group the
`db-sg` rule names, one whose doesn't. Same database, same port, same instant:
one gets a row back, the other cannot open a connection at all.

The VM half of the same proof (a real Lima VM instead of a client container)
is tests/simulate/test_sg_gates_backing_e2e.py; this one is the fast,
no-VM-needed proof of the mechanism itself, and it also pins the
NON-NEGOTIABLE compatibility constraint: the published HOST port keeps
working (a host-side `pg_ready` still succeeds), because the overlay is an
ADDITIONAL path, not a replacement.

Store root: deliberately under the repo tree, NOT `tmp_path`. Colima only
shares $HOME into its VM, so a `/private/var/folders/...` bind mount comes up
EMPTY inside the container (the same constraint `BackingAws`'s goaws config
mount already lives with) -- and the sidecar reads its cert/config from a bind
mount. `mesh_root` cleans up after itself.
"""
from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from odin.aws.rds import PostgresRds
from odin.fabric.models import FirewallRule, FirewallRules
from odin.fabric.nebula import LighthouseManager, ensure_network
from odin.fabric.sidecar import MeshSidecar, underlay_ip
from odin.reconcile.assertions import pg_ready
from odin.runtime.colima import ColimaRuntime, ContainerSpec

pytestmark = pytest.mark.integration

ENV = "backing-mesh-e2e"
PG_IMAGE = "postgres:16-alpine"
PASSWORD = "apppass123"

# The `db-sg` a canvas draws: 5432, and ONLY from the web tier. `sg_rules_to_
# firewall` compiles an AWS UserIdGroupPairs rule to exactly this shape.
DB_SG_FIREWALL = FirewallRules(
    inbound=[FirewallRule(port="5432", proto="tcp", group="sg-web")],
    outbound=[FirewallRule(port="any", proto="any")],
)


@pytest.fixture
def mesh_root(tmp_path_factory):
    """A throwaway store root INSIDE the repo tree (see module docstring)."""
    root = Path(".odin-mesh-it") / secrets.token_hex(4)
    root.mkdir(parents=True)
    yield root.resolve()
    shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture
def containers():
    """Exact-name teardown for every container this test starts (backings AND
    their mesh sidecars), so a failure leaves nothing behind."""
    names: list[str] = []
    yield names
    for name in reversed(names):
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)


@pytest.fixture
def lighthouse_cleanup():
    envs: list[tuple] = []
    yield envs
    for root, env in envs:
        LighthouseManager().ensure_stopped(root, env)


async def _client(runtime: ColimaRuntime, mesh: MeshSidecar, name: str, group: str, containers: list[str]) -> None:
    """A mesh member that is just a psql client: the SAME postgres image (so
    `psql` is present) doing nothing, plus its own nebula sidecar whose cert
    carries `group`."""
    await runtime.stop(name)
    await runtime.run_container(ContainerSpec(
        name=name, image=PG_IMAGE, command=("sleep", "infinity"), labels={"odin-env": ENV},
    ))
    containers.append(name)
    containers.append(mesh.sidecar_name(name))
    assert await mesh.ensure(name, name, groups=(group,)) is not None, f"{name} never joined the mesh"


def _psql(client: str, host: str) -> subprocess.CompletedProcess:
    # `-d postgres` explicitly: that's the database the substrate really creates
    # (`create_db`'s `db_name` default, W2.7) and the one the DATABASE_URL fact
    # points at -- psql would otherwise default the dbname to the USER.
    return subprocess.run(
        ["docker", "exec", "-e", f"PGPASSWORD={PASSWORD}", "-e", "PGCONNECT_TIMEOUT=8", client,
         "psql", "-h", host, "-U", "app", "-d", "postgres", "-tAc", "select 'overlay-ok'"],
        capture_output=True, text=True, timeout=60,
    )


async def test_a_drawn_sg_gates_real_postgres_traffic_over_the_overlay(mesh_root, containers, lighthouse_cleanup):
    assert shutil.which("docker"), "docker (Colima) required"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"

    runtime = ColimaRuntime()
    lighthouse_cleanup.append((mesh_root, ENV))
    # What CreateVpc does when a canvas draws a VPC: the env's CA + overlay.
    await ensure_network(mesh_root, ENV, underlay_ip())

    rds = PostgresRds(runtime, ENV, root=mesh_root)
    containers.append(rds.container_name("db"))
    containers.append(f"{rds.container_name('db')}-mesh")
    await rds.create_db("db", "app", PASSWORD)

    db_ip = await rds.join_mesh("db", DB_SG_FIREWALL)
    assert db_ip, "the database never joined the env's mesh"
    assert rds.overlay_endpoint("db") == (db_ip, 5432)
    print(f"[W2.6-p2] postgres:16-alpine (unmodified) is on the mesh at {db_ip}, gated by db-sg")

    # The NON-NEGOTIABLE constraint: the published host port still works, so
    # the gateway's forwarding, the RDS model's own create-waiter probe, and
    # host-side clients are untouched by mesh membership.
    host, port = await rds.endpoint("db")
    ready = None
    for _ in range(60):
        ready = await pg_ready(host, port, "app", PASSWORD)
        if ready.ok:
            break
    assert ready.ok, f"the HOST path must keep working: {ready.error}"
    print(f"[W2.6-p2] host path still live at {host}:{port} (pg_ready ok)")

    mesh = MeshSidecar(runtime, ENV, mesh_root)
    await _client(runtime, mesh, f"odin-mesh-web-{ENV}", "sg-web", containers)
    await _client(runtime, mesh, f"odin-mesh-other-{ENV}", "sg-other", containers)

    # The allowed side polls: both sidecars must still handshake with the
    # lighthouse and set up a relayed tunnel after `ensure` returned.
    allowed = None
    for _ in range(20):
        allowed = _psql(f"odin-mesh-web-{ENV}", db_ip)
        if allowed.returncode == 0:
            break
    print(f"[W2.6-p2] psql {db_ip} from the sg-web member: rc={allowed.returncode} {allowed.stdout.strip()!r}")
    assert allowed.returncode == 0, f"the sg-web member must reach the DB over the overlay:\n{allowed.stderr}"
    assert "overlay-ok" in allowed.stdout

    denied = _psql(f"odin-mesh-other-{ENV}", db_ip)
    print(f"[W2.6-p2] psql {db_ip} from the sg-other member: rc={denied.returncode} {denied.stderr.strip()[:120]!r}")
    assert denied.returncode != 0, "a member outside db-sg must NOT reach the database over the overlay"
    assert "overlay-ok" not in denied.stdout

    # Leaving the mesh is real too: the sidecar goes away with the database.
    await rds.delete_db("db")
    assert await runtime.status(f"{rds.container_name('db')}-mesh") == "absent"
