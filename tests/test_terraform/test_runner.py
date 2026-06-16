from __future__ import annotations

import socket
import time

import boto3
import pytest

from odin.process import Daemon
from odin.terraform.runner import TofuRunner

pytestmark = pytest.mark.integration

PORT = 4299
ENDPOINT = f"http://127.0.0.1:{PORT}"


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


async def test_tofu_lifecycle_against_moto(tmp_path):
    """End to end: validate → plan → apply real HCL against a Moto server."""
    moto = Daemon("moto_server", "-H", "127.0.0.1", "-p", str(PORT))
    moto.start()
    try:
        assert _wait_for_port(PORT), "moto_server did not come up"
        runner = TofuRunner(tmp_path, ENDPOINT)
        (tmp_path / "main.tf").write_text(
            'resource "aws_vpc" "v" {\n  cidr_block = "10.9.0.0/16"\n}\n'
        )

        validated = await runner.validate()
        assert validated.ok, validated.diagnostics

        planned = await runner.plan()
        assert planned.ok, planned.diagnostics

        applied = await runner.apply()
        assert applied.ok, applied.diagnostics

        ec2 = boto3.client(
            "ec2", endpoint_url=ENDPOINT, aws_access_key_id="x",
            aws_secret_access_key="x", region_name="us-east-1",
        )
        cidrs = [v["CidrBlock"] for v in ec2.describe_vpcs()["Vpcs"]]
        assert "10.9.0.0/16" in cidrs
    finally:
        moto.stop()


async def test_validate_reports_bad_hcl(tmp_path):
    moto = Daemon("moto_server", "-H", "127.0.0.1", "-p", str(PORT))
    moto.start()
    try:
        assert _wait_for_port(PORT)
        runner = TofuRunner(tmp_path, ENDPOINT)
        # Reference to an undeclared resource → validate must reject it.
        (tmp_path / "main.tf").write_text(
            'resource "aws_subnet" "s" {\n'
            "  vpc_id     = aws_vpc.does_not_exist.id\n"
            '  cidr_block = "10.0.1.0/24"\n'
            "}\n"
        )
        result = await runner.validate()
        assert not result.ok
        assert result.errors
    finally:
        moto.stop()
