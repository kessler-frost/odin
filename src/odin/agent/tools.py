from __future__ import annotations

from typing import Any

from odin.mcp.tools import OdinTools
from odin.simulator.engine import MotoEngine
from odin.simulator.registry import ResourceRegistry


def create_odin_tools(
    engine: MotoEngine,
    registry: ResourceRegistry,
    ws_manager: Any = None,
) -> dict[str, Any]:
    """Create a dict of tool functions for the agent. These wrap OdinTools methods."""
    odin = OdinTools(engine, registry, ws_manager)

    return {
        "validate_file": odin.validate_file,
        "get_infrastructure_state": odin.get_infrastructure_state,
    }
