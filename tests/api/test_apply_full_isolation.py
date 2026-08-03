"""The RATCHET: a fake runtime must actually isolate `/apply-full`.

`create_app(runtime=FakeRuntime(), rds=FakeRds(), aws=FakeAws(), backings=False)`
reads as hermetic and was not. Measured 2026-07-29, after it leaked four
containers into every unit-suite run: the route ran a REAL `tofu apply`, and
every post-apply pass built its OWN substrate from the module default rather
than from the runtime the app was handed -- `ecsctl.converge_services` a bare
`TaskRuntime()`, `lambdactl.converge_functions` a
`FunctionRuntime(ColimaRuntime(), ...)`, `rdsctl` a real `PostgresRds`,
`drift.sweep_compute` a real `ColimaRuntime`, `ec2compute`/`route53_hosts` a
real limactl-backed `InstanceVm`. So the injected fakes were bypassed and real
Postgres, Redis and RIE containers started.

The asymmetry was the defect and it GUARANTEED a leak rather than risking one:
creation was real and teardown was fake. `/destroy` runs through the
reconciler, which DOES honour `FakeRds`, so it destroyed a fake and left the
container standing.

## Why this file exists rather than a paragraph in ROADMAP

Prose about what a fixture isolates has gone stale in this repo twice, and
prose cannot fail a build. This measures the thing itself: it hooks the two
lowest points in CPython at which a process can be born and fails if the route
reaches for a machine.

    subprocess.Popen.__init__               every sync spawn
    BaseEventLoop.subprocess_exec           every asyncio.create_subprocess_exec

Both, and at that depth, deliberately -- honesty rule 5. A check written
against `odin.util.run_command_async` would share a source with its subject:
a new call site that imported `asyncio` directly would slip past it, and the
guard would report the silence as isolation. CPython's own spawn points are
somewhere the subject cannot reach.

MUTATION-TESTED, both directions. Every seam this pins was reverted one at a
time to its pre-fix default and the matching assertion failed:
`TaskRuntime()` -> `docker`, `FunctionRuntime(ColimaRuntime(), ...)` ->
`docker`, `PostgresRds(ColimaRuntime(), ...)` -> `docker`,
`sweep_compute(...)` with no `containers` -> `docker ps`, `InstanceVm()` ->
`limactl`, and the real `TfRunner` -> `tofu`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from odin.agent.translate import TranslateResult
from odin.aws.rds import container_name as db_container_name
from odin.compute.functions import container_name as function_container_name
from odin.server import create_app
from odin.spec.store import SpecStore

from tests.api.test_apply_full import FakeAws, FakeRds, FakeRuntime
from tests.substrates import NoTofu, NoVm, SpawnRecorder

ENV = "default"


# One node of every kind whose post-apply pass has a real substrate behind it.
# Not a minimal canvas on purpose: an empty one proves nothing, because most of
# these passes return after a single store read when the env holds no records.
EVERY_SUBSTRATE = {
    "nodes": [
        {"id": "db-1", "type": "rds", "position": {"x": 0, "y": 0},
         "data": {"label": "app-db", "engine": "postgres", "dbName": "postgres",
                  "username": "app", "password": "apppass123"}},
        {"id": "ecs-1", "type": "ecs", "position": {"x": 40, "y": 0},
         "data": {"label": "web", "image": "nginx:alpine"}},
        {"id": "lam-1", "type": "lambda", "position": {"x": 80, "y": 0},
         "data": {"label": "worker", "code": "def lambda_handler(e, c):\n    return e"}},
        {"id": "ec2-1", "type": "ec2", "position": {"x": 120, "y": 0},
         "data": {"label": "box"}},
        {"id": "s3-1", "type": "s3", "position": {"x": 160, "y": 0},
         "data": {"label": "uploads"}},
    ],
    "edges": [],
}


class _RunningContainers(FakeRuntime):
    """`FakeRuntime` that KEEPS BOOKS: the two LIVE records' containers are
    there from the start, and anything this runtime is asked to boot joins them.

    The seeded half exists because the drift sweep would otherwise mark both
    live records dead the moment the apply starts, and two passes downstream
    (`ensure_db_mesh`, which only touches an `available` instance) would never
    run at all. Measured: with everything seeded dead, reverting `sweep_compute`
    and `ensure_db_mesh` to their real-machine defaults made NO process spawn,
    because neither pass had anything to do -- the mutation survived and the
    guard read green over a seam it had never exercised.

    The BOOKKEEPING half exists because `FakeRuntime.run_container` used to
    return a handle and forget, so a task this apply had just launched was
    absent from the very next listing. The apply then answered
    `applied_services_unhealthy` after waiting out the whole ECS steady-state
    budget -- 199s for the file, and, far worse, a ratchet that FAILED
    UNMUTATED. Every mutation would have been "caught" for free, which is
    honesty rule 5 exactly: a check that cannot pass cannot fail either.

    The container names come from the product's own naming functions rather than
    being spelled out. That is the right direction for a fixture INPUT (the
    opposite of an expectation): a hand-written name that drifted from
    `container_name` would silently stop matching, and the pass would go quiet
    again in the way this class exists to prevent."""

    def __init__(self) -> None:
        super().__init__()
        self.booted: set[str] = {
            db_container_name(ENV, "live-db"), function_container_name(ENV, "live-fn"),
        }

    async def run_container(self, spec):
        self.booted.add(spec.name)
        return await super().run_container(spec)

    async def container_names(self) -> list[str]:
        return sorted(self.booted)

    async def status(self, name: str) -> str:
        return "running" if name in self.booted else "absent"


def _isolated_app(tmp_path: Path, runner: NoTofu):
    """The whole hermetic combination, in one place so a test cannot half-take
    it. Four seams because there are four substrates, not because three would
    do: `runtime` covers every container, `rds` the database, `vm` Lima and
    `runner` tofu."""
    return create_app(
        runtime=_RunningContainers(), store=SpecStore(tmp_path), rds=FakeRds(), aws=FakeAws(),
        backings=False, vm=NoVm(), runner=runner,
    )


def _seed_mesh(root: Path, env: str = ENV) -> None:
    """An env whose Nebula mesh is BOOTSTRAPPED, written directly rather than
    through `ensure_network` (which spawns two real `nebula-cert` runs).

    It is what makes the two mesh passes reach for a machine at all, and both
    gates had to be found by MUTATING rather than by reading:

      `overlay.json` with a `lighthouse_underlay_ip` -- `InstanceVm._refresh`
      returns `skipped` for an env with no overlay or no underlay address, so
      `ensure_instance_mesh` built its real limactl VM and never called it.

      `ca.crt` -- `MeshSidecar.enabled()` is `(nebula_dir / "ca.crt").exists()`,
      and `ensure_db_mesh` -> `PostgresRds.join_mesh` -> `MeshSidecar.ensure`
      returns None on that gate before touching anything. Reverting that seam
      SURVIVED its mutation for this reason alone, with the fix working and the
      guard silent.

    Both are the same lesson one level down from the one this file is about: a
    pass that has nothing to do proves nothing about the substrate it was
    handed. The bytes are placeholders -- nothing here parses them, and the
    correct (fake) path never gets this far."""
    nebula = Path(root) / env / "nebula"
    nebula.mkdir(parents=True, exist_ok=True)
    (nebula / "overlay.json").write_text(json.dumps({
        "network": env, "lighthouse_underlay_ip": "127.0.0.1", "lighthouse_port": 4342,
    }))
    (nebula / "ca.crt").write_text("-- odin test CA, never parsed --\n")
    (nebula / "ca.key").write_text("-- odin test CA key, never parsed --\n")


def _seed_failed_records(app, env: str = ENV) -> None:
    """A dead lambda, a dead database and a live-but-short service -- plus a
    LIVE one of each, whose containers `_RunningContainers` says are there.

    Both halves are load-bearing. The dead records drive the converge/wait
    passes; the live ones drive the sweep and the mesh pass, which skip a
    resource that never claimed to be up. Without this the route is isolated for
    an uninteresting reason (nothing to do), and a seam could regress with no
    assertion noticing."""
    stores = app.state.gateway_stores
    stores.lambdactl.set(env, "fn:live-fn", {
        "function_name": "live-fn", "state": "Active", "state_reason": "The function is ready.",
        "state_reason_code": "Idle", "last_update_status": "Successful",
        "last_update_status_reason": None, "last_update_status_reason_code": None,
        "runtime": "python3.12", "handler": "lambda_function.lambda_handler",
        "environment": {}, "memory_size": 128,
        "function_arn": "arn:aws:lambda:us-east-1:000000000000:function:live-fn",
    })
    stores.rdsctl.set(env, "db:live-db", {
        "db_instance_identifier": "live-db", "status": "available", "status_reason": None,
        "master_username": "app", "master_password": "apppass123", "db_name": "postgres",
        "vpc_security_group_ids": ["sg-1"], "overlay_ip": None,
        "endpoint_address": "127.0.0.1", "endpoint_port": 54329, "engine": "postgres",
    })
    stores.lambdactl.set(env, "fn:worker", {
        "function_name": "worker", "state": "Failed",
        "state_reason": "container removed outside odin", "state_reason_code": "InternalError",
        "last_update_status": "Successful", "last_update_status_reason": None,
        "last_update_status_reason_code": None, "runtime": "python3.12",
        "handler": "lambda_function.lambda_handler", "environment": {}, "memory_size": 128,
        "function_arn": "arn:aws:lambda:us-east-1:000000000000:function:worker",
    })
    stores.rdsctl.set(env, "db:app-db", {
        "db_instance_identifier": "app-db", "status": "failed",
        "status_reason": "container removed outside odin",
        "master_username": "app", "master_password": "apppass123", "db_name": "postgres",
        "vpc_security_group_ids": [], "overlay_ip": None, "endpoint_address": "127.0.0.1",
        "endpoint_port": 0, "engine": "postgres",
    })
    stores.ecsctl.set(env, "service:odin:web", {
        "cluster_name": "odin", "service_name": "web", "status": "ACTIVE", "desired_count": 1,
        "task_definition_arn": "arn:aws:ecs:us-east-1:000000000000:task-definition/web:1",
    })
    stores.ecsctl.set(env, "taskdef:web:1", {
        "family": "web", "revision": 1, "cpu": "256", "memory": "512",
        "container_definitions": [{"name": "web", "image": "nginx:alpine"}],
    })
    stores.ec2net.set(env, "sg:sg-1", {
        "group_id": "sg-1", "group_name": "default", "vpc_id": "vpc-1",
        "description": "default", "ingress": [], "egress": [],
    })
    stores.ec2net.set(env, "vpc:vpc-1", {
        "vpc_id": "vpc-1", "cidr_block": "10.0.0.0/16", "default_sg_id": "sg-1",
        "nebula_network": "10.88.0.0/16",
    })
    stores.ec2compute.set(env, "instance:i-1", {
        "instance_id": "i-1", "state_name": "running", "instance_type": "t3.micro",
        "vpc_id": "vpc-1", "security_group_ids": [], "private_ip": "10.0.0.5",
        "image_id": "ami-odin", "tags": {},
    })
    stores.route53ctl.set(env, "zone:Z1", {"id": "Z1", "name": "svc.internal."})
    stores.route53ctl.set(env, "rrset:Z1", [
        {"Name": "box.svc.internal.", "Type": "A", "TTL": 60,
         "ResourceRecords": [{"Value": "10.0.0.5"}]},
    ])


def _patch_translate(monkeypatch, files: dict[str, str]) -> None:
    async def fake_translate(stack, **kwargs):
        return TranslateResult(files=files, refined=False)

    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)


def test_apply_full_on_a_fake_runtime_spawns_no_process_at_all(tmp_path, monkeypatch):
    """The headline. A canvas with every substrate-backed kind on it, records
    seeded so every converge and every sweep has real work, and NOT ONE
    process."""
    _patch_translate(monkeypatch, {"main.tf": 'resource "aws_s3_bucket" "uploads" {}\n'})
    runner = NoTofu()
    app = _isolated_app(tmp_path, runner)
    _seed_failed_records(app)
    _seed_mesh(tmp_path)

    with SpawnRecorder() as spawns, TestClient(app) as client:
        resp = client.post("/apply-full", json=EVERY_SUBSTRATE)

    assert resp.status_code == 200, resp.text
    assert spawns.machine_calls == [], spawns.machine_calls
    # ...and the route really did run the whole way, rather than being isolated
    # by refusing early. THIS half is what stops the silence above being free: a
    # 409 spawns nothing either, and so does a route that raised on line one.
    body = resp.json()
    assert runner.applied == [ENV], "the tofu half never ran -- the silence above proves nothing"
    # Every recovery pass ran and REPORTED, which is the other in-band witness
    # that the post-apply half was reached rather than skipped. Named in full
    # rather than counted: a count would survive one of the two going quiet.
    assert {r["node"] for r in body.get("recovered_resources", [])} == {"app-db", "worker"}, body
    # The lambda converge fails on this fixture (`FakeRuntime` has no
    # `host_port`, so the RIE boot cannot complete) and the apply says so --
    # which is the honest status here, and pinned exactly rather than as one of
    # a permissive set. A set would have hidden the bug this line caught: the
    # fixture used to answer `applied_services_unhealthy` after waiting out the
    # whole ECS steady-state budget, so the ratchet FAILED unmutated and every
    # mutation was "caught" for free.
    assert body["status"] == "applied_resources_unhealthy", body


def test_the_recorder_sees_a_process_when_one_is_born(tmp_path):
    """The recorder's own falsification, in-band. A guard that silently records
    nothing looks EXACTLY like isolation -- this repo has paid for that twice
    (a `kill -STOP` that signalled nothing; a fault injection that was a
    no-op). So the recorder proves it can fire before any test believes its
    silence.

    Both hooks, because they are independent: `Popen` catches the sync spawns
    and `subprocess_exec` catches the asyncio ones, and a fix that reverted
    only one of them would leave a hole this asserts is closed.

    The paths are DELIBERATELY absent (`/nonexistent/...`), so this test does
    not itself run the very binaries the file is about -- and it still proves
    what it needs to, because both hooks record BEFORE the exec: a spawn that
    fails with `FileNotFoundError` has already been seen. `machine_calls`
    matches on the basename, so `/nonexistent/docker` is a docker call for
    exactly the reason a real one is."""
    with SpawnRecorder() as spawns:
        with contextlib.suppress(FileNotFoundError):
            subprocess.run(["/nonexistent/docker", "--odin-probe"], capture_output=True, check=False)

        async def spawn() -> None:
            with contextlib.suppress(FileNotFoundError):
                await asyncio.create_subprocess_exec(
                    "/nonexistent/limactl", "--odin-probe",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )

        asyncio.run(spawn())

    seen = " ".join(spawns.machine_calls)
    assert "/nonexistent/docker --odin-probe" in seen, spawns.machine_calls
    assert "/nonexistent/limactl --odin-probe" in seen, spawns.machine_calls


def test_destroy_on_a_fake_runtime_spawns_no_process_at_all(tmp_path, monkeypatch):
    """The other half of the asymmetry that made the leak certain. Creation was
    real and teardown was fake, so `/destroy` reaped a stand-in and left the
    container running -- measured, `odin-rds-conn2-app-db` and
    `odin-rds-conn2-other-db` still up after a destroy reported success.

    A destroy that spawns nothing is only honest if creation spawned nothing
    either, which is what the test above pins; the two are one contract."""
    _patch_translate(monkeypatch, {"main.tf": 'resource "aws_s3_bucket" "uploads" {}\n'})
    runner = NoTofu()
    app = _isolated_app(tmp_path, runner)
    _seed_failed_records(app)
    _seed_mesh(tmp_path)

    with TestClient(app) as client:
        client.post("/apply-full", json=EVERY_SUBSTRATE)
        with SpawnRecorder() as spawns:
            resp = client.post("/destroy")

    assert resp.status_code == 200, resp.text
    assert spawns.machine_calls == [], spawns.machine_calls
