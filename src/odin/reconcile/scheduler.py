"""Memory-aware admission control for one host.

The Brain proposes; the Scheduler is the authority on whether a workload fits.
On a single 48GB box you schedule-and-evict to fit memory, you do not scale out:
a workload whose footprint won't fit the remaining budget stays `queued` until
something frees memory. Footprint comes from an explicit `memory_mib` field or a
per-kind default.
"""
from __future__ import annotations

from odin.spec.models import ResourceDesired

DEFAULT_FOOTPRINT_MIB: dict[str, float] = {
    "service": 256.0,
    "dep": 128.0,
    "rds": 256.0,
    "batch": 256.0,
    "llm": 4096.0,
}


class Scheduler:
    def __init__(self, budget_mib: float) -> None:
        self._budget = budget_mib

    def footprint(self, res: ResourceDesired) -> float:
        if "memory_mib" in res.fields:
            return float(res.fields["memory_mib"].value)
        return DEFAULT_FOOTPRINT_MIB.get(res.kind, 256.0)

    def admits(self, res: ResourceDesired, running_mib: float) -> bool:
        return running_mib + self.footprint(res) <= self._budget

    def evict_for(
        self, res: ResourceDesired, candidates: list[ResourceDesired], running_mib: float
    ) -> list[str]:
        """LLM ids to evict (idle-LRU order from the caller) so `res` fits.

        Returns [] if `res` already fits OR eviction still can't free enough (the
        caller then queues it). Only ever evicts the provided candidates (LLMs).
        """
        deficit = (running_mib + self.footprint(res)) - self._budget
        if deficit <= 0:
            return []
        evicted: list[str] = []
        freed = 0.0
        for candidate in candidates:
            evicted.append(candidate.id)
            freed += self.footprint(candidate)
            if freed >= deficit:
                return evicted
        return []
