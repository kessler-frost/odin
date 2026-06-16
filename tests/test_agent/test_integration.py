from __future__ import annotations

import pytest


@pytest.mark.integration
async def test_validate_and_deploy_flow():
    """Full flow against the real Claude agent + tofu + Moto.

    Requires Claude credentials, so it's off by default. The agent-free pieces
    of the same path are covered by the `tofu`-marked orchestrator tests.
    """
    pass
