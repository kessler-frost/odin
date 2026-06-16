# Odin — AI-Controlled Local AWS Simulator

## Overview
Local AWS simulator: AI agent (Claude Agent SDK) writes boto3 code → Moto simulates → Lima VMs/containers execute. Users never write code.

## Tech Stack
- **Backend:** Python 3.12+ (uv), FastAPI + WebSocket, Moto, Lima, Nebula
- **UI:** React 19 + ReactFlow + Tailwind CSS v4 + Vite — high-contrast dark industrial aesthetic. Run from `ui/` with `bun install && bun run dev`.
- **Agent:** Claude Agent SDK (`claude-agent-sdk` Python package) — wraps Claude Code CLI as subprocess, provides `ClaudeSDKClient` for multi-turn conversations, in-process MCP tools via `@tool` decorator. Uses Claude Sonnet 4.6 (`claude-sonnet-4-6`) for speed in agentic loops.

## Architecture
Single Python monolith: `src/odin/` with modules for `agent/`, `simulator/`, `compute/`, `network/`, `mcp/`, `api/`, and `orchestrator.py`.

- Single agent (ClaudeSDKClient) handles validation + smart defaults, session persists via `.odin/agent_session_id`
- MCP tools via `@tool` decorator (validate_file, get_infrastructure_state)
- Agent code lives in `.odin/infra/` (one file per resource)
- Pipeline: agent writes file → calls validate_file MCP tool → moto → registry → WebSocket → UI
- Two simulation levels: API-level (moto) + real execution (Lima VMs, Nebula networking)

## Conventions
- **ALWAYS use `bun` instead of `npm`, `npx`, `yarn`, or `pnpm`** — `bun install`, `bun run`, `bunx` for all JS/TS operations
- **ALWAYS use `uv` instead of `pip`, `pip3`, `python -m pip`** — `uv add`, `uv run`, `uv tool install` for all Python operations
- **ALWAYS use `python` (not `python3`)** — the project uses uv which manages the Python version
- Pathlib for paths, imports at top of files
- Minimize if/else and try/except
- Structured I/O (Pydantic) over regex
- Always merge to `main` locally, push after merge, never create PRs
- Lima via `limactl` CLI (`--tty=false --format=json`), containers via `nerdctl` in Lima VMs, Nebula via `nebula-cert` CLI

## CLI (Typer + Rich)
- Installed as editable uv tool: `uv tool install --editable ".[dev]"`
- Built with Typer (`add_completion=False`), Rich Console for test output
- `odin start` — build UI + start server on :4200 (background)
- `odin start --dev` — Vite HMR (:4200) + uvicorn reload (:4201), logs to `.odin/dev.log`
- `odin stop` / `odin status` / `odin clean` / `odin clean --all` (full `.odin/` reset)

## Key Files
- `ROADMAP.md` — Project phases
- `src/odin/orchestrator.py` — Central deploy/destroy logic
- `src/odin/server.py` — FastAPI app factory
- `src/odin/agent/prompt.py` — Agent system prompt, graph formatting, containment rules
- `src/odin/api/canvas.py` — Canvas CRUD, validate endpoint, status management
- `src/odin/api/ws.py` — WebSocket manager, persists events to `.odin/events.jsonl`
- `.odin/registry.json` — Resource state manifest
- `.odin/canvas.json` — Canvas layout state
- `.odin/events.jsonl` — Persisted WS events (agent logs, status updates)
- `.odin/infra/` — Agent-generated boto3 files (one per resource)
- `.odin/agent_session_id` — Persistent agent session ID
- `src/odin/__main__.py` — CLI entry point (start/stop/status/dev/clean)
- `ui/` — React 19 + ReactFlow v12 + Tailwind v4 frontend (the real UI)

## UI Design Rules
- **Grid alignment**: Background grid is 20px. All node internal sections must be exact multiples of 20px tall (header=40px, single-line meta=20px, two-line meta=40px, button row=40px). This ensures section dividers align with the grid.
- **Snap to grid**: Nodes snap to 20px grid. All initial node positions and default sizes must be multiples of 20.
- **High-contrast dark theme**: Near-black backgrounds (#050508, #0a0a10), bright borders (#333345, #4a4a60), high-contrast text. Inspired by Dark Reader extension aesthetic.
- **Neon accents**: Each resource type has a signature neon color — VPC=purple, Subnet=blue, EC2=orange, Lambda=yellow, S3=green, SG=red. Used for borders, text labels, and status badges.
- **Solid borders** on all nodes (not dashed). Container nodes (VPC, Subnet) use their neon color border with subtle background tint.
- **Node overflow prevention**: Headers use `overflow-hidden whitespace-nowrap`, type labels use `shrink-0`, name labels use `truncate`. Leaf nodes (EC2/Lambda/S3) use `w-full h-full` to fill when resized.
- **Z-index layering**: VPC=0, Subnet=1, leaf nodes (EC2/Lambda/S3)=2. `elevateNodesOnSelect={false}` to preserve layering. This ensures leaf nodes are clickable on top of container nodes.
- **No parent-child constraints**: Nodes are fully independent (no `parentId`). Visual containment is conveyed by spatial nesting and z-index layering, not ReactFlow's parent-child system.
- **Canvas controls**: Left-drag on canvas=pan, left-drag on node=move, Shift+drag=pan (even over nodes, via `nodesDraggable={!shiftHeld}`), Cmd+drag=selection rectangle, Cmd+click=multi-select. Delete/Backspace=remove, Cmd+Z/Cmd+Shift+Z=undo/redo, Cmd+C/V=copy/paste, Cmd+F=fit view, Cmd+A=select all. Keyboard shortcuts must not fire when typing in input fields. Selection bounding rectangle is hidden via CSS.
- **Gridlines background** (not dots) — matches the industrial/engineering aesthetic.
- **ReactFlow**: Custom node types in `ui/src/components/nodes/`. Handles on EC2/Lambda/S3 for edge connections. MiniMap and Controls styled to match dark theme via CSS in `app.css`. NodeResizer on all nodes with minimum widths (VPC=280, Subnet=260, EC2/Lambda=200, S3=180).

## Status Management
- **Canonical name format**: `{type}_{label}` (e.g., `vpc_prod-vpc`, `ec2_web-server`) used in registry, WS broadcasts, and MCP tool
- **Validate flow**: pre-register as "validating" (persists to registry for refresh) → agent runs → MCP tool updates to validated/error → final pass restores previous status for nodes agent skipped
- **Spatial containment**: full bounding box inside container = "inside", partial overlap = "outside" (agent-interpreted from prompt, not computed in code)
- **Node sizes**: `@xyflow/react` v12 NodeResizer sets `node.width/height` (not `node.style`). Save captures `n.width ?? n.style?.width`. Load merges with defaults: `{ ...defaultStyleForType, ...savedSize }`
- **Agent output**: prefix symbols per line (`+` created, `~` updated, `.` skipped, `!` warning, `x` error, `-` deleted)
- **Events**: persisted to `.odin/events.jsonl` (append-only JSONL), served via `/events`, BottomPanel loads on mount

## Current Phase — Phase 4 (Resource Integration) Complete
Moto-only validation working end-to-end. Single persistent agent with MCP tools (validate_file, get_infrastructure_state). Smart defaults disabled (experimental). Deploy buttons show "coming soon" until real infra (Lima/Nebula) is wired. All state in `.odin/` directory.
