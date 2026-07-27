"""Shared test fixtures. (The old Moto/OpenTofu fixtures were retired with that
path; odin tests use the Spec Store + real Colima backings directly.)"""
from __future__ import annotations

import os

# server.py's lifespan now always starts the gateway's real uvicorn listener
# (G3). Every `create_app()` call that doesn't pass an explicit
# `gateway_port` reads this env var -- default it to an ephemeral port so
# the wider suite never binds the real 0.0.0.0:4266 (port collisions across
# tests, a stray firewall prompt). `setdefault` leaves a deliberately-set
# value (CI, a developer testing the real port) untouched.
os.environ.setdefault("ODIN_GATEWAY_PORT", "0")


# --- a forgotten `await` must FAIL, not pass vacuously ----------------------
#
# The de-threading work (v0.7.7) turned ~180 defs into coroutines, and a
# coroutine object is TRUTHY -- so `assert aws.exists(...)` with a missing
# `await` passes while running none of the body. A vacuously green suite is
# this repo's worst failure mode, so the suite has to refuse it.
#
# What does NOT work, probed rather than assumed (all three measured to exit 0
# on a deliberately un-awaited coroutine):
#   * `filterwarnings = error:coroutine .* was never awaited` in pyproject
#   * `-W error::RuntimeWarning` on the command line
#   * either of those plus a `gc.collect()` in teardown
# The reason is that CPython emits this warning from the coroutine's
# DEALLOCATOR. Turning a warning into an exception there makes it UNRAISABLE:
# it gets printed and swallowed, because no exception can propagate out of a
# deallocation. So the filter is real, it fires, and it still cannot fail a
# test.
#
# CAVEAT, so the failure is not misread: the orphan is blamed on whichever
# test was running when it was COLLECTED, which is not always the test that
# created it -- a failing test's traceback keeps its frames alive, so its
# orphan can surface in a later test's teardown. The coroutine is named in the
# message, so trust the NAME over the test it is attached to.
#
# Recording the warnings and failing explicitly afterwards side-steps that
# entirely -- the deallocator only has to append to a list, and the failure is
# raised from fixture teardown where it CAN propagate. Mutation-tested: a
# deliberately un-awaited coroutine fails with this fixture and passes without
# it.
import gc  # noqa: E402
import warnings  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fail_on_unawaited_coroutines():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield
        gc.collect()  # force orphaned coroutines to finalize while still recording
    orphans = sorted({
        str(w.message) for w in caught
        if issubclass(w.category, RuntimeWarning) and "was never awaited" in str(w.message)
    })
    if orphans:
        pytest.fail("forgotten `await` -- " + "; ".join(orphans), pytrace=False)
