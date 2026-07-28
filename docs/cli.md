# Driving odin from a terminal

Everything the UI does, odin does from the command line, and both go through the
same HTTP API. The contracts below are what a script or CI job can rely on.

`odin --help` lists the whole command surface; what follows is the handful of
details that bite. Start with a round trip an agent might run:

```bash
odin canvas get \
  | jq '.nodes += [{"id":"x1","type":"s3","position":{"x":80,"y":80},"data":{"label":"backups"}}]' \
  | odin canvas set -
odin apply
```

**Environments.** Every command that touches one takes `--env`, defaulting to
`default`, which is also the env the browser opens on. An environment owns its
canvas as well as its state (`.odin/<env>/canvas.json`), so two envs can hold
genuinely different architectures — switching the top-bar selector loads that
env's own canvas, and `odin canvas get --env staging` prints the same document
the browser shows you there. `odin envs` lists the envs that have had something
applied, and answers `default` on a store where nothing ever has.

**`odin start`** backgrounds the server but does not return until it answers `GET
/health`, so `odin start && odin apply` works on the first try. If the server dies
on the way up or does not answer within two minutes, it says which, prints the tail
of `.odin/server.log`, and exits 1. A second `odin start` while one is up starts
nothing and does not adopt a new `--port`/`--host`; it says so and exits 0.
`odin start --dev` stays in the foreground, and its `-p/--port` moves only the Vite
frontend, since the backend always binds `:4201` in that mode.

### The canvas JSON schema

`.odin/<env>/canvas.json` is a plain `{"nodes": [...], "edges": [...]}` document,
the same file the UI reads and writes for that environment, so anything you
author by hand shows up on the canvas and vice versa.

Two fields are worth knowing about when you write one by hand, because odin has
to guess without them and a guess renders wrong rather than loudly:
`sourceHandle`/`targetHandle` on an edge (odin infers the sides the two nodes
face each other on, which is right for a straight run and arbitrary for a
deliberate layout), and a node's `type`, which must be a kind odin knows —
an unrecognised one draws as `unknown kind: <what you wrote>` rather than
silently rendering as something else.

```json
{
  "nodes": [
    { "id": "x1", "type": "s3", "position": {"x": 80, "y": 80},
      "data": { "label": "backups" } }
  ],
  "edges": [
    { "id": "e1", "source": "fn1", "target": "x1",
      "data": { "edgeType": "iam", "permissions": ["s3:GetObject"] } }
  ]
}
```

| field | required | what it is |
| ----- | -------- | ---------- |
| `id` | yes | unique within the canvas; what edges point at |
| `type` | yes | one of the kinds above; anything else is skipped, never applied |
| `position` | yes | coordinates on odin's 20px grid. Only the UI reads it, and a node without one used to blank the canvas; `odin canvas set` now fills one in and says so on stderr |
| `data.label` | yes | the canonical id: the name in `odin world`, in the Terraform, and in `${{...}}` references. Falls back to `id` |
| `data.*` | no | config fields as the panel writes them (`cidr`, `engine`, `image`, `password`, …). A value like `${{other.ATTR}}` becomes a live reference — see the note below for which kinds can be referenced |
| `data.vpc` / `data.subnet` | no | containment, as the *label* of the containing node. The UI derives these from geometry; by hand, set them yourself |
| `size` | no | width/height, for the container kinds (`vpc`, `subnet`) whose geometry is what nesting means |

`edgeType` says what the line MEANS. Six values, and only the first four change
anything odin builds:

| `edgeType` | between | what it does |
| ---------- | ------- | ------------ |
| `iam` | a compute kind and an IAM target | a real grant — `permissions` is exactly what the workload's key may do |
| `sg` | `sg` and `ec2`/`rds` | membership: adds the group to that node's `securityGroups` |
| `role` | `iam_role` and `lambda` | sets the lambda's execution role (a role typed into the node wins) |
| `subscription` | `sns` and `sqs` | the topic fans out to the queue |
| `target` | `alb` and `ecs` | the load balancer fronts that service |
| `unmodelled` | anything else | odin has no model for the pair: stored, drawn grey, acts on nothing |

Permissions with no `edgeType` are treated as `iam`; an edge with neither is
`unmodelled`. `"network"` is the old name for `unmodelled` and still parses, so
a canvas saved before v0.8.14 loads unchanged.

Two things worth knowing, because they are not what the table implies.
**`subscription` and `target` are descriptive only** — the generator matches on
the two node KINDS, not on `edgeType`, so *any* edge between an `sns` and an
`sqs` node creates a real subscription (the same for `alb`↔`ecs`). And
**direction does not matter anywhere**: `sqs → sns` is read as the subscription
it obviously is, exactly as `sg → ec2` and `ec2 → sg` are the same membership.
The one exception is `iam`, where the arrow is the grant: it runs from the
workload to the thing it may use.

