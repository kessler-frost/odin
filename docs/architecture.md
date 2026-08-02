# odin — how it actually works

Every diagram here describes what runs, not what was planned. Where a claim could
not be measured it is named as unmeasured in [limits.md](limits.md) rather than
drawn as though it were proven.

GitHub renders these diagrams inline. For the ASCII forms used in the README,
`./scripts/render-diagrams.sh` regenerates them from `docs/diagrams/*.mmd`.

## The whole system

```mermaid
graph TD
  Canvas["Canvas — React + ReactFlow"] -->|"canvas_to_stack"| Stack["Stack (desired state)<br/>append-only, content-addressed"]
  Stack -->|"agent/hcl.py — deterministic"| TF["main.tf + lambda zips"]
  TF -->|"tofu apply"| GW["odin gateway<br/>one SigV4 endpoint"]
  GW --> Sub["Substrates: containers + Lima VMs"]
  Sub -->|"observe"| World["World (observed state)"]
  World -->|"WorldDelta over SSE"| Canvas
  TF -.->|"agent/import_tf.py"| Stack
  GW -.->|"reconcile/dispatch.py"| Sub
```

**Both translation directions are deterministic code.** The agent SDK sits behind
an off-by-default refine pass a guardrail can reject outright — it is
structurally incapable of deciding what gets applied. Status is a one-way
projection: drivers author facts, the reconciler emits a delta, the UI is a pure
view of it.

## Inside the gateway — every AWS call

```mermaid
graph LR
  Call["boto3 / aws-cli / tofu<br/>SigV4-signed"] --> Verify["verify signature"]
  Verify --> Classify["classify → (action, resource)<br/>15 services"]
  Classify --> Eval{"applied IAM<br/>allows it?"}
  Eval -->|no| Deny["403 AccessDenied<br/>+ access_denied event"]
  Eval -->|yes| Route{"pure answer<br/>or forward?"}
  Route -->|"synth model"| Synth["iamctl · logsctl · ssmctl<br/>secretsctl · eventsctl · ecsctl"]
  Route -->|"forward"| Backing["RustFS · goaws · dynalite<br/>registry:2"]
  Classify -.->|"unknown service"| Deny
```

**Closed world.** A service the classifier does not know is a denial, not a
pass-through — which is why EventBridge could not be applied at all until its
classifier landed. Authorization reads the IAM that was *applied*, not the edges
you drew: a permission takes effect after an apply, never before.

## Per service — what is really underneath

### S3

`rustfs/rustfs — one container per env`

```mermaid
graph LR
  A["s3 node"] -->|"aws_s3_bucket"| G["gateway"]
  G -->|"re-signed with<br/>RustFS credentials"| R["RustFS :9000"]
  G -->|"PutObject seen"| N["notification enqueued"]
```

S3 is the one service whose forward is **re-signed** — RustFS enforces SigV4 itself. Bucket notifications are **synthesized by odin**: RustFS rejects every notification config and stores it anyway, so pass-through would fail the apply while a later plan read it back clean.

### RDS

`postgres:16-alpine + named volume + mesh`

```mermaid
graph TD
  A["rds node"] -->|"aws_db_instance"| G["gateway rdsctl"]
  G --> C["Postgres container"]
  C --> V["named volume<br/>odin-rds-env-node-data"]
  C --> M["Nebula sidecar<br/>SG firewall applies"]
  C -->|"DATABASE_URL fact"| W["World"]
```

The volume is why a repair is **non-destructive**: `docker rm -f -v` deliberately does not remove a named volume, so a killed container comes back with its rows. The apply says whether the data survived by reading the real volume list, not by assuming.

### SQS & SNS

`admiralpiett/goaws — one container serves both`

```mermaid
graph LR
  Q["sqs node"] --> G["gateway"]
  T["sns node"] --> G
  G -->|"forwarded unsigned"| GA["goaws :4100"]
  T -.->|"subscription edge"| Q
  GA -->|"long poll"| G
```

One process holds both, so **SNS→SQS fan-out is real delivery**, not a simulation. Long polling needed a derived timeout: goaws holds a poll about **1.5× the wait it is given** — measured 29.8–30.9s for a 20s wait, because each internal loop rescans the queue.

### Lambda

`public.ecr.aws/lambda/* — RIE, one warm container per function`

```mermaid
graph TD
  L["lambda node"] -->|"aws_lambda_function"| G["gateway lambdactl"]
  G --> C["RIE container<br/>code dir mounted"]
  C -->|"logs"| CW["/aws/lambda/name"]
  C -->|"own creds injected"| G
  D["dispatcher"] -->|"invoke"| G
```

A function's own calls go back **through the gateway with its own credentials**, so its IAM is enforced on itself. Packaging takes a whole directory, and the archive is byte-deterministic — otherwise `source_code_hash` would churn and every plan would show a change.

### ECS

`Colima containers, one per task`

```mermaid
graph TD
  S["ecs node"] -->|"aws_ecs_service<br/>+ task definition"| G["gateway ecsctl"]
  G --> T1["task container"]
  G --> T2["task container"]
  E["ecr node"] -.->|"image edge"| S
  R["rds node"] -.->|"connection edge<br/>DATABASE_URL"| S
```

