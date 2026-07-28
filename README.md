# Odin

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/kessler-frost/odin/actions/workflows/ci.yml/badge.svg)](https://github.com/kessler-frost/odin/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-blue.svg)

Design AWS architectures on a canvas, compile them to Terraform, and run the
result on your laptop with IAM enforced.

Odin is a local cloud you draw. Drag S3, RDS, Lambda, EC2 and the rest onto a
canvas and wire them together; odin compiles that drawing into OpenTofu
configuration, applies it against its own AWS-compatible gateway, and the
gateway provisions each resource as a running service — Postgres for
RDS, RustFS for S3, a Lima VM for EC2, a RIE container for Lambda. The
permission edges you draw between nodes become policy the gateway checks on
every API call, so an unauthorised request comes back `AccessDenied` instead of
succeeding quietly.

The translation is a compiler, not a prompt: the same canvas always produces
byte-identical HCL, and Terraform reads back into canvas nodes the same way
every time. No model call sits in that path.

[NORTHSTAR.md](NORTHSTAR.md) has the direction; [ROADMAP.md](ROADMAP.md) has
what is next.

![Odin — a VPC/Subnet/EC2 stack, an SG, S3/SQS/SNS/DynamoDB/RDS, a Lambda, an ECS service, an IAM role and an ECR repo, drawn on the canvas with an IAM permission edge (EC2 → S3, GetObject/PutObject/ListBucket)](assets/odin-canvas.png)

**Draw it, press Apply, watch it come up.** Three resources placed on the
canvas, applied, and healthy — a RustFS bucket, a dynalite table and a Postgres
container behind those badges. The clip runs at roughly 4×; the apply it records
took 103 seconds:

![Three resources drawn on odin's canvas, applied, and going healthy — S3, DynamoDB and RDS badges turning green, with the database's live host:port shown on the node](assets/odin-draw-apply.gif)

**IAM permissions are edges you draw**, and the canvas updates live when
anything changes it, including the CLI. This clip authors the edge with
`odin canvas set`; the already-open browser converges it with no reload:

![An IAM permission edge appearing on odin's canvas, from an EC2 instance to an S3 bucket, labelled GetObject, PutObject, ListBucket](assets/odin-iam-edge.gif)

**The `{ }` button shows the Terraform the canvas compiles to** — the same HCL
Apply runs, not a preview of something else:

![odin's code panel scrolling through the Terraform generated from the canvas: aws_dynamodb_table, aws_db_instance and aws_s3_bucket resources](assets/odin-code-panel.gif)

## Install

macOS with [Homebrew](https://brew.sh). This installs colima, the docker CLI,
opentofu, uv and lima, starts colima, installs odin, and runs `odin doctor`:

```bash
curl -fsSL https://raw.githubusercontent.com/kessler-frost/odin/main/scripts/install.sh | sh
odin start          # serves http://localhost:4200 and backgrounds itself
```

Draw something from the sidebar and press **Apply**. The Events tab streams the
`tofu apply` output, badges go `healthy`, and the `{ }` button shows the
generated Terraform.

To remove everything the installer put on the machine, including the Homebrew
packages it added:

```bash
curl -fsSL https://raw.githubusercontent.com/kessler-frost/odin/main/scripts/uninstall.sh | sh
```

<details>
<summary>Manual install, and what <code>odin doctor</code> checks</summary>

```bash
brew install colima docker opentofu uv lima
colima start
git clone https://github.com/kessler-frost/odin && cd odin
uv sync --extra dev
uv run odin doctor        # every prerequisite, with a fix line for each
```

`odin doctor` reports on colima, docker, opentofu, lima and the `claude` CLI,
and exits non-zero when something needed is missing — so it works as a CI
preflight as well as a first-run check.

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

Nodes drawn inside a VPC or Subnet box belong to it — geometry compiles to
`vpc_id` and `subnet_id`. An ECS box drawn inside an EC2 box runs its tasks in
that VM. One node can expand to several Terraform resources: an ALB becomes a
load balancer plus a target group plus a listener; a Lambda drawn without a role
gets one generated for it.

The sidebar also carries nine tiles odin cannot build yet — `kinesis`, `kms`,
`route53`, `apigateway`, `efs`, `events`, `ebs`, `eip`, `igw`. Each is labelled
**`(placeholder)`**, and the marker means exactly one thing in both directions: a
marked tile is reported under `skipped` and never touched, and an unmarked tile is
a service odin models end to end. A test reads the modelled-kinds list out of
`translate.py` itself, so the label cannot drift from what Apply does.

Environments are independent: `--env staging` and `--env prod` keep separate
canvases, separate containers and separate state.

## Apply

Apply commits the canvas as the desired state, generates Terraform, runs
`tofu apply` against the gateway, and waits for what it built to be reachable
before reporting success. A resource that never becomes healthy fails the apply
and says which one and why.

```bash
odin apply --env prod
odin world --env prod          # one line per resource, with its live facts
odin destroy --env prod
```

If a container is killed or removed out of band, the next apply rebuilds it and
tells you what that cost:

```
note: desired state applied; rds app-db was re-created because container
odin-rds-prod-app-db is not running (exit 137) (its data did not survive —
the container is new and empty)
```

## Edges are IAM

An edge from a workload to a resource is a permission. Draw one and pick the
actions; the gateway compiles them into policy and checks every request against
it. A call with no matching grant gets `AccessDenied`.

The edge type is chosen from what you connected — `lambda → dynamodb` is an IAM
grant with sensible defaults, `sg → ec2` is group membership, `ec2 → subnet` is
containment. Across the whole catalog no pair is ambiguous, so odin never has to
guess.

One thing worth knowing: **a drawn permission is enforced by odin's gateway, not
written into the generated Terraform.** `odin translate` says so on stderr, and
[docs/limits.md](docs/limits.md) explains what that means if you take the HCL
elsewhere.

## Driving it from a terminal

The UI is one client; the CLI is another, and both go through the same HTTP API.

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

Two features call a model, and neither of them is the translation.

**`odin chat`** turns plain English into canvas edits. It applies them directly —
the canvas is the review surface, the change appears in your open tab, and Cmd-Z
undoes it. It never presses Apply; building from the drawing stays yours.

```
$ odin chat "give the thumbnailer lambda read access to the uploads bucket"
Granted thumbnailer read access to the uploads bucket.
  - draw a iam edge from 'thumbnailer' to 'uploads' granting s3:GetObject, s3:ListBucket
canvas saved (4d18a5091659) — Cmd-Z in the UI undoes it
```

It proposes operations rather than rewriting the canvas, so every change is one
reviewable line and nothing you did not ask for can happen. It refuses to touch
what odin derives (containment, status) or to rename a node as if it were a
field edit — a label is the resource's name, so a rename destroys and recreates,
and it says so. It sees field *names*, never their values, so a canvas holding
an RDS password stays that way.

**"What's wrong here?"** selects nodes in the UI and asks why they are broken.
Odin gathers each node's config, observed phase, crash verdict, recent events
and a tail of its logs, and answers in plain English with per-node suspects. It
reads state and returns prose; it cannot change anything.

`ODIN_AI=0` turns off every model call odin can make. Both features then say so
and carry on — the debug panel answers with the reason, `odin chat` reports
`agent unavailable` and changes nothing.

## How it works

A canvas is the desired state. `spec/` stores it as append-only, content-addressed
revisions; `agent/hcl.py` compiles it to HCL; `simulate/` runs `tofu apply`
against `gateway/`, odin's own AWS API implementation; `reconcile/` observes what
actually exists and projects it back as node status.

The gateway is the interesting part: it speaks enough of S3, RDS, SQS, SNS,
DynamoDB, Lambda, ECS, EC2, IAM, ECR, Secrets Manager, SSM, CloudWatch Logs and
ELBv2 for the real AWS provider to apply against it, and it enforces SigV4 and
IAM on every call.

[docs/internals.md](docs/internals.md) has the architecture and how each claim
in this README is verified.

## Known limits

The ones most likely to matter:

- **A drawn IAM edge is not in the generated Terraform.** It is enforced by the
  gateway; `main.tf` taken to real AWS grants nothing.
- **An RDS container keeps no volume**, so anything that replaces it returns an
  empty database. Odin's own repair says so rather than reporting a bare success.
- **Import is narrower than `--live`**, and a live-imported RDS arrives with
  odin's default password because no AWS API returns one.
- **Lambda is inline code only**, one version, no S3-deployed packages.
- **Nebula is single-host.** The mesh, firewall and per-VM daemons work; a
  second machine joining the same environment is not built yet.

[docs/limits.md](docs/limits.md) has the complete list, including every argument
odin re-emits with its own value.

## Security

Odin has no authentication of its own. The control app binds `127.0.0.1` by
default, and applying a canvas runs what is on it: container images, EC2
user-data as root, Lambda code. That is what the tool is for, and it means a
canvas from someone else should be treated like a shell script you are about to
run.

Canvas secrets (an RDS `password`, a `secret` or `ssm` value) are stored and used
in cleartext across the canvas, Stack revisions, `world.json`, `events.jsonl` and
the generated Terraform and state. Those files are `0600` and `.odin/` is `0700`.

[SECURITY.md](SECURITY.md) has the full model, including what the gateway does
and does not check.

## Contributing

Issues and pull requests are welcome. `uv run pytest` runs the suite;
`uv run pytest -m integration` runs the slow tests that need Colima and
OpenTofu. [CONTRIBUTING.md](CONTRIBUTING.md) has the rest.

## Acknowledgements

Odin stands on [OpenTofu](https://opentofu.org), the
[Terraform AWS provider](https://github.com/hashicorp/terraform-provider-aws),
[RustFS](https://github.com/rustfs/rustfs), [goaws](https://github.com/Admiral-Piett/goaws),
[dynalite](https://github.com/mhart/dynalite), [Nebula](https://github.com/slackhq/nebula),
[Lima](https://lima-vm.io), [Colima](https://github.com/abiosoft/colima),
[FastAPI](https://fastapi.tiangolo.com) and [React Flow](https://reactflow.dev).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
