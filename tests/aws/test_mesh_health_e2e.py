"""The mesh-path assertion, against a REAL nebula overlay (container level).

Field test 2's mesh class of bugs came from one gap: nothing ever checked the
overlay path odin publishes. This proves the check itself works on the real
thing -- a real host lighthouse, a real `nebula` sidecar in a real Postgres
container's network namespace, real certs -- and that it catches the exact
failure the field found:

  1. a healthy member's published overlay address answers  (`mesh_ready_sync`)
  2. `docker kill` + recreate the database -> the sidecar is stranded in the
     DEAD container's namespace, `attached_to` says so, and the probe FAILS
     while the host port is perfectly fine (HIGH-2, the state odin used to
     report as `healthy`)
  3. one re-join heals it: the sidecar is re-created against the LIVE
     container and the overlay answers again  (the fix)
  4. with the env's lighthouse stopped, the verdict says so rather than
     claiming the mesh is fine (B8)

The VM-level proof -- the same recovery seen from inside a real Lima VM over
the overlay, through /world and a real Apply -- is
tests/simulate/test_mesh_recovery_e2e.py. This one needs no VM at all.

Store root: under the repo tree, NOT `tmp_path` (Colima shares only $HOME, and
the sidecar reads its config from a bind mount -- see
tests/aws/test_backing_mesh_e2e.py's note).
"""
from __future__ import annotations

import asyncio
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from odin.aws.rds import POSTGRES_PORT, PostgresRds
from odin.fabric.models import FirewallRule, FirewallRules
from odin.fabric.nebula import LighthouseManager, ensure_network, mesh_state
from odin.fabric.sidecar import MeshSidecar, underlay_ip
from odin.reconcile import mesh_health
from odin.reconcile.assertions import mesh_ready_sync, pg_ready
from odin.runtime.colima import ColimaRuntime

pytestmark = pytest.mark.integration

ENV = "mesh-health-e2e"
PASSWORD = "apppass123"
DB_SG_FIREWALL = FirewallRules(
    inbound=[FirewallRule(port="5432", proto="tcp", group="sg-web")],
    outbound=[FirewallRule(port="any", proto="any")],
)


@pytest.fixture
def mesh_root():
    root = Path(".odin-mesh-it") / secrets.token_hex(4)
    root.mkdir(parents=True)
    yield root.resolve()
    shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture
def containers():
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


def _wait_probe(runtime, sidecar: str, ip: str, want: bool, timeout: float = 45.0):
    """Poll the real probe until it agrees with `want` (or give up and return
    the last answer). The overlay needs a moment after a (re-)join: nebula
    creates its tun device and handshakes with the lighthouse asynchronously."""
    deadline = time.monotonic() + timeout
    result = mesh_ready_sync(runtime, sidecar, ip, POSTGRES_PORT)
    while time.monotonic() < deadline and result.ok is not want:
        time.sleep(1.5)
        result = mesh_ready_sync(runtime, sidecar, ip, POSTGRES_PORT)
    return result


def test_the_mesh_probe_catches_a_stranded_sidecar_and_clears_after_a_re_join(
    mesh_root, containers, lighthouse_cleanup,
):
    assert shutil.which("docker"), "docker (Colima) required"
    assert shutil.which("nebula") and shutil.which("nebula-cert"), "brew install nebula (MIT) required"

    runtime = ColimaRuntime()
    lighthouse_cleanup.append((mesh_root, ENV))
    ensure_network(mesh_root, ENV, underlay_ip())  # what CreateVpc does
    port = mesh_state(mesh_root, ENV).lighthouse_port
    print(f"[mesh-health] env {ENV!r} owns lighthouse UDP {port}")
    assert port is not None, "every env must record its OWN lighthouse port"

    rds = PostgresRds(runtime, ENV, root=mesh_root)
    target = rds.container_name("db")
    sidecar = f"{target}-mesh"
    containers += [target, sidecar]
    rds.create_db("db", "app", PASSWORD)
    db_ip = rds.join_mesh("db", DB_SG_FIREWALL)
    assert db_ip, "the database never joined the env's mesh"

    # 1. the healthy case: the address odin PUBLISHES really answers.
    live = _wait_probe(runtime, sidecar, db_ip, want=True)
    print(f"[mesh-health] live overlay probe {db_ip}:{POSTGRES_PORT} -> ok={live.ok} err={live.error!r}")
    assert live.ok, f"the published mesh endpoint must answer on the overlay: {live.error}"
    assert mesh_health.check(mesh_root, ENV, target, f"{db_ip}:{POSTGRES_PORT}",
                             sidecar_target=target, sidecar_port=POSTGRES_PORT).ok

    # 2. HIGH-2: the database is killed and comes back as a NEW container,
    #    which is what the documented recovery (`converge_db_instances`) does.
    old_id = runtime.container_id(target)
    subprocess.run(["docker", "kill", target], capture_output=True, timeout=60)
    rds.create_db("db", "app", PASSWORD)  # the recovery: a NEW container, same name
    new_id = runtime.container_id(target)
    assert new_id and new_id != old_id, "the recovery must really replace the container"
    mesh = MeshSidecar(runtime, ENV, mesh_root)
    assert mesh.attached_to(target) is False, "the sidecar is now in the DEAD container's namespace"

    host, host_port = rds.endpoint("db")
    ready = None
    for _ in range(40):
        ready = asyncio.run(pg_ready(host, host_port, "app", PASSWORD))
        if ready.ok:
            break
        time.sleep(1.0)  # the replacement Postgres is still running initdb
    assert ready.ok, f"the HOST port is fine -- which is exactly why nothing noticed: {ready.error}"

    stranded = _wait_probe(runtime, sidecar, db_ip, want=False)
    print(f"[mesh-health] stranded overlay probe -> ok={stranded.ok} err={stranded.error!r}")
    assert stranded.ok is False, "a stranded sidecar's overlay address must NOT read as reachable"
    mesh_health.reset_cache()
    verdict = mesh_health.check(mesh_root, ENV, target, f"{db_ip}:{POSTGRES_PORT}",
                               sidecar_target=target, sidecar_port=POSTGRES_PORT)
    assert verdict.ok is False and "REPLACED" in verdict.reason, verdict

    # 3. the fix: one re-join (what every Apply now does) restores the mesh.
    assert rds.join_mesh("db", DB_SG_FIREWALL) == db_ip, "the overlay IP is sticky across recreation"
    assert mesh.attached_to(target) is True
    healed = _wait_probe(runtime, sidecar, db_ip, want=True)
    print(f"[mesh-health] after ONE re-join -> ok={healed.ok} err={healed.error!r}")
    assert healed.ok, f"the mesh endpoint must work again after a re-join: {healed.error}"

    # 4. B8: a dead lighthouse is a verdict, not silence.
    LighthouseManager().ensure_stopped(mesh_root, ENV)
    mesh_health.reset_cache()
    dark = mesh_health.check(mesh_root, ENV, target, f"{db_ip}:{POSTGRES_PORT}",
                             sidecar_target=target, sidecar_port=POSTGRES_PORT)
    print(f"[mesh-health] lighthouse stopped -> ok={dark.ok} reason={dark.reason!r}")
    assert dark.ok is False and "lighthouse is not running" in dark.reason
    mesh_health.reset_cache()
