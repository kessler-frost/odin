"""CLI test fixtures: a CliRunner against the fully-registered shared app."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

import odin.cli.commands  # noqa: F401  (registers every control-surface command)

BASE = "http://localhost:4200"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()
