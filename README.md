# Odin

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/kessler-frost/odin/actions/workflows/ci.yml/badge.svg)](https://github.com/kessler-frost/odin/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-blue.svg)

Odin is an intelligent canvas for orchestrating your infrastructure and application
deployments. You drop apps and resources onto a visual canvas, an AI fills in the
config you leave blank, and a continuous control loop runs them **for real** on your
Mac — real containers, a real embedded AWS control plane — supervising them and
streaming live status back to the canvas. Think Railway, but local-first on a single
Mac, with an AI operator. You draw; you don't write config.

![Odin — draw your stack, the AI fills it in, the reconciler runs it for real](assets/odin-canvas.gif)

## What it does

- **Draw, don't configure.** Drop app services, dependencies (Redis, Postgres),
  batch jobs, local LLMs, and AWS resources (S3, SQS, SNS, DynamoDB, RDS) onto the
  canvas and wire them together with `${{node.attr}}` references.
- **The AI completes the blanks.** A Claude Agent SDK brain fills in whatever config
  you leave unspecified (your explicit values always win) and reviews IAM —
  best-effort, with safe defaults when it can't.
- **Runs for real, locally.** A deterministic reconciler (observe → plan → execute)
  runs everything as real containers on [Colima](https://github.com/abiosoft/colima)
  (or inside a [Lima](https://lima-vm.io/) VM for isolation) and backs AWS resources
  with a real embedded control plane — no cloud account, no Terraform, no mocks.
- **Supervised, with live status.** The reconciler watches health and restarts what
  breaks; every phase (starting / healthy / blocked / crashed / …) streams to the
  canvas over WebSocket.
- **Environments.** Multiple named environments reconcile independently, each
  isolated from the others.

## How it's built

- **UI:** React 19 + ReactFlow + Tailwind v4, served by Vite (`ui/`, `bun`).
- **Backend:** Python 3.12+ (`uv`), FastAPI + WebSocket, Pydantic.
- **Control loop:** a Spec Store (Stack = desired, World = observed) with a pure,
  idempotent `plan(Stack, World) → [Action]` reconciler that drives reality and
  verifies it with per-kind health assertions.
- **Runtime:** real containers via Colima (the default) or a Lima VM, behind a single
  `RuntimeDriver` protocol.
- **AWS control plane:** [MiniStack](https://pypi.org/project/ministack/) embedded
  in-process — S3, SQS, SNS, DynamoDB, and RDS backed for real (RDS → a real Postgres
  container).
- **Brain:** the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
  completes blank config and reviews IAM.

## Requirements

- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- [Colima](https://github.com/abiosoft/colima) for the container runtime (or
  [Lima](https://lima-vm.io/) for VM isolation)
- [bun](https://bun.sh/) — only for building the UI from a dev clone
- Claude access for the agent (via the Claude Code CLI the Agent SDK wraps)

## Install

Install the latest release (UI bundled in, no `bun` needed):

```bash
uv tool install git+https://github.com/kessler-frost/odin.git@latest
```

`latest` is a branch CI fast-forwards on every release, so this always tracks the
newest tagged version, not `main`.

Or from a local clone, for development:

```bash
git clone https://github.com/kessler-frost/odin.git
cd odin
uv tool install --editable ".[dev]"
cd ui && bun install
```

## Quick start

```bash
odin start            # build the UI and serve on http://localhost:4200
odin start --dev      # Vite HMR + uvicorn reload
```

```
odin start        Build UI + start the server
odin start --dev  Hot-reloading dev server
odin stop         Stop the server
odin status       Show running state
odin clean        Reset local state (odin clean --all wipes everything)
```

## Status

The canvas, AI config-completion, and the reconciler running real workloads
(services, dependencies, batch jobs, local LLMs) with an embedded AWS control plane
work end to end. Odin is moving toward a **fully local-only** model — dropping the
AWS-emulation layer in favor of plain local containers and processes. See
[ROADMAP.md](ROADMAP.md).

## Acknowledgements

Odin stands on the shoulders of open source giants — most of what makes it work is
other people's excellent work, and a lot of the thanks belongs to them:

- **[MiniStack](https://pypi.org/project/ministack/)** — the embedded AWS control plane
- **[Colima](https://github.com/abiosoft/colima)** + **[Lima](https://lima-vm.io/)** — containers and VMs on the Mac
- **[PostgreSQL](https://www.postgresql.org/)** — the real backing for RDS
- **[FastAPI](https://fastapi.tiangolo.com/)**, **[Pydantic](https://pydantic.dev/)**, **[boto3](https://github.com/boto/boto3)** — the backend
- **[React](https://react.dev/)** + **[React Flow](https://reactflow.dev/)** + **[Tailwind CSS](https://tailwindcss.com/)** + **[Vite](https://vitejs.dev/)** — the canvas UI
- **[uv](https://github.com/astral-sh/uv)** + **[bun](https://bun.sh/)** — the toolchain
- the **[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)** — the agent that completes config

Thank you to every one of these projects and their maintainers. 🙏

## License

Apache License 2.0. See [LICENSE](LICENSE).
