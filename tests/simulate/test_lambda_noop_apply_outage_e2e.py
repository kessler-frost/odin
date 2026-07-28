"""Field test 3's hole, for LAMBDA -- the kind the fix never reached.

`test_ecs_noop_apply_outage_e2e.py` proves it for ECS. Lambda has the identical
fire-and-verify-later shape and had no guard at all: `converge_functions` starts
a redeploy on an Apply and returns, so /apply-full answered `applied` (and
`odin apply` exited 0) the instant that thread was spawned, whatever became of
it. tofu cannot close this one EITHER -- and for a stricter reason than ECS's:
an `aws_lambda_function`'s config is unchanged when its execution environment
dies, and the provider's schema has no state attribute to diff on, so its plan
is empty forever and its create waiter never runs a second time.

The trigger is the field-verified one, and it is deliberately the SAME one the
ECS test uses: a broken `${{...}}` ref in the node's `env` map. That map is
injected at container launch and is NOT part of the `aws_lambda_function`
resource (`agent/hcl.py::_lambda` says so in as many words), so adding it is a
guaranteed empty plan -- the exact path that had no guard.

  1. a healthy function                         -> applied, exit 0, prompt
  2. + the broken ref, function still Active    -> applied, exit 0  (no false
                                                   positive on a no-op)
  3. the RIE container is REMOVED; the reality
     sweep marks it Failed; re-apply            -> FAILS, names the function
                                                   and the broken ref itself
  4. drop the ref, re-apply                     -> applied, exit 0, recovered

Step 3 is the bug. Steps 2 and 4 are the guardrails: a no-op apply on a healthy
function must stay green, and a function that CAN converge must be converged by
the Apply rather than failed by it.

Same substrate constraint as `test_lambda_tf_e2e.py`: the store root must live
under `$HOME` (Colima only mounts that tree, and an empty `/var/task` is a real
`Runtime.ImportModuleError`), so `store_root` roots it in the checkout rather
than pytest's `tmp_path`. Container hygiene is absolute and scoped to this
env's own container name.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.agent import hcl
from odin.agent import translate as translate_mod
from odin.compute.functions import container_name
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "lambda-noop-outage-e2e"
NODE = "worker"
BROKEN_REF = "${{ghost.ENDPOINT}}"
CODE = "def lambda_handler(event, context):\n    return event\n"

EMPTY_CANVAS: dict = {"nodes": [], "edges": []}


def _canvas(env_map: dict[str, str] | None = None) -> dict:
    data = {"label": NODE, "runtime": "python3.12", "code": CODE}
    return {
        "nodes": [{"id": "n1", "type": "lambda", "data": {**data, **({"env": env_map} if env_map else {})}}],
        "edges": [],
    }


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def _fn_record(root: Path) -> dict:
    path = root / ENV / "gateway" / "lambdactl.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return next((fn for key, fn in state.items() if key.startswith("fn:")), {})


@pytest.fixture
def store_root():
    """Under the repo checkout, never pytest's `tmp_path` -- see the module
    docstring (Colima mounts `$HOME` only)."""
    root = Path(__file__).resolve().parents[2] / ".odin-lambda-noop-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def lambda_cleanup():
    """Scoped to THIS env's own container name -- never a blanket
    `label=odin=1` sweep, which would rm containers this test did not create."""
    yield
    _docker("rm", "-f", "-v", container_name(ENV, NODE))


def _apply(client, canvas: dict) -> tuple[dict, float]:
    started = time.monotonic()
    resp = client.post("/apply-full", params={"env": ENV}, json=canvas)
    assert resp.status_code == 200, resp.text
    return resp.json(), time.monotonic() - started


def _await_state(root: Path, want: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        record = _fn_record(root)
        if record.get("state") == want:
            return record
        assert time.monotonic() < deadline, f"never reached {want}: {record.get('state')}"
        time.sleep(0.5)


def test_a_noop_apply_cannot_report_success_on_a_function_that_never_came_back(
    store_root, monkeypatch, lambda_cleanup,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    # The reality sweep on EVERY tick, so step 3's removed container is noticed
    # in seconds rather than on the production ~10-tick cadence. It is the same
    # sweep either way -- only its period changes.
    #
    # LEGITIMATE HERE, and field test 5 is why that needs saying (honesty rule
    # 1b). What this test measures is an apply whose CONVERGENCE cannot succeed
    # -- the redeploy dies on an unresolvable `${{ghost.ENDPOINT}}` -- and a
    # `Failed` record is that scenario's PRECONDITION, not its guard. Shortening
    # the cadence only reaches the precondition sooner. The residual this file
    # used to step around (is the apply honest BEFORE any sweep has run?) is
    # measured at the full default cadence in test_false_green_window_e2e.py.
    monkeypatch.setenv("ODIN_DRIFT_SWEEP_TICKS", "1")

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        # --- 1. a genuinely healthy function --------------------------------
        body, elapsed = _apply(client, _canvas())
        print(f"\n[FT3-lambda] fresh apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert body["tf"]["status"] == "ok", body
        assert "unhealthy_resources" not in body, body
        assert _fn_record(store_root)["state"] == "Active"
        assert container_name(ENV, NODE) in _docker("ps", "--format", "{{.Names}}").stdout

        # --- 2. add the broken ref: a no-op apply on a HEALTHY function -----
        # The `env` map is not in the `aws_lambda_function` resource, so tofu's
        # plan is empty and the running container is untouched. Must stay green,
        # and must stay FAST -- the verification may not tax the happy path.
        body, elapsed = _apply(client, _canvas({"NEED": BROKEN_REF}))
        print(f"[FT3-lambda] no-op apply on a healthy function took {elapsed:.1f}s")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body
        assert "unhealthy_resources" not in body, body
        assert elapsed < 90, f"a healthy no-op apply must stay prompt, took {elapsed:.1f}s"

        # --- 3. the sandbox is removed, then THE BUG ------------------------
        # `docker rm -f` is what a container destroyed out of band looks like to
        # the reality sweep, which marks the function Failed. Every redeploy now
        # dies on the unresolvable ref, so the function cannot converge -- while
        # tofu still has nothing whatsoever to do.
        _docker("rm", "-f", "-v", container_name(ENV, NODE))
        _await_state(store_root, "Failed")

        body, elapsed = _apply(client, _canvas({"NEED": BROKEN_REF}))
        print(f"[FT3-lambda] no-op apply on a BROKEN function took {elapsed:.1f}s -> {body['status']}")
        # tofu genuinely had nothing to do -- exactly why it could never have
        # caught this, and why odin has to.
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        # THE regression: this used to be `applied`, exit 0, on a dead function.
        assert body["status"] == "applied_resources_unhealthy", body
        (fault,) = body["unhealthy_resources"]
        assert fault["kind"] == "lambda", fault
        assert fault["node"] == NODE, fault
        assert fault["observed"] == "Failed", fault
        # ...and it names the real underlying reason, in the APPLY's own output.
        assert "ghost" in (fault["reason"] or ""), fault
        assert "ghost" in body["note"] and NODE in body["note"], body["note"]
        assert elapsed < 180, f"the failure must be bounded, took {elapsed:.1f}s"

        # --- 4. drop the ref: the Apply is the recovery, and it is quick -----
        body, elapsed = _apply(client, _canvas())
        print(f"[FT3-lambda] recovery apply took {elapsed:.1f}s")
        assert body["status"] == "applied", body
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert "unhealthy_resources" not in body, body
        assert elapsed < 180, f"recovery must not crawl, took {elapsed:.1f}s"
        assert _fn_record(store_root)["state"] == "Active"
        assert container_name(ENV, NODE) in _docker("ps", "--format", "{{.Names}}").stdout

        # --- 5. teardown still completes promptly ---------------------------
        body, elapsed = _apply(client, EMPTY_CANVAS)
        print(f"[FT3-lambda] teardown apply took {elapsed:.1f}s")
        assert body["tf"] == {"status": "ok", "exit_code": 0}, body
        assert body["status"] == "applied", body

    leftover = _docker("ps", "-aq", "--filter", f"name={container_name(ENV, NODE)}")
    assert leftover.stdout.strip() == "", f"the RIE container survived: {leftover.stdout}"


def test_a_broken_ref_is_refused_upfront_instead_of_applied(store_root, monkeypatch, lambda_cleanup):
    """Half of the split (owner decision, 2026-07-27); see the ECS twin.

    odin's wiring guard now refuses an apply carrying an unresolvable
    `${{ghost.ENDPOINT}}` ref BEFORE it runs. That is strictly better than
    applying it and discovering the damage later -- and it is what makes the
    no-op test above unreachable by its original route, since that route WAS
    the broken ref. Asserted here as the guarantee it now is.
    """
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    with TestClient(create_app(store=SpecStore(store_root))) as client:
        resp = client.post("/apply-full", params={"env": ENV}, json=_canvas({"NEED": BROKEN_REF}))
        assert resp.status_code == 409, f"an unresolvable ref must be refused, got {resp.status_code}: {resp.text}"
        assert "ghost" in resp.text, resp.text
        # Refused BEFORE anything ran: no container was built.
        ps = _docker("ps", "-aq", "--filter", f"name={container_name(ENV, NODE)}")
        assert ps.stdout.strip() == "", f"a refused apply must not have created a container: {ps.stdout}"
