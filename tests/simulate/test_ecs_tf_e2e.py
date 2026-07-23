"""V5d -- the LAST coverage-plan flagship: a real `tofu apply` creates an
`aws_ecs_cluster` + `aws_ecs_task_definition` + `aws_ecs_service`(count=2,
nginx:alpine) through the real gateway -- proving NORTHSTAR directive 5's
whole ECS slice end-to-end (CreateService -> a gateway-internal reconcile ->
REAL Colima containers -> the service's runningCount reaching 2 REAL
containers -> a zero-drift plan -- the research §2e drift this module's own
docstring set out to kill -- -> UpdateService desiredCount=1 via re-apply ->
one container remains -> destroy -> zero leftovers).

Modeled on test_ec2_tf_e2e.py/test_lambda_tf_e2e.py, with ECS's own
asynchronous shape: CreateService/UpdateService return immediately (ACTIVE,
runningCount possibly still 0) while a background thread converges real
containers -- so, unlike those two tests, THIS test's own wait is a real
`describe_services` poll through the gateway (boto3 ecs client, operator
creds), not something `tofu apply` blocks on (aws_ecs_service has no
create-time waiter by default -- `wait_for_steady_state` was never set).

Container hygiene ABSOLUTE (the brief's own words): task container names
embed a per-task RANDOM id minted only once the background reconcile runs,
so -- unlike Lambda's deterministic container name -- this test discovers
real names from the persisted `ecsctl.json` state (the exact technique
V3d's `vm_cleanup` uses for Lima VM names) and registers each one with the
`ecs_cleanup` fixture as soon as it appears, at every checkpoint below, so a
mid-test failure still gets exact-name force-removal on teardown.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent.hcl import TfProject
from odin.compute.tasks import container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.server import create_app
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ecs-tf-e2e"
CLUSTER_NAME = "v5d-cluster"
SERVICE_NAME = "v5d-service"
FAMILY = "v5d-app"
CONTAINER_NAME = "app"


def _main_tf(desired_count: int) -> str:
    cluster = f"""resource "aws_ecs_cluster" "main" {{
  name = {hcl.quote(CLUSTER_NAME)}
}}"""
    taskdef = f"""resource "aws_ecs_task_definition" "app" {{
  family                   = {hcl.quote(FAMILY)}
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"

  container_definitions = jsonencode([
    {{
      name      = {hcl.quote(CONTAINER_NAME)}
      image     = "nginx:alpine"
      essential = true
      portMappings = [
        {{ containerPort = 80, hostPort = 0, protocol = "tcp" }}
      ]
    }}
  ])
}}"""
    service = f"""resource "aws_ecs_service" "app" {{
  name            = {hcl.quote(SERVICE_NAME)}
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = {desired_count}
  launch_type     = "EC2"
}}"""
    return "\n\n".join([hcl.HEADER, hcl.provider_block(), cluster, taskdef, service]) + "\n"


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


def _tofu(args: list[str], workspace, env_vars: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def _ecsctl_state(root: Path, env: str) -> dict:
    path = root / env / "gateway" / "ecsctl.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _running_container_names(root: Path, env: str) -> set[str]:
    state = _ecsctl_state(root, env)
    tasks = [v for k, v in state.items() if k.startswith("task:")]
    return {container_name(env, t["task_id"], t["container_name"]) for t in tasks}


@pytest.fixture
def ecs_cleanup():
    """Container hygiene ABSOLUTE: names appended here are force-removed by
    EXACT name on teardown, regardless of test outcome -- same guarantee
    V4d's `lambda_cleanup` gives, needed here even more since task container
    names aren't known ahead of time (see module docstring)."""
    names: set[str] = set()
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


def _register_running_containers(store_root: Path, ecs_cleanup: set[str]) -> None:
    ecs_cleanup.update(_running_container_names(store_root, ENV))


