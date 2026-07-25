# Odin

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/kessler-frost/odin/actions/workflows/ci.yml/badge.svg)](https://github.com/kessler-frost/odin/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-blue.svg)

Odin is a local-first AWS: you draw an architecture on a canvas, one button
turns it into Terraform and runs `tofu apply` against odin's own gateway, and
the gateway fulfills every AWS call with real local substitutes — a real
Postgres for RDS, a real Lima VM for EC2, real containers for ECS and Lambda,
and so on. IAM permissions are edges you draw between nodes; the gateway
enforces them for real, so a workload with no edge to a bucket gets a real
`AccessDenied`, not a warning. See [NORTHSTAR.md](NORTHSTAR.md) for the full
direction this is built toward.

![Odin — a VPC/Subnet/EC2 stack, an SG, S3/SQS/SNS/DynamoDB/RDS, a Lambda, an ECS service, an IAM role and an ECR repo, drawn on the canvas with an IAM permission edge (EC2 → S3, GetObject/PutObject/ListBucket)](assets/odin-canvas.png)

## What Apply actually does

There is one button. Draw nodes, wire edges, click **Apply**:

- Odin generates Terraform from the canvas and runs `tofu apply` against its
  own gateway (a SigV4-verifying reverse proxy on `127.0.0.1`, one per
  running server).
- The gateway answers each AWS call by either forwarding to a real backing
  (S3 → [RustFS](https://github.com/rustfs/rustfs), SQS/SNS →
  [goaws](https://github.com/Admiral-Piett/goaws), DynamoDB →
  [dynalite](https://github.com/mhart/dynalite)) or owning the resource model
  itself and driving a real substrate (EC2 → a [Lima](https://lima-vm.io/)
  VM, ECS → [Colima](https://github.com/abiosoft/colima) containers, Lambda →
  a real [AWS RIE](https://github.com/aws/aws-lambda-runtime-interface-emulator)
  container, ECR → a [`registry:2`](https://github.com/distribution/distribution)
  container, RDS → a real Postgres container). Every drawable kind is on
  Terraform now; anything a canvas asks for that odin can't stand behind
  (a MySQL engine, say) is listed in the code panel with the reason
  instead of being silently dropped.
- Every workload node (EC2, ECS, Lambda) is issued its own AWS keypair and
  gets it automatically — baked into EC2's cloud-init, injected into each ECS
  task's and Lambda's container environment. It only has whatever permissions
  you drew as edges.
- Deleting a node and clicking Apply removes exactly that resource. There is
  no Destroy button: an **empty canvas + Apply tears everything down** — every
  container, the Lima VM, the Terraform state.

Everything above is exercised end to end by `tests/gateway/test_gateway_e2e.py`,
`tests/simulate/test_*_tf_e2e.py`, and was re-verified live for this release
(see [Verification](#verification) below) — drawing the full stack in the
screenshot above, applying it for real, checking it with `docker ps` /
`limactl list` / `aws-cli` against the gateway, and tearing it back down to
nothing.

## Edges are IAM

Draw an edge from a compute node (EC2, Lambda, ECS) to a resource (S3,
DynamoDB, SQS, SNS, RDS) and its config panel offers an "IAM Policy" edge type
with a checkbox per action (`s3:GetObject`, `s3:PutObject`, …). Whatever you
check is what that workload's issued key can do — nothing else. Verified live
against a running gateway:

```
$ aws --endpoint-url http://host.docker.internal:4266 s3api list-objects-v2 --bucket bucket1
  (fn1's key, edge only grants s3:GetObject)
An error occurred (AccessDenied) when calling the ListObjectsV2 operation:
User is not authorized to perform: s3:ListBucket

$ aws --endpoint-url http://host.docker.internal:4266 s3api get-object --bucket bucket1 --key nope.txt out.txt
  (same key, same bucket, the granted action)
An error occurred (NoSuchKey) when calling the GetObject operation:
The specified key does not exist.
  (past auth — a real 404, not a blanket denial)

$ aws --endpoint-url http://host.docker.internal:4266 s3api get-object --bucket bucket1 --key nope.txt out.txt
  (a different workload's key, with no edge to this bucket at all)
An error occurred (AccessDenied) when calling the GetObject operation:
User is not authorized to perform: s3:GetObject
```

Network reachability (who can talk to whom on the wire) is a separate concern,
handled by [Nebula](https://github.com/slackhq/nebula) — VPCs and Security
Groups drawn on the canvas compile to real Nebula network + firewall config.

## "What's wrong here?"

Select one or more nodes on the canvas (click, or Cmd-drag a region) and a bar
appears with a **What's wrong here?** button and a free-form question box. Odin
gathers each selected node's desired config, its references to other nodes, its
observed phase and real crash verdict (an ECS task's `stoppedReason` + exit
code, an EC2 or Lambda `StateReason`, a Postgres connection error), its last
few events, and a tail of its real container/VM logs — plus the last lines of
the environment's own `tofu apply`/`destroy` output, which belong to no single
node — then one model call answers in plain English and names per-node suspects
with reasons. A node that was deleted out from under odin (`limactl delete`,
`docker rm`) carries the reality sweep's own verdict, so "your VM is gone —
re-Apply to recreate" is part of the evidence too.

This is the one place in odin where the AI is load-bearing. Generating
Terraform from the canvas and reading it back are deterministic code, on
purpose; there is no deterministic function from *exit code 1 + forty lines of
stdout* to *"this task exits because the config it expects was never
supplied"*. The compiler builds; the agent explains.

Honest about what it needs and what it can't do:

- It requires the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
  — the `claude` CLI on your `PATH`, signed in. Without it the answer is
  literally `agent unavailable` and the panel says so. Nothing else in odin
  needs it; `ODIN_DEBUG_AGENT=0` turns the feature off outright, and
  `ODIN_DEBUG_TIMEOUT` (default 90s) bounds the call.
- Secrets never reach the model: env-var **values** are reduced to key names,
  any field odin flags sensitive is `[REDACTED]`, and every string in the
  evidence — including log lines, tofu's own output, and an RDS node's
  `DATABASE_URL` facts — is scrubbed of known secret values first.
- It reads state and returns prose. It cannot change your canvas, your
  Terraform, or anything running.
- The evidence is capped (40 log lines and 10 events per node, 20 nodes, 20
  lines of tofu output), so a failure whose cause scrolled past that window
  won't be in the answer. The Logs tab has the full tail.

## What's on the canvas today

Compute: EC2, Lambda, ECS. Networking: VPC, Subnet, Security Group (draw an
EC2 inside a Subnet inside a VPC — nesting is spatial, not a special
connector; drag a node's corner into a container and it belongs to it).
Storage/data: S3, DynamoDB, RDS. Messaging: SQS, SNS. Identity/registry: IAM
Role, ECR. Everything else in the sidebar is drawable but not yet backed by
Terraform generation — Apply reports those honestly as unsupported instead of
pretending they applied.

Known v1 limits, recorded rather than hidden:

- **Lambda**: inline code only (paste it in the config panel — odin zips and
  ships it), one version (`$LATEST`); no S3-deployed packages, versions, or
  aliases yet.
- **ECS**: `network_configuration` (awsvpc/Fargate-style ENIs) isn't modeled —
  odin's tasks run `launch_type = "EC2"` / `network_mode = "bridge"`, which
  needs none; a task that dies between API calls isn't auto-replaced until
  the next Apply reconciles the service; a `tags` block on the service can
  show as drift on a subsequent `tofu plan` (tags aren't echoed back yet).
- **SNS→SQS subscriptions**: adding the edge to an *already-healthy* topic
  doesn't retroactively re-provision the subscription — remove and re-add the
  topic (or its edge) to force it. Fixed on create; the live-edit path is a
  known gap. Every subscription odin generates also sets
  `raw_message_delivery = true` — the queue gets the published body verbatim,
  not SNS's JSON envelope — including on an import round trip where your `.tf`
  didn't have it. It's deliberate (odin's own SQS/SNS substitute is subscribed
  the same way, so `tofu apply` and Apply deliver identically), but it changes
  what a consumer reads, so it's said out loud rather than left to be
  discovered.
- **Importing Terraform**: `odin import-tf` takes a file or a whole directory
  (every `*.tf` in it becomes one canvas). Every argument odin doesn't model is
  named on stderr rather than dropped in silence, and a few are re-emitted with
  odin's own value (`internal`, `force_destroy`, `skip_final_snapshot`,
  `recovery_window_in_days`) — those warn too when your value differs. Every
  resource also gains an `odin:node` tag, so a byte-identical round trip is
  impossible by design.
- **RDS** is Terraform-managed (`aws_db_instance` → a real Postgres
  container), but Postgres-only: choosing MySQL or MariaDB is declined with
  a reason rather than quietly given a Postgres. `allocated_storage` and
  `instance_class` round-trip faithfully but resize nothing, there are no
  snapshots, and a node's name must be a valid RDS identifier (lowercase,
  hyphen-separated).
- **Nebula** is live single-host: VPC/SG config compiles to real Nebula
  network + firewall primitives, the host runs a real (and fully
  unprivileged — no root, no sudo, no one-time setup) lighthouse process,
  and every VPC-joined EC2 VM runs a real `nebula` daemon carrying the
  compiled SG firewall. Cross-Mac reachability (a second machine joining the
  same mesh) lands with multi-Mac support.

## How it's built

- **UI:** React 19 + ReactFlow + Tailwind v4, served by Vite (`ui/`, `bun`).
- **Backend:** Python 3.12+ (`uv`), FastAPI + WebSocket, Pydantic.
- **The gateway** (`src/odin/gateway/`): verifies SigV4, classifies each call
  into (service, action, resource), evaluates it against the edges you drew,
  then either forwards to a real backing or answers from its own per-service
  model store (EC2/VPC/SG/IAM/ECR/Lambda/ECS — nobody makes an open-source AWS
  API for these, so odin owns the model and binds it to a real substrate).
- **Canvas ↔ Terraform translation** (`src/odin/agent/`): deterministic in
  both directions — the same canvas always produces the same `.tf`, and
  `/import-tf` parses HCL (or resolves live resources) back into canvas
  nodes, no model call in the loop for either. An optional agent pass
  (`claude-agent-sdk`; set `ODIN_TRANSLATE_REFINE=1` to turn it on — off by
  default) can review the generated file and add comments or tags; every
  return is re-validated against the skeleton (same resource set, every
  argument's value byte-identical) and discarded on any deviation, so it
  cannot change what gets applied. The one genuinely agent-shaped job in here
  is failure explanation (`agent/debugger.py`, `POST /agent/debug`) — see
  ["What's wrong here?"](#whats-wrong-here) above.
- **Runtime:** real containers via Colima (default) or inside a Lima VM
  (`src/odin/runtime/`), and a real Lima VM for EC2 (`src/odin/compute/`).
- **Control loop:** a Spec Store (Stack = desired, World = observed) with a
  pure, idempotent `plan(Stack, World) → [Action]` reconciler that drives
  the non-Terraform resources (the AWS-shaped backings) and projects
  Terraform-owned resources' live status back into World too, so every
  node's badge reflects reality regardless of which path provisioned it.

## Requirements

- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- [Colima](https://github.com/abiosoft/colima) for the container runtime
- [Lima](https://lima-vm.io/) (`limactl`) — required for any canvas with an
  **EC2** node, since each one is a real Lima VM, and for running the
  containers inside a VM instead of on Colima directly. `odin doctor` reports
  it as optional and says exactly what it gates.
- [OpenTofu](https://opentofu.org/) on your `PATH` (Apply shells out to it)
- [bun](https://bun.sh/) — only if you're building the UI from a clone; the
  released package ships a pre-built UI, no `bun` needed

## Install

One command, if you have Homebrew (installs colima/opentofu/uv, starts
colima, installs odin, runs `odin doctor`):

```bash
curl -fsSL https://raw.githubusercontent.com/kessler-frost/odin/main/scripts/install.sh | sh
```

To undo it — stop the server, remove odin-managed containers/VMs, and
uninstall the tool (your Homebrew tools are left alone):

```bash
curl -fsSL https://raw.githubusercontent.com/kessler-frost/odin/main/scripts/uninstall.sh | sh
```

Or from a local clone, for development (verified verbatim against this repo):

```bash
git clone https://github.com/kessler-frost/odin.git
cd odin
uv tool install --editable ".[dev]"
cd ui && bun install
```

Or just the package, UI bundled in, no bun needed (CI fast-forwards the
`latest` branch on every tagged release — verified working):

```bash
uv tool install "git+https://github.com/kessler-frost/odin.git@latest"
```

## Quick start

```bash
odin start            # build the UI (first run) and serve on http://localhost:4200, in the background
odin start --dev      # Vite HMR + uvicorn --reload; runs in the FOREGROUND (Ctrl+C to stop)
odin stop             # stop a background `odin start`
odin status            # is it running?
odin clean             # remove test artifacts/logs (--all wipes .odin/ entirely)
```

Two things worth knowing before you hit `--dev`: it doesn't background
itself like plain `start` does — it stays attached to your terminal. And
`-p/--port` in `--dev` mode only repositions the Vite frontend; the backend
always binds `:4201` there (plain `start` has no such split — `-p` controls
the one port it uses).

Once it's up: draw something from the sidebar, click **Apply**, watch the
Events tab stream the `tofu apply` output and the node badges go
`healthy`. Open the `{ }` button in the top bar for the generated Terraform.

## The CLI is the same product

Everything the canvas does is drivable from a terminal — which also means
an agent (Claude Code, or anything that can run commands) can operate odin
directly. All commands take `--url`/`ODIN_URL` (default `localhost:4200`)
and `-o json` for machine-readable output.

```bash
odin canvas get                     # the drawn canvas, as JSON
odin canvas set my-canvas.json      # replace it (or pipe: ... | odin canvas set -)
odin translate                      # print the Terraform your canvas becomes
odin apply --env dev                # the Apply button, as a command
odin world --env dev                # live resource phases
odin events --env dev               # the event stream, one JSON line each
odin tf status --env dev            # tofu-side state
odin destroy --env dev              # full teardown (tofu half included)
odin import-tf existing.tf          # TF -> canvas JSON (pipe into canvas set -)
odin export --env dev               # back an env's state up to a tar.gz
odin import odin-dev-export.tar.gz  # restore it (works with odin down)
odin doctor                         # toolchain health, with exact fixes
odin --version                      # which odin this is
```

Exit codes are the contract: `0` success, `1` a refusal or a real failure, `2`
a usage/format error. One thing an exit-code-only check misses — a node Apply
skipped as unsupported still exits `0`, so gate on the payload instead:

```bash
odin apply --env dev -o json | jq -e '.unsupported | length == 0'
```

A round-trip example an agent might run:

```bash
odin canvas get | jq '.nodes += [{"id":"x1","type":"s3","data":{"label":"backups"}}]' | odin canvas set -
odin apply --env dev
```

## Backup and restore

`.odin/` is the only record that an env exists. Lose it and every container
that env owns is orphaned — nothing left knows they're odin's — and the next
startup reaper run, seeing no envs, deletes every odin VM. So take a
snapshot:

```bash
odin export --env dev                       # -> odin-dev-export.tar.gz
odin export --env dev -o ~/backups/dev.tgz  # or wherever you want it

odin stop                                   # restore is a server-down operation
odin import odin-dev-export.tar.gz          # back into env `dev`
odin import odin-dev-export.tar.gz --env dev2   # or alongside, under a new name
```

Both commands work directly on the filesystem — no server, no HTTP — because
the failure they exist for is the one where odin can't start.

**What's in the archive:** the env's whole control plane. The Stack revision
lineage and `HEAD`, `world.json`, the env's issued gateway credentials, the
gateway's synth stores and Lambda zips, and the tofu workspace including
`terraform.tfstate`. Plus a `manifest.json` recording the odin version, env
name, and timestamp. The only thing deliberately left out is
`tf/.terraform/` — `tofu init` rebuilds the provider cache from the same
`main.tf`, and it's hundreds of megabytes.

**What isn't:** data. This is control-plane state — it records that a bucket
named `uploads` should exist, never the objects inside it. Restore an env and
you get fresh, empty backings matching the archived desired state; a file you
had put in that bucket is gone. Container volumes are not backed up.

One exception worth knowing, because it cuts the other way: **CloudWatch
log-group events survive** an export → wipe → import round trip. Odin's log
sink is a control-plane sidecar (`.odin/<env>/gateway/logsctl.json`), not a
container volume, so log history comes back while bucket objects don't.

Importing state doesn't boot anything either. It puts odin's model of the
world back; `odin start` plus one Apply converges reality to it.

Guardrails, because both of these are destructive by nature: `import` refuses
to overwrite an existing env directory unless you pass `--force`, refuses to
run at all while odin is up — however you started it, `odin start` or
`uvicorn odin.server:create_app` by hand — and rejects any archive containing
an absolute path, a `..` traversal, or a symlink member. `odin status` and
`odin stop` see the same servers the refusal does; a server running against a
*different* store directory doesn't get in your way. The shared `.odin/canvas.json`
travels in the archive but is restored only under `--with-canvas` — a restore
should never silently replace the canvas you're drawing on.

The archive contains the env's credentials in cleartext. It's written `0600`,
and every file inside it is stored `0600` so a restore can only tighten a
store's modes — but treat the file like a private key anyway, because copying
it anywhere else won't preserve that. See [SECURITY.md](SECURITY.md#secrets).

## Security

Odin has no authentication of its own — the control app binds to
`127.0.0.1` by default, and applying a canvas runs whatever's on it for
real (container images, EC2 user-data as root, Lambda code). That's the
point of the tool, not a bug, but it means a canvas from someone else
should be treated like a shell script you're about to run.

A canvas secret (an RDS `password`, a `secret` or `ssm` node's value) is
stored and used in cleartext, in more than one file: the canvas, every Stack
revision, `world.json`, `events.jsonl`, and the generated Terraform plus its
state. All of them are `0600`, in `0700` directories, and that file mode is
the entire protection — there is no encryption at rest and no KMS. SECURITY.md
lists every file by name; treat canvas secrets as dev/test-grade. See
[SECURITY.md](SECURITY.md) for the full threat model and how to report a
vulnerability.

## Verification

Every claim above was checked against a real, running instance for the 0.4.0
release: the full canvas in the screenshot applied end-to-end (`tofu apply`
exit 0, every resource — including the ones Terraform owns — landing in
`/world` as `healthy`), real containers confirmed with `docker ps`
(`odin-lambda-*` running the actual `public.ecr.aws/lambda/python:3.12`
image, two `odin-ecs-*` task containers for a task-count-2 service, a
real `postgres:16-alpine` for RDS, the RustFS/goaws/dynalite/registry:2
backings), the IAM deny-check above run against the live gateway, and a full
teardown that really did destroy all 14 Terraform resources (down to a real
45-second SQS purge-then-delete wait) and leave `docker ps` empty. Environment
isolation was checked with a second env applying and tearing down its own S3
bucket independently. See `.superpowers/sdd/release-verify-report.md` for the
full pass/fail table and evidence.

## Acknowledgements

Odin stands on the shoulders of open source giants — most of what makes it work is
other people's excellent work, and a lot of the thanks belongs to them:

- **[OpenTofu](https://opentofu.org/)** + the [Terraform AWS provider](https://github.com/hashicorp/terraform-provider-aws) — the apply engine odin's gateway sits behind
- **[Colima](https://github.com/abiosoft/colima)** + **[Lima](https://lima-vm.io/)** — containers and VMs on the Mac
- **[PostgreSQL](https://www.postgresql.org/)** — the real backing for RDS
- **[RustFS](https://github.com/rustfs/rustfs)**, **[goaws](https://github.com/Admiral-Piett/goaws)**, **[dynalite](https://github.com/mhart/dynalite)** — the real backings for S3, SQS/SNS, and DynamoDB
- **[registry:2](https://github.com/distribution/distribution)** — the real backing for ECR image storage
- **[AWS Lambda RIE](https://github.com/aws/aws-lambda-runtime-interface-emulator)** — the real backing for Lambda invocation
- **[Nebula](https://github.com/slackhq/nebula)** — the mesh/firewall substrate for VPCs and Security Groups
- **[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)** — the optional, off-by-default comment/tag refinement pass over the deterministic canvas↔Terraform translation
- **[FastAPI](https://fastapi.tiangolo.com/)**, **[Pydantic](https://pydantic.dev/)**, **[boto3](https://github.com/boto/boto3)**, **[python-hcl2](https://github.com/amplify-education/python-hcl2)** — the backend
- **[React](https://react.dev/)** + **[React Flow](https://reactflow.dev/)** + **[Tailwind CSS](https://tailwindcss.com/)** + **[Vite](https://vitejs.dev/)** — the canvas UI
- **[uv](https://github.com/astral-sh/uv)** + **[bun](https://bun.sh/)** — the toolchain

Thank you to every one of these projects and their maintainers. 🙏

## License

Apache License 2.0. See [LICENSE](LICENSE).
