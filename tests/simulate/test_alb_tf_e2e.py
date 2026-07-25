"""W2.5's flagship: a real Apply puts a REAL nginx reverse proxy in front of a
REAL two-task ECS service, and killing one task doesn't stop the load balancer
serving 200s -- which is the entire point of a load balancer, proven rather
than asserted.

The whole path runs for real, through the UI's own single button
(`POST /apply-full`): canvas -> `canvas_to_stack` -> `generate_tf`'s three-
resource alb expansion (aws_lb + aws_lb_target_group + aws_lb_listener) ->
`tofu apply` -> the gateway's elbv2 model -> `docker run nginx:alpine` +
`docker run` per ECS task -> `curl` the load balancer's REAL published port.

Two tests, cheapest first:
 1. `test_alb_alone_...` -- vpc+subnet+alb with no targets: the proxy container
    is really running, the LB reports `active` with a reachable endpoint, that
    endpoint answers **503** (a real ALB's own answer when nothing is healthy),
    and `plan -detailed-exitcode` is CLEAN. That last check is the zero-drift
    hard gate over the surface where elbv2 drift hides: tags and the two
    attribute maps.
 2. `test_alb_fronts_...` -- the flagship above, plus zero-drift again with the
    ECS service in the picture, plus the empty-canvas Apply teardown.

Container hygiene ABSOLUTE: ECS task container names embed a per-task random
id, so (like test_ecs_tf_e2e.py) real names are discovered from the persisted
`ecsctl.json` at every checkpoint and registered with a cleanup fixture that
force-removes them by EXACT name on teardown. The proxy container's name is
deterministic (`odin-alb-{env}-{lb}`) and registered up front.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from odin.compute.proxy import conf_path as proxy_conf_path
from odin.compute.proxy import container_name as proxy_container_name
from odin.compute.tasks import container_name as task_container_name
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.gateway.models import elbv2ctl
from odin.server import create_app
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV_ALONE = "alb-alone-e2e"
ENV_FLEET = "alb-fleet-e2e"
VPC = "alb-net"
SUBNET = "alb-subnet"
LB = "web-lb"
SERVICE = "web-svc"


def _canvas(*, with_service: int = 0) -> dict:
    """A canvas exactly as the UI would post it -- including the `vpc`/`subnet`
    containment stamps `ui/src/lib/containment.ts` writes onto every node drawn
    inside a container box (the backend never re-derives them from geometry)."""
    nodes = [
        {"id": "n-vpc", "type": "vpc", "position": {"x": 0, "y": 0},
         "data": {"label": VPC, "cidr": "10.0.0.0/16"}},
        {"id": "n-subnet", "type": "subnet", "position": {"x": 20, "y": 40},
         "data": {"label": SUBNET, "cidr": "10.0.1.0/24", "vpc": VPC}},
        {"id": "n-alb", "type": "alb", "position": {"x": 60, "y": 80},
         "data": {
             "label": LB, "lbType": "application", "listenerPort": "80",
             "port": "80", "healthCheckPath": "/", "vpc": VPC, "subnet": SUBNET,
         }},
    ]
    edges: list[dict] = []
    if with_service:
        nodes.append({
            "id": "n-ecs", "type": "ecs", "position": {"x": 400, "y": 80},
            "data": {"label": SERVICE, "image": "nginx:alpine", "count": str(with_service), "port": "80"},
        })
        # The TARGET edge: a `network` edge between an alb node and the compute
        # it fronts (ui/src/lib/iam.ts's `albTargetTypes`). hcl.py's pass 1.5
        # turns it into the `load_balancer` block on the aws_ecs_service.
        edges.append({"id": "e-alb-ecs", "source": "n-alb", "target": "n-ecs",
                      "data": {"edgeType": "network"}})
    return {"nodes": nodes, "edges": edges}


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


def _tofu(args: list[str], workspace: Path, env_vars: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _gateway_state(root: Path, env: str, name: str) -> dict:
    path = root / env / "gateway" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _lb_record(root: Path, env: str) -> dict:
    return _gateway_state(root, env, "elbv2ctl").get(f"lb:{LB}", {})


def _registered_targets(root: Path, env: str) -> list[dict]:
    return _gateway_state(root, env, "elbv2ctl").get(f"targets:{LB}-tg", [])


def _task_container_names(root: Path, env: str) -> set[str]:
    state = _gateway_state(root, env, "ecsctl")
    return {
        task_container_name(env, t["task_id"], t["container_name"])
        for k, t in state.items() if k.startswith("task:")
    }


@pytest.fixture
def container_cleanup():
    """Force-remove by EXACT name on teardown regardless of outcome -- the
    guarantee V4d/V5d's own fixtures give, needed here for the same reason
    (task container names aren't knowable before the run)."""
    names: set[str] = set()
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


def _apply(client: TestClient, env: str, canvas: dict, timeout: float = 420.0) -> dict:
    response = client.post(f"/apply-full?env={env}", json=canvas, timeout=timeout)
    assert response.status_code == 200, response.text
    body = response.json()
    tf = body.get("tf") or {}
    # The tofu tail is the WHOLE diagnostic when an apply fails -- surface it in
    # the assertion instead of a bare "failed != ok".
    assert tf.get("status") == "ok", f"{body.get('status')}: {json.dumps(tf, indent=2)}"
    assert body["unsupported"] == [], body
    return body


def _wait_until(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.5)
    raise AssertionError(f"{what} never happened (last seen: {last!r})")


def _get(url: str, tries: int = 20) -> httpx.Response:
    """GET with a short retry -- nginx's SIGHUP reload is fast but not
    instantaneous, and a freshly (re)created proxy container needs a moment to
    bind. A refused connection is retried; any real HTTP answer is returned."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            return httpx.get(url, timeout=3.0)
        except httpx.HTTPError as exc:
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"never got an HTTP answer from {url}: {last!r}")


def test_alb_alone_runs_a_real_proxy_answers_503_and_plans_clean(tmp_path, container_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    container_cleanup.add(proxy_container_name(ENV_ALONE, LB))
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        _apply(client, ENV_ALONE, _canvas())

        # The proxy container is REAL, not a model fiction.
        ps = _docker("ps", "--filter", f"name={proxy_container_name(ENV_ALONE, LB)}", "--format", "{{.Image}}")
        assert ps.stdout.strip() == "nginx:alpine", ps.stdout

        record = _wait_until(
            lambda: _lb_record(store.root, ENV_ALONE) if _lb_record(store.root, ENV_ALONE).get("state") == "active" else None,
            timeout=60.0, what="the load balancer reaching state=active",
        )
        endpoint = elbv2ctl.endpoint_url(record)
        assert endpoint, record

        # A listener with no healthy target answers 503 -- real ALB behaviour,
        # and proof the request reached OUR nginx rather than nothing at all.
        assert _get(endpoint).status_code == 503

        # THE hard gate: apply -> plan changes nothing. Covers the three
        # resources' tags AND both attribute maps, where elbv2 drift hides.
        env_vars = _tf_env(gateway_port, *app.state.gateway_keys.issue(ENV_ALONE, OPERATOR_NODE_ID))
        workspace = store.root / ENV_ALONE / "tf"
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # Empty canvas + Apply == full teardown (the NORTHSTAR promise).
        _apply(client, ENV_ALONE, {"nodes": [], "edges": []})
        assert not _lb_record(store.root, ENV_ALONE), _gateway_state(store.root, ENV_ALONE, "elbv2ctl")

    gone = _docker("ps", "-a", "--filter", f"name={proxy_container_name(ENV_ALONE, LB)}", "--format", "{{.Names}}")
    assert gone.stdout.strip() == "", f"the proxy container survived teardown: {gone.stdout}"


def test_alb_fronts_two_ecs_tasks_and_still_serves_when_one_dies(tmp_path, container_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    container_cleanup.add(proxy_container_name(ENV_FLEET, LB))
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        _apply(client, ENV_FLEET, _canvas(with_service=2))
        container_cleanup.update(_task_container_names(store.root, ENV_FLEET))

        # `wait_for_steady_state = true` means apply already waited for both
        # tasks; registration finishes on the launching thread a beat later.
        targets = _wait_until(
            lambda: t if len(t := _registered_targets(store.root, ENV_FLEET)) == 2 else None,
            timeout=90.0, what="both ECS tasks registering as load-balancer targets",
        )
        container_cleanup.update(_task_container_names(store.root, ENV_FLEET))
        assert {t["id"] for t in targets} == {"host.docker.internal"}, targets

        running = _docker("ps", "--filter", f"name=odin-ecs-{ENV_FLEET}-", "--format", "{{.Names}}")
        task_names = sorted(line for line in running.stdout.splitlines() if line)
        assert len(task_names) == 2, running.stdout

        record = _wait_until(
            lambda: r if (r := _lb_record(store.root, ENV_FLEET)).get("state") == "active" else None,
            timeout=60.0, what="the load balancer reaching state=active",
        )
        endpoint = elbv2ctl.endpoint_url(record)
        assert endpoint, record

        # A task really answers through the load balancer.
        first = _wait_until(lambda: r if (r := _get(endpoint)).status_code == 200 else None,
                            timeout=60.0, what="a 200 through the load balancer")
        assert "nginx" in first.text.lower(), first.text[:200]

        # THE POINT OF A LOAD BALANCER: kill one task outright (out of band,
        # exactly as W2.2's drift sweep would find it) and the endpoint keeps
        # serving 200s from the survivor.
        #
        # Two DIFFERENT mechanisms can produce that 200 and both are wanted:
        # nginx's own `proxy_next_upstream error` retries the surviving upstream
        # inside the SAME client request (so it holds the instant the container
        # dies, with the dead server still in the config), and separately odin's
        # sweep eventually deregisters the dead target and re-renders. Which one
        # answers first is a genuine race against the reconciler tick, so the
        # HARD assertion is the invariant that matters -- never a non-200 -- and
        # the config snapshot below records which mechanism was exercised rather
        # than pretending the race is deterministic. `render_conf`'s side of the
        # failover is unit-tested directly in tests/compute/test_proxy.py.
        conf_before = proxy_conf_path(store.root, ENV_FLEET, LB).read_text()
        assert conf_before.count("\n    server ") == 2, conf_before
        _docker("rm", "-f", "-v", task_names[0])
        assert _docker("ps", "--filter", f"name={task_names[0]}", "--format", "{{.Names}}").stdout.strip() == ""
        for attempt in range(6):
            answer = _get(endpoint)
            upstreams = proxy_conf_path(store.root, ENV_FLEET, LB).read_text().count("\n    server ")
            assert answer.status_code == 200, (
                f"attempt {attempt}: the LB stopped serving after one task died "
                f"({answer.status_code}); upstreams still in the config: {upstreams}"
            )

        # Zero drift with the whole shape live -- including the ecs service's
        # own `load_balancer` block and `tags`.
        env_vars = _tf_env(gateway_port, *app.state.gateway_keys.issue(ENV_FLEET, OPERATOR_NODE_ID))
        workspace = store.root / ENV_FLEET / "tf"
        plan = _tofu(["plan", "-detailed-exitcode"], workspace, env_vars)
        assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

        # The dead task left the rotation once odin noticed (the plan's own
        # DescribeServices refresh runs ecsctl's sweep) -- honest upstreams.
        survivors = _wait_until(
            lambda: t if len(t := _registered_targets(store.root, ENV_FLEET)) == 1 else None,
            timeout=60.0, what="the dead task being deregistered from the target group",
        )
        assert len(survivors) == 1, survivors
        assert _get(endpoint).status_code == 200

        _apply(client, ENV_FLEET, {"nodes": [], "edges": []})

    leftover = _docker("ps", "-a", "--filter", "label=odin=1", "--format", "{{.Names}}")
    surviving = [n for n in leftover.stdout.splitlines() if ENV_FLEET in n]
    assert surviving == [], f"containers survived teardown: {surviving}"
