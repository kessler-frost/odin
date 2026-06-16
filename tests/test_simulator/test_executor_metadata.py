from __future__ import annotations

from odin.simulator.executor import Executor


def test_detect_service_subnet():
    assert Executor.detect_service(type("P", (), {"stem": "subnet_public"})()) == "subnet"


def test_detect_service_sg():
    assert Executor.detect_service(type("P", (), {"stem": "sg_web"})()) == "sg"
