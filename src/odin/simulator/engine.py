from __future__ import annotations

import boto3
from moto import mock_aws


SUPPORTED_SERVICES = frozenset({"ec2", "s3", "iam", "lambda", "sts"})


class MotoEngine:
    """Manages moto mock contexts for AWS service simulation."""

    def __init__(self) -> None:
        self._mock: mock_aws | None = None
        self._clients: dict[str, boto3.client] = {}

    @property
    def supported_services(self) -> frozenset[str]:
        return SUPPORTED_SERVICES

    def start(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self._clients.clear()

    def stop(self) -> None:
        self._clients.clear()
        if self._mock:
            self._mock.stop()
            self._mock = None

    def reset(self) -> None:
        self.stop()
        self.start()

    def get_client(self, service_name: str) -> boto3.client:
        if service_name not in self._clients:
            self._clients[service_name] = boto3.client(
                service_name, region_name="us-east-1"
            )
        return self._clients[service_name]
