"""odin's checking reverse proxy for AWS-shaped workload traffic.

`GATEWAY_PORT_ENV`/`DEFAULT_GATEWAY_PORT` live at the package root (not in
`app.py`, which imports `odin.aws.backings`) so `odin.aws.backings` can
import them back without a cycle -- both server.py and BackingAws need the
one well-known gateway port.
"""
from __future__ import annotations

GATEWAY_PORT_ENV = "ODIN_GATEWAY_PORT"
DEFAULT_GATEWAY_PORT = 4266
