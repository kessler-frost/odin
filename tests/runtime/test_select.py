"""`ODIN_RUNTIME` really selects the container backend the app runs on.

Before this, `server.py` read `runtime or ColimaRuntime()` and no setting
existed, so `LimaRuntime` -- which is proven to work, 5 passed in 49.27s against
a real VM in `test_lima_integration.py` -- was reachable only through
`create_app(runtime=...)`, a test seam. This file is the ratchet on the door
that replaced it.

EVERY ASSERTION IS ON THE CONSTRUCTED OBJECT, never on a log line or a settings
read. `app.state.runtime` IS the object `Substrates` binds every container
substrate to, so `type(app.state.runtime) is LimaRuntime` is the property a user
actually gets; "settings.compute.runtime == 'lima'" would only prove pydantic
parses a string, which was never in doubt.

THE ACCEPTED VALUES ARE SPELLED OUT HERE BY HAND (honesty rule 5). Reading them
off `ComputeSettings.runtime`'s own `Literal`, or parametrizing over
`select._BACKENDS`, would make this file share a source with its subject: drop
`lima` from either and the case that guards it DISAPPEARS, and a test count
falling from N to N-1 reads as success. So the pairs below are literals, this
file is the second producer, and deleting a backend fails a named test instead
of quietly shrinking the run.

MUTATION-TESTED (2026-08-03), each reverted one at a time and the named test
confirmed failing:
  * `runtime or build_runtime()` -> `runtime or ColimaRuntime()`
        fails `test_odin_runtime_lima_really_builds_a_lima_runtime`
  * `runtime or build_runtime()` -> `build_runtime()`
        fails `test_an_explicitly_passed_runtime_beats_the_setting`
  * `ComputeSettings.runtime` default `"colima"` -> `"lima"`
        fails `test_the_default_backend_has_not_moved`
  * `Literal["colima", "lima"]` -> `str`
        fails `test_an_unrecognised_runtime_fails_at_startup_naming_the_variable`
  * `_BACKENDS` losing its `"lima"` entry
        fails `test_every_accepted_value_builds_its_own_backend[lima-LimaRuntime]`
        AND three others -- 4 failed, 7 passed, i.e. the run still COLLECTS 11.
        That is the property rule 5 asks for and the reason the pairs are
        literals: a file that parametrized over `_BACKENDS` would have gone
        green at 10 tests, and a test count falling by one reads as success.

ONE MUTATION SURVIVES, AND IT IS REPORTED RATHER THAN PAPERED OVER.
`_BACKENDS[...]` -> `_BACKENDS.get(..., ColimaRuntime)` passes all 11. It
survives BY CONSTRUCTION, not by a gap here: `ComputeSettings.runtime` is a
`Literal`, so an unrecognised value raises one expression earlier and the
`.get` default is unreachable. It is reachable only in combination with
loosening that `Literal` -- and the loosening half is killed on its own by
`test_an_unrecognised_runtime_fails_at_startup_naming_the_variable`. Measured
directly with `ODIN_RUNTIME=limaa`: `Literal`+subscript raises ValidationError,
`str`+subscript raises KeyError (still loud), and only `str`+`.get` silently
returns a `ColimaRuntime`. No test is written for the third row, because a test
that has to break two things to observe one would pin the mutant rather than
the behaviour.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from odin.runtime.colima import ColimaRuntime
from odin.runtime.lima import LimaRuntime
from odin.runtime.select import build_runtime
from odin.server import create_app
from odin.spec.store import SpecStore


@pytest.fixture(autouse=True)
def _no_ambient_runtime(monkeypatch):
    """The suite must not inherit a developer's own `ODIN_RUNTIME`.

    Without this, `test_the_default_backend_has_not_moved` would pass or fail
    according to the shell it ran in -- and the failure mode that matters (it
    passes on a machine that happens to export `colima`) is invisible.
    """
    monkeypatch.delenv("ODIN_RUNTIME", raising=False)


def _app_runtime(tmp_path, **kwargs):
    """The runtime a real `create_app` ended up with.

    `store=` keeps the app off the production `.odin` tree (and, as a side
    effect `create_app` documents, turns the startup EC2 reaper off), and
    `backings=False` keeps the reconciler's real substrates out of it. Nothing
    here enters the app's lifespan, so no container, VM or subprocess is
    created: `LimaRuntime()` boots a VM in `ensure_host()`, not in `__init__`.
    """
    app = create_app(store=SpecStore(tmp_path), backings=False, **kwargs)
    return app.state.runtime


# ---------------------------------------------------------------- the switch

def test_odin_runtime_lima_really_builds_a_lima_runtime(tmp_path, monkeypatch):
    """PROOF 1. The whole point: a user can now choose the second backend."""
    monkeypatch.setenv("ODIN_RUNTIME", "lima")

    runtime = _app_runtime(tmp_path)

    assert type(runtime) is LimaRuntime, (
        f"ODIN_RUNTIME=lima built a {type(runtime).__name__}; the setting is not wired "
        "into create_app"
    )
    # Not merely the right class -- the right VM. A LimaRuntime pointed at some
    # other instance's VM would satisfy the type check and run odin's backings
    # inside an EC2 node.
    assert runtime.VM == "odin-host"
    assert runtime.CLI == "nerdctl"


def test_the_default_backend_has_not_moved(tmp_path):
    """PROOF 2. This is a new option, not a migration: with nothing set the
    constructed object is the same `ColimaRuntime` it has always been."""
    runtime = _app_runtime(tmp_path)

    assert type(runtime) is ColimaRuntime, (
        f"with ODIN_RUNTIME unset create_app built a {type(runtime).__name__}; the "
        "default moved, which this change was not allowed to do"
    )
    assert runtime.CLI == "docker"


def test_an_explicitly_passed_runtime_beats_the_setting(tmp_path, monkeypatch):
    """PROOF 3. `create_app(runtime=...)` is the INJECTION seam -- the thing
    `tests/api/test_apply_full_isolation.py` builds a hermetic app with. If the
    ambient environment could take an injected fake back, a developer with
    `ODIN_RUNTIME=lima` exported would silently run the fake-runtime tests
    against a real Lima VM."""
    monkeypatch.setenv("ODIN_RUNTIME", "lima")
    injected = ColimaRuntime()

    runtime = _app_runtime(tmp_path, runtime=injected)

    assert runtime is injected, (
        "ODIN_RUNTIME overrode an explicitly injected runtime; the argument must win"
    )


def test_the_setting_is_read_at_call_time_not_captured_at_import(tmp_path, monkeypatch):
    """The trap `settings.py` exists to avoid, at this switch specifically: two
    apps built in ONE process, from one import, must honour the environment as
    it stood at each call. A backend resolved once at import would make the
    second of these silently wrong -- and every `monkeypatch.setenv` above
    would be passing for the wrong reason."""
    monkeypatch.setenv("ODIN_RUNTIME", "lima")
    first = _app_runtime(tmp_path)
    monkeypatch.setenv("ODIN_RUNTIME", "colima")
    second = _app_runtime(tmp_path)

    assert (type(first), type(second)) == (LimaRuntime, ColimaRuntime)


# ------------------------------------------------------- loud on a bad value

def test_an_unrecognised_runtime_fails_at_startup_naming_the_variable(tmp_path, monkeypatch):
    """STRICT, not lenient -- and deliberately the opposite of `ODIN_AI` and
    friends, which must fail SAFE on a typo because they have a dangerous
    direction. Neither backend is the dangerous one, so there is nothing to
    fall back to: reading `limaa` as `colima` would hand a user who asked for VM
    isolation containers on the host and say nothing.

    The message is asserted in full because it is the entire user-facing
    contract of a rejected value."""
    monkeypatch.setenv("ODIN_RUNTIME", "limaa")

    with pytest.raises(ValidationError) as caught:
        _app_runtime(tmp_path)

    message = str(caught.value)
    assert "ODIN_RUNTIME" in message, f"the error does not name the variable: {message}"
    assert "Input should be 'colima' or 'lima'" in message, (
        f"the error does not spell out the accepted values: {message}"
    )
    assert "limaa" in message, f"the error does not quote what was typed: {message}"


def test_a_bad_runtime_is_rejected_before_any_backend_is_built(tmp_path, monkeypatch):
    """`create_app` calls `settings.validate_all()` first, so the failure is a
    startup validation error and not a `KeyError` from the backend map. The
    distinction is the whole reason `build_runtime` has no `.get(...)` default:
    one names the variable, the other names an internal dict."""
    monkeypatch.setenv("ODIN_RUNTIME", "podman")

    with pytest.raises(ValidationError):
        _app_runtime(tmp_path)


def test_the_values_are_lower_case(tmp_path, monkeypatch):
    """Measured, not assumed: pydantic rejects `LIMA` rather than coercing it.
    Documented in `docs/config.md` because a user who exports the upper-case
    form deserves to learn it here rather than from a stack trace."""
    monkeypatch.setenv("ODIN_RUNTIME", "LIMA")

    with pytest.raises(ValidationError, match="ODIN_RUNTIME"):
        _app_runtime(tmp_path)


# ------------------------------------------- the registry keeps its promises

# Written out by hand -- see the module docstring. `settings.py` spells the
# accepted values as a `Literal` and `runtime/select.py` spells the classes as a
# map; this is the third, independent copy, and its job is to fail when those
# two disagree with each other or with what is documented.
ACCEPTED = {"colima": ColimaRuntime, "lima": LimaRuntime}


@pytest.mark.parametrize(
    ("value", "expected"), [("colima", ColimaRuntime), ("lima", LimaRuntime)],
    ids=["colima-ColimaRuntime", "lima-LimaRuntime"],
)
def test_every_accepted_value_builds_its_own_backend(value, expected, monkeypatch):
    """A registry entry is a PROMISE. `ODIN_RUNTIME=lima` claims you get a
    `LimaRuntime`; a map that had lost the entry would raise `KeyError` at
    startup, and one that pointed both names at the same class would be a knob
    that does nothing."""
    monkeypatch.setenv("ODIN_RUNTIME", value)

    assert type(build_runtime()) is expected


def test_the_two_backends_are_not_the_same_class():
    """The failure the parametrized cases above cannot see individually: a map
    of `{"colima": ColimaRuntime, "lima": ColimaRuntime}` passes one of them and
    makes the setting a no-op."""
    assert ACCEPTED["colima"] is not ACCEPTED["lima"]


def test_settings_accepts_exactly_these_values_and_no_others(monkeypatch):
    """The set of legal values, from the OUTSIDE. Every name in `ACCEPTED` must
    be accepted, and a name that is not in it must be refused -- so adding a
    backend to `settings.py` without a class behind it, or a class without a
    settings value in front of it, fails here.

    `build_runtime()` is the oracle rather than the `Literal` itself: it is the
    only thing that proves a value both validates AND has a backend."""
    for value in ACCEPTED:
        monkeypatch.setenv("ODIN_RUNTIME", value)
        assert type(build_runtime()) is ACCEPTED[value]

    for absent in ("podman", "docker", "nerdctl", "colima ", ""):
        monkeypatch.setenv("ODIN_RUNTIME", absent)
        if absent == "":
            # `env_ignore_empty=True`: an empty export means UNSET, repo-wide.
            assert type(build_runtime()) is ColimaRuntime
            continue
        with pytest.raises(ValidationError):
            build_runtime()
