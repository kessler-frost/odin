"""Every command that reads the canvas must read the env's OWN canvas.

## The bug, found by field test 7 (2026-07-28)

The canvas became per-environment in v0.7.9 and `/canvas` has honoured `?env=`
ever since. Two CLI call sites were never updated:

    odin apply     --env ft   ->  GET /canvas          (no env!)  ->  POST /apply-full?env=ft
    odin translate --env ft   ->  GET /canvas          (no env!)

So `odin apply --env ft` fetched the DEFAULT env's canvas and built it in `ft`.
Measured: an `ft` canvas holding s3 + sqs + rds came up as the default env's
lambda + its auto-role, and `ft`'s own three resources never existed at all.

`translate` merely printed the wrong Terraform. `apply` is the dangerous one,
because an apply RECONCILES: any resource the target env legitimately had and
the default canvas does not name gets torn down. A user with `prod` and
`staging` drawing different architectures had no way to apply either from the
CLI, and would have destroyed one with the other.

## Why this file checks the SHAPE

Fixing the two instances is not the fix -- three earlier releases each fixed one
instance of "a route ignores `?env=`" and the next one appeared somewhere else
(the canvas router's own docstring tells that story about the route itself). So
this asserts the property over EVERY canvas-reading command: whatever `--env`
you pass is the env whose canvas is fetched. A new command that forgets fails
here rather than in someone's environment.
"""
from __future__ import annotations

import pytest

from odin.cli import apply as apply_mod
from odin.cli import translate as translate_mod

CANVAS = {"nodes": [{"id": "s3-1", "type": "s3", "position": {"x": 0, "y": 0},
                     "data": {"label": "uploads"}}], "edges": []}


class _Recorder:
    """Stands in for `http.request`, recording what each call asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, url: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs.get("params") or {}))
        return _Response(CANVAS if path == "/canvas" else {"status": "applied", "env": "x", "rev": "r"})

    def env_for(self, path: str) -> str | None:
        return next((params.get("env") for method, p, params in self.calls if p == path), None)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict:
        return self._payload


@pytest.mark.parametrize("env", ["ft", "prod", "staging"])
def test_apply_reads_the_canvas_of_the_env_it_was_given(monkeypatch, env):
    """THE bug. `odin apply --env prod` built prod from DEFAULT's drawing."""
    recorder = _Recorder()
    monkeypatch.setattr(apply_mod.http, "request", recorder)
    monkeypatch.setattr(apply_mod.http, "body_or_fail", lambda resp, *a, **k: resp.json())

    apply_mod._graph("http://x", env, None, apply_mod.OutputFormat.text)

    assert recorder.env_for("/canvas") == env, (
        f"apply fetched the canvas without env={env!r} — it would build this "
        "environment from the DEFAULT env's drawing, and tear down whatever the "
        "default canvas does not name"
    )


@pytest.mark.parametrize("env", ["ft", "prod"])
def test_translate_previews_the_canvas_of_the_env_it_was_given(monkeypatch, env):
    recorder = _Recorder()
    monkeypatch.setattr(translate_mod.http, "request", recorder)
    monkeypatch.setattr(translate_mod.http, "body_or_fail", lambda resp, *a, **k: resp.json())

    translate_mod._graph("http://x", env, None)

    assert recorder.env_for("/canvas") == env


def test_a_file_argument_still_bypasses_the_server_entirely(monkeypatch, tmp_path):
    """`--file` previews an UNSAVED canvas, so it must not fetch at all — the env
    is irrelevant and a request here would be a surprise round trip."""
    recorder = _Recorder()
    monkeypatch.setattr(apply_mod.http, "request", recorder)
    path = tmp_path / "c.json"
    path.write_text('{"nodes": [], "edges": []}')

    graph = apply_mod._graph("http://x", "prod", path, apply_mod.OutputFormat.text)

    assert graph == {"nodes": [], "edges": []}
    assert recorder.calls == []


# --- the shape ----------------------------------------------------------------


def test_no_cli_module_fetches_the_canvas_without_an_env():
    """The ratchet. Three releases each fixed one instance of "a route ignores
    `?env=`"; the next appeared elsewhere. A new command that forgets fails
    HERE, naming itself, rather than in someone's environment.
    """
    import re
    from pathlib import Path

    cli = Path(__file__).resolve().parents[2] / "src" / "odin" / "cli"
    offenders: list[str] = []
    for module in sorted(cli.glob("*.py")):
        for match in re.finditer(r'request\(\s*"[A-Z]+",\s*url,\s*"/canvas"(.*?)\)', module.read_text(), re.S):
            if "env" not in match.group(1):
                offenders.append(f"{module.name}: {match.group(0)[:70]}")
    assert offenders == [], (
        "these fetch or write the canvas without an env, so they act on the "
        f"DEFAULT env whatever --env says: {offenders}"
    )
