"""The Brain: the LLM fills a resource's missing fields (schema-native completion).

Best-effort and off the critical path — if the agent is slow or unavailable the
Stack is applied as-is and the Reconciler's defaults take over (mirroring odin's
"server runs without agent" fallback). The deterministic merge in
`agent.completion` is the only thing that touches the Stack: user values always
win, AI-filled fields are tagged `provenance="ai"`.
"""
from __future__ import annotations

import json
import logging
import os
import re

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from odin.agent.completion import merge_completion, needs_completion
from odin.spec.models import ResourceDesired, Stack

log = logging.getLogger("odin.brain")

_SYSTEM = (
    "You complete missing configuration fields for local infrastructure resources. "
    "Reply with ONLY a compact JSON object mapping each requested field name to a "
    "sensible value. No prose, no markdown fences."
)


def _prompt(res: ResourceDesired, missing: list[str]) -> str:
    known = {k: fv.value for k, fv in res.fields.items()}
    return (
        f"Resource id={res.id!r}, kind={res.kind!r}. Already set: {json.dumps(known)}. "
        f"Fill these missing fields: {missing}. Return ONLY a JSON object."
    )


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0)) if match else {}


async def _fill(res: ResourceDesired, missing: list[str]) -> dict:
    os.environ.pop("CLAUDECODE", None)  # avoid nested-Claude-Code confusion
    options = ClaudeAgentOptions(system_prompt=_SYSTEM, allowed_tools=[])
    text = ""
    async for msg in query(prompt=_prompt(res, missing), options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text += block.text
    return _extract_json(text)


async def claude_complete(stack: Stack) -> Stack:
    """Fill every resource's gaps via the model, then merge (user values win)."""
    gaps = needs_completion(stack)
    if not gaps:
        return stack
    by_id = {r.id: r for r in stack.resources}
    filled: dict[str, dict] = {}
    for rid, missing in gaps.items():
        try:
            filled[rid] = await _fill(by_id[rid], missing)
        except Exception:
            log.exception("brain fill failed for %s", rid)
            filled[rid] = {}
    return merge_completion(stack, filled)
