"""S3b — the translation agent: canvas -> Terraform, refined.

Flow: `generate_tf` (S3a) produces the deterministic skeleton FIRST —
determinism before intelligence. A best-effort claude-agent-sdk pass then
reviews it: the agent receives the skeleton + a canvas summary and may
propose refinements, but ONLY through the typed `emit_terraform` MCP tool
(the Global Constraint: "the agent NEVER writes files; it returns structured
output odin materializes"). Whatever the agent returns is deterministically
re-validated — never trusted — by `validate_refinement`: the resource SET
(type+name pairs) must equal the skeleton's exactly (arguments/comments/tags
may change, resources may not), and the files must both `tofu fmt`-parse and
`tofu validate`. Any failure at any stage — no supported resources, an SDK
error or timeout, a guardrail violation — falls back to the skeleton
verbatim, so Simulate always has something tofu-valid to apply.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypedDict

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SdkMcpTool, create_sdk_mcp_server, tool
from pydantic import BaseModel

from odin.agent import hcl
from odin.agent.hcl import TfProject, generate_tf
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.models import REDACTED, Stack, scrub
from odin.spec.store import rev_of

log = logging.getLogger("odin.translate")

_MODEL = "claude-sonnet-5"


def _default_timeout() -> float:
    """Release finding #5: the SDK refine pass's budget. The pass no longer
    sits on any request's critical path -- `translate()` returns the
    deterministic skeleton immediately and refines on a BACKGROUND task (see
    `TranslateCache`) -- so this timeout only bounds how long that background
    task runs before giving up, never how long a `/translate` or `/apply-full`
    caller waits. `ODIN_TRANSLATE_TIMEOUT` overrides it."""
    return float(os.environ.get("ODIN_TRANSLATE_TIMEOUT", "45"))


_TIMEOUT_S = _default_timeout()
_TAIL_LINES = 15

# The Global Constraint (plan doc): agent-emitted HCL stays portable -- no
# endpoints, no skip_* flags, no credentials, no local URLs (those live in
# odin's runtime-generated override.tf + env vars, never in agent output).
# The system prompt already tells the agent this; this is the deterministic
# backstop -- mirrors test_hcl.py's own check on the skeleton.
_FORBIDDEN_SUBSTRINGS = ("endpoints {", "skip_", "access_key", "secret_key", "127.0.0.1", "localhost")

_SYSTEM = (
    "You review Terraform that was deterministically generated from a visual "
    "infrastructure canvas. You may refine resource ARGUMENTS and add comments "
    "or tags for clarity. You must NEVER add or remove a resource block, and "
    "NEVER add provider endpoints, skip_* flags, or credentials — those are "
    "injected separately at apply time and must stay out of your output. "
    "Call emit_terraform exactly once with the complete file set (echo back "
    "any file you have no refinement for, unchanged)."
)


class TfFileInput(TypedDict):
    path: str
    content: str


class EmitTerraformInput(TypedDict):
    files: list[TfFileInput]
    notes: list[str]


class TranslateResult(BaseModel):
    model_config = {"frozen": True}
    files: dict[str, str] = {}
    notes: list[str] = []
    unsupported: list[str] = []
    refined: bool = False
    # V4c/release finding #1: a lambda node's zip'd deployment package. The
    # agent never sees it (only main.tf is in its prompt) and can't refine
    # it, so every return path below carries the skeleton's copy verbatim.
    binary_files: dict[str, bytes] = {}

    def for_display(self) -> dict:
        """The read-only `/translate` HTTP response shape (release finding #1).
        `binary_files` (a lambda's zip'd package, raw non-UTF8 bytes) is NOT
        JSON-serializable and the code panel / `odin translate` never need it --
        only /apply-full does, and it reads them off this object directly, never
        through this projection. Excluding them here is what keeps a Lambda
        canvas's `/translate` from 500-ing on serialization."""
        return self.model_dump(exclude={"binary_files"})


def make_emit_tool(collector: list[dict]) -> SdkMcpTool:
    """The ONE typed tool the agent may call. `collector` accumulates every
    call's raw args — the only effect channel out of the SDK pass; nothing
    else the agent does touches odin's state."""

    @tool("emit_terraform", "Emit the reviewed/refined Terraform file set for this canvas.", EmitTerraformInput)
    async def emit_terraform(args: EmitTerraformInput) -> dict:
        collector.append(dict(args))
        return {"content": [{"type": "text", "text": "recorded"}]}

    return emit_terraform


def _prompt(skeleton: TfProject, stack: Stack) -> str:
    # Security finding #3: the agent only ever REVIEWS argument values --
    # it never needs a real secret to do that, and this prompt is the one
    # place a canvas's raw field values leave odin's process (to Anthropic's
    # API). Sensitive fields go in redacted; a field embedded in the
    # generated HCL itself (e.g. an ECS task's secret-looking env var) is
    # additionally scrubbed out of the main.tf preview below, so the agent
    # never sees the value either way, but the REAL skeleton `generate_tf`
    # already built (never touched here) still carries it for the actual apply.
    resources = [
        {"id": r.id, "kind": r.kind,
         "fields": {k: (REDACTED if fv.sensitive else fv.value) for k, fv in r.fields.items()}}
        for r in stack.resources
    ]
    main_tf = scrub(skeleton.files.get("main.tf", ""), stack.sensitive_values())
    return (
        f"Canvas resources: {resources!r}\n\n"
        f"Deterministically generated main.tf:\n```hcl\n{main_tf}```\n\n"
        "Review this Terraform. You may refine argument values or add comments/tags "
        "for clarity — never add or remove a resource. Then call emit_terraform ONCE "
        "with the complete file set."
    )


async def _run_agent(prompt: str, options: ClaudeAgentOptions, client_cls: type) -> None:
    async with client_cls(options=options) as client:
        await client.query(prompt)
        async for _ in client.receive_response():
            pass


async def _refine(skeleton: TfProject, stack: Stack, client_cls: type, timeout: float) -> dict | None:
    """Runs the SDK pass; returns the `emit_terraform` payload if the agent
    called it, else None (SDK failure, timeout, or no call — every caller
    treats these identically: keep the skeleton)."""
    os.environ.pop("CLAUDECODE", None)  # avoid nested-Claude-Code confusion (brain.py precedent)
    collected: list[dict] = []
    server = create_sdk_mcp_server(name="translate", tools=[make_emit_tool(collected)])
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM,
        model=_MODEL,
        mcp_servers={"translate": server},
        allowed_tools=["mcp__translate__emit_terraform"],
    )
    try:
        await asyncio.wait_for(_run_agent(_prompt(skeleton, stack), options, client_cls), timeout=timeout)
    except Exception:
        log.exception("translate agent SDK pass failed for env %s", stack.env)
        return None
    return collected[-1] if collected else None


