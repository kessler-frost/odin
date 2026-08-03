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
resource (`iac/hcl.py::_lambda` says so in as many words), so adding it is a
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

from odin.iac import hcl
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


# --- the no-op claim: where it lives now, and why not here ------------------
#
# The e2e version of that test USED TO stand here, and it reached its zero-state
# with a broken `${{ghost.ENDPOINT}}` ref in the node's `env` map -- chosen
# because that map is injected at container launch and is NOT part of the
# `aws_lambda_function` resource, making every apply a guaranteed empty plan.
#
# odin now REFUSES that apply with a 409 (asserted below), so the route is gone
# by design. Unlike ECS, lambda has no substitute route through real
# containers, and all three candidates were measured to be dead ends:
#
#   bad runtime   `functions.py` does RUNTIME_IMAGES.get(runtime,
#                 RUNTIME_IMAGES[DEFAULT_RUNTIME]) -- an unknown runtime falls
#                 back to the DEFAULT image and boots a WORKING container
#   memory        `memory_size` is neither settable from the canvas nor emitted
#                 by `iac/hcl.py`, so it cannot make `docker run` refuse
#   broken code   readiness is a TCP check on the RIE port
#                 (`functions.py::_await_ready`), which a broken handler still
#                 satisfies -- the error only appears on invoke
#
# The CLAIM is not retired, it moved to where it can be made honestly:
# `tests/api/test_apply_full.py::
#  test_apply_full_fails_on_a_function_whose_container_is_gone_though_the_record_says_active`
# seeds an `Active` record, removes the container, injects a FunctionRuntime
# whose boot raises, and asserts `applied_resources_unhealthy`. Its "tofu had
# nothing to do" premise is STRONGER than this file's empty plan: tofu is not
# installed at all in that test, so no plan runs. What is lost is only the real
# -container substrate, which the ECS e2e still covers for the same shape.


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
