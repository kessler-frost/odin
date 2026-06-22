"""M3 — memory-aware admission control."""
from __future__ import annotations

from odin.reconcile.scheduler import Scheduler
from odin.spec.models import FieldValue, ResourceDesired


def _res(kind, **fields):
    return ResourceDesired(id="x", kind=kind,
                           fields={k: FieldValue(value=v) for k, v in fields.items()})


def test_footprint_default_and_override():
    sched = Scheduler(budget_mib=1000)
    assert sched.footprint(_res("dep")) == 128.0          # per-kind default
    assert sched.footprint(_res("service", memory_mib=512)) == 512.0  # explicit


def test_admits_within_budget_only():
    sched = Scheduler(budget_mib=1000)
    res = _res("service")  # 256
    assert sched.admits(res, running_mib=700) is True     # 700+256 <= 1000
    assert sched.admits(res, running_mib=800) is False    # 800+256 > 1000
