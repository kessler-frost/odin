"""Field test 5 (HIGH): `odin destroy` on a server that cannot find `tofu`.

The trigger is mundane and this test reproduces it exactly rather than
stubbing it: odin is launched from something that is not a login shell, so
`/opt/homebrew/bin` is not on its PATH and `shutil.which("tofu")` finds
nothing. v0.7.4 answered `status: destroyed`, exit 0, over an env whose every
Terraform-managed resource was still in state -- and committed an empty Stack
on the way out, so the NEXT destroy's `ensure_backings(last_applied)` started
no backing containers and tofu's AWS calls 503-retried to the 300s deadline.

Real substrate throughout: a real `tofu apply` creates a real bucket in a real
rustfs backing, the destroy that cannot find tofu must fail honestly and leave
the desired state alone, and the retry -- with tofu back on PATH -- must
actually work, which it can only do if the backing was booted from a Stack
that survived.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import typer
from fastapi.testclient import TestClient

from odin.iac import hcl
from odin.agent import translate as translate_mod
from odin.cli import http
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "destroy-no-tofu-e2e"
CANVAS = {"nodes": [{"id": "n1", "type": "s3", "data": {"label": "ledger"}}], "edges": []}


@pytest.fixture
def store_root():
    root = Path(__file__).resolve().parents[2] / ".odin-destroy-no-tofu-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _state_resources(root: Path) -> list[str]:
    state = root / ENV / "tf" / "terraform.tfstate"
    parsed = json.loads(state.read_text()) if state.exists() else {}
    return sorted(f"{r['type']}.{r['name']}" for r in parsed.get("resources", []) if r.get("mode") != "data")


def _path_without_tofu(shim: Path) -> str:
    """The operator's own situation, reproduced rather than stubbed: the
    directory holding `tofu` is not on this process's PATH, so the REAL
    preflight (`shutil.which("tofu")`) finds nothing. Nothing about
    `TfRunner` is monkeypatched.

    `docker` lives in that same directory on a Homebrew machine and odin's
    backings genuinely need it, so it is symlinked into `shim` and kept --
    otherwise the test would be about a missing container runtime rather than
    about a missing tofu."""
    tofu_dir = Path(shutil.which("tofu")).parent
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "docker").symlink_to(shutil.which("docker"))
    kept = [p for p in os.environ["PATH"].split(os.pathsep) if p != str(tofu_dir)]
    return os.pathsep.join([str(shim), *kept])


def test_a_destroy_that_cannot_find_tofu_fails_honestly_and_does_not_brick_the_env(
    store_root, tmp_path, monkeypatch,
):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    real_path = os.environ["PATH"]

    async def fake_translate(stack, **kwargs):
        skeleton = hcl.generate_tf(stack)
        return translate_mod.TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        )
    monkeypatch.setattr("odin.server.translate_mod.translate", fake_translate)

    app = create_app(store=SpecStore(store_root))
    with TestClient(app) as client:
        applied = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied", applied.json()
        assert _state_resources(store_root) == ["aws_s3_bucket.ledger"]
        desired = app.state.store.get_stack(ENV)
        assert desired.resources != ()

        # ...and now tofu is simply not findable, exactly as it isn't for a
        # server started outside a login shell.
        monkeypatch.setenv("PATH", _path_without_tofu(tmp_path / "shim"))
        assert shutil.which("tofu") is None
        assert shutil.which("docker") is not None, "this test is about a missing tofu, nothing else"
        refused = client.post("/destroy", params={"env": ENV})

        assert refused.status_code == 500, refused.text
        body = refused.json()
        assert body["status"] == "destroy_unavailable", body
        assert "not on this server's PATH" in body["error"], body["error"]
        assert "aws_s3_bucket.ledger" in body["error"], body["error"]
        # `odin destroy` exits nonzero on this -- v0.7.4 exited 0.
        with pytest.raises(typer.Exit):
            http.body_or_fail(refused)

        # The bucket really is still there, and so is the desired state that a
        # retry needs in order to boot the backing it lives in.
        assert _state_resources(store_root) == ["aws_s3_bucket.ledger"]
        assert app.state.store.get_stack(ENV) == desired, "the env was bricked: the Stack was emptied"

        # The retry, with tofu back. This can only succeed if `ensure_backings`
        # had a non-empty Stack to boot rustfs from -- the empty Stack is what
        # made this hang for 5:00.38 and need the original canvas to recover.
        monkeypatch.setenv("PATH", real_path)
        retried = client.post("/destroy", params={"env": ENV})
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "destroyed", retried.json()
        assert _state_resources(store_root) == []
        assert app.state.store.get_stack(ENV).resources == ()
