"""Is the mesh endpoint odin ADVERTISES actually alive? (the durable fix for
field test 2's mesh class of bugs)

The systemic defect that report found: odin publishes a `*_MESH` fact -- the
SG-gated overlay address, and per ROADMAP the ONLY governed path to a
backing -- but every health probe in the product dials the published HOST
port. So two independent breakages (a sidecar stranded in a dead container's
network namespace; an env whose lighthouse never started because the
lighthouse port was machine-global) both presented identically: `healthy`
everywhere, the mesh address still advertised, and a consumer on the overlay
timing out. Nothing verified the path odin was recommending.

This module is the check that was missing. It answers ONE question per
mesh-joined resource -- "would a mesh consumer find something at the address
we publish?" -- and its answer is allowed to take a resource out of
`healthy`:

  1. does this env even have a mesh?  (a filesystem stat; an env with no VPC
     drawn pays nothing at all -- see `gate`'s early return, which is reached
     before any of this when no `*_MESH` fact is published)
  2. is the env's lighthouse process alive?  (a pidfile + signal-0 check, no
     subprocess -- discovery AND relay ride it, so a member is unreachable
     from any peer without it: fabric/nebula.py's R5 note)
  3. is the sidecar running, and in the CURRENT target's namespace?
     (`MeshSidecar.attached_to` -- HIGH-2's exact failure)
  4. does the overlay address answer?  (`assertions.mesh_ready_sync`, one
     bounded `nc -z` from inside the member's own namespace)

REPORT, DON'T HEAL -- `reconcile/drift.py`'s rule, for its reason: the
recovery is `rdsctl.ensure_db_mesh` on the next Apply (now genuinely
effective, since `MeshSidecar` re-joins a replaced target), and a projection
that restarted daemons behind the user's back would fight it. What this does
instead is refuse to keep publishing an address that doesn't answer, and say
why.

WHAT THIS STILL CANNOT SEE, and where that is handled instead. Check 4 stands
in the member's OWN namespace, so by construction it cannot observe a PEER
holding a stale tunnel to this member -- which is exactly the shape field test
3 MED-2 measured: for ~10s after a mesh restart, `/world` said `healthy` and
advertised the address while a peer's probe timed out, because that peer was
still sending into the tunnel that had just died (nebula deliberately ignores
the first few `recv_error`s before dropping a tunnel -- correct anti-DoS
behaviour, ~10s at a TCP probe's retransmit cadence). No probe run from here
could ever catch it. It is fixed at the source instead: a member that restarts
now pokes every peer into re-handshaking immediately
(`fabric/nebula.py::rehandshake_script`), so the window closes in one round
trip rather than being detected after the fact.

COST. Cached per (root, env, member) with two TTLs: a passing member is
re-checked every `ODIN_MESH_SWEEP_SECONDS` (default 30), a FAILING one every
`ODIN_MESH_RECHECK_SECONDS` (default 5) so a recovery shows up promptly
instead of being pinned crashed for half a minute. At the production 1s tick
that is ~4 subprocess calls per mesh member per 30s (~0.13/s), and EXACTLY
ZERO for a resource with no mesh fact -- `gate` returns before touching the
runtime. The cache is process-wide because the projection builds its callers
fresh every tick (`reconcile/tf_status.py`), the same reason
`fabric/nebula.py` keeps its overlay locks at module level.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from odin.fabric.nebula import LighthouseManager
from odin.fabric.sidecar import MeshSidecar
from odin.reconcile.assertions import mesh_ready_sync
from odin.runtime.colima import ColimaRuntime

log = logging.getLogger("odin.reconcile.mesh_health")

# `label -> (kind, phase, facts, verdict)`'s value -- reconcile/tf_status.py's
# `Projected` entry, which `gate` takes and returns.
Entry = tuple[str, str, dict, str | None]

_OK_SECONDS = 30.0
_FAIL_SECONDS = 5.0

_cache: dict[tuple[str, str, str], tuple[float, "MeshVerdict"]] = {}


@dataclass(frozen=True)
class MeshVerdict:
    ok: bool
    reason: str | None = None


def _ok_seconds() -> float:
    return float(os.environ.get("ODIN_MESH_SWEEP_SECONDS", _OK_SECONDS))


def _fail_seconds() -> float:
    return float(os.environ.get("ODIN_MESH_RECHECK_SECONDS", _FAIL_SECONDS))


def reset_cache() -> None:
    """Test seam only (no production caller): the cache is process-wide, so a
    test asserting the sweep cadence must be able to start from empty."""
    _cache.clear()


def check(
    root: Path, env: str, member: str, address: str,
    *, sidecar_target: str | None = None, sidecar_port: int | None = None,
    runtime=None, lighthouse: LighthouseManager | None = None, now: float | None = None,
) -> MeshVerdict:
    """The cached, cadenced answer for one mesh member. Never raises: a
    verdict is observability, and an exploding docker CLI must not take down a
    reconciler tick (`_probe` turns any failure into an honest reason)."""
    stamp = time.monotonic() if now is None else now
    key = (str(root), env, member)
    cached = _cache.get(key)
    if cached is not None and stamp < cached[0]:
        return cached[1]
    verdict = _probe(root, env, address, sidecar_target, sidecar_port, runtime, lighthouse)
    _cache[key] = (stamp + (_ok_seconds() if verdict.ok else _fail_seconds()), verdict)
    return verdict


def _probe(
    root: Path, env: str, address: str, sidecar_target: str | None, sidecar_port: int | None,
    runtime, lighthouse: LighthouseManager | None,
) -> MeshVerdict:
    rt = runtime or ColimaRuntime()
    manager = lighthouse or LighthouseManager()
    mesh = MeshSidecar(rt, env, root, lighthouse=manager)
    try:
        return _verdict(root, env, address, sidecar_target, sidecar_port, rt, mesh, manager)
    except Exception as exc:  # noqa: BLE001 -- a verdict must never fail a tick
        log.warning("mesh health check failed for %s (env %r): %s", address, env, exc)
        return MeshVerdict(ok=False, reason=f"the mesh health check itself failed: {exc}")


def _verdict(
    root: Path, env: str, address: str, sidecar_target: str | None, sidecar_port: int | None,
    runtime, mesh: MeshSidecar, lighthouse: LighthouseManager,
) -> MeshVerdict:
    """`sidecar_target`/`sidecar_port` present -- a container member odin can
    stand inside (rds and the AWS backings): the full four checks. Absent -- an
    EC2 member, whose nebula runs as a systemd unit inside a Lima VM: only the
    lighthouse is checkable at a tick's price (a `limactl shell` per VM per
    sweep is not, and an unreachable lighthouse already means no peer can find
    or relay to it, which is the honest half we can afford)."""
    if not mesh.enabled():
        # No CA -> this env has no Nebula network, so there is no overlay claim
        # to verify (and nothing published one).
        return MeshVerdict(ok=True)
    if not lighthouse.is_running(root, env):
        return MeshVerdict(ok=False, reason=(
            f"the {env!r} nebula lighthouse is not running, so no peer can find or relay to "
            f"{address} (see {Path(root) / env / 'nebula' / 'lighthouse.log'})"
        ))
    if sidecar_target is None or sidecar_port is None:
        return MeshVerdict(ok=True)
    target, port = sidecar_target, sidecar_port
    sidecar = mesh.sidecar_name(target)
    if not mesh.running(target):
        return MeshVerdict(ok=False, reason=f"the mesh sidecar {sidecar} is not running")
    if mesh.attached_to(target) is False:
        return MeshVerdict(ok=False, reason=(
            f"the mesh sidecar {sidecar} is in a REPLACED container's network namespace"
        ))
    ready = mesh_ready_sync(runtime, sidecar, address.rsplit(":", 1)[0], port)
    return MeshVerdict(ok=ready.ok, reason=ready.error)


def gate(
    entry: Entry, *, root: Path, env: str, member: str, overlay_ip: str | None,
    mesh_keys: tuple[str, ...], sidecar_target: str | None = None, sidecar_port: int | None = None,
    runtime=None, lighthouse: LighthouseManager | None = None, now: float | None = None,
) -> Entry:
    """Hold a projected resource to its OWN mesh advertisement.

    Returns the entry unchanged unless it publishes one of `mesh_keys` AND the
    overlay path behind that key is down -- in which case the mesh facts are
    withheld (odin stops handing out a dead address) and a `healthy` phase
    becomes `crashed` with the real reason. A phase that was already
    non-healthy keeps its own verdict: the mesh being down is not a better
    explanation than "the database is failed".

    The `mesh_keys`-not-published early return is what makes this free for
    every env with no mesh drawn."""
    kind, phase, facts, verdict = entry
    if not overlay_ip or not any(key in facts for key in mesh_keys):
        return entry
    address = f"{overlay_ip}:{sidecar_port}" if sidecar_port else overlay_ip
    result = check(
        root, env, member, address, sidecar_target=sidecar_target, sidecar_port=sidecar_port,
        runtime=runtime, lighthouse=lighthouse, now=now,
    )
    if result.ok:
        return entry
    withheld = {key: value for key, value in facts.items() if key not in mesh_keys}
    reason = (
        f"the published mesh address {address} is unreachable: {result.reason} "
        f"-- the SG-gated overlay path is down (any published host port is unaffected); re-Apply to re-join"
    )
    return (kind, "crashed" if phase == "healthy" else phase, withheld, verdict or reason)
