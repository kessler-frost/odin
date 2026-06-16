from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk.types import StreamEvent

from odin.agent.prompt import (
    build_suggest_defaults_prompt,
    build_system_prompt,
    build_validate_prompt,
)
from odin.api.canvas import CanvasGraph
from odin.mcp.tools import OdinTools

ODIN_DIR = Path(".odin")
SESSION_ID_FILE = ODIN_DIR / "agent_session_id"


class AgentEvent(BaseModel):
    type: str
    data: dict[str, Any] = {}


class OdinAgent:
    """Persistent Claude Agent SDK client for infrastructure code generation.

    Wraps ClaudeSDKClient to provide a high-level interface for sending canvas
    graphs to the agent and streaming back events as it generates infra code.
    Registers MCP tools (validate_infrastructure, get_infrastructure_state) so
    the agent can write Terraform HCL and validate it against the Moto server.
    """

    def __init__(
        self,
        tf_dir: str = ".odin/tf",
        tools: OdinTools | None = None,
    ) -> None:
        self._tf_dir = tf_dir
        self._tools = tools
        self._client: ClaudeSDKClient | None = None
        self._session_id: str | None = None

    @property
    def is_running(self) -> bool:
        return self._client is not None

    def _load_session_id(self) -> str | None:
        if SESSION_ID_FILE.exists():
            return SESSION_ID_FILE.read_text().strip() or None
        return None

    def _save_session_id(self, session_id: str) -> None:
        ODIN_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_ID_FILE.write_text(session_id)
        self._session_id = session_id

    def _create_mcp_server(self):
        """Build an in-process MCP server with validate_infrastructure + get_infrastructure_state."""
        tools_instance = self._tools

        @tool(
            "validate_infrastructure",
            "Run tofu validate + plan on the current Terraform config against Moto and report errors.",
            {},
        )
        async def validate_infrastructure(args):
            result = await tools_instance.validate_infrastructure()
            return {"content": [{"type": "text", "text": str(result)}]}

        @tool(
            "get_infrastructure_state",
            "Get the current main.tf config and the registry's resource list.",
            {"service": str},
        )
        async def get_infrastructure_state(args):
            service = args.get("service") or None
            result = tools_instance.get_infrastructure_state(service=service)
            return {"content": [{"type": "text", "text": str(result)}]}

        return create_sdk_mcp_server(
            name="odin",
            version="1.0.0",
            tools=[validate_infrastructure, get_infrastructure_state],
        )

    async def start(self) -> None:
        """Initialize and connect the persistent ClaudeSDKClient."""
        os.environ.pop("CLAUDECODE", None)

        mcp_servers = {}
        allowed_mcp_tools: list[str] = []

        if self._tools:
            mcp_servers["odin"] = self._create_mcp_server()
            allowed_mcp_tools = [
                "mcp__odin__validate_infrastructure",
                "mcp__odin__get_infrastructure_state",
            ]

        saved_session = self._load_session_id()

        options = ClaudeAgentOptions(
            system_prompt=build_system_prompt(self._tf_dir),
            permission_mode="acceptEdits",
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
            + allowed_mcp_tools,
            mcp_servers=mcp_servers,
            cwd=str(Path.cwd()),
            resume=saved_session,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

    async def stop(self) -> None:
        """Disconnect and release the client."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except (RuntimeError, AttributeError):
                pass  # Cross-task cancel scope issue in anyio — safe to ignore
            self._client = None

    async def reset(self) -> None:
        """Clear session and restart the agent (used for Reset to Draft)."""
        await self.stop()
        SESSION_ID_FILE.unlink(missing_ok=True)
        self._session_id = None
        await self.start()

    async def validate(self, graph: CanvasGraph) -> AsyncIterator[AgentEvent]:
        """Send a canvas graph to the agent and yield events as it works.

        The agent receives a text description of the graph and generates or
        updates boto3 files accordingly. Events are yielded for each assistant
        message, tool use, and the final result. The session ID is persisted
        from the ResultMessage for future conversation resumption.
        """
        prompt = build_validate_prompt(graph)
        async for event in self._send_and_stream(prompt):
            yield event

    async def suggest_defaults(self, graph: CanvasGraph) -> AsyncIterator[AgentEvent]:
        """Suggest smart default values for nodes on the canvas.

        Uses a specialized prompt that asks the agent to fill in missing
        configuration values (CIDRs, instance types, runtimes, etc.) based
        on best practices and spatial relationships in the graph.
        """
        prompt = build_suggest_defaults_prompt(graph)
        async for event in self._send_and_stream(prompt):
            yield event

    async def _send_and_stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Send a prompt and yield AgentEvents from the response stream."""
        await self._client.query(prompt)

        async for msg in self._receive_safe():
            match msg:
                case AssistantMessage(content=content):
                    for block in content:
                        match block:
                            case TextBlock(text=text):
                                yield AgentEvent(
                                    type="agent_message",
                                    data={"text": text},
                                )
                            case ToolUseBlock(name=name, input=tool_input):
                                yield AgentEvent(
                                    type="tool_use",
                                    data={"tool": name, "input": tool_input},
                                )
                case ResultMessage() as result:
                    if result.session_id:
                        self._save_session_id(result.session_id)
                    yield AgentEvent(
                        type="agent_done",
                        data={
                            "result": result.result or "",
                            "is_error": result.is_error,
                            "num_turns": result.num_turns,
                            "cost_usd": result.total_cost_usd,
                            "session_id": result.session_id,
                        },
                    )
                case StreamEvent(event=event):
                    yield AgentEvent(
                        type="stream_event",
                        data=event,
                    )

    async def _receive_safe(self) -> AsyncIterator:
        """Wrap receive_response to skip unknown message types (e.g. rate_limit_event)."""
        import logging

        log = logging.getLogger("odin.agent")
        while True:
            try:
                async for msg in self._client.receive_response():
                    yield msg
                return
            except MessageParseError as e:
                log.debug("Skipping unknown message type: %s", e)
                continue
