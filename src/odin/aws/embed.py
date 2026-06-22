"""Embed the MiniStack AWS emulator in-process as allfather's AWS control plane.

MiniStack (`ministack.app:app`) is a bare ASGI3 callable. We run it on a
loopback port via uvicorn in a background thread (same process, so the
container-spawn monkeypatch in `runtime.shim` takes effect; real socket, so
boto3 — which does not speak ASGI — works normally). We run with
``lifespan="off"`` so MiniStack's startup never fires: its lifespan reaps any
container labelled ``ministack=…`` via a real Docker client and starts an SFTP
listener + scheduler + thread pool we do not want. allfather's own containers
use ``allfather=1`` labels and its runner is wired in via the shim, so none of
that is needed.

Env that MiniStack freezes at import (host is baked into regexes) is set here
*before* ``ministack.app`` is ever imported.
"""
from __future__ import annotations

import os
import socket
import threading

import boto3

ACCOUNT_ID = "000000000000"

# Frozen-at-import MiniStack config — must be set before `import ministack.app`.
os.environ.setdefault("MINISTACK_HOST", "localhost")
os.environ.setdefault("MINISTACK_ACCOUNT_ID", ACCOUNT_ID)
os.environ.setdefault("RDS_BASE_PORT", "15432")

_server = None
_thread: threading.Thread | None = None
_port: int | None = None


def build_ministack_app():
    """Import and return MiniStack's ASGI3 app (env already set above)."""
    import ministack.app as ministack_app

    return ministack_app.app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_serving(port: int, timeout: float = 15.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"embedded MiniStack did not start on :{port} within {timeout}s")


def start_ministack(port: int | None = None) -> int:
    """Start the embedded MiniStack server on a loopback port (idempotent)."""
    global _server, _thread, _port
    if _thread is not None and _thread.is_alive():
        return _port  # type: ignore[return-value]

    import uvicorn

    _port = port or _free_port()
    config = uvicorn.Config(
        build_ministack_app(),
        host="127.0.0.1",
        port=_port,
        lifespan="off",
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    _thread = threading.Thread(target=_server.run, name="ministack", daemon=True)
    _thread.start()
    _wait_until_serving(_port)
    return _port


def stop_ministack() -> None:
    global _server, _thread, _port
    if _server is not None:
        _server.should_exit = True
    if _thread is not None:
        _thread.join(timeout=5)
    _server = None
    _thread = None
    _port = None


def current_port() -> int:
    if _port is None:
        raise RuntimeError("MiniStack is not running — call start_ministack() first")
    return _port


def install_rds_spawn_rewire(runtime) -> None:
    """Route MiniStack's RDS container spawns through allfather's runtime.

    Monkeypatches `ministack.services.rds._docker` with the shim so a
    `CreateDBInstance` boots a real Postgres via allfather's ColimaRuntime
    (joining allfather's World) instead of MiniStack's own docker client.
    """
    import ministack.services.rds as rds

    from odin.runtime.shim import AllfatherDockerShim

    rds._docker = AllfatherDockerShim(runtime)


def ministack_boto_client(service: str):
    """A boto3 client for the embedded MiniStack (the 12-digit key = account id)."""
    return boto3.client(
        service,
        endpoint_url=f"http://127.0.0.1:{current_port()}",
        aws_access_key_id=ACCOUNT_ID,
        aws_secret_access_key="x",
        region_name="us-east-1",
    )
