"""The cadence, and the ratchet against fabricating it.

.claude/CLAUDE.md honesty rule 1b exists because two e2e tests set
`ODIN_DRIFT_SWEEP_TICKS=1` and *waited for the sweep* before asserting, which
measured a guard only after the input it depends on had provably arrived and
stepped around the entire residual. Measured honestly, that residual was four
consecutive `applied`/exit-0 applies over ~8s with zero containers -- four
times worse than the prose disclosing it.

A dispatcher is more exposed to that than a sweep is, because being late is not
a late report here, it is a trigger the user calls broken. So the cadence is 1
tick and NOTHING may turn it down. The ratchet below is a grep over the repo,
not a unit assertion about a constant, because the failure mode is a test that
shortens the cadence -- and a constant cannot notice that.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest

from odin.gateway.stores import SynthStores
from odin.reconcile import dispatch
from odin.reconcile.dispatch import Dispatcher
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import ColimaRuntime
from odin.settings import ReconcileSettings
from odin.spec.models import Stack
from odin.spec.store import SpecStore

from .test_dispatch import ENV, FakeFunctions, MovableClock, seed_function, seed_rule

REPO = Path(__file__).resolve().parents[2]
ENV_VAR = "ODIN_DISPATCH_TICKS"


def test_the_default_cadence_is_one_tick_not_the_drift_sweeps_ten(monkeypatch):
    """`drift.py`'s sweep is every 10 ticks because a sweep is a REPORT. This
    is an ACTION, so it is every tick."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert dispatch._dispatch_ticks() == 1
    # The SHIPPED default, read off the settings class rather than the reader
    # above -- so an edit to `settings.py` that raises it fails here even if
    # somebody's environment happens to hold it at 1.
    assert ReconcileSettings.model_fields["dispatch_ticks"].default == 1


# An ASSIGNMENT of the variable, not a mention of it. The first version of this
# ratchet matched the bare name and immediately flagged the e2e file whose
# docstring says "`ODIN_DISPATCH_TICKS` is never set" -- a guard firing on the
# sentence that promises the thing it checks. Talking about the cadence has to
# stay free; only setting it is the finding.
_ASSIGNMENT = re.compile(rf"""setenv\s*\(\s*["']{ENV_VAR}|{ENV_VAR}["']?\s*[=:]""")


def test_no_file_in_this_repo_shortens_the_dispatch_cadence():
    """THE ratchet, and the reason it is a grep rather than an assertion about
    a constant.

    A test that sets `ODIN_DISPATCH_TICKS` is a test measuring a promptness it
    manufactured. The default is already the minimum, so the only thing anyone
    could do with this variable is make the cadence LONGER and then assert
    something about a window no user experiences -- or reach for it out of
    habit copied from the drift sweep's e2e tests, which is exactly the move
    rule 1b exists to stop. A constant cannot notice any of that; a grep can.

    `dispatch.py`'s own `os.environ.get(...)` is a READ and does not match:
    the name there is followed by a comma, not by `=` or `:`."""
    offenders = sorted(
        f"{path.relative_to(REPO).as_posix()}:{n}"
        for path in [*REPO.glob("src/**/*.py"), *REPO.glob("tests/**/*.py")]
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _ASSIGNMENT.search(line) and path.name != "test_dispatch_cadence.py"
    )
    assert not offenders, (
        f"{ENV_VAR} is ASSIGNED outside the dispatcher: {offenders}. "
        "Test at the cadence a user gets (honesty rule 1b)."
    )


async def test_a_pass_runs_on_every_tick_at_the_real_cadence(tmp_path):
    """Counted, not slept for: N ticks must produce N passes.

    This is the cadence assertion that cannot be faked by a shorter interval,
    because it does not read the interval at all -- it drives the real
    `Reconciler.tick()` and counts."""
    passes = []

    class CountingDispatcher(Dispatcher):
        async def _pass(self, stores, env, sqs_port):
            passes.append(sqs_port)
            return []

    reconciler = _reconciler(tmp_path, CountingDispatcher())
    for _ in range(5):
        await reconciler.tick()
    assert len(passes) == 5, "the dispatcher must run on EVERY tick, not on a sweep cadence"


async def test_an_apply_suspends_dispatch_without_costing_the_next_tick(tmp_path):
    """`hold()` is what /apply-full opens. A suspended tick delivers nothing and
    the tick after it delivers normally."""
    passes = []

    class CountingDispatcher(Dispatcher):
        async def _pass(self, stores, env, sqs_port):
            passes.append(sqs_port)
            return []

    reconciler = _reconciler(tmp_path, CountingDispatcher())
    async with reconciler.hold():
        await reconciler.tick()
        await reconciler.tick()
    assert passes == [], "nothing may be dispatched while an apply holds the env"

    await reconciler.tick()
    assert len(passes) == 1


