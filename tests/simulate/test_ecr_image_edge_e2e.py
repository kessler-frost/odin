"""v0.8.15: an ECR edge really sets an ECS service's image, and the address it
sets is one the runtime can actually pull from.

`_ecs_container_definitions` read the node's hand-typed `image` field and
consulted no edge at all, so drawing a repository to a service granted
`ecr:BatchGetImage` and left the service running whatever was typed. The edge
now emits `image = "${aws_ecr_repository.<n>.repository_url}:latest"`.

WHY THIS NEEDS A CONTAINER TO PROVE. Every claim in that sentence is about a
component this repo does not own, and a unit test would agree with itself
about all of them:
  * that `aws_ecr_repository` exposes `repository_url` at all — provider schema;
  * that tofu RESOLVES it before RegisterTaskDefinition, so what `ecsctl`
    stores is an address and not a literal `${...}` — tofu's evaluation order;
  * that `127.0.0.1:{port}/{name}`, the address `gateway/models/ecr.py` mints,
    is reachable BY THE DOCKER DAEMON. That last one was the open question:
    the daemon runs inside the Colima VM, and `ecr.py`'s docstring only ever
    claimed the address works for a HOST-side `docker` CLI ("this slice's only
    real consumer"). Nothing had ever asked a task to pull from it.

TWO APPLIES, on purpose. The image has to exist before anything can run it, so
this is push-then-deploy exactly as a user would do it: apply the repository,
`docker push` to the address odin published, then apply the service that
consumes it. One combined apply would fail on a missing tag and prove only
that ECS's converge timeout works.

Hygiene: everything is named with this agent's `edgebld-` prefix and removed
by EXACT name; task container names embed a per-task random id, so they are
discovered from the persisted `ecsctl.json` before teardown.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from odin.compute.tasks import container_name as task_container_name
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "edgebld-ecr-image"
REPO = "edgebld-app"
SERVICE = "edgebld-svc"
# Tagged from whatever is already cached locally, so the test needs no network.
# `nginx:alpine` rather than `alpine:3.20` because the task must STAY UP long
# enough for the ECS service to reach steady state -- a plain `alpine` exits
# immediately and would fail the apply for a reason that has nothing to do with
# where the image came from.
BASE_IMAGE = "nginx:alpine"


def _repo_canvas() -> dict:
    return {
        "nodes": [{"id": "n-ecr", "type": "ecr", "position": {"x": 0, "y": 0},
                   "data": {"label": REPO}}],
        "edges": [],
    }


def _service_canvas() -> dict:
    """The ecs node carries NO `image` field. That is the whole point: the only
    thing that can give it one is the edge.

    The edge is typed `network`, which is what every canvas saved before the
    edge-type registry carries -- `iac/hcl.py`'s image pass matches on the
    two NODE kinds and never on `edge.kind`, and a test that typed it `iam`
    would step around that hazard rather than pin it.
    """
    return {
        "nodes": [
            {"id": "n-ecr", "type": "ecr", "position": {"x": 0, "y": 0},
             "data": {"label": REPO}},
            {"id": "n-ecs", "type": "ecs", "position": {"x": 300, "y": 0},
             "data": {"label": SERVICE, "count": "1", "port": "80"}},
        ],
        "edges": [{"id": "e-ecr-ecs", "source": "n-ecs", "target": "n-ecr",
                   "data": {"edgeType": "network"}}],
    }


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)


def _gateway_state(root: Path, name: str) -> dict:
    path = root / ENV / "gateway" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _repo_record(root: Path) -> dict:
    return _gateway_state(root, "ecr").get(f"repo:{REPO}", {})


def _task_container_names(root: Path) -> set[str]:
    state = _gateway_state(root, "ecsctl")
    return {
        task_container_name(ENV, t["task_id"], t["container_name"])
        for k, t in state.items() if k.startswith("task:")
    }


def _taskdef_images(root: Path) -> list[str]:
    state = _gateway_state(root, "ecsctl")
    return [
        container["image"]
        for key, record in state.items() if key.startswith("taskdef:")
        for container in record.get("container_definitions", [])
    ]


@pytest.fixture
def container_cleanup():
    names: set[str] = set()
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


def _apply(client: TestClient, canvas: dict, timeout: float = 600.0) -> dict:
    response = client.post(f"/apply-full?env={ENV}", json=canvas, timeout=timeout)
    assert response.status_code == 200, response.text
    body = response.json()
    tf = body.get("tf") or {}
    assert tf.get("status") == "ok", f"{body.get('status')}: {json.dumps(tf, indent=2)}"
    assert body["unsupported"] == [], body
    return body


def test_an_ecr_edge_sets_an_image_the_runtime_can_actually_pull(tmp_path, container_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    container_cleanup.add(f"odin-aws-registry-{ENV}")
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        # --- 1. the repository, and the address odin publishes for it --------
        _apply(client, _repo_canvas())
        repo = _repo_record(store.root)
        repository_uri = repo.get("repository_uri", "")
        assert repository_uri.startswith("127.0.0.1:"), repo
        print(f"\n[edgebld] repositoryUri = {repository_uri}")

        # --- 2. a REAL push to exactly that address -------------------------
        image_ref = f"{repository_uri}:latest"
        tag = _docker("tag", BASE_IMAGE, image_ref)
        assert tag.returncode == 0, tag.stderr
        push = _docker("push", image_ref)
        assert push.returncode == 0, push.stdout + push.stderr
        registry_port = repository_uri.split(":")[1].split("/")[0]
        tags = httpx.get(f"http://127.0.0.1:{registry_port}/v2/{REPO}/tags/list", timeout=10.0)
        assert tags.status_code == 200, tags.text
        assert "latest" in tags.json()["tags"], tags.text
        # Remove the local tag, so a pull that "works" cannot be the daemon
        # quietly reusing a locally-tagged image instead of reaching the
        # registry. Without this the whole test could pass with the registry
        # switched off.
        rmi = _docker("rmi", image_ref)
        assert rmi.returncode == 0, rmi.stderr

        # --- 3. the service, whose image comes ONLY from the edge -----------
        _apply(client, _service_canvas())
        container_cleanup.update(_task_container_names(store.root))

        # The generated file carries the INTERPOLATION, not a resolved value --
        # a resolved address in `main.tf` would drift on every apply.
        main_tf = (store.root / ENV / "tf" / "main.tf").read_text()
        assert "${aws_ecr_repository." in main_tf, main_tf
        assert repository_uri not in main_tf, "the port leaked into the file; it must stay computed"

        # ...and what tofu actually SENT is the resolved address. This is the
        # step no unit test can reach: it proves tofu evaluated the attribute
        # before RegisterTaskDefinition.
        images = _taskdef_images(store.root)
        assert images == [image_ref], images
        print(f"[edgebld] taskdef image as stored by ecsctl = {images[0]}")

        # --- 4. a REAL container, running the image pulled from odin's own
        # registry. `docker inspect` reads the daemon, not odin's state.
        names = _task_container_names(store.root)
        assert names, "no task container was ever launched"
        for name in names:
            inspect = _docker("inspect", "-f", "{{.Config.Image}}\t{{.State.Running}}", name)
            assert inspect.returncode == 0, inspect.stderr
            image, running = inspect.stdout.strip().split("\t")
            assert image == image_ref, f"{name} is running {image!r}, not the ECR image"
            assert running == "true", f"{name} is not running: {inspect.stdout}"
            print(f"[edgebld] {name} running {image}")

        # --- 5. teardown through the product's own path ---------------------
        empty = client.post(f"/apply-full?env={ENV}", json={"nodes": [], "edges": []}, timeout=600.0)
        assert empty.status_code == 200, empty.text
        assert (empty.json().get("tf") or {}).get("status") == "ok", empty.text
        time.sleep(1.0)
        for name in names:
            ps = _docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}")
            assert ps.stdout.strip() == "", f"task container survived teardown: {ps.stdout}"
