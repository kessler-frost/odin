# Odin — End-to-End User Scenarios

Realistic multi-service platform architectures, each **built through the actual
UI** (drag-drop nodes from the sidebar, draw edges handle-to-handle) and
**validated through the live agent**. A scenario "passes" when every node reaches
`validated` and the agent emits the expected resources (including IAM roles
auto-derived from IAM edges).

**Validate = plan.** The **Validate** button runs the agent → `tofu validate` +
`plan` against the ephemeral Moto server — a fast preview/check, no apply.
Actually running the architecture for real is **Simulate** (Lima VMs +
containers); **Destroy** tears that down.

How they're driven (for future sessions): the dev server runs (`uv run odin
start --dev`), a Playwright browser drives `localhost:4200`. Nodes are dropped by
dispatching an HTML5 `drop` with a `DataTransfer` carrying the sidebar abbr;
edges are drawn by `page.mouse` dragging from a source node's handle to a
target's; then the top-bar **Validate** button runs the agent. Note: a prompt
change only takes effect after clearing `.odin/agent_session_id` (a resumed agent
session keeps its original system prompt).

## Results

All 15 pass: every node reaches `validated` through the live agent, built via the
real UI (run 2026-06-21).

| # | Scenario | Services exercised | Result |
|---|----------|--------------------|--------|
| S1 | Serverless REST API | API Gateway, Lambda, DynamoDB, CloudWatch Logs, IAM | ✅ 4/4 (agent also created the Lambda IAM role) |
| S2 | 3-tier web app | VPC, Subnet, Security Group, EC2, RDS, ALB, S3 | ✅ 7/7 |
| S3 | Event-driven data pipeline | S3, 2×Lambda, SQS, DynamoDB, SNS, EventBridge, IAM | ✅ 7/7 |
| S4 | Container microservices | VPC, Subnet, ECS, ALB, RDS, Secrets Manager, S3 | ✅ 7/7 |
| S5 | Secure API + storage | API Gateway, Lambda, S3, KMS, Secrets Manager, Route 53, Logs | ✅ 7/7 |
| S6 | Multi-AZ HA web tier | VPC, 2×Subnet, 2×EC2, ALB, RDS, Security Group | ✅ 8/8 (caught the subnet-CIDR bug below) |
| S7 | Static site + dynamic API | S3, Route 53, API Gateway, Lambda, DynamoDB | ✅ 5/5 |
| S8 | Streaming analytics | Kinesis, Lambda, DynamoDB, S3, SNS, CloudWatch Logs | ✅ 6/6 |
| S9 | Scheduled secure batch | EventBridge, Lambda, RDS, Secrets Manager, KMS, SQS, Logs | ✅ 7/7 |
| S10 | Full VPC networking stack | VPC, Internet Gateway, 2×Subnet, EC2, Elastic IP, ALB, Security Group, Route 53 | ✅ 9/9 |
| S11 | Container platform + storage | VPC, Subnet, ECS, EFS, ALB, Security Group | ✅ 6/6 |
| S12 | Full event mesh | EventBridge, Lambda, Kinesis, SQS, SNS, DynamoDB, Logs | ✅ 7/7 |
| S13 | Compute + block storage | VPC, Subnet, EC2, EBS, Elastic IP, Security Group, Internet Gateway | ✅ 7/7 |
| S14 | Secure data API | API Gateway, Lambda, RDS, Secrets Manager, KMS, Logs | ✅ 6/6 |
| S15 | Big mixed stress test | VPC, Subnet, EC2, RDS, API GW, Lambda, DynamoDB, S3, SQS, SNS, Secrets, KMS, Logs | ✅ 13/13 (Lambda → 7 targets) |

### Bugs found & fixed

- **Duplicate node labels collided.** Two nodes of one type dropped with the same
  default label merged into one registry entry (it keys on `{type}_{label}`).
  Fixed by auto-suffixing the default label (`new-function-2`, …) on drop/add
  (`ui/src/components/Canvas.tsx`).
- **Identical subnet CIDRs.** Surfaced while exercising `tofu apply` during
  testing: the agent gave two subnets in one VPC the same `10.0.1.0/24`, which
  passes `plan` but would fail `apply` (`InvalidSubnet.Conflict`). Fixed with a
  subnet prompt hint requiring distinct, non-overlapping CIDRs across AZs
  (`src/odin/resources.py`) — the generated HCL is now correct regardless of
  apply. S6/S10 get `10.0.1.0/24` + `10.0.2.0/24`.
- **Simulate/Destroy did nothing in dev.** The Vite dev proxy didn't forward
  `/simulate` or `/simulate-destroy`, so the buttons' requests stopped at Vite
  (:4200) and never reached the backend. Fixed the proxy (`ui/vite.config.js`).
  After the fix, verified the full lifecycle through the UI: EC2 → real Lima VM
  (Running, SSH-able), S3 → real RustFS container, Destroy → clean teardown
  (0 VMs, 0 orphaned processes). The earlier "containerd not ready in 360s" was
  contention from orphaned `limactl hostagent` processes left by repeated
  attempts — with a clean process state containerd is ready in seconds.

## Scenario details

**S1 — Serverless REST API.** API Gateway → Lambda → DynamoDB; Lambda → Logs.
IAM edges from Lambda. Agent adds an `aws_iam_role` for the Lambda.

**S2 — 3-tier web app.** VPC ⊃ Subnet ⊃ EC2; Security Group; RDS; ALB → EC2; S3.

**S3 — Event-driven pipeline.** S3 → Lambda (ingest) → SQS → Lambda (worker) →
DynamoDB; SNS; EventBridge. Two distinct Lambdas (dup-label fix).

**S4 — Container microservices.** VPC ⊃ Subnet ⊃ ECS; ALB → ECS; RDS; Secrets; S3.

**S5 — Secure API + storage.** API Gateway → Lambda → S3 + KMS + Secrets; Route
53; Logs.

**S6 — Multi-AZ HA web tier.** VPC, two Subnets (distinct CIDRs/AZs), an EC2 in
each, ALB, RDS, Security Group.

**S7 — Static site + dynamic API.** S3 + Route 53; plus API Gateway → Lambda →
DynamoDB.

**S8 — Streaming analytics.** Kinesis → Lambda → DynamoDB + S3; SNS; Logs.

**S9 — Scheduled secure batch.** EventBridge → Lambda → RDS + Secrets + KMS; SQS;
Logs.

**S10 — Full VPC networking stack.** VPC, Internet Gateway, two Subnets, EC2,
Elastic IP, ALB, Security Group, Route 53.

**S11 — Container platform + storage.** VPC ⊃ Subnet ⊃ ECS mounting EFS; ALB →
ECS; Security Group.

**S12 — Full event mesh.** EventBridge → Lambda; Lambda ↔ Kinesis, SQS, SNS,
DynamoDB, Logs.

**S13 — Compute + block storage.** VPC ⊃ Subnet ⊃ EC2 with an attached EBS
volume; Elastic IP; Security Group; Internet Gateway.

**S14 — Secure data API.** API Gateway → Lambda → RDS + Secrets + KMS; Logs.

**S15 — Big mixed stress test.** 13 services: VPC ⊃ Subnet ⊃ EC2 → RDS, plus API
Gateway → Lambda fanning out to DynamoDB, S3, SQS, SNS, Secrets, KMS, and Logs.
Stresses the agent with a large graph; all applied cleanly.
