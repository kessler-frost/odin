"""Runs `moto_server` as a subprocess and exposes boto3 clients pointed at it."""
from __future__ import annotations

import socket
import sys
import time
import urllib.request

import boto3

from odin.process import Daemon
from odin.resources import MOTO_SERVICES

SUPPORTED_SERVICES = frozenset(MOTO_SERVICES) | {"iam"}
DEFAULT_PORT = 4202


class MotoEngine:
    """A local Moto HTTP server. Terraform and boto3 both talk to its endpoint."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
        self._host = host
        self._port = port
        # Invoke via the current interpreter so it works regardless of PATH
        # (the `moto_server` script is only on PATH inside the venv).
        self._daemon = Daemon(
            sys.executable, "-m", "moto.server", "-H", host, "-p", str(port)
        )
        self._clients: dict[str, boto3.client] = {}

    @property
    def supported_services(self) -> frozenset[str]:
        return SUPPORTED_SERVICES

    @property
    def endpoint_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self, timeout: float = 20.0) -> None:
        self._clients.clear()
        if self._daemon.running:
            return
        self._daemon.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((self._host, self._port)) == 0:
                    return
            time.sleep(0.1)
        raise RuntimeError(f"moto_server did not come up on {self.endpoint_url}")

    def stop(self) -> None:
        self._clients.clear()
        self._daemon.stop()

    def reset(self) -> None:
        """Wipe all simulated state via Moto's reset endpoint."""
        request = urllib.request.Request(
            f"{self.endpoint_url}/moto-api/reset", method="POST"
        )
        urllib.request.urlopen(request, timeout=5).close()

    def get_client(self, service_name: str) -> boto3.client:
        if service_name not in self._clients:
            self._clients[service_name] = boto3.client(
                service_name,
                endpoint_url=self.endpoint_url,
                aws_access_key_id="test",
                aws_secret_access_key="test",
                region_name="us-east-1",
            )
        return self._clients[service_name]
