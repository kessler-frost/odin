"""w1 observability -- ONE real integration check, end to end: apply a
canvas with an ecs node, launch a REAL ECS task whose container command
deliberately crashes (`sh -c '...; exit 1'`, the module docstring's own
"closest honest equivalent" to a real crash-looping deploy), and prove:

1. `odin logs <node>` (the actual CLI, over real HTTP against a real running
   server) shows the crashing container's real stdout.
2. The node projects `crashed` in `/world` with a real verdict (the
   stoppedReason + exit code ecsctl.py's own lazy sweep records), not a
   permanent "starting" -- w1's flagship bug (tf_status.py's old
   running==desired-only comparison).

The whole app (not just the gateway) is served on a REAL bound port via
`serve_in_thread` (the exact helper the gateway sub-server already uses) so
the CLI -- a real httpx client over real HTTP -- can hit it, unlike
`TestClient`'s in-process ASGI transport.
"""
from __future__ import annotations

import shutil
import subprocess
import time

import boto3
import httpx
import pytest
from botocore.config import Config
from typer.testing import CliRunner

import odin.cli.commands  # noqa: F401  (registers `odin logs` + co. on the shared Typer app)
from odin.cli.app import app as cli_app
from odin.gateway.app import serve_in_thread, stop_in_thread
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ecs-crash-observability-e2e"
NODE = "crashy"
CRASH_MARKER = "boom-crash-w1"
CANVAS = {"nodes": [{"id": "n1", "type": "ecs", "data": {"label": NODE}}], "edges": []}


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


async def test_crash_looping_ecs_task_is_visible_via_logs_and_a_real_verdict(tmp_path):
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    server, thread, port = serve_in_thread(app, port=0)
    base = f"http://127.0.0.1:{port}"
    try:
        # Put "crashy" in the desired Stack AND start the env's reconciler
        # ticking (lazily created by /apply) -- without a running
        # reconciler, tf_status.py's projection (and its sweep_tasks fix)
        # never runs at all.
        resp = httpx.post(f"{base}/apply", params={"env": ENV}, json=CANVAS, timeout=30)
        assert resp.status_code == 200, resp.text

        gateway_port = httpx.get(f"{base}/health", timeout=10).json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        ecs = await boto3.client(
            "ecs", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
            config=Config(connect_timeout=10, read_timeout=15, retries={"max_attempts": 0}),
        )
        ecs.create_cluster(clusterName="odin")
        ecs.register_task_definition(
            family=NODE, requiresCompatibilities=["EC2"], networkMode="bridge",
            containerDefinitions=[{
                "name": NODE, "image": "busybox:latest", "essential": True,
                "command": ["sh", "-c", f"echo {CRASH_MARKER}; sleep 1; exit 1"],
                "portMappings": [],
            }],
        )
        ecs.create_service(
            cluster="odin", serviceName=NODE, taskDefinition=NODE, desiredCount=1, launchType="EC2",
            tags=[{"key": "odin:node", "value": NODE}],
        )

        # THE proof for finding #3: the reconciler's own tick sweeps the
        # real container's exit and projects "crashed" -- not a permanent
        # "starting" (tf_status.py's old running==desired-only comparison).
        deadline = time.monotonic() + 120
        observed = None
        while time.monotonic() < deadline:
            world = httpx.get(f"{base}/world", params={"env": ENV}, timeout=10).json()
            observed = next((r for r in world["resources"] if r["id"] == NODE), None)
            if observed is not None and observed["phase"] == "crashed":
                break
            time.sleep(1)
        assert observed is not None and observed["phase"] == "crashed", (
            f"never projected crashed (last seen: {observed})"
        )
        assert observed["verdict"], "a crashed ecs node must carry a real verdict, not silence"
        assert "exit" in observed["verdict"].lower() or "1" in observed["verdict"]

        # THE proof for findings #1/#2: real logs, over real HTTP, via the
        # actual CLI -- not a route unit test.
        runner = CliRunner()
        result = await runner.invoke(cli_app, ["logs", NODE, "--env", ENV, "--url", base])
        assert result.exit_code == 0, result.output
        assert CRASH_MARKER in result.output, result.output

        # The route itself, directly -- confirms `found`/`running` are honest.
        logs_body = httpx.get(f"{base}/logs", params={"env": ENV, "node": NODE, "tail": 50}, timeout=10).json()
        assert logs_body["found"] is True
        assert logs_body["running"] is False  # the container already exited
        assert CRASH_MARKER in logs_body["lines"]

        # Clean up the REAL resources (delete_service stops+removes the real
        # container synchronously -- ecsctl.py's own documented contract).
        ecs.delete_service(cluster="odin", service=NODE, force=True)
    finally:
        stop_in_thread(server, thread)

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    if leftover.stdout.strip():
        _docker("rm", "-f", "-v", *leftover.stdout.split())
        leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"ECS task containers survived: {leftover.stdout}"