class RecordingWs:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast(self, message: dict) -> None:
        self.messages.append(message)


async def test_a_verdict_for_a_label_world_does_not_carry_is_still_reported(tmp_path):
    """THE REGRESSION the e2e found, pinned.

    A rule may target a Lambda that does not exist -- real EventBridge does not
    validate that, and `PutTargets` deliberately does not either. Such a
    function has no record, so it is not in the projection, so the emit loop
    never visits its label and the verdict was silently DROPPED.

    Nothing caught it: `test_dispatch.py` asserts on the dispatcher's own
    return value, which was correct the whole time. Only the integration test
    -- which waits on what a user can actually read -- failed, after 150s of
    waiting for a report that was never coming."""
    ws = RecordingWs()
    stores = SynthStores(tmp_path / "stores")
    reconciler = _reconciler(tmp_path, _FixedDispatcher({"ghost-fn": "rule 'r' could not run: no such function"}),
                             stores=stores, ws=ws)
    await reconciler.tick()

    logs = [m for m in ws.messages if m.get("type") == "log"]
    assert logs, "a verdict the projection cannot carry must still reach the user"
    assert logs[0]["source"] == "ghost-fn"
    assert logs[0]["level"] == "error"
    assert "could not run" in logs[0]["text"]


async def test_an_unprojected_verdict_is_reported_every_time_it_happens(tmp_path):
    """NOT deduplicated, unlike `_emit`'s unchanged-status skip: each of these
    is a distinct delivery that was attempted and failed, so collapsing them
    would report one failure for a trigger that has now missed three."""
    ws = RecordingWs()
    reconciler = _reconciler(tmp_path, _FixedDispatcher({"ghost-fn": "rule 'r' could not run: no such function"}),
                             stores=SynthStores(tmp_path / "stores"), ws=ws)
    for _ in range(3):
        await reconciler.tick()
    assert len([m for m in ws.messages if m.get("type") == "log"]) == 3


class _FixedDispatcher(Dispatcher):
    """A dispatcher that always reports the same verdicts -- so the test is
    about what the RECONCILER does with them, not about how they arose."""

    def __init__(self, verdicts: dict[str, str]) -> None:
        super().__init__()
        self._fixed = verdicts

    async def verdicts(self, stores, env, sqs_port=None, dispatch=True):
        return dict(self._fixed) if dispatch else {}


@pytest.mark.integration
async def test_the_delivery_window_is_one_poll_interval(tmp_path):
    """THE MEASURED NUMBER that goes in docs/limits.md, taken at the production
    cadence with nothing shortened.

    A rule becomes due, and the question is how long until the background loop
    notices. `poll_interval=1.0` is what `server.py` wires, `ODIN_DISPATCH_TICKS`
    is untouched, and the assertion is deliberately loose (< 3s) for the reason
    .claude/CLAUDE.md gives about wall-clock bounds: a tight bound measures CI
    load rather than the code. The number this PRINTS is the one the docs
    quote."""
    fired: list[float] = []

    class TimingFunctions(FakeFunctions):
        async def invoke(self, env, name, payload):
            fired.append(time.monotonic())
            return await super().invoke(env, name, payload)

    stores = SynthStores(tmp_path / "stores")
    clock = MovableClock()
    reconciler = _reconciler(tmp_path, Dispatcher(TimingFunctions(), clock), stores=stores)
    seed_function(stores)
    seed_rule(stores, schedule="rate(1 minute)")

    await reconciler.start()
    try:
        await reconciler.tick()          # anchor the rule
        clock.advance(120)               # it is now overdue
        due_at = time.monotonic()
        deadline = due_at + 10.0
        while not fired and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
    finally:
        await reconciler.stop()

    assert fired, "a due rule must fire off the background loop with no tick() by hand"
    window = fired[0] - due_at
    print(f"\nMEASURED dispatch window at poll_interval=1.0s, ODIN_DISPATCH_TICKS unset: {window:.2f}s")
    assert window < 3.0, f"a due rule took {window:.2f}s to fire"


def _reconciler(tmp_path: Path, dispatcher, stores=None, ws=None):
    """A real `Reconciler` on the REAL production poll interval (1.0s, what
    `server.py` wires), with no `aws` and no `drift` -- so the only thing this
    loop does per tick is the projection, which is where dispatch hangs."""
    store = SpecStore(tmp_path / "spec")
    store.apply(Stack(env=ENV))
    return Reconciler(
        store, ColimaRuntime(), env=ENV, poll_interval=1.0, ws=ws,
        stores=stores or SynthStores(tmp_path / "stores"), dispatcher=dispatcher,
    )
