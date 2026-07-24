"""Shared test fixtures. (The old Moto/OpenTofu fixtures were retired with that
path; odin tests use the Spec Store + real Colima backings directly.)"""
from __future__ import annotations

import os

# server.py's lifespan now always starts the gateway's real uvicorn listener
# (G3). Every `create_app()` call that doesn't pass an explicit
# `gateway_port` reads this env var -- default it to an ephemeral port so
# the wider suite never binds the real 0.0.0.0:4266 (port collisions across
# tests, a stray firewall prompt). `setdefault` leaves a deliberately-set
# value (CI, a developer testing the real port) untouched.
os.environ.setdefault("ODIN_GATEWAY_PORT", "0")
