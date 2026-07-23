"""Registration aggregator for the control-surface CLI.

Importing this module attaches every control-surface command to the ONE
shared Typer instance in `odin.cli.app` — `odin/__main__.py` imports it for
exactly that side effect.
"""
from __future__ import annotations

from odin.cli import (  # noqa: F401  (registration side effects)
    apply,
    canvas,
    observe,
    tf,
    translate,
)
