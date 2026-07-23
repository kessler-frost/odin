"""The single shared Typer app instance for the `odin` CLI.

Every command — the original process-management ones in `odin/__main__.py`
(`start`/`stop`/`status`/`clean`) and the control-surface + doctor ones added
in `odin/cli/*.py` — attaches to THIS object, so `odin --help` lists all of
them together under one binary.
"""
from __future__ import annotations

import typer

app = typer.Typer(help="Odin server + control-surface CLI", no_args_is_help=True, add_completion=False)
