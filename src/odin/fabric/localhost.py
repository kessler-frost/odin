"""Localhost fabric: resolve `${{node.VAR}}` references from observed World facts.

For the skeleton, inter-node addressing is just loopback host ports. A producer
publishes its address as a World fact (e.g. `facts["DATABASE_URL"]`) when it goes
healthy; `resolve()` reads it. A reference to a not-yet-healthy producer raises
`Unresolved`, which the Reconciler turns into a deterministic `blocked` phase
(never a silent empty value). Nebula/`*.local`/tailscale are later milestones
behind this same interface.
"""
from __future__ import annotations

from odin.spec.models import Ref, World


class Unresolved(Exception):
    pass


class LocalhostFabric:
    def resolve(self, ref: Ref, world: World) -> str:
        target = world.get(ref.target_id)
        if target is None or target.phase != "healthy":
            raise Unresolved(f"{ref.target_id} is not healthy")
        value = target.facts.get(ref.target_attr)
        if value is None:
            raise Unresolved(f"{ref.target_id} exposes no {ref.target_attr}")
        return str(value)

    def localhost_endpoint(self, host_port: int, scheme: str = "tcp") -> str:
        return f"{scheme}://127.0.0.1:{host_port}"
