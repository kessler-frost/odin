# Odin — End-to-End User Scenarios

Realistic multi-service platform architectures, each **built through the actual
UI** (drag-drop nodes from the sidebar, draw edges handle-to-handle) and
**validated through the live agent** (canvas → agent → OpenTofu `plan` → Moto →
per-node status). A scenario "passes" when every node reaches `validated` and the
agent emits the expected resources (including IAM roles auto-derived from IAM
edges).

How they're driven (for future sessions): the dev server runs (`uv run odin
start --dev`), a Playwright browser drives `localhost:4200`. Nodes are dropped by
dispatching an HTML5 `drop` with a `DataTransfer` carrying the sidebar abbr;
edges are drawn by `page.mouse` dragging from a source node's handle to a
target's; then the top-bar **VALIDATE ALL** button runs the agent. See the
session transcript for the exact Playwright helpers.

## Results

All 10 pass: every node reaches `validated` through the live agent, built via the
real UI. Run 2026-06-21.

| # | Scenario | Services exercised | Result |
|---|----------|--------------------|--------|
| S1 | Serverless REST API | API Gateway, Lambda, DynamoDB, CloudWatch Logs, IAM (edges) | ✅ 4/4 (agent also created `new-function-role`) |
| S2 | 3-tier web app | VPC, Subnet, Security Group, EC2, RDS, ALB, S3 | ✅ 7/7 |
| S3 | Event-driven data pipeline | S3, 2×Lambda, SQS, DynamoDB, SNS, EventBridge, IAM | ✅ 7/7 |
| S4 | Container microservices | VPC, Subnet, ECS, ALB, RDS, Secrets Manager, S3 | ✅ 7/7 |
| S5 | Secure API + storage | API Gateway, Lambda, S3, KMS, Secrets Manager, Route 53, Logs | ✅ 7/7 |
| S6 | Multi-AZ HA web tier | VPC, 2×Subnet, 2×EC2, ALB, RDS, Security Group | ✅ 8/8 |
| S7 | Static site + dynamic API | S3, Route 53, API Gateway, Lambda, DynamoDB | ✅ 5/5 |
| S8 | Streaming analytics | Kinesis, Lambda, DynamoDB, S3, SNS, CloudWatch Logs | ✅ 6/6 |
| S9 | Scheduled secure batch | EventBridge, Lambda, RDS, Secrets Manager, KMS, SQS, Logs | ✅ 7/7 |
| S10 | Full VPC networking stack | VPC, Internet Gateway, 2×Subnet, EC2, Elastic IP, ALB, Security Group, Route 53 | ✅ 9/9 |

### Bugs found & fixed during this run

- **Duplicate node labels collided.** Dropping two nodes of one type (e.g. S3's
  two Lambdas, S6's two Subnets/EC2s) gave both the same default label, and the
  registry keys on `{type}_{label}` — so they silently merged into one entry.
  Fixed by auto-suffixing the default label (`new-function-2`, …) on drop/add
  (`ui/src/components/Canvas.tsx`). S3 and S6 then validated all nodes distinctly.

## Scenario details

**S1 — Serverless REST API.** API Gateway → Lambda → DynamoDB; Lambda →
CloudWatch Logs. IAM edges from Lambda to DynamoDB and Logs. Expect the agent to
add an `aws_iam_role` + policy for the Lambda. ✅ All validated.

**S2 — 3-tier web app.** A VPC containing a Subnet containing an EC2 web server;
a Security Group; an RDS database; an ALB in front of the EC2; an S3 bucket for
static assets. Tests spatial containment + DB + load balancer.

**S3 — Event-driven data pipeline.** S3 (uploads) → Lambda (ingest) → SQS →
Lambda (worker) → DynamoDB; SNS for alerts; EventBridge for a schedule. Tests
messaging fan-out + multiple IAM edges.

**S4 — Container microservices.** VPC ⊃ Subnet ⊃ ECS cluster; ALB → ECS; RDS;
Secrets Manager for DB credentials; S3. Tests containers + secrets + LB + DB.

**S5 — Secure API + storage.** API Gateway → Lambda → S3 + KMS + Secrets
Manager; Route 53 for DNS; CloudWatch Logs. Tests the security services + DNS.

**S6 — Multi-AZ HA web tier.** VPC with two Subnets (different AZs), an EC2 in
each, an ALB spanning both, an RDS, and a Security Group. Tests a high-
availability layout.

**S7 — Static site + dynamic API.** S3 website + Route 53; plus API Gateway →
Lambda → DynamoDB for the dynamic part. Tests a hybrid static/serverless app.

**S8 — Streaming analytics.** Kinesis → Lambda → DynamoDB + S3 (data lake); SNS
alerts; CloudWatch Logs. Tests a streaming/data-processing pipeline.

**S9 — Scheduled secure batch.** EventBridge (schedule) → Lambda → RDS + Secrets
Manager + KMS; SQS as a dead-letter queue; CloudWatch Logs. Tests a secure
scheduled job.

**S10 — Full VPC networking stack.** VPC with an Internet Gateway, two Subnets,
an EC2, an Elastic IP, an ALB, a Security Group, and Route 53. Networking-heavy.
