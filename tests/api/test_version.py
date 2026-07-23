"""Release finding #1 (version coherence): pyproject.toml's version and the
FastAPI app's advertised `version=` must agree -- both read from the SAME
source (`importlib.metadata.version("odin")`), so a version bump is a
one-line change, not two files drifting apart at tag time."""
from __future__ import annotations

import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path

from fastapi.testclient import TestClient

from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime


def test_pyproject_version_matches_installed_package_metadata():
    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == pkg_version("odin")


def test_app_advertises_the_installed_package_version(tmp_path):
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    assert app.version == pkg_version("odin")
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True  # app boots with the real version wired in
