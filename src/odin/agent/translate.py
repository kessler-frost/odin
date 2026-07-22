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
from pathlib import Path
from typing import TypedDict

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SdkMcpTool, create_sdk_mcp_server, tool
from pydantic import BaseModel

from odin.agent import hcl
from odin.agent.hcl import TfProject, generate_tf
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.models import Stack

log = logging.getLogger("odin.translate")

_MODEL = "claude-sonnet-5"
_TIMEOUT_S = 120.0
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
    resources = [
        {"id": r.id, "kind": r.kind, "fields": {k: fv.value for k, fv in r.fields.items()}}
        for r in stack.resources
    ]
    main_tf = skeleton.files.get("main.tf", "")
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
    agent_files: dict[str, str], skeleton_files: dict[str, str],
) -> tuple[str | None, dict[str, str] | None]:
    """The guardrail — deterministic validation, never trust. Returns
    `(violation_reason, formatted_files)`; exactly one is None. A violation
    means odin must fall back to the skeleton verbatim; no violation means
    `formatted_files` (tofu fmt-canonicalized) is what odin should keep.

    Order: the resource-SET equality check is pure Python and runs first, so
    an agent that added or removed a resource never pays for the two tofu
    subprocess calls that follow.
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


async def translate(stack: Stack, client_cls: type = ClaudeSDKClient, timeout: float = _TIMEOUT_S) -> TranslateResult:
    """S3b entry point. `client_cls` is a `ClaudeSDKClient`-shaped seam
    (async-context-manager `__init__(options=...)`, `.query()`,
    `.receive_response()`) — tests inject a fake to drive the real MCP tool
    dispatch without spawning the Claude Code CLI."""
    skeleton = generate_tf(stack)
    if not hcl.resource_set(skeleton.files):
        return TranslateResult(files=skeleton.files, unsupported=skeleton.unsupported)

    payload = await _refine(skeleton, stack, client_cls, timeout)
    if payload is None:
        return TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported,
            notes=["agent proposed no refinement -- using the deterministic skeleton"],
        )

    agent_files = {f["path"]: f["content"] for f in payload.get("files", [])}
    notes = list(payload.get("notes", []))
    violation, formatted = await validate_refinement(agent_files, skeleton.files)
    if violation is not None:
        log.warning("translate guardrail rejected agent output for env %s: %s", stack.env, violation)
        return TranslateResult(
            files=skeleton.files, unsupported=skeleton.unsupported,
            notes=[*notes, f"refinement rejected ({violation}) -- using the deterministic skeleton"],
        )
    return TranslateResult(files=formatted, unsupported=skeleton.unsupported, notes=notes, refined=True)
