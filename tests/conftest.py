from __future__ import annotations

import os
from pathlib import Path

import pytest

from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry
from odin.terraform.runner import TofuRunner

# A test port distinct from the dev defaults (4200/4201) and the engine default (4202).
TEST_PORT = 4298


@pytest.fixture(scope="session", autouse=True)
def _tofu_plugin_cache():
    """Cache the downloaded AWS provider so repeated `tofu init` calls are fast."""
    cache = Path("/tmp/odin-tofu-plugin-cache")
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["TF_PLUGIN_CACHE_DIR"] = str(cache)
    yield


@pytest.fixture
def registry(tmp_path) -> ResourceRegistry:
    path = tmp_path / "registry.json"
    path.write_text('{"resources": {}}')
    return ResourceRegistry(path)


@pytest.fixture
def moto_engine():
    engine = MotoEngine(port=TEST_PORT)
    engine.start()
    yield engine
    engine.stop()


@pytest.fixture
def tofu_runner(tmp_path, moto_engine) -> TofuRunner:
    return TofuRunner(tmp_path / "tf", moto_engine.endpoint_url)