Rolling updates count only **current-revision** tasks as healthy — a waiter that counted stale ones would report a failed deploy as green. An `ecr` edge sets the image; a `connection` edge writes the endpoint into the container's environment at launch.

### EC2

`Lima VM, vzNAT, joined to the Nebula mesh`

```mermaid
graph TD
  I["ec2 node"] -->|"aws_instance"| G["gateway ec2compute"]
  G --> VM["real Lima VM<br/>cloud-init"]
  VM --> N["nebula on the VM<br/>/etc/nebula/config.yml"]
  SG["sg node"] -.->|"compiled firewall"| N
  VM -.->|"vzNAT — ungated"| Net["host / internet"]
```

A real virtual machine, not a container pretending. Security groups gate **overlay traffic only** — the same VM whose overlay packet was just dropped can still reach the internet over vzNAT, which is stated rather than implied.

### VPC · Subnet · Security Group

`Nebula — a network per env, compiled firewall rules`

```mermaid
graph TD
  V["vpc / subnet<br/>containment on the canvas"] --> ID["vpc_id · subnet_id"]
  SG["sg rules<br/>proto:port:peer"] --> C["compiled firewall"]
  C --> IN["inbound rules"]
  C --> OUT["outbound rules"]
  IN --> Mesh["nebula config on<br/>VMs and RDS sidecars"]
  OUT --> Mesh
```

Geometry compiles to infrastructure: a node fully inside a VPC box gets its `vpc_id`. **Egress is enforced since v0.8.17** — it had been authored, planned and stored while gating nothing. Ports may be ranges; a single port is the degenerate case.

### ALB

`nginx container, dynamic host port`

```mermaid
graph LR
  A["alb node"] -->|"aws_lb + listener"| G["gateway elbv2ctl"]
  G --> P["nginx proxy container"]
  P --> E["ecs task"]
  P --> V["ec2 VM<br/>via vzNAT address"]
  P -->|"ALB_ENDPOINT fact"| W["World"]
```

Targets resolve to a **real address**: an `i-…` target is looked up to the VM's own vzNAT IP — a container on Colima really can reach it, which was measured rather than assumed. The endpoint rides out as a fact because `DNSName` cannot carry a dynamic port.

### Event delivery

`reconcile/dispatch.py — one tick, three sources`

```mermaid
graph TD
  SCH["EventBridge rule<br/>rate(1 minute)"] --> D["dispatcher"]
  SQS["SQS event source<br/>mapping"] --> D
  S3N["S3 notification<br/>enqueued on write"] --> D
  D -->|"lambdactl.invoke"| L["Lambda RIE container"]
  L -->|"verdict"| W["World + events.jsonl"]
```

Real AWS's event source mapping *is* a poller, so odin polling is the actual architecture rather than a compromise. What cannot be delivered is **refused loudly** — pattern rules, cron, non-Lambda targets — because a trigger that applies clean and never fires is the worst failure available.

### DynamoDB · ElastiCache · ECR

`dynalite · redis:7-alpine · registry:2`

```mermaid
graph LR
  D["dynamodb node"] --> G["gateway"]
  C["elasticache node"] --> G
  E["ecr node"] --> G
  G --> DL["dynalite :4567"]
  G --> RD["Redis container"]
  G --> RG["registry:2"]
  RG -.->|"docker push / pull<br/>direct, not proxied"| Host["host + Colima daemon"]
```

ECR's data plane is deliberately **not proxied** — a real `docker push` dials the registry's published port, and the daemon inside Colima can pull from it. Redis's wire protocol is not AWS-signed, so no IAM edge can gate a GET or SET; only its control plane is gated.

### IAM · Secrets · SSM · CloudWatch Logs

`JSON sidecars under .odin/<env>/gateway/`

```mermaid
graph TD
  Edge["IAM edge on the canvas"] -->|"aws_iam_role_policy"| TF["main.tf"]
  TF -->|"tofu apply"| IC["iamctl store"]
  IC -->|"compile_policies_from_iam"| EV["evaluate()"]
  W["workload"] -->|"role · task_role_arn<br/>· instance profile"| IC
  LG["logs node"] --> LC["logsctl"]
```

Each workload reaches its role through its **own service record** — a lambda's `role`, a task definition's `task_role_arn`, an instance's profile — never a naming convention. That is what makes a drawn permission take effect only after it is applied.

## Measured, not estimated

| | |
|---|---|
| scheduled rule → invoke | **60.5s**, once per period, on a real RIE container |
| goaws poll overhead | **1.5×** — 29.8–30.9s held for a 20s wait |
| gateway added latency | **~2ms** — verify → classify → evaluate → forward |
| integration suite | **86 tests**, three partitions, ~65 min total |

## The boundary, stated

The mesh gates overlay traffic and nothing else. ECS tasks, the ALB proxy, Lambda
containers and ElastiCache are not mesh members, so their egress is ungated
whatever group they are drawn into. A security group cannot stop a workload
reaching the internet over Colima/Lima NAT. Drawing those as gated would have
made a prettier diagram and a false one.
