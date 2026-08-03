"""Configuration lives in `settings.py`, and nowhere else reads the environment.

The tenth ratchet. It exists for the same reason as `test_thread_inventory.py`:
the state this replaced was 28 `ODIN_*` variables read directly in 19 files,
each with its own ad-hoc parse and none validated -- and the only thing that
would have stopped a 29th appearing was somebody remembering. Prose cannot fail
a build.

TWO ASSERTIONS, AND THEY CATCH DIFFERENT THINGS.

1. **No module outside `settings.py` reads an `ODIN_*` variable from the
   environment.** This is the ratchet proper: it is what a new knob added
   "just here, just this once" trips.

2. **The full inventory of variable names is spelled out below as literals.**
   Honesty rule 5: a check that derives its expectation from the thing it is
   checking cannot fail. Reading the names off `settings.py`'s own fields would
   pass just as happily if a variable were renamed, dropped, or its legacy
   alias deleted -- which is precisely the regression that would break a user's
   shell profile silently. So the names are written out here by hand, and this
   file is the second producer.

   `ODIN_MEMORY_BUDGET_MIB` is the one that most needs it: it is the ORIGINAL
   name of the container memory budget, it is in ROADMAP and in `odin doctor`'s
   output and in whatever CI jobs already export it, and it survives only as an
   alias. An alias has no field of its own to notice its absence.

NOT IN THE INVENTORY, deliberately -- both are documented in `settings.py`:
`ODIN_URL` (typer reads it via `envvar=`, so it never reaches `os.environ` in
odin's code) and `ODIN_KEEP_IT_ARTIFACTS` (a test-harness switch, read only
under `tests/`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from odin.settings import env_names, settings

SRC = Path(__file__).resolve().parents[1] / "src" / "odin"

# Every `ODIN_*` variable odin's own code honours, written out by hand -- see
# the docstring for why this list is NOT generated from `settings.py`.
INVENTORY: set[str] = {
    # GatewaySettings
    "ODIN_GATEWAY_PORT",
    "ODIN_REAP_EC2_VMS",
    "ODIN_ECS_STEADY_TIMEOUT",
    "ODIN_LAMBDA_ACTIVE_TIMEOUT",
    "ODIN_RDS_AVAILABLE_TIMEOUT",
    # ReconcileSettings
    "ODIN_DISPATCH_TICKS",
    "ODIN_DRIFT_SWEEP_TICKS",
    # SimulateSettings
    "ODIN_TOFU_TIMEOUT",
    "ODIN_TOFU_DESTROY_TIMEOUT",
    "ODIN_TOFU_PARALLELISM",
    # ComputeSettings
    "ODIN_BOOT_TIMEOUT",
    "ODIN_MAX_CONCURRENT_VM_BOOTS",
    "ODIN_MIN_DISK_GIB",
    "ODIN_CONTAINER_MEMORY_BUDGET_MIB",
    "ODIN_MEMORY_BUDGET_MIB",  # the legacy alias -- it has no field to speak for it
    "ODIN_VM_MEMORY_BUDGET_MIB",
    # MeshSettings
    "ODIN_LIGHTHOUSE_PORT",
    "ODIN_BACKING_MESH",
    "ODIN_MESH_UNDERLAY",
    "ODIN_MESH_SWEEP_SECONDS",
    "ODIN_MESH_RECHECK_SECONDS",
    # AiSettings
    "ODIN_AI",
    "ODIN_TRANSLATE_REFINE",
    "ODIN_TRANSLATE_TIMEOUT",
    "ODIN_DEBUG_AGENT",
    "ODIN_DEBUG_TIMEOUT",
    "ODIN_CHAT_TIMEOUT",
}

# An `ODIN_*` name on a line that also touches `os.environ`/`getenv` -- the
# shape every reader this module replaced had. Anchored on the read, not on the
# bare name, so a docstring naming a variable stays free (the same distinction
# `test_dispatch_cadence.py`'s ratchet had to make).
_DIRECT_READ = re.compile(r"(os\.environ|getenv)[^\n]*ODIN_[A-Z0-9_]|ODIN_[A-Z0-9_]*[^\n]*(os\.environ|getenv)")


def test_no_module_outside_settings_reads_an_odin_variable_from_the_environment():
    offenders = sorted(
        f"{path.relative_to(SRC).as_posix()}:{n}"
        for path in SRC.rglob("*.py")
        if path.name != "settings.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _DIRECT_READ.search(line)
    )
    assert not offenders, (
        f"an ODIN_* variable is read straight from the environment outside settings.py: "
        f"{offenders}. Add a typed field to the right section in src/odin/settings.py "
        "instead -- a knob nobody can find is the state this ratchet exists to prevent."
    )


@pytest.mark.parametrize("variable", sorted(INVENTORY))
def test_every_documented_variable_still_reaches_a_settings_field(variable):
    """Each name in the inventory is honoured by SOME field on SOME section.

    Deleting a field, renaming one, or dropping a legacy alias fails here --
    and it fails per-variable, so the failure names the variable that stopped
    working rather than a set difference.
    """
    honoured = {
        name
        for section in settings.sections()
        for field in type(section).model_fields
        for name in env_names(type(section), field)
    }
    assert variable in honoured, (
        f"{variable} is documented as a working odin knob and no settings field reads it "
        "any more. Every existing variable name must keep working -- this was a refactor, "
        "not a rename."
    )


def test_the_inventory_has_no_extras_nobody_wrote_down():
    """The other direction: a field added to `settings.py` without being added
    here is a knob with no independent record of what it is called."""
    honoured = {
        name
        for section in settings.sections()
        for field in type(section).model_fields
        for name in env_names(type(section), field)
    }
    assert honoured - INVENTORY == set(), (
        f"settings.py honours variables this inventory does not list: {sorted(honoured - INVENTORY)}. "
        "Add them here (and to docs/config.md) so there is a record outside the code."
    )
