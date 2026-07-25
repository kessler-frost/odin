"""FIELD TEST 3 -- what an operator actually EXPERIENCES during a failed ECS
image update, sampled with real containers.

v0.7.1 made a bad image update fail the apply loudly, and that holds. What it
deliberately did NOT do was stop the service dropping to zero tasks first.
The measured cost, sampling every 2 seconds: three healthy tasks serving HTTP
200 at 18:28:27, **zero tasks by 18:28:31** -- roughly four seconds after the
apply started -- 108 consecutive samples at zero, every port refusing, and the
apply did not admit anything until 18:29:30. So a full outage inside ~4
seconds, ~59 seconds of CI showing "running" while the service was 100% down,
and no self-heal at all afterwards.

The trade-off that produced it was judged a real one: `minimumHealthyPercent
= 100` would keep the old tasks serving, but a revision-blind projection
would then have reported the node `healthy` while the deployment was dead --
"a worse lie than the outage". The dichotomy was false. odin already knows
which revision each task runs (`ecsctl._on_current_revision`, added by the
B1 fix), so "2 tasks serving the PREVIOUS revision, the new deployment
failed" is computable, and `Phase` already carries `error` for a terminal
failure needing operator action. Both halves ship together
(`ecsctl._retire_stale` + `tf_status._ecs_services`).

This test is the sampled proof of the pair:
 1. across the ENTIRE failed apply, every previous-revision task keeps
    answering HTTP 200 -- the outage window is zero seconds;
 2. once `/world` is projected again it reads `error` -- never `crashed`
    (the service is serving) and never back to `healthy` -- with a verdict
    naming the surviving task count AND the image that failed;
 3. the apply still fails loudly (`applied_tf_failed`) -- v0.7.1's behavior
    unregressed;
 4. a GOOD update afterwards still rolls cleanly and quickly.

Served on a REAL bound port (`serve_in_thread`, the pattern
test_ecs_crash_observability_e2e.py already uses) so the sampler thread can
poll `/world` over real HTTP while the main thread blocks in `/apply-full` --
`TestClient`'s in-process transport cannot do that.

Deliberately no ALB: each task publishes its own host port, so reachability is
measured directly against the tasks that are (or are not) serving -- which is
how the original field-test measurement was taken ("all ports refusing").

STILL OPEN, and this test's sampling shows it plainly: `/apply-full` holds
`Reconciler.hold()` for the whole tofu run, so NO tick projects World while an
apply is in flight and `/world` stays frozen at its last pre-apply reading for
~60s. That is the "operator isn't told for ~59 seconds" half of the field-test
finding, it lives in reconcile/reconciler.py (not in this fix's scope), and it
is unchanged here. What DID change is what the frozen reading conceals: it used
to paper over a 100% outage, and now papers over a rollout whose old revision
is still serving every request. Recorded rather than asserted away.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.gateway.app import serve_in_thread, stop_in_thread
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "ecs-keep-serving-e2e"
NODE = "web"
GOOD_IMAGE = "nginx:alpine"
NEXT_IMAGE = "nginx:1.27-alpine"
BAD_IMAGE = "nginx:this-tag-does-not-exist-9z9z"
COUNT = 3
SAMPLE_INTERVAL = 2.0
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _canvas(image: str) -> dict:
    return {
        "nodes": [{"id": "n1", "type": "ecs", "data": {
            "label": NODE, "image": image, "count": str(COUNT), "port": "80"}}],
        "edges": [],
    }


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _task_records(root: Path) -> list[dict]:
    path = root / ENV / "gateway" / "ecsctl.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [task for key, task in state.items() if key.startswith("task:")]


def _serving_ports(root: Path) -> list[int]:
    """Every host port a RUNNING task is currently published on -- the real
    addresses a client would reach this service at, with no ALB in the way."""
    return [
        int(port)
        for task in _task_records(root)
        if task["last_status"] == "RUNNING"
        for port in (task.get("host_ports") or {}).values()
    ]


def _http_ok(port: int) -> bool:
    """One real request. A refused connection is the outage signal the field
    test recorded, so a failure to connect must count as "not served", never
    raise -- hence the bare-Exception catch (this is a measurement, not
    control flow)."""
    try:
        return httpx.get(f"http://127.0.0.1:{port}/", timeout=2.0).status_code == 200
    except Exception:
        return False


@dataclass
class Sample:
    at: float
    running: int
    reachable: int
    phase: str
    verdict: str


@dataclass
class Sampler:
    """Every ~2s: how many tasks are RUNNING, how many of their ports actually
    answer HTTP 200, and what `/world` says -- the operator's whole view."""

    base: str
    root: Path
    stop: threading.Event = field(default_factory=threading.Event)
    samples: list[Sample] = field(default_factory=list)
    _thread: threading.Thread | None = None

    def _world(self) -> tuple[str, str]:
        body = httpx.get(f"{self.base}/world", params={"env": ENV}, timeout=10).json()
        node = next((r for r in body["resources"] if r["id"] == NODE), None)
        return ((node or {}).get("phase") or "absent", (node or {}).get("verdict") or "")

    def _loop(self, started: float) -> None:
        while not self.stop.is_set():
            ports = _serving_ports(self.root)
            phase, verdict = self._world()
            self.samples.append(Sample(
                at=time.monotonic() - started, running=len(ports),
                reachable=sum(1 for p in ports if _http_ok(p)), phase=phase, verdict=verdict,
            ))
            self.stop.wait(SAMPLE_INTERVAL)

    def __enter__(self) -> Sampler:
        self._thread = threading.Thread(target=self._loop, args=(time.monotonic(),), daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self._thread.join(timeout=30)

    def report(self, title: str) -> None:
        print(f"\n[field-3] {title}")
        print("[field-3]   t(s)  tasks  http200  phase")
        for s in self.samples:
            print(f"[field-3]  {s.at:5.1f}  {s.running:5d}  {s.reachable:7d}  {s.phase}")


@pytest.fixture
def ecs_cleanup():
    yield
    ps = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    for container_id in (line for line in ps.stdout.splitlines() if line):
        _docker("rm", "-f", "-v", container_id)


def test_a_failed_image_update_never_stops_serving_and_never_reads_healthy(tmp_path, monkeypatch, ecs_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    store = SpecStore(tmp_path)
    app = create_app(store=store)
    server, thread, port = serve_in_thread(app, port=0)
    base = f"http://127.0.0.1:{port}"
    try:
        # --- 1. a genuinely healthy 3-task service, really serving ----------
        first = httpx.post(f"{base}/apply-full", params={"env": ENV}, json=_canvas(GOOD_IMAGE), timeout=600)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "applied", first.json()

        ports = _serving_ports(store.root)
        assert len(ports) == COUNT, f"expected {COUNT} serving tasks, got {ports}"
        assert all(_http_ok(p) for p in ports), f"baseline is not actually serving: {ports}"

        # --- 2. one typo'd tag, sampled across the WHOLE apply --------------
        with Sampler(base=base, root=store.root) as sampler:
            bad_start = time.monotonic()
            second = httpx.post(f"{base}/apply-full", params={"env": ENV}, json=_canvas(BAD_IMAGE), timeout=600)
            bad_elapsed = time.monotonic() - bad_start
            # Two extra sampling rounds AFTER the apply returns: the measured
            # failure included "no self-heal", so the state the operator is
            # left in matters as much as the state during.
            time.sleep(SAMPLE_INTERVAL * 2)
        sampler.report(f"bad-image update ({bad_elapsed:.1f}s apply)")

        # v0.7.1, unregressed: the apply still fails, loudly.
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "applied_tf_failed", second.json()
        assert second.json()["tf"]["exit_code"] != 0, second.json()

        assert sampler.samples, "the sampler never ran"
        # THE measurement. Before: zero tasks ~4s in, 108 consecutive samples
        # at zero, every port refusing. After: no sample below full capacity.
        outage = [s for s in sampler.samples if s.reachable == 0]
        assert not outage, f"the service stopped serving during the failed apply: {outage}"
        assert min(s.reachable for s in sampler.samples) == COUNT, (
            f"capacity dipped during the failed apply: {sampler.samples}"
        )

        # ... and the projection is honest about it, in BOTH directions.
        # NOTE on the leading `healthy` samples: World is FROZEN for the whole
        # apply (`Reconciler.hold()` -- see the module docstring), so those are
        # the last pre-apply reading, not a fresh wrong answer. The claim under
        # test is what the first re-projection says, and that it never reverts.
        phases = [s.phase for s in sampler.samples]
        assert "crashed" not in phases, f"a service still serving must not read crashed: {phases}"
        first_error = next((i for i, p in enumerate(phases) if p == "error"), None)
        assert first_error is not None, f"a failed deployment must surface: {phases}"
        assert "healthy" not in phases[first_error:], f"read healthy after the deployment failed: {phases}"

        final = sampler.samples[-1]
        assert final.phase == "error", final
        assert "serving the previous revision" in final.verdict, final.verdict
        assert BAD_IMAGE in final.verdict, final.verdict
        assert str(COUNT) in final.verdict, final.verdict

        # --- 3. a GOOD update still rolls cleanly, and quickly --------------
        with Sampler(base=base, root=store.root) as good_sampler:
            good_start = time.monotonic()
            third = httpx.post(f"{base}/apply-full", params={"env": ENV}, json=_canvas(NEXT_IMAGE), timeout=600)
            good_elapsed = time.monotonic() - good_start
        good_sampler.report(f"GOOD update ({good_elapsed:.1f}s apply)")

        assert third.status_code == 200, third.text
        assert third.json()["status"] == "applied", third.json()
        assert third.json()["tf"]["status"] == "ok", third.json()
        assert min(s.reachable for s in good_sampler.samples) >= 1, (
            f"a good rollout must never stop serving either: {good_sampler.samples}"
        )

        # The previous revision is genuinely RETIRED on success -- keeping old
        # tasks alive is a failure path, never a leak.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and len(_serving_ports(store.root)) != COUNT:
            time.sleep(1)
        assert len(_serving_ports(store.root)) == COUNT, _task_records(store.root)

        world = httpx.get(f"{base}/world", params={"env": ENV}, timeout=10).json()
        healthy = next((r for r in world["resources"] if r["id"] == NODE), None)
        assert healthy is not None and healthy["phase"] == "healthy", healthy

        # --- 4. teardown ----------------------------------------------------
        fourth = httpx.post(f"{base}/apply-full", params={"env": ENV}, json=EMPTY_CANVAS, timeout=600)
        assert fourth.status_code == 200, fourth.text
        assert fourth.json()["tf"] == {"status": "ok", "exit_code": 0}, fourth.json()
    finally:
        stop_in_thread(server, thread)

    leftover = _docker("ps", "-aq", "--filter", "label=odin=1", "--filter", f"name=odin-ecs-{ENV}-")
    assert leftover.stdout.strip() == "", f"ECS task containers survived: {leftover.stdout}"
