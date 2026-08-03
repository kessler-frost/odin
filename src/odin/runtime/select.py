"""Which container backend this odin runs on -- `ODIN_RUNTIME`.

odin has had two `RuntimeDriver` implementations for a long time and no way to
choose between them. `LimaRuntime` is not hypothetical: measured 2026-08-03,
`tests/runtime/test_lima_integration.py` boots a real Lima VM, runs a real
container inside it through `nerdctl`, reads its published port and tears the
VM down -- 5 passed in 49.27s. What did not exist was any way for a user to ask
for it. `server.py` said `runtime or ColimaRuntime()`, so the only door was the
programmatic `create_app(runtime=...)` seam, which is a test seam, and the
integration gate had therefore never been run against the second backend at
all.

THE MAP IS A REGISTRY, NOT A SETTING (.claude/CLAUDE.md's configuration rule).
`ODIN_RUNTIME` is the knob and it lives in `settings.py` as a typed field; the
name -> class table below is static domain knowledge -- the set of backends odin
has -- which nobody overrides with an environment variable. It is spelled here,
beside the classes, and the accepted VALUES are spelled independently as a
`Literal` in `settings.py`. That duplication is deliberate and is what
`tests/runtime/test_select.py` checks: a registry entry is a promise, and one
list generated from the other could not catch the two halves disagreeing.

DEFAULTS ARE UNCHANGED. `build_runtime()` with nothing set returns a
`ColimaRuntime()` -- the same object `server.py` built before this module
existed, constructed the same way -- so this is a new option and not a
migration.
"""
from __future__ import annotations

from odin.runtime.colima import ColimaRuntime
from odin.runtime.driver import RuntimeDriver
from odin.runtime.lima import LimaRuntime
from odin.settings import settings

# `LimaRuntime()` takes the SHARED `odin-host` VM (its `DEFAULT_VM`), which is
# the right default here and not merely the convenient one: `ODIN_RUNTIME=lima`
# asks for VM-level isolation between odin and the Mac, not between odin's own
# environments, and one VM holding every env's containers is precisely what
# Colima already is. Envs stay isolated the way they always have -- by container
# name (`odin-aws-<backing>-<env>`, `odin-rds-<env>-<id>`) -- so nothing about
# multi-env changes when the containers move inside a VM.
#
# `LimaRuntime(vm=...)` DOES name a per-instance VM, and that meaning is taken:
# `gateway/models/ecsctl.py::runtime_for_service` binds a placed ECS service to
# its EC2 node's own `odin-ec2-<env>-<id>`. That is placement, decided per
# service by the canvas, and it is not what a process-wide backend switch can
# express -- which is why this knob selects a BACKEND and never a VM name.
_BACKENDS: dict[str, type[RuntimeDriver]] = {
    "colima": ColimaRuntime,
    "lima": LimaRuntime,
}


def build_runtime() -> RuntimeDriver:
    """The `RuntimeDriver` `ODIN_RUNTIME` asks for, built fresh.

    Read at USE time, never captured at import -- the whole reason `settings`
    builds each section from the current environment (see `settings.py`'s
    module docstring). A value captured here at import would make every
    `monkeypatch.setenv("ODIN_RUNTIME", ...)` silently ineffective and the
    tests would pass for the wrong reason.

    No `KeyError` guard, deliberately: the only values that reach this line are
    the ones `ComputeSettings.runtime`'s `Literal` allows, and `create_app`
    calls `settings.validate_all()` before anything else, so an unrecognised
    `ODIN_RUNTIME` has already failed the process by name. A `.get(...,
    ColimaRuntime)` here would take that startup failure away and hand back the
    default instead -- the exact silent fallback the strict `Literal` exists to
    prevent.
    """
    return _BACKENDS[settings.compute.runtime]()
