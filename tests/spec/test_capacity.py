"""An instance cannot hold more than it has, and odin should say so first.

The third of placement's four named costs. An EC2 node is a real Lima VM sized
by its instance type, and every ECS task gets a real memory cap, so "three
services of two tasks each, drawn inside a t3.micro" is not something odin can
honour. The only question is whether it says so before applying, or the user
discovers it as OOM-killed containers minutes later.
"""
from __future__ import annotations

from odin.compute.tasks import _DEFAULT_MEMORY_MIB
from odin.spec.capacity import DEFAULT_TASK_MEMORY_MIB, overcommitted
from odin.spec.translate import canvas_to_stack


def _canvas(services: list[dict], instance_type: str = "t3.micro") -> dict:
    nodes = [
        {"id": "v1", "type": "vpc", "position": {"x": 0, "y": 0}, "data": {"label": "prod-vpc"}},
        {"id": "s1", "type": "subnet", "position": {"x": 0, "y": 0},
         "data": {"label": "app-subnet", "vpc": "prod-vpc"}},
        {"id": "e1", "type": "ec2", "position": {"x": 0, "y": 0},
         "data": {"label": "api-server", "instance_type": instance_type, "subnet": "app-subnet"}},
    ]
    for index, service in enumerate(services):
        nodes.append({
            "id": f"c{index}", "type": "ecs", "position": {"x": 0, "y": 0},
            "data": {"image": "nginx:alpine", "port": "80", "host": "api-server", **service},
        })
    return {"nodes": nodes, "edges": []}


def _problems(canvas: dict) -> list[str]:
    return overcommitted(canvas_to_stack(canvas))


def test_the_default_task_size_matches_what_the_runtime_actually_caps_at():
    """Two constants describing one number. If `tasks.py` changes its cap and
    this does not, every capacity answer here is quietly wrong."""
    assert DEFAULT_TASK_MEMORY_MIB == _DEFAULT_MEMORY_MIB


def test_a_canvas_that_fits_is_silent():
    # t3.micro = 1024 MiB, less 256 MiB overhead = 768 available; one 512 task.
    assert _problems(_canvas([{"label": "web", "count": "1"}])) == []


def test_placing_more_than_the_instance_has_is_refused_with_the_numbers():
    (problem,) = _problems(_canvas([{"label": "web", "count": "3"}]))
    assert "api-server" in problem
    assert "1536 MiB" in problem, problem      # 3 x 512 asked for
    assert "768 MiB" in problem, problem       # what a t3.micro leaves
    assert "t3.micro" in problem
    # ...and it says what to DO, not just that it is broken.
    assert "larger instance type" in problem


def test_several_services_on_one_instance_are_summed():
    problems = _problems(_canvas([
        {"label": "web", "count": "1"},
        {"label": "worker", "count": "1"},
    ]))
    (problem,) = problems
    assert "web" in problem and "worker" in problem
    assert "1024 MiB" in problem


def test_a_bigger_instance_type_fits_the_same_workload():
    # t2.medium = 4GiB, so the same three tasks are fine.
    assert _problems(_canvas([{"label": "web", "count": "3"}], instance_type="t2.medium")) == []


def test_a_taskdef_memory_overrides_the_default():
    assert _problems(_canvas([{"label": "web", "count": "2", "memory": "128"}])) == []
    (problem,) = _problems(_canvas([{"label": "web", "count": "2", "memory": "900"}]))
    assert "1800 MiB" in problem


def test_workloads_NOT_placed_inside_an_instance_are_not_counted():
    """The overwhelmingly common canvas: services that run on the shared host
    are limited by the host, not by an instance nobody put them in."""
    canvas = _canvas([{"label": "web", "count": "8"}])
    for node in canvas["nodes"]:
        node.get("data", {}).pop("host", None)
    assert _problems(canvas) == []


def test_a_canvas_with_no_instances_at_all_pays_nothing():
    assert overcommitted(canvas_to_stack({"nodes": [], "edges": []})) == []


def test_an_unknown_instance_type_falls_back_rather_than_reading_as_infinite():
    """`get_instance_type` defaults to t2.micro. Treating an unrecognised type
    as unlimited would let the guard pass exactly when it is least sure."""
    (problem,) = _problems(_canvas([{"label": "web", "count": "3"}], instance_type="m9.enormous"))
    assert "api-server" in problem


# --- the guard has to actually run on /apply-full ----------------------------
#
# The unit tests above prove the arithmetic; this proves an apply consults it.
# Without this, deleting the call in server.py leaves every test in this file
# green and the refusal gone.

def test_apply_refuses_an_overcommitted_canvas_before_building_anything(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from odin.server import create_app
    from odin.spec.store import SpecStore
    from tests.api.test_apply import FakeRds, FakeRuntime

    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    canvas = _canvas([{"label": "web", "count": "6"}])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/apply-full", params={"env": "cap"}, json=canvas)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["capacity_problems"], body
    assert "api-server" in body["note"]


def test_apply_is_unaffected_by_a_canvas_that_fits(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from odin.server import create_app
    from odin.spec.store import SpecStore
    from tests.api.test_apply import FakeRds, FakeRuntime

    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: None)
    app = create_app(runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(), backings=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/apply-full", params={"env": "cap"}, json=_canvas([{"label": "web", "count": "1"}]),
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] != "refused"
