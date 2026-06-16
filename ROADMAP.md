# Odin Roadmap

## Phase 1 — Foundation (Complete)

Core infrastructure: agent client, Moto simulator, and project scaffolding.

- [x] Project scaffolding (uv, pyproject.toml, directory structure)
- [x] Agent client (Claude Agent SDK)
- [x] Moto simulator engine (EC2, S3, Lambda, IAM, VPC mocking)
- [x] Resource registry + executor pipeline
- [x] MCP tool server for agent integration
- [x] FastAPI server with REST endpoints + WebSocket
- [x] Orchestrator wiring all components together

## Phase 2 — Real Execution (Backend built, not yet wired to the UI)

The managers and orchestrator deploy/destroy logic for real local compute and
networking exist, but deploy/destroy are currently gated behind a "coming soon"
notice in the UI. The validated-against-Moto pipeline (Phase 4) is what runs end
to end today.

- [x] Lima VM Manager + YAML/cloud-init templates for EC2
- [x] Nebula mesh networking (cert-based VPC isolation, lighthouse auto-provisioning)
- [x] ContainerManager (nerdctl) + Lambda deploy/destroy/invoke
- [x] Nebula overlay networking for Lambda containers
- [x] `/invoke` API endpoint
- [ ] Wire deploy/destroy through to the UI (today the buttons show "coming soon")

## Phase 3 — UI (Complete)

React 19 + ReactFlow + Tailwind CSS v4 — high-contrast dark industrial interface.

- [x] React 19 + Tailwind CSS v4 + Vite
- [x] Component scaffold (TopBar, Sidebar, Canvas, ConfigPanel, BottomPanel)
- [x] High-contrast dark industrial color palette
- [x] ReactFlow interactive canvas (pan/zoom, drag-and-drop, connections, undo/redo, z-index layering)
- [x] Collapsible panels (sidebar, config, bottom panel)
- [x] Config panel edits propagate to canvas nodes and backend
- [x] IAM edge permissions (auto-detect, permission selection UI)
- [x] WebSocket connection with reconnection and event buffer
- [x] Security Group node type
- [x] Per-resource validation status on the canvas
- [x] Agent conversation streaming to the bottom panel via WebSocket

## Phase 4 — Resource Integration (Complete)

Moto-backed validation pipeline and `.odin/` state consolidation.

- [x] Validate pipeline: canvas → agent → boto3 → Moto → UI status
- [x] MCP tools: `validate_file`, `get_infrastructure_state`
- [x] Destroy resets validated/error → draft
- [x] `.odin/` directory consolidation
- [x] Agent session persistence across server restarts
- [ ] Smart defaults: reactive agent fills config on canvas changes (experimental, disabled)

## Next — Terraform / OpenTofu Pivot (active, in design)

Replace boto3 generation with Terraform (OpenTofu) HCL, run against a local Moto
server using AWS provider endpoint overrides. Planned split: **validate =
`tofu plan`** (fast per-edit feedback), **deploy = `tofu apply`** (explicit). A
full design spec is being written; this section will be expanded once it lands.

## Phase 5 — Extended AWS Services

Beyond core compute + storage.

- [ ] API Gateway simulation
- [ ] DynamoDB simulation
- [ ] SQS/SNS simulation
- [ ] EventBridge simulation
- [ ] ECS/Fargate simulation
- [ ] CloudWatch basics (logs, metrics)
- [ ] Route 53 (DNS) simulation via Nebula DNS
- [ ] Real S3 storage backend (RustFS/MinIO) for object persistence

## Testing

- [x] pytest suite (unit + API; integration tests gated by a marker)
- [x] GitHub Actions CI: backend (ruff + pytest on Python 3.12 / 3.13 / 3.14) and frontend (typecheck + build)
- [ ] Broader end-to-end scenario coverage

## Performance

- [ ] Validation speed: the agent can be slow for simple resources — profile token usage, trim the prompt, cache unchanged files, parallelize validation calls

## Open Questions

- Which additional AWS services matter most for Phase 5
- Multi-region simulation (multiple Nebula networks?)
- Cost estimation (simulate AWS billing?)