**`${{producer.ATTR}}` works between specific kinds, not any two nodes.** Only
four kinds publish an address a reference can resolve — **`rds`**
(`DATABASE_URL`, `endpoint`, and the `_VM`/`_MESH` variants),
**`elasticache`** (`REDIS_URL`, `endpoint`, `port`, `_VM`), **`alb`**
(`ALB_ENDPOINT`) and **`ec2`** (`PRIVATE_IP`, `MESH_IP`) — and only **`ecs`** and
**`lambda`** nodes consume refs, since a ref is delivered as an environment
variable when the workload's container launches. That is also *when* it resolves:
at launch, by the gateway, not when the canvas is applied.

A ref to any other kind is **refused before tofu runs** (HTTP 409, naming the
reference and the variable that would have gone unset) rather than failing
mid-apply. The trap it saves you from: `s3`, `sqs`, `sns` and `dynamodb` *do*
publish facts you can see in `odin world` — `BUCKET`, `QUEUE_URL`, `TOPIC_ARN`,
`TABLE` — but those are **observed state, not wiring values**, so the syntax
invites a reference that can never resolve. Reach those by name with the AWS SDK
instead: odin already injects `AWS_ENDPOINT_URL` and the node's own credentials
into the container.

## Contracts a script can rely on

Exit codes: `0` success, `1` a refusal or a real failure, `2` a usage error or an
unreachable server. Two commands answer a question rather than perform an action,
and there the code *is* the answer:

| command | `0` | non-zero |
| ------- | --- | -------- |
| `odin status` | running, and every env's reconciler is ticking | `1` not running, **or** a reconciler has stopped converging; `2` running, but whether its reconcilers are converging is **UNKNOWN** |
| `odin tf plan` | no changes | `2` changes, `1` error or refusal, `3` server unreachable or an unusable `--url` |

`status` exits `2` when it holds the store lock — so odin is definitely running —
but the server did not answer `/health`, usually because it is on another port and
no `--url` was passed. That is a third answer: `0` claims both
halves ("running **and** converging") and reporting `1` would invent a reconciler
failure out of a URL guess. So `odin status && odin apply` still refuses to apply
into an env nothing will converge, without ever crying wolf.

`tf plan` answers `3` rather than `2` when it cannot reach the server, so a down
odin can never be read as drift — and for the same reason a `--url`/`ODIN_URL`
odin cannot dial answers `3` there too, where every other command answers `2`.
A typo'd URL must not pass a CI drift gate as real drift. `odin stop` is the mirror of `status`: nothing
running is exit `0`, since that is the end state it was asked for, and it waits
up to 20 seconds for the server to release the store lock before answering. Both
of its non-zero cases mean odin is still up.

**A failed destroy does not leave things half-gone — it leaves them coming back.**
The env's desired state is deliberately kept, which is what makes a retry possible
at all, so the reconciler re-creates any `s3`/`sqs`/`sns`/`dynamodb` resource the
destroy already removed, within about one tick (measured: 0.74–0.76s at the default
1s poll). So `still_standing` in the failure body is tofu's state plus real
containers *at that moment*, not a list of what exists now — `odin world` answers
that. To freeze an env while you diagnose, run `odin stop` first: nothing converges
an env while odin is not running.

Three things a JSON reader should know about failure bodies. `still_standing.tf_state`
and `still_standing.containers` are `null` when odin **could not tell** (an
unreadable state file, a docker daemon that would not answer) as opposed to `[]`
for "nothing there" — `jq '.still_standing.containers | length'` breaks on null, and
that distinction is the point, because an empty list used to be reported for both.
`unhealthy_resources` now also appears when tofu itself failed, carrying odin's own
recorded reason for a resource tofu could only describe as an unexpected state.
And a store file odin cannot read is its own status, `store_unreadable`, with a
`store: {path, role}` block naming the file and whether it is a rebuildable cache
or your desired state.

### The one gate a CI pipeline needs

A node odin did not act on does **not** make Apply exit nonzero. Read the
payload:

```bash
odin apply -o json | jq -e '.not_covered | length == 0'
```

`not_covered` is the union of `skipped` (a node type odin has no model for: an
unbuilt kind, or a typo) and `unsupported` (a resource odin models but declined to
generate, with the reason); both are also published separately. With a `kinesis`
node on the canvas, apply exits `0` and reports
`{"not_covered":["kinesis"],"skipped":["kinesis"],"unsupported":[]}`, and that
`jq -e` exits 1. A broken `${{...}}` reference is deliberately *not* in there: it
is a mistake in what you wrote rather than a gap in what odin covers, and it is
refused before anything runs, naming the reference and the variable that would
have gone unset. So a green `not_covered` means "odin can build everything you
drew", not "everything you drew is correct".

**Apply also refuses to destroy something still on the canvas.** Uncovered means
"not in this apply", which for something that already exists means "deleted":
`count: "2"` mistyped as `"two"` on a live ECS node, or `type: "s3"` grown a
trailing space, would otherwise destroy the service or the bucket while reporting
`applied`. Such an apply exits nonzero, names every affected node and what about
it isn't covered, and changes nothing. Deleting a node from the canvas is
unaffected — that is the teardown story, and an empty canvas is still a full
destroy. `?allow_destroying_uncovered=true` on `/apply-full`, `/apply` or
`/tf/apply` overrides the guard.

### Drift, and why not to run tofu by hand

