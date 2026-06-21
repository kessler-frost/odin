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

## Phase 2 — Real Execution / "Simulate" (Parked)

The Lima VM, Nebula mesh, and nerdctl container managers are built and tested,
but parked: they're the foundation for a future **Simulate** mode that runs
resources for real. They are no longer wired into deploy — deploy now goes
through Terraform against Moto (see below).

- [x] Lima VM Manager + YAML/cloud-init templates for EC2
- [x] Nebula mesh networking (cert-based VPC isolation, lighthouse auto-provisioning)
- [x] ContainerManager (nerdctl) + Lambda deploy/destroy/invoke
- [x] Nebula overlay networking for Lambda containers
- [ ] "Simulate" mode: wire these into a real-execution path in the UI

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

- [x] Validate pipeline: canvas → agent → Terraform → `tofu plan` → Moto → UI status
- [x] MCP tools: `validate_infrastructure`, `get_infrastructure_state`
- [x] Destroy resets validated/error → draft
- [x] `.odin/` directory consolidation
- [x] Agent session persistence across server restarts
- [ ] Smart defaults: reactive agent fills config on canvas changes (experimental, disabled)

## Terraform / OpenTofu (Complete)

The agent writes one whole-canvas Terraform config (not boto3), run against a
local Moto server via AWS provider endpoint overrides.

- [x] Moto runs as a standalone `moto_server` subprocess
- [x] Agent writes one declarative `main.tf` for the whole canvas
- [x] validate = `tofu plan`, deploy = `tofu apply`, destroy = `tofu destroy`
- [x] Per-node canvas status mapped from tofu results
- [x] boto3 generation path removed; CI runs the Moto + tofu path

## Phase 5 — Broad AWS Service Coverage

Goal: cover the AWS services people actually use every day (skip the niche
ones). Each service is one `ResourceSpec` (backend) + one catalog entry
(frontend) + a Moto-backed tofu test. Services are grouped by category in the
sidebar.

Resource definitions are centralized: backend in `src/odin/resources.py`
(`RESOURCE_SPECS` → node→AWS type map, provider endpoints, agent prompt hints);
frontend in `ui/src/lib/catalog.ts` (node, config fields, sidebar group, IAM).

**Done (22 resource types), each verified deploying to Moto:**
VPC, Subnet, Security Group, EC2, Lambda, S3, DynamoDB, SQS, SNS, Kinesis,
RDS, Secrets Manager, KMS, IAM Role, Route 53, API Gateway, EFS, SSM Parameter,
ECS, CloudWatch Log Group, EventBridge, EBS Volume.

**Still to add:**
- [ ] ELB / ALB (load balancer + target group — needs subnets/SG wiring)
- [ ] Step Functions (state machine — needs role + definition)
- [ ] CloudFront (distribution)
- [ ] CloudWatch Alarm
- [ ] Internet Gateway / NAT Gateway / Route Table

**Dropped — not cleanly Moto-simulatable:**
- ElastiCache: Moto never transitions the cluster out of "creating", so
  `tofu apply` hangs on the status wait.

**Networking polish:**
- [ ] Real S3 storage backend (RustFS/MinIO) for object persistence
- [ ] Simulate mode: wire parked Lima/Nebula real-execution into a UI "Simulate"
      action (top-bar `...` overflow, to avoid crowding the primary actions)

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
