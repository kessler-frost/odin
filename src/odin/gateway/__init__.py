"""odin's checking reverse proxy for AWS-shaped workload traffic.

`DEFAULT_GATEWAY_PORT` is re-exported here (not in `app.py`, which imports
`odin.aws.backings`) so `odin.aws.backings` can import it back without a
cycle -- both server.py and BackingAws need the one well-known gateway port.
It is defined in `odin.settings`, where `GatewaySettings.port` uses it as the
default, so the constant and the environment override cannot drift apart.
"""
from __future__ import annotations

from odin.settings import DEFAULT_GATEWAY_PORT

__all__ = ["DEFAULT_GATEWAY_PORT"]