**Running `tofu` yourself inside `.odin/<env>/tf` talks to REAL AWS.** The
`main.tf` odin generates there is portable, real-AWS Terraform on purpose: no
`endpoints` block, no `127.0.0.1`, no credentials in the file. Odin injects the
endpoint (its own gateway) and this env's operator credentials at run time. A
hand-run `tofu plan` has none of that, so it goes to Amazon, and with real
credentials in your environment it plans against your real account. A field
engineer did exactly this and got a genuine `UnrecognizedClientException` back
from AWS. Every workspace now carries a `README.md` saying so.

`odin tf plan` is the safe path: same workspace, same injected endpoint, same
credentials as Apply, and it changes nothing. Two things its exit code cannot
carry. First, **it plans the last-applied Stack, not the saved canvas** — an edit
you have not applied is not drift, so `tf plan` reports `no changes` with four new
resources sitting in that env's `canvas.json`. It notes on stderr when the two differ and sets
**`.canvas_drift`** in `-o json` — that field, not the exit code, is what a CI check
should gate on, and only Apply closes the gap. Second, `no_changes` means "no drift in what odin can
generate": a node odin has no Terraform for was never in the plan, and the command
names those on their own line (`-o json` puts them in `.not_covered`).

### When the reconciler stops

Every phase in `/world`, in `odin world` and on the canvas is written by one
per-env reconciler loop, so a stopped loop would leave the last snapshot sitting
there looking converged. Instead: `GET /world` carries a `reconciler` block and
prefixes **every resource's verdict with `[STALE: …]`**, so a script walking
`resources` cannot read a frozen `healthy` as a live one; `GET /health` reports
the same per env under `reconcilers`; `odin status` exits 1 with
`RECONCILER DOWN: …`; `odin world` prints it on stderr; and it lands in the server
log, the UI's Logs tab and `odin events`. A gone task is reported instantly, a
hung or continuously-raising one after `poll_interval + 30s` with no completed
tick. Odin does not restart the loop for you, since a dead loop is an odin bug and
a silent restart would hide it in exactly these surfaces; the remedy is
`odin stop && odin start`, which the verdict names.

## Backup and restore

`.odin/` is the only record that an env exists. Lose it and every container that
env owns is orphaned, and the next startup reaper run, seeing no envs, deletes
every odin VM.

```bash
odin export                                    # -> odin-default-export.tar.gz
odin export -o ~/backups/default.tgz --env staging

odin stop                                      # restore is a server-down operation
odin import odin-default-export.tar.gz
odin import odin-default-export.tar.gz --env scratch   # or alongside, under a new name
```

Both work directly on the filesystem, no server and no HTTP, because the failure
they exist for is the one where odin cannot start.

**In the archive:** the env's whole control plane — the Stack revision lineage and
`HEAD`, `world.json`, the env's issued gateway credentials, the gateway's synth
stores and Lambda zips, the tofu workspace including `terraform.tfstate`, and a
`manifest.json` recording odin's version, the env name and a timestamp. Left out
on purpose: `tf/.terraform/`, since `tofu init` rebuilds the provider cache from
the same `main.tf` and it is hundreds of megabytes.

**Not in the archive:** data. This is control-plane state — it records that a
bucket named `uploads` should exist, never the objects in it, and container volumes
are not backed up. One exception cuts the other way: CloudWatch log-group events
survive a round trip, because odin's log sink is a control-plane sidecar
(`.odin/<env>/gateway/logsctl.json`) rather than a volume. Importing boots nothing
either; it puts odin's model of the world back, and `odin start` plus one Apply
converges reality to it.

Both operations are destructive, so `import` refuses to overwrite an existing env
directory without `--force`, refuses to run while odin is up, and rejects any
archive containing an absolute path, a `..` traversal, or a link member. The env's own
`canvas.json` rides along but is restored only under `--with-canvas`, because a
restore should never silently replace the canvas you are drawing on — without
the flag the canvas you currently have is carried across the restore intact.

**How odin knows a server is up**, since a wrong answer here is expensive in both
directions: a running control app writes a pidfile and holds an exclusive lock on
`.odin/lock` for its whole life, and `odin status`/`stop`/`import` ask exactly
those two questions — is that pid alive, and who holds that lock. Nothing parses
`ps` output or matches command lines. A script with
`uvicorn odin.server:create_app` in its own argv (an ops wrapper that restores a
backup and *then* starts the app is exactly that) is not a server, and odin will
not tell you to kill it. The lock dies with the process, `kill -9` included, so a
crashed odin never leaves a restore blocked. Two consequences:

- A server still shutting down still holds the store, so `odin import` **waits**
  up to 20 seconds for it to let go rather than refusing on your timing.
  `odin stop && odin import backup.tgz` in one script just works, no `sleep`.
- If odin ever refuses a restore you are sure is safe,
  `odin import --ignore-live-server` skips the check outright. It is named in the
  refusal message too. A restore is the worst possible moment to be stuck behind
  a guard.

The archive holds the env's credentials in cleartext. It is written `0600` with
every member stored `0600`, so a restore can only tighten a store's modes.
Copying it elsewhere will not preserve that, so treat it like a private key.

