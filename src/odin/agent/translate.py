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

from odin.agent import ai, hcl
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


def refine_enabled() -> bool:
    """`ODIN_AI=0` wins over everything here (see `agent/ai.py`): it is the one
    switch for every model call in the process, so a user who set it does not
    also have to know this flag exists.

    `ODIN_TRANSLATE_REFINE` opts IN to the claude-agent-sdk refine pass --
    OFF by default. The canvas -> Terraform translation (`agent/hcl.py`) is
    fully deterministic; this pass is a best-effort ADD-ON that can only
    attach comments/tags/unset arguments (`validate_refinement`'s
    value-fidelity check rejects anything else), never change the
    architecture -- so leaving it off costs polish, not correctness. Read
    fresh on every call (not cached at import), same convention as
    `_default_timeout` -- an env var flip takes effect on the next call, no
    restart required. Checked ONLY on the cached (production, see
    server.py) path in `translate()` below: without it, a no-API-key
    install used to re-kick a doomed ~`_TIMEOUT_S`-second SDK pass, in the
    background, on every single `/translate`/`/apply-full` call for nothing.
    (`ai.refuse_if_off()` is what covers the UNCACHED path, which has no gate
    of its own -- see `_refine`.)"""
    if ai.off_reason() is not None:
        return False
    return os.environ.get("ODIN_TRANSLATE_REFINE", "").strip().lower() in ("1", "true", "yes", "on")


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
    # Carried from the skeleton on EVERY return path below, exactly like
    # `unsupported`: a broken canvas wiring ref is a property of the Stack, not
    # of whether the optional agent pass ran. Kept separate from `unsupported`
    # because it means something different -- see `hcl.TfProject.wiring_errors`
    # (field test 5: a wiring typo must not be reported as a coverage gap).
    wiring_errors: list[str] = []
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
    treats these identically: keep the skeleton).

    `ai.refuse_if_off()` first, and it is the reason `ODIN_AI=0` is a real
    switch rather than a suggestion: this is the ONE place this module builds a
    client, and `translate(stack)` without a `cache` reaches it with no
    `refine_enabled()` check at all. Raising here happens before
    `create_sdk_mcp_server` and before any client exists, so nothing is spawned
    and nothing can hang; `_refine_once` turns it into the same
    keep-the-skeleton result every other failure mode produces."""
    ai.refuse_if_off()
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
        await asyncio.wait_for(await _run_agent(_prompt(skeleton, stack), options, client_cls), timeout=timeout)
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

    Order: the resource-SET equality check and the value-fidelity check are
    both pure Python and run first, so an agent that added/removed a resource
    or silently rewrote an argument's VALUE (the drift liability -- nothing
    else compares agent output to the canvas) never pays for the two tofu
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
        agent_attrs, skeleton_attrs = hcl.resource_attrs(agent_files), hcl.resource_attrs(skeleton_files)
    except Exception as exc:
        return f"agent output failed to parse: {exc}", None
    agent_set, skeleton_set = frozenset(agent_attrs), frozenset(skeleton_attrs)
    if agent_set != skeleton_set:
        return f"resource set changed (skeleton={sorted(skeleton_set)}, agent={sorted(agent_set)})", None
    drifted = sorted(
        f"{rtype}.{name}" for (rtype, name), attrs in skeleton_attrs.items()
        if not hcl.values_preserved(attrs, agent_attrs[(rtype, name)])
    )
    if drifted:
        return f"argument value(s) changed on {drifted} -- only comments/tags/new arguments may be added", None
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
_REFINE_DISABLED_NOTE = (
    "the optional AI refine pass is off (set ODIN_TRANSLATE_REFINE=1 to enable) -- "
    "using the deterministic translation"
)


def _disabled_note() -> str:
    """Which flag actually held the refine pass back, in the note the caller
    prints. Telling a user to set `ODIN_TRANSLATE_REFINE=1` when `ODIN_AI=0` is
    what really stopped it would send them to a flag that changes nothing."""
    reason = ai.off_reason()
    if reason is None:
        return _REFINE_DISABLED_NOTE
    return f"no AI refine pass: {reason} -- using the deterministic translation"


def _fallback_result(skeleton: TfProject, notes: list[str]) -> TranslateResult:
    """A non-refined result carrying the deterministic skeleton verbatim
    (files + the lambda zip bytes) -- the no-supported-resources short-circuit,
    every fallback inside `_refine_once`, and the immediate background-refine
    return all share this shape."""
    return TranslateResult(
        files=skeleton.files, unsupported=skeleton.unsupported,
        wiring_errors=skeleton.wiring_errors,
        binary_files=skeleton.binary_files, notes=notes,
    )


async def _refine_once(skeleton: TfProject, stack: Stack, client_cls: type, timeout: float) -> TranslateResult:
    """The blocking refine: run the SDK pass, run the deterministic guardrail
    on whatever it returned, and produce either the refined result
    (`refined=True`) or the skeleton fallback (`refined=False`). Never touches
    any cache -- the caller decides whether to await this inline (no cache) or
    on a background task (`TranslateCache`).

    `AiDisabled` is the switch-off boundary refusing (`agent/ai.py`), and it
    lands in the same place every other non-refinement does: the deterministic
    skeleton, plus a note naming why. It is caught HERE rather than in
    `_refine` so the raise cannot escape past any caller -- including the
    uncached path, whose only gate this is."""
    try:
        payload = await _refine(skeleton, stack, client_cls, timeout)
    except ai.AiDisabled as disabled:
        return _fallback_result(skeleton, [f"no AI refine pass: {disabled} -- using the deterministic translation"])
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
        wiring_errors=skeleton.wiring_errors, notes=notes, refined=True,
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

    async def refine_in_background(self, rev: str, refine: Callable[[], Coroutine[Any, Any, TranslateResult]]) -> None:
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

        # `create_task(_run())`, NEVER `create_task(await _run())` -- the latter
        # runs the refine to completion inline (defeating the entire point of a
        # background pass, and blocking the `/translate` response on an SDK
        # call) and then hands `None` to `create_task`, which raises. The task
        # object is retained in `self._tasks`, which is also what keeps a strong
        # reference to it: asyncio holds only a weak one, so a task nobody
        # stores can be garbage collected mid-flight.
        self._tasks[rev] = asyncio.create_task(_run(), name=f"odin-refine-{rev}")

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
    tests drive.

    `refine_enabled()` (owner directive, opt-in decoration) gates ONLY the
    cached/production path: when off (the default), no background task is
    even created -- every `/translate`/`/apply-full` call returns the
    deterministic result immediately, full stop. The uncached path is left
    alone (it's the guardrail/fallback unit tests' own seam, never a
    production caller -- server.py always passes a cache)."""
    skeleton = generate_tf(stack)
    if not hcl.resource_set(skeleton.files):
        return _fallback_result(skeleton, [])

    if cache is None:
        return await _refine_once(skeleton, stack, client_cls, timeout)

    if not refine_enabled():
        return _fallback_result(skeleton, [_disabled_note()])

    rev = rev_of(stack)
    cached = cache.get(rev)
    if cached is not None:
        return cached
    # The `lambda` is a coroutine FACTORY, not an awaited call: it builds the
    # coroutine only when the task runs (see `refine_in_background`), so a
    # cancelled task never leaves an un-awaited coroutine behind. `await` here
    # only covers starting the task, which returns immediately.
    await cache.refine_in_background(rev, lambda: _refine_once(skeleton, stack, client_cls, timeout))
    return _fallback_result(skeleton, [_BACKGROUND_NOTE])
