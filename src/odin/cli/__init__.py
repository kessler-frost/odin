"""The odin control-surface CLI (`odin canvas`, `odin apply`, `odin doctor`, …).

`app` (in `odin.cli.app`) is the one shared Typer instance every command in
this package attaches to. `odin/__main__.py` imports it (instead of defining
its own) and imports `odin.cli.commands` / `odin.cli.doctor` for their
registration side effects before running it.
"""
from __future__ import annotations
