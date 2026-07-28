"""W2.4 -- THE LEAK TEST: a `secret`/`ssm` node's plaintext must never reach a
WebSocket frame or a line of `events.jsonl`.

v0.6.0 (security finding #3) built the machinery -- `FieldValue.sensitive`,
`Stack.sensitive_values()`, `scrub()` over the LLM prompt and over every
streamed `tofu` line -- but nothing wired the two kinds whose whole PURPOSE is
holding a secret into it, because neither kind existed yet. This test is the
end-to-end proof that they are wired now, and it goes through the REAL
`ConnectionManager` (so `events.jsonl` is genuinely written to disk and read
back) plus a fake `tofu` that behaves like the worst realistic case: a
provider printing the argument values verbatim.

It also asserts the ONE place the plaintext legitimately DOES appear -- the
generated `main.tf`, because tofu has to send the value to create the resource
-- so the test can never pass by accident on a canvas that lost its secret.
"""
from __future__ import annotations

import asyncio

import stat
from pathlib import Path

import pytest

from odin.agent import hcl
from odin.agent.translate import _prompt
from odin.api.events import ConnectionManager
from odin.simulate.runner import TfRunner
from odin.spec.models import REDACTED
from odin.spec.translate import canvas_to_stack

ENV = "leak-check"
SECRET_VALUE = "hunter2-and-then-some"
PARAM_VALUE = "param-s3cr3t-99"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "secret", "data": {"label": "db-password", "secretString": SECRET_VALUE}},
        {"id": "n2", "type": "ssm", "data": {
            "label": "/odin/api-key", "paramType": "SecureString", "paramValue": PARAM_VALUE,
        }},
    ],
    "edges": [],
}

# The worst realistic case: tofu's own diff printing both values verbatim (it
# has no concept of odin's `sensitive` flag -- the provider marking them
# sensitive is its choice, not something odin can rely on).
_FAKE_TOFU = (
    'if [ "$1" = "init" ]; then echo "Initializing..."; exit 0; fi\n'
    'if [ "$1" = "apply" ]; then\n'
    '  echo "aws_secretsmanager_secret_version.db_password: secret_string = ' + SECRET_VALUE + '"\n'
    '  echo "aws_ssm_parameter._odin_api_key: value = ' + PARAM_VALUE + '"\n'
    "  exit 0\nfi\n"
    'if [ "$1" = "destroy" ]; then echo "value = ' + PARAM_VALUE + '"; exit 0; fi'
)


def _write_fake_tofu(path: Path) -> Path:
    tofu = path / "tofu"
    tofu.write_text(f"#!/bin/sh\n{_FAKE_TOFU}\n")
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC)
    return tofu


class RecordingViewer:
    """A live SSE subscriber, recording exactly what would go out on the wire --
    the frames, not just the durable log.

    v0.8.7: this was a WebSocket stand-in with a `send_json` method, added
    straight to the manager's connection set. The stream is SSE now, so a
    subscriber IS a queue; this wraps one so the assertions below read the same.
    Added directly to `_subscribers` rather than through `subscribe()`, which is
    an async context manager built for a real request lifecycle."""

    def __init__(self, manager) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        manager._subscribers.add(self.queue)

    @property
    def frames(self) -> list[dict]:
        out: list[dict] = []
        while not self.queue.empty():
            message = self.queue.get_nowait()
            if message is not None:
                out.append(message)
        return out


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A real ConnectionManager (so `events.jsonl` is really written) with one
    recording viewer attached, plus the fake tofu on `which`."""
    _write_fake_tofu(tmp_path)
    monkeypatch.setattr("odin.simulate.runner.shutil.which", lambda name: str(tmp_path / "tofu"))
    ws = ConnectionManager(tmp_path)
    viewer = RecordingViewer(ws)
    return ws, viewer, tmp_path


async def test_a_secret_value_reaches_neither_a_stream_frame_nor_events_jsonl(wired):
    ws, viewer, root = wired
    stack = canvas_to_stack(CANVAS, env=ENV)
    project = hcl.generate_tf(stack)
    secrets = stack.sensitive_values()
    assert secrets == frozenset({SECRET_VALUE, PARAM_VALUE}), "both values must be collected as sensitive"

    runner = TfRunner(root, ws=ws)
    result = await runner.apply(ENV, project, 4266, "ak", "sk", secrets=secrets)
    await runner.destroy(ENV, 4266, "ak", "sk", secrets=secrets)

    assert result.ok is True
    events_log = (root / ENV / "events.jsonl").read_text()
    frames = repr(viewer.frames)
    for value in (SECRET_VALUE, PARAM_VALUE):
        assert value not in events_log, f"{value!r} leaked into events.jsonl"
        assert value not in frames, f"{value!r} leaked into a WebSocket frame"
        assert value not in " ".join(result.tail), f"{value!r} leaked into the failure tail"
    # ...and the redaction really happened (rather than the lines vanishing).
    assert REDACTED in events_log
    assert events_log.count(REDACTED) == 3  # two apply lines + one destroy line


async def test_the_generated_terraform_is_the_one_place_the_plaintext_appears(wired):
    """Not a leak -- the point. tofu has to send the value to create the
    resource, which is exactly why the redaction above has to exist at all.
    Asserting it also keeps the leak test honest: it can't pass because the
    canvas quietly lost its secret."""
    _ws, _viewer, _root = wired
    main_tf = hcl.generate_tf(canvas_to_stack(CANVAS, env=ENV)).files["main.tf"]

    assert f'secret_string = "{SECRET_VALUE}"' in main_tf
    assert f'value = "{PARAM_VALUE}"' in main_tf


async def test_neither_value_reaches_the_translation_agents_prompt(wired):
    """The other text surface v0.6.0 closed: the prompt is the one place a
    canvas's raw field values would leave the machine entirely (to the Claude
    API). Both the field listing and the embedded main.tf preview are scrubbed.
    """
    _ws, _viewer, _root = wired
    stack = canvas_to_stack(CANVAS, env=ENV)
    prompt = _prompt(hcl.generate_tf(stack), stack)

    assert SECRET_VALUE not in prompt
    assert PARAM_VALUE not in prompt
    assert REDACTED in prompt
    # The non-secret structure the agent actually needs is still there.
    assert "db-password" in prompt
    assert "aws_ssm_parameter" in prompt
