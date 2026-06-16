# Odin

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/kessler-frost/odin/actions/workflows/ci.yml/badge.svg)](https://github.com/kessler-frost/odin/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-blue.svg)

A visual canvas for designing AWS infrastructure on your own machine.

You lay out your infrastructure on a canvas — VPCs, subnets, EC2, Lambda, S3,
security groups — and Odin keeps a Terraform (OpenTofu) configuration in sync
with what you draw, checking it against a local [Moto](https://github.com/getmoto/moto)
server. An AI agent does the translation from canvas to HCL, so you don't
hand-write the Terraform.

![Odin: drawing a VPC with an EC2 instance and a Lambda, then validating](assets/odin-canvas.gif)

## How it works

```
draw on canvas  ──►  Terraform (HCL)  ──►  validate / deploy on a local Moto server
                     (the agent writes it)     (tofu plan / tofu apply)
```

- **Canvas (UI):** React 19 + ReactFlow + Tailwind, served by Vite.
- **Backend:** FastAPI + WebSocket, a resource registry, and a local Moto server.
- **Infrastructure as code:** the agent writes one Terraform config for the whole
  canvas; `tofu plan` validates it and `tofu apply` runs it — all locally, against
  Moto (the AWS provider's endpoints are pointed at the Moto server).
- **Agent:** the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).
  It's the translator from canvas to HCL, not the centerpiece.

## Requirements

- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- [bun](https://bun.sh/) for the UI
- [OpenTofu](https://opentofu.org/) (`tofu`)
- Claude access for the agent (via the Claude Code CLI the Agent SDK wraps)

## Quick start

```bash
uv tool install --editable ".[dev]"   # install the `odin` CLI

odin start            # build the UI and serve on http://localhost:4200
odin start --dev      # dev mode: Vite HMR + uvicorn reload
```

Open the canvas in your browser and start drawing. As you edit, the agent keeps
`main.tf` in sync and `tofu plan` reports whether it's valid; **Deploy** runs
`tofu apply` against the local Moto server.

```
odin start        Build UI + start the server
odin start --dev  Hot-reloading dev server
odin stop         Stop the server
odin status       Show running state
odin clean        Reset local state (odin clean --all wipes everything)
```

## Status

The canvas, the canvas→Terraform translation, and `tofu` validate/deploy against
Moto work end to end. A **Simulate** mode that runs resources for real (Lima VMs,
containers, Nebula networking) is planned. See [ROADMAP.md](ROADMAP.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