def _tail(output: str) -> str:
    return "\n".join(output.strip().splitlines()[-_TAIL_LINES:])


async def _tofu_run(tofu: str, args: tuple[str, ...], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        tofu, *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def validate_refinement(
    agent_files: dict[str, str], skeleton_files: dict[str, str], binary_files: dict[str, bytes] | None = None,
) -> tuple[str | None, dict[str, str] | None]:
    """The guardrail — deterministic validation, never trust. Returns
    `(violation_reason, formatted_files)`; exactly one is None. A violation
    means odin must fall back to the skeleton verbatim; no violation means
    `formatted_files` (tofu fmt-canonicalized) is what odin should keep.

    Order: the resource-SET equality check is pure Python and runs first, so
    an agent that added or removed a resource never pays for the two tofu
    subprocess calls that follow.

    `binary_files` (release finding #1): the skeleton's zip'd lambda
    deployment packages, verbatim -- the agent never sees or edits them, but
    `tofu validate` still evaluates `filebase64sha256(...)` calls in a
    lambda's main.tf, so the scratch dir needs the real bytes on disk or
    validation fails with a spurious "no such file" for every lambda canvas.
    """
    if not agent_files:
        return "agent returned no files", None
    try:
        agent_set, skeleton_set = hcl.resource_set(agent_files), hcl.resource_set(skeleton_files)
    except Exception as exc:
        return f"agent output failed to parse: {exc}", None
    if agent_set != skeleton_set:
        return f"resource set changed (skeleton={sorted(skeleton_set)}, agent={sorted(agent_set)})", None
    combined = "\n".join(agent_files.values()).lower()
    hit = next((token for token in _FORBIDDEN_SUBSTRINGS if token in combined), None)
    if hit is not None:
        return f"output is not portable (contains {hit!r})", None

    tofu = shutil.which("tofu")
    if tofu is None:
        return "tofu not on PATH -- cannot validate refinement", None
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for name, content in agent_files.items():
            (scratch / name).write_text(content)
        for name, content in (binary_files or {}).items():
            (scratch / name).write_bytes(content)
        code, out = await _tofu_run(tofu, ("fmt",), scratch)
        if code != 0:
            return f"tofu fmt failed: {_tail(out)}", None
        formatted = {name: (scratch / name).read_text() for name in agent_files}
        env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR), "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        code, out = await _tofu_run(tofu, ("init", "-input=false"), scratch, env)
        if code != 0:
            return f"tofu init failed: {_tail(out)}", None
        code, out = await _tofu_run(tofu, ("validate", "-no-color"), scratch, env)
        if code != 0:
            return f"tofu validate failed: {_tail(out)}", None
    return None, formatted


_BACKGROUND_NOTE = "refinement is running in the background -- using the deterministic skeleton for now"


def _fallback_result(skeleton: TfProject, notes: list[str]) -> TranslateResult:
    """A non-refined result carrying the deterministic skeleton verbatim
    (files + the lambda zip bytes) -- the no-supported-resources short-circuit,
    every fallback inside `_refine_once`, and the immediate background-refine
    return all share this shape."""
    return TranslateResult(
        files=skeleton.files, unsupported=skeleton.unsupported,
        binary_files=skeleton.binary_files, notes=notes,
    )