def _wait_for_running_count(ecs_client, want: int, store_root: Path, ecs_cleanup: set[str], timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        _register_running_containers(store_root, ecs_cleanup)
        response = ecs_client.describe_services(cluster=CLUSTER_NAME, services=[SERVICE_NAME])
        (last,) = response["services"]
        if last["runningCount"] == want:
            return last
        time.sleep(0.5)
    raise AssertionError(f"service never reached runningCount={want} (last seen {last})")


def test_tf_apply_converges_real_containers_zero_drift_scale_destroy(tmp_path, ecs_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        workspace = workspace_mod.materialize(store.root, ENV, TfProject(files={"main.tf": _main_tf(2)}))

        init = _tofu(["init"], workspace, env_vars)
        assert init.returncode == 0, f"init failed:\n{init.stdout}\n{init.stderr}"

        apply_start = time.monotonic()
        apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=180)
        apply_elapsed = time.monotonic() - apply_start
        print(f"\n[V5d] tofu apply (cluster+taskdef+service) took {apply_elapsed:.1f}s")
        _register_running_containers(store.root, ecs_cleanup)
        assert apply.returncode == 0, f"apply failed:\n{apply.stdout}\n{apply.stderr}"

        ecs_client = boto3.client(
            "ecs", endpoint_url=f"http://127.0.0.1:{gateway_port}",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        )

        # aws_ecs_service has no create-time waiter by default (no
        # wait_for_steady_state) -- tofu apply's own return proves nothing
        # about runningCount. THIS is the real convergence proof.
        converge_start = time.monotonic()
        service = _wait_for_running_count(ecs_client, 2, store.root, ecs_cleanup)
        converge_elapsed = time.monotonic() - converge_start
        print(f"[V5d] runningCount converged to 2 in {converge_elapsed:.1f}s (incl. any nginx:alpine pull)")
        assert service["desiredCount"] == 2
        assert service["pendingCount"] == 0

        # THE proof the containers are real, not a model fiction.
        ps = _docker("ps", "--filter", f"name=allfather-ecs-{ENV}-", "--format", "{{.Image}}")
        images = [line for line in ps.stdout.splitlines() if line]
        assert len(images) == 2, f"expected 2 real running containers, docker ps says: {ps.stdout!r}"
        assert all(image == "nginx:alpine" for image in images)

        # Zero drift: apply -> plan changes NOTHING -- the research §2e bar
        # this module's own "echo containerDefinitions verbatim" design
        # exists to clear.
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # UpdateService via re-apply: desiredCount 2 -> 1.
        (workspace / "main.tf").write_text(_main_tf(1))
        scale_apply = _tofu(["apply", "-auto-approve"], workspace, env_vars, timeout=120)
        assert scale_apply.returncode == 0, f"scale-down apply failed:\n{scale_apply.stdout}\n{scale_apply.stderr}"

        service = _wait_for_running_count(ecs_client, 1, store.root, ecs_cleanup)
        assert service["desiredCount"] == 1

        ps_after_scale = _docker("ps", "--filter", f"name=allfather-ecs-{ENV}-", "--format", "{{.Names}}")
        remaining = [line for line in ps_after_scale.stdout.splitlines() if line]
        assert len(remaining) == 1, f"expected exactly 1 real running container, docker ps says: {ps_after_scale.stdout!r}"

        scale_plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert scale_plan.returncode == 0, f"drift detected after scale-down (exit {scale_plan.returncode}):\n{scale_plan.stdout}\n{scale_plan.stderr}"

        destroy = _tofu(["destroy", "-auto-approve"], workspace, env_vars, timeout=120)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"

        # The containers are actually gone -- not just the model records.
        ps_final = _docker("ps", "-a", "--filter", f"name=allfather-ecs-{ENV}-", "--format", "{{.Names}}")
        assert ps_final.stdout.strip() == "", f"ECS task containers survived teardown: {ps_final.stdout}"

        # NOT a blanket "== {}" -- real AWS soft-deletes here too:
        # DeregisterTaskDefinition marks INACTIVE (never removes the
        # record, so a stale revision stays describable) and DeleteService
        # keeps its record around briefly, also INACTIVE, precisely so a
        # real delete-waiter can observe the transition (see ecsctl.py's
        # `_INACTIVE_SERVICE_SWEEP_SECONDS` docstring -- this is the fix
        # for the destroy-hangs-forever bug this test itself found). What
        # MUST be gone: the cluster record (no soft-delete) and every task
        # (their containers are verifiably gone above).
        final_state = _ecsctl_state(store.root, ENV)
        assert not any(k.startswith("cluster:") for k in final_state), final_state
        assert not any(k.startswith("task:") for k in final_state), final_state
        (taskdef,) = [v for k, v in final_state.items() if k.startswith("taskdef:")]
        assert taskdef["status"] == "INACTIVE"
        (service,) = [v for k, v in final_state.items() if k.startswith("service:")]
        assert service["status"] == "INACTIVE"

    # Belt-and-braces: no allfather-labelled container left for this env.
    leftover = _docker("ps", "-aq", "--filter", "label=allfather=1", "--filter", f"name={ENV}")
    assert leftover.stdout.strip() == ""
