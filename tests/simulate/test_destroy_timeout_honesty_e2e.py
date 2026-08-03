"""Field test 4 (HIGH): `odin destroy` exited 0 after 300s claiming success.

The response said `status: destroyed` while its OWN nested payload said
`tf: failed (exit code -9)`; the env was still listed, six resources were still
in tofu state and containers were still running. The suggested remedy ("apply
first, then destroy") produced another 300.31s and another exit 0.

This proves the fix against a REAL `tofu destroy` that really is killed on its
whole-call deadline -- not a stubbed runner. `ODIN_TOFU_DESTROY_TIMEOUT` is
shrunk to a fraction of a second, which reaches the same code path the field
hit at 300s: `TfRunner._run` kills the process GROUP with SIGKILL, so the result
carries a negative exit code, which is what separates "ran out of time, nothing
was diagnosed" from "tofu errored".

The canvas is deliberately container-free (`aws_iam_role`s, synthesized
entirely inside the gateway) -- this test is about the destroy REPORT, and
booting real backings to prove it would only make it slow and flaky.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.agent import translate as translate_mod
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "destroy-timeout-e2e"
NODE = "app-role-00"
# Enough resources that a real `tofu destroy` cannot possibly finish inside the
# 1-second floor `TfRunner._remaining` gives a phase whose deadline is already
# blown -- this must be a genuine kill, not a race with a fast machine.
ROLES = 20

CANVAS = {
    "nodes": [
        {"id": f"r{i}", "type": "iam_role", "data": {"label": f"app-role-{i:02d}"}}
        for i in range(ROLES)
    ],
    "edges": [],
}


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-destroy-timeout-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _state_resources(root: Path) -> list[str]:
    state = root / ENV / "tf" / "terraform.tfstate"
    parsed = json.loads(state.read_text()) if state.exists() else {}
    return [f"{r['type']}.{r['name']}" for r in parsed.get("resources", []) if r.get("mode") != "data"]


def test_a_real_destroy_killed_on_its_deadline_is_not_reported_as_destroyed(store_root, monkeypatch):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)
    # Read once, in TfRunner.__init__ -- so it has to be set before create_app.
    monkeypatch.setenv("ODIN_TOFU_DESTROY_TIMEOUT", "0.1")

    store = SpecStore(store_root)
    app = create_app(store=store)
    with TestClient(app) as client:
        applied = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied", applied.json()
        assert len(_state_resources(store_root)) == ROLES

        started = time.monotonic()
        resp = client.post("/destroy", params={"env": ENV})
        elapsed = time.monotonic() - started
        print(f"\n[FT4] a destroy killed at its deadline took {elapsed:.1f}s -> {resp.status_code}")

    body = resp.json()
    # THE bug: this used to be 200 / `destroyed` / exit 0, with the failure
    # visible only in the nested `tf` payload nobody keys on.
    assert resp.status_code == 500, body
    assert body["status"] == "destroy_timed_out", body
    assert body["tf"]["status"] == "failed", body
    assert body["tf"]["exit_code"] < 0, "a whole-call-deadline kill is a SIGNAL, not a tofu verdict"
    # `cli/http.body_or_fail` keys on `error`, so this is what makes the CLI
    # exit nonzero -- and it names what is still standing.
    assert body["error"], body
    assert "whole-call deadline" in body["error"], body["error"]
    assert "aws_iam_role.app_role_00" in body["error"], body["error"]
    assert len(body["still_standing"]["tf_state"]) == ROLES, body["still_standing"]
    # ...and the claim is TRUE: the resources really are still in tofu's state.
    assert len(_state_resources(store_root)) == ROLES
