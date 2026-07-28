# Odin

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/kessler-frost/odin/actions/workflows/ci.yml/badge.svg)](https://github.com/kessler-frost/odin/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-blue.svg)

Design AWS architectures on a canvas, compile them to Terraform, and run the
result on your laptop with IAM enforced.

Odin is a local cloud you draw. Drag S3, RDS, Lambda, EC2 and the rest onto a
canvas and wire them together. Odin compiles that drawing into OpenTofu
configuration and applies it against its own AWS-compatible gateway, which
provisions each resource as a running service: Postgres for RDS, RustFS for S3,
a Lima VM for EC2, a RIE container for Lambda. The permission edges you draw
become policy the gateway checks on every API call, so an unauthorised request
comes back `AccessDenied`.

The translation is deterministic. The same canvas always produces byte-identical
HCL, Terraform reads back into canvas nodes the same way every time, and no model
call sits anywhere in that path.

## Contents

[Install](#install) · [What you can do](#what-you-can-do) · [Apply](#apply) ·
[Edges are IAM](#edges-are-iam) · [Terminal](#driving-it-from-a-terminal) ·
[AI](#ai-two-features-one-switch) · [How it works](#how-it-works) ·
[Known limits](#known-limits) · [Security](#security) ·
[Contributing](#contributing) · [Where it's going](#where-its-going)

![Odin — a VPC/Subnet/EC2 stack, an SG, S3/SQS/SNS/DynamoDB/RDS, a Lambda, an ECS service, an IAM role and an ECR repo, drawn on the canvas with an IAM permission edge (EC2 → S3, GetObject/PutObject/ListBucket)](assets/odin-canvas.png)

**Draw it, press Apply, watch it come up.** Three resources placed on the canvas,
applied, and healthy, with a RustFS bucket, a dynalite table and a Postgres
container behind those badges. The clip runs at roughly 4×; the apply it records
took 103 seconds.

![Three resources drawn on odin's canvas, applied, and going healthy — S3, DynamoDB and RDS badges turning green, with the database's live host:port shown on the node](assets/odin-draw-apply.gif)

**IAM permissions are edges you draw**, and the canvas updates live when anything
changes it, including the CLI. This clip authors the edge with `odin canvas set`,
and the already-open browser converges it with no reload.

![An IAM permission edge appearing on odin's canvas, from an EC2 instance to an S3 bucket, labelled GetObject, PutObject, ListBucket](assets/odin-iam-edge.gif)

**The `{ }` button shows the Terraform the canvas compiles to** — the same HCL
that Apply runs.

![odin's code panel scrolling through the Terraform generated from the canvas: aws_dynamodb_table, aws_db_instance and aws_s3_bucket resources](assets/odin-code-panel.gif)

## Install

macOS with [Homebrew](https://brew.sh). This installs colima, the docker CLI,
opentofu, uv and lima, starts colima, installs odin, and runs `odin doctor`:

```bash
curl -fsSL https://raw.githubusercontent.com/kessler-frost/odin/main/install.sh | sh
odin start          # serves http://localhost:4200 and backgrounds itself
```

Draw something from the sidebar and press **Apply**. The Events tab streams the
`tofu apply` output, badges go `healthy`, and the `{ }` button shows the
generated Terraform.

To remove everything the installer put on the machine, including the Homebrew
packages it added:

```bash
curl -fsSL https://raw.githubusercontent.com/kessler-frost/odin/main/uninstall.sh | sh
```

<details>
<summary>Manual install</summary>

```bash
brew install colima docker opentofu uv lima
colima start
git clone https://github.com/kessler-frost/odin && cd odin
uv sync --extra dev
uv run odin doctor        # every prerequisite, with a fix line for each
```

`odin doctor` reports on colima, docker, opentofu, lima and the `claude` CLI, and
exits non-zero when something needed is missing, so it works as a CI preflight
too.

</details>

## What you can do

Drag any of these onto the canvas, wire them up, and press Apply:

| Kind | What it becomes |
|---|---|
| **S3** | A RustFS bucket, addressable with boto3 |
| **RDS** | A Postgres container, with a live `DATABASE_URL` on the node |
| **SQS / SNS** | goaws queues and topics, subscriptions included |
| **DynamoDB** | A dynalite table |
| **ElastiCache** | A Redis container |
| **Lambda** | A container running the AWS Lambda Runtime Interface Emulator |
| **ECS** | Containers scheduled per task, with placement and rolling updates |
| **EC2** | A Lima VM, joined to the environment's Nebula mesh |
| **VPC / Subnet / SG** | Nebula networks and compiled firewall rules |
| **IAM role, ECR, Secrets, SSM, CloudWatch Logs, ALB** | Gateway-backed AWS APIs |

Geometry carries meaning. A node drawn inside a VPC or Subnet box belongs to it,
which compiles to `vpc_id` and `subnet_id`, and an ECS box drawn inside an EC2 box
runs its tasks in that VM. One node can expand to several Terraform resources: an
ALB becomes a load balancer plus a target group plus a listener, and a Lambda
drawn without a role gets one generated for it.

Environments are independent. `--env staging` and `--env prod` keep separate
canvases, containers and state. `odin envs` lists them, and `odin env rm <name>`
decommissions one — teardown, then the environment itself.

## Apply

Apply commits the canvas as the desired state, generates Terraform, runs `tofu
apply` against the gateway, and waits for what it built to answer before
reporting success. A resource that never becomes healthy fails the apply, and the
output names which one and why.

```bash
odin apply --env prod
odin world --env prod          # one line per resource, with its live facts
odin destroy --env prod        # tear the resources down, keep the environment
odin env rm prod               # ...and remove the environment itself
```

`odin destroy` and `odin env rm` are different verbs. Destroy keeps the
environment on purpose: its desired state is what makes a retry possible and its
reconciler is what converges the next apply. `env rm` is the decommission — the
same teardown, then the `.odin/prod/` directory, the gateway credentials it
issued, its synthesized control-plane records, its reconciler, and its entry in
`odin envs`. It is not undoable (`odin export --env prod` first if you might want
it back), and it exits non-zero, having deleted nothing, if the environment is
not actually gone.

If a container is killed or removed out of band, the next apply rebuilds it and
tells you what that cost:

```
note: desired state applied; rds app-db was re-created because container
odin-rds-prod-app-db is not running (exit 137) (its data did not survive —
the container is new and empty)
```

## Edges are IAM

An edge from a workload to a resource is a permission. Draw one, pick the actions,
and the gateway compiles them into policy it checks on every request. A call with
no matching grant gets `AccessDenied`.

The edge type comes from what you connected: `lambda → dynamodb` is an IAM grant
with sensible defaults, `sg → ec2` is group membership, `ec2 → subnet` is
containment. Across the whole catalog no pair is ambiguous, so odin never guesses.

A drawn permission is emitted as a real `aws_iam_role_policy` on the workload's
role, so it survives into the Terraform and back out of it. One caveat if you take
the HCL to Amazon: the policy's `Resource` is odin's node label, which is what the
gateway matches on, where AWS expects an ARN. `odin translate` says so per policy
on stderr.

## Driving it from a terminal

The UI is one client and the CLI is another, both going through the same HTTP API.

```bash
odin canvas set architecture.json --env prod   # author a canvas from a file
odin translate --env prod                      # print the Terraform it compiles to
odin import-tf ./terraform-project             # read an existing project back in
odin chat "give the worker read access to uploads" --env prod
odin export prod backup.tar.gz                 # and `odin import` to restore
```

[docs/cli.md](docs/cli.md) has the full surface, the JSON shapes, and the exit
codes and output contracts a CI job can depend on.

## AI: two features, one switch

Two features call a model, and the translation is never one of them.

**`odin chat`** turns plain English into canvas edits and applies them directly.
The canvas is where you review them: the change appears in your open tab, and
Cmd-Z undoes it. It never presses Apply, so building from the drawing stays yours.

```
$ odin chat "give the thumbnailer lambda read access to the uploads bucket"
Granted thumbnailer read access to the uploads bucket.
  - draw a iam edge from 'thumbnailer' to 'uploads' granting s3:GetObject, s3:ListBucket
canvas saved (4d18a5091659) — Cmd-Z in the UI undoes it
```

It works in named operations, so every change is one reviewable line and anything
you did not ask for is impossible to reach. It declines to write what odin derives
from geometry or from the live world, and it treats renaming as its own explicit
act, because a label is the resource's name and a rename destroys and recreates.
It sees field *names* and never their values, which keeps an RDS password out of
the prompt.

**"What's wrong here?"** selects nodes in the UI and asks why they are broken.
Odin gathers each node's config, observed phase, crash verdict, recent events and
a tail of its logs, then answers in plain English with per-node suspects. It reads
state and returns prose, and it can change nothing.

**Both are off until you turn them on.** The top bar carries an `AI OFF` switch,
and odin makes no model call of any kind while it reads that way. Flip it and
both features come alive; flip it back and they say so and carry on, with the
debug panel answering with the reason and `odin chat` reporting
`agent unavailable` while changing nothing.

Setting `ODIN_AI` in the environment overrides the switch in both directions,
which is what a CI job or a headless run wants. The switch renders disabled and
names the variable when that happens, so it never looks like a control that does
nothing.

## How it works

A canvas is the desired state. `spec/` stores it as append-only,
content-addressed revisions, `agent/hcl.py` compiles it to HCL, `simulate/` runs
`tofu apply` against `gateway/`, and `reconcile/` observes what exists and
projects it back as node status.

<details>
<summary><b>How an apply works</b> — diagram</summary>

```
+-----------------------------------+
|                                   |
|               Canvas              |    <----+
|                                   |         |
+-----------------------------------+         |
                  |                           |
               compile                        |
                  |                           |
                  |                           |
                  v                           |
+-----------------------------------+         |
|                                   |         |
|             Terraform             |         |
|                                   |         |
+-----------------------------------+         |
                  |                           |
             tofu apply                       |
                  |                           |
                  |                           |
                  v                           |
+-----------------------------------+         |
|                                   |         |
|            odin gateway           |         |
|                                   |         |
+-----------------------------------+         |
                  |                           |
              provision                       |
                  |                           |
                  |                           |
                  v                           |
+-----------------------------------+         |
|                                   |         |
| Postgres, RustFS, goaws, Lima VMs |   observe
|                                   |
+-----------------------------------+
```

Terraform never talks to Amazon. `tofu apply` is pointed at odin's gateway, which
speaks enough of S3, RDS, SQS, SNS, DynamoDB, Lambda, ECS, EC2, IAM, ECR, Secrets
Manager, SSM, CloudWatch Logs and ELBv2 for the AWS provider to apply against it.
Status flows the other way on its own loop, so a container that dies turns its
node red without anyone pressing anything.

</details>

<details>
<summary><b>How a permission is enforced</b> — diagram</summary>

```
+-----------------------------------+
|                                   |
|   boto3 call from your workload   |
|                                   |
+-----------------------------------+
                  |
                  |
                  v
+-----------------------------------+
|                                   |
|       verify SigV4 signature      |
|                                   |
+-----------------------------------+
                  |
                  |
                  v
+-----------------------------------+
|                                   |
| classify: s3:GetObject on uploads |
|                                   |
+-----------------------------------+
                  |
                  |
                  v
<----------------------------------->
|                                   |
|   does the APPLIED IAM allow it?  |---------------+
|                                   |               |
<----------------------------------->              no
                  |                                 |
                 yes                                |
                  |                                 |
                  v                                 v
+-----------------------------------+     +------------------+
|                                   |     |                  |
|   run it against the substitute   |     | 403 AccessDenied |
|                                   |     |                  |
+-----------------------------------+     +------------------+
```

The gateway holds a per-environment key for each workload, so it knows which node
is calling. The edges you drew compile to policy statements, and a call is matched
against them by action and resource before it reaches any substitute.

</details>

[docs/internals.md](docs/internals.md) has the architecture in full, and how each
claim in this README is verified.

## Known limits

The ones most likely to matter:

- **An emitted IAM policy names resources by label, not ARN.** It round-trips
  through odin perfectly; taken to Amazon each policy needs its `Resource`
  rewritten as an ARN.
- **An RDS container keeps no volume**, so anything that replaces it comes back
  empty. Odin's own repair says so in the apply output.
- **Import is narrower in `--live` mode**, and a live-imported RDS arrives with
  odin's default password, because no AWS API returns one.
- **Lambda is inline code only**, one version, with no S3-deployed packages.
- **Nebula is single-host.** The mesh, firewall and per-VM daemons work; a second
  machine joining the same environment is still to come.

[docs/limits.md](docs/limits.md) has the complete list, including every argument
odin re-emits with its own value.

## Security

Odin has no authentication of its own. The control app binds `127.0.0.1` by
default, and applying a canvas runs what is on it: container images, EC2 user-data
as root, Lambda code. That is what the tool is for, and it means a canvas from
someone else deserves the same caution as a shell script you are about to run.

Canvas secrets — an RDS `password`, a `secret` or `ssm` value — are stored and
used in cleartext across the canvas, Stack revisions, `world.json`,
`events.jsonl`, and the generated Terraform and state. Those files are `0600` and
`.odin/` is `0700`. [SECURITY.md](SECURITY.md) has the full model.

## Contributing

Issues and pull requests are welcome. `uv run pytest` runs the suite, and
`uv run pytest -m integration` runs the slow tests that need Colima and OpenTofu.
[CONTRIBUTING.md](CONTRIBUTING.md) has the rest.

## Where it's going

[NORTHSTAR.md](NORTHSTAR.md) has the direction and [ROADMAP.md](ROADMAP.md) has
what comes next, including closing the known limits above.

## Acknowledgements

Odin stands on [OpenTofu](https://opentofu.org), the
[Terraform AWS provider](https://github.com/hashicorp/terraform-provider-aws),
[RustFS](https://github.com/rustfs/rustfs), [goaws](https://github.com/Admiral-Piett/goaws),
[dynalite](https://github.com/mhart/dynalite), [Nebula](https://github.com/slackhq/nebula),
[Lima](https://lima-vm.io), [Colima](https://github.com/abiosoft/colima),
[FastAPI](https://fastapi.tiangolo.com) and [React Flow](https://reactflow.dev).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
