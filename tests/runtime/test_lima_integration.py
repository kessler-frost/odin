"""M7 (single-host) — LimaRuntime actually runs a container inside a real Lima VM.

Marked `integration`: boots a Lima VM (slow). Cleans up the VM afterwards.
"""
from __future__ import annotations

import time

import pytest

from odin.runtime.colima import ContainerSpec
from odin.runtime.lima import LimaRuntime

pytestmark = pytest.mark.integration

NAME = "lima-allfather-test"


@pytest.fixture
def lima():
    rt = LimaRuntime()
    yield rt
    rt.stop(NAME)
    rt._lima("delete", "--force", rt.VM, check=False)  # reclaim the VM disk


def test_runs_a_container_in_a_real_vm(lima):
    lima.ensure_host()  # boots the VM + nerdctl
    handle = lima.run_container(ContainerSpec(name=NAME, image="busybox", command=("sleep", "60")))
    assert handle.id

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and lima.status(NAME) != "running":
        time.sleep(2)
    assert lima.status(NAME) == "running"  # container is live inside the VM

    lima.stop(NAME)
    assert lima.status(NAME) == "absent"
