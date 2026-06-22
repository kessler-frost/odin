"""S1.4 — localhost fabric resolves refs from World facts; gates on health."""
from __future__ import annotations

import pytest

from odin.fabric.localhost import LocalhostFabric, Unresolved
from odin.spec.models import Ref, ResourceObserved, World

REF = Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL")
URL = "postgres://app:pw@127.0.0.1:15432/postgres"


def _world(phase: str, facts: dict) -> World:
    return World(resources=(ResourceObserved(id="db", kind="rds", phase=phase, facts=facts),))


def test_resolves_when_target_healthy():
    fabric = LocalhostFabric()
    assert fabric.resolve(REF, _world("healthy", {"DATABASE_URL": URL})) == URL


def test_unresolved_when_target_not_healthy():
    fabric = LocalhostFabric()
    with pytest.raises(Unresolved):
        fabric.resolve(REF, _world("starting", {"DATABASE_URL": URL}))


def test_unresolved_when_target_absent():
    fabric = LocalhostFabric()
    with pytest.raises(Unresolved):
        fabric.resolve(REF, World())


def test_unresolved_when_attr_missing():
    fabric = LocalhostFabric()
    with pytest.raises(Unresolved):
        fabric.resolve(REF, _world("healthy", {}))