async def _refine_once(skeleton: TfProject, stack: Stack, client_cls: type, timeout: float) -> TranslateResult:
    """The blocking refine: run the SDK pass, run the deterministic guardrail
    on whatever it returned, and produce either the refined result
    (`refined=True`) or the skeleton fallback (`refined=False`). Never touches
    any cache -- the caller decides whether to await this inline (no cache) or
    on a background task (`TranslateCache`)."""
    payload = await _refine(skeleton, stack, client_cls, timeout)
    if payload is None:
        return _fallback_result(skeleton, ["agent proposed no refinement -- using the deterministic skeleton"])

    agent_files = {f["path"]: f["content"] for f in payload.get("files", [])}
    notes = list(payload.get("notes", []))
    violation, formatted = await validate_refinement(agent_files, skeleton.files, skeleton.binary_files)
    if violation is not None:
        log.warning("translate guardrail rejected agent output for env %s: %s", stack.env, violation)
        return _fallback_result(skeleton, [*notes, f"refinement rejected ({violation}) -- using the deterministic skeleton"])
    return TranslateResult(
        files=formatted, unsupported=skeleton.unsupported, binary_files=skeleton.binary_files,
        notes=notes, refined=True,
    )


class TranslateCache:
    """Release finding #5: the per-app translation cache + background-refine
    orchestrator. The claude-agent-sdk refine pass is genuinely slow (and, when
    the SDK is unreachable, sits until its timeout), so it must NEVER block a
    `/translate` or `/apply-full` request: `translate()` returns the
    deterministic skeleton immediately and, for a canvas revision it has
    neither refined nor already got an in-flight refine for, kicks that pass on
    a background task. The task's ONLY effect is to store a guardrail-passing
    refinement here, keyed by the Stack's own content hash (`spec.store.rev_of`
    -- any canvas edit misses and re-refines). A LATER translate/apply for the
    SAME revision then serves that refined output. A fallback (SDK
    failure/timeout/guardrail rejection) is never stored, so a transient
    failure is retried on the next call."""

    def __init__(self) -> None:
        self._results: dict[str, TranslateResult] = {}
        # Strong refs: asyncio only weakly references a running task, so
        # without this the background refine could be garbage-collected
        # mid-flight.
        self._tasks: dict[str, asyncio.Task] = {}

    def get(self, rev: str) -> TranslateResult | None:
        return self._results.get(rev)

    def _refining(self, rev: str) -> bool:
        task = self._tasks.get(rev)
        return task is not None and not task.done()

    def refine_in_background(self, rev: str, refine: Callable[[], Coroutine[Any, Any, TranslateResult]]) -> None:
        """Start a background refine for `rev`. `refine` is a factory (the coro
        is built only when the task actually runs, so a task cancelled before it
        starts leaves no un-awaited coroutine). Idempotent per rev: a call while
        one is already in flight is a no-op, so repeated `/translate` polls of
        the same unchanged canvas never stack up duplicate SDK passes."""
        if self._refining(rev):
            return

        async def _run() -> None:
            result = await refine()
            if result.refined:
                self._results[rev] = result

        self._tasks[rev] = asyncio.create_task(_run())

    async def _drain(self, rev: str) -> None:
        """Test seam: await the in-flight background refine for `rev` (there is
        no such wait on any request path -- refinement is fire-and-forget)."""
        task = self._tasks.get(rev)
        if task is not None:
            await task


async def translate(
    stack: Stack, client_cls: type = ClaudeSDKClient, timeout: float = _TIMEOUT_S,
    cache: TranslateCache | None = None,
) -> TranslateResult:
    """S3b entry point. `client_cls` is a `ClaudeSDKClient`-shaped seam
    (async-context-manager `__init__(options=...)`, `.query()`,
    `.receive_response()`) — tests inject a fake to drive the real MCP tool
    dispatch without spawning the Claude Code CLI.

    With a `cache` (release finding #5, the production path), this NEVER blocks
    on the SDK: it returns the deterministic skeleton immediately and refines on
    a background task whose result a later same-revision call serves from the
    cache. Without a cache it runs the refine inline and returns the refined (or
    fallback) result synchronously -- the shape the guardrail/fallback unit
    tests drive."""
    skeleton = generate_tf(stack)
    if not hcl.resource_set(skeleton.files):
        return _fallback_result(skeleton, [])

    if cache is None:
        return await _refine_once(skeleton, stack, client_cls, timeout)

    rev = rev_of(stack)
    cached = cache.get(rev)
    if cached is not None:
        return cached
    cache.refine_in_background(rev, lambda: _refine_once(skeleton, stack, client_cls, timeout))
    return _fallback_result(skeleton, [_BACKGROUND_NOTE])
