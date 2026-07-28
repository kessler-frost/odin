# Known limits

Every one of these is a thing odin does not do, or does differently from AWS.
They are listed because finding one by surprise is worse than reading it here.


- **A Lambda's CODE needs the whole directory, not just the HCL.** A function's
  body lives in a zip beside `main.tf`, so `odin translate import <dir>` recovers
  it and reading HCL text alone cannot — in that case the node comes back with
  odin's default placeholder payload, and the import says so rather than letting
  it pass for your function. A multi-file package comes back as the node's
  `files` map (every member, so re-applying it regenerates a byte-identical
  archive); a member that is not text — a vendored `.so`, a compiled asset —
  cannot live on a canvas, so it is named on its own line and left behind.
  `--live` is narrower than either —
  `s3`, `sqs`, `sns`, `dynamodb`, `rds`, `vpc`, `subnet` only — and a
  live-imported RDS arrives with odin's default password, because no AWS API
  returns a master password.
- **A security group's OUTBOUND rules are authored but not ENFORCED.** Since
  v0.8.14 the sg node has an `egressRules` field, in the same
  `protocol:port:destination` form as `ingressRules`, and it emits real `egress`
  blocks that reach the gateway — so a restricted egress survives generation and
  `tofu plan`. What still does not gate it is the mesh: `fabric/nebula.py::
  _compiled_firewall` compiles the group's INGRESS rules only, and every Nebula
  config odin writes carries `outbound: any`. Treat an egress rule as portable
  configuration, not as a control. An empty field still emits AWS's own
  allow-all egress, which is what every canvas drawn before the field existed
  gets and why their generated file is byte-identical to what it was.
- **A security group is IPv4 only.** An IPv6 CIDR in either rule field is
  declined with that reason, and the reason is real rather than a parser
  limitation: `sg_rules_to_firewall` compiles `IpRanges` and `UserIdGroupPairs`
  and nothing else, so an IPv6 rule would be carried by Terraform, stored by the
  gateway, visible in `tofu plan`, and enforced by nothing. Making it real is a
  Nebula change, not a canvas one. One bad line still declines the whole group,
  deliberately: silently dropping one rule from a firewall is worse than
  refusing the group.
- **A security group's rules are a single port each.** `tcp:443:0.0.0.0/0`, not
  a range — so an imported ingress block with a port RANGE is reported and left
  out, and the regenerated group allows *less* than the source.
- **A workload's `${{producer.ATTR}}` references are carried as TAGS, and the
  resolved values still are not.** The distinction the design rests on: a
  reference names a producer and an attribute, while the string it resolves to
  at container launch carries the database password. Since v0.8.14 each
  reference travels as an `odin:ref:<VAR>` tag on the workload's own resource
  (value `<producer>.<attr>`), so the generated Terraform states the wiring and
  the `depends_on` odin re-derives from it. The values never appear — nor do a
  node's STATIC env entries, which a user may well have typed a credential into,
  so an `API_TOKEN = "..."` you set on the canvas is not in `main.tf` and is not
  in `terraform.tfstate`. It arrives at launch, from `gateway/wiring.py`, and
  nowhere else.
- **Whether an IMPORT recovers either of the two above is the import direction's
  half, and at the time of writing it does not.** The generator states them; the
  HCL reader has not been taught to read `odin:ref:` tags back into a node's
  `env`, nor `egress` blocks back into `egressRules`. Until it is, a round trip
  through `odin translate import` still drops both — for a different reason than
  before (the file now carries them), which is the only part this entry is
  claiming.
- **Some arguments are re-emitted with odin's own value whatever you wrote**, and
  each one warns on its own line — `imported with CHANGED argument(s) -- odin
  substitutes its own value` — kept separate from the `imported without unmodeled
  attribute(s)` line, which reports an argument that was *dropped*. The list: an
  ALB's `internal`, `load_balancer_type` and any subnet past the first; a target
  group's `protocol`, `target_type` and `name`; a listener's `protocol`; an
  ElastiCache cluster's `engine` and `num_cache_nodes` (odin's cache is always
  single-node Redis, so a three-node memcached cluster comes back as one Redis —
  it now says so); a DynamoDB table's `billing_mode`; a bucket's `force_destroy`;
  an RDS `skip_final_snapshot` and `allocated_storage`; a log group's
  `retention_in_days`; a secret's `recovery_window_in_days`; a subscription's
  `raw_message_delivery`; and **any resource name odin takes from the HCL block
  label instead of the `name` you wrote** — which is what happens whenever the
  real name is computed, so a project whose names come from variables gets
  renamed on import and is told.
- **What stays silent is the inverse: an argument you did not write.** AWS's
  default is the opposite of odin's for `force_destroy`, `billing_mode` and
  `skip_final_snapshot`, so omitting them changes meaning too — and odin invents a
  value where the source had none (an RDS `password`/`username`/`db_name`, a VPC or
  subnet CIDR, a cache `node_type`). Neither warns, because both would fire on
  every ordinary import. An `aws_iam_role`'s `assume_role_policy` is reported only
  as unmodeled, since odin's own output is a `jsonencode(...)` expression that
  cannot be compared back. Primary resources also gain an `odin:node` tag
  (companions such as a key pair or a generated execution role do not), so a
  byte-identical round trip is not a goal.
- **Lambda dependencies are VENDORED ONLY — odin never runs a package manager.**
  Since v0.8.14 a function can be a whole directory: set `sourceDir` on the node
  to a path on the machine running odin and the entire tree is packaged, so a
  handler can import its own modules. That is also the only way to get a
  dependency in: install it INTO that directory yourself
  (`pip install -t <dir> requests`, `npm install --prefix <dir>`), and it ships
  with the tree. Nothing is fetched at apply time, there is no `requirements.txt`
  / `package.json` step, and no build container — an apply is offline by
  construction and stays that way. Two consequences worth knowing before you hit
  them: a dependency with a COMPILED extension has to be built for the container,
  not for your Mac (the runtime is `public.ecr.aws/lambda/*`, so linux/x86_64 —
  `pip install --platform manylinux2014_x86_64 --only-binary=:all: -t <dir>`),
  and `__pycache__` / `*.pyc` / `.venv` are excluded from the archive on purpose,
  because a `.pyc` embeds its source's mtime and would change the package's hash
  on every translate. `sourceDir` overrides the Code textarea entirely.
- **Lambda has one version and no S3-deployed packages.** `$LATEST` only — no
  versions, no aliases, and no `s3_bucket`/`s3_key` on `aws_lambda_function`
  (the gateway's CreateFunction accepts an inline `Code.ZipFile` and refuses
  `S3Bucket`/`S3Key` by name rather than silently ignoring it).
- **odin declines a Lambda whose package does not contain its handler's module.**
  A package missing `lambda_function.py` deploys perfectly happily — the RIE
  container answers a TCP connect whether or not the module exists — and fails
  only when somebody invokes it. So the apply refuses at translate time and names
  the file it wanted. The cost is a false refusal for anyone whose handler
  resolves some way odin cannot see; the field it checks is the node's own
  `handler`, so renaming that is the way out. The package is also capped at
  AWS's own 250 MB unzipped quota, measured before anything is read.
- **ECS:** `network_configuration` (awsvpc/Fargate-style ENIs) is not modeled;
  tasks run `launch_type = "EC2"` with `network_mode = "bridge"`, which needs
  none. A task that dies **is** noticed — the task sweep runs every reconciler
  tick and records the container's real exit code — but nothing replaces it until
  the next mutating call or Apply. There is no background scheduler.
  A **failed image update keeps your old tasks serving**: odin honors
  `deployment_minimum_healthy_percent = 100` and launches replacements before
  retiring anything, so a typo'd tag costs zero downtime (measured: 3 tasks and 3
  HTTP 200s on every 2-second sample across a 62s failed apply) while the apply
  still exits nonzero. The node reads **`error`**, not `healthy`, naming how many
  tasks serve the previous revision and why the deployment failed, about four
  seconds in rather than after the apply returns.
  One trap, observed by hand rather than pinned by a test: removing a local image
  tag (`docker system prune`, a manual `docker rmi`) next to a live ECS service
  leaves it serving but un-appliable, since re-applying even the exact image those
  tasks are running can no longer resolve the tag, and each attempt burns the full
  60s deployment timeout. The verdict names the missing image.
- **An apply suspends the reconciler's actions but not its observation**, so
  `/world` and the badges keep updating throughout. That is not the whole tick:
  while an apply holds the loop, odin skips observing the S3/SQS/SNS/DynamoDB
  backings and the prune, and reads a cached drift result instead of sweeping. ECS
  stays genuinely live because its task sweep runs on every one of those ticks;
  EC2, Lambda and RDS drift can be up to one sweep cadence stale for the duration.
- **A drawn edge means something to odin only for six kind pairs out of 378.**
  Since v0.8.14 every ordered pair of node kinds resolves to exactly one edge
  type, and the honest majority answer is `unmodelled` — 338 of the 378 unordered
  pairs, drawn as a grey line labelled *Not modelled*, stored in the Stack and
  read by nothing. It was called `network` until now, which was a claim about
  layer 3 that odin never checked. The pairs that do mean something: `iam`
  (35 pairs, a real policy), `sg` (2, security-group membership), `role`
  (`iam_role ↔ lambda`), `target` (`alb ↔ ecs`) and `subscription`
  (`sns ↔ sqs`). Drawing anything else is decoration, and now says so.
- **A `role` edge works for lambda only.** `iam_role → lambda` folds into the
  lambda's `role` field, which is what `agent/hcl.py` already reads, so the edge
  really does decide the execution role in the generated Terraform. **ec2 and ecs
  reach a role differently** — an auto-generated role plus an instance profile /
  `task_role_arn`, with no `role` field anywhere — so odin does *not* offer a
  role edge to them: `iam_role ↔ ec2` and `iam_role ↔ ecs` stay `unmodelled`, and
  the label on the canvas is the report. Honouring them needs `hcl.py` to accept
  a drawn role in place of the auto-role for those two kinds; until it does,
  set the role on the node, not on a line.
  Two *different* role edges drawn to one lambda is a contradiction odin cannot
  resolve: the alphabetically lowest role name wins, deterministically, so the
  generated file never depends on edge ordering. Nothing reports the conflict.
- **`edge.kind` decides nothing in any builder.** The subscription and ALB passes
  in `agent/hcl.py`, and `reconcile/reconciler.py::_desired_subs`, all match on
  the two NODE kinds and never read the edge's kind — so an `iam`-typed line
  between an SNS node and an SQS node still emits a real
  `aws_sns_topic_subscription`. `odin chat` now refuses an edge kind odin does
  not model, but that closes the smaller half: kind-blindness survives it.
  This is deliberate, not an oversight. Every canvas saved before edge types were
  named carries `network` on those edges; a builder that started *requiring* the
  new name without a migration in the same commit would drop the subscription
  from the generated HCL for all of them, and `tofu` would **destroy the live
  subscription** on the next apply — with the reconciler silent, because
  `_desired_subs` only ever adds a missing subscription and never unsubscribes.
- **SNS→SQS subscriptions** are all generated with `raw_message_delivery = true`,
  so the queue gets the published body verbatim rather than SNS's JSON envelope,
  and that holds on an import round trip even if your `.tf` said otherwise. It
  keeps `tofu apply` and Apply delivering identically, but it changes what a
  consumer reads.
- **`odin env rm` refuses when another environment's name ends with this one's.**
  Its last check before deleting anything is "does this machine still have a
  container of this env's", and it answers that from odin's container *naming*
  (`odin-aws-rustfs-<env>`, `odin-rds-<env>-…`) rather than a label, so a
  `-`-suffix collision reads across. Measured: with `a` and `b-a` both live,
  removing `a` sees `odin-aws-rustfs-b-a` and stops, having deleted nothing —
  `odin env rm b-a` first, or rename. It errs this way on purpose: refusing a
  legitimate removal is recoverable, and deleting the last record of a running
  container is not. Nothing else about the two environments is affected; the
  matching rule is the same one a failed `odin destroy` uses to report what
  survived.
- **An RDS instance's data is a Docker volume and nothing else — there are no
  snapshots and no backups.** This entry used to say the opposite of its first
  half: the container held its data on the image's *anonymous* volume, which
  `docker rm -f -v` deleted with it, so odin's own repair handed back an empty
  database. Since v0.8.14 each instance gets a **named** volume
  (`odin-rds-<env>-<node>-data`) that outlives its container, and a repair is
  non-destructive — measured end to end in `tests/simulate/test_rds_tf_e2e.py`:
  rows `[42, 43]` written over the published `DATABASE_URL`, `docker kill`, one
  Apply, the same `[42, 43]` read back. The Apply still discloses the repair
  (`recovered_resources`, and a `note` naming the resource), and it checks that
  volume before claiming *its data survived* — remove the volume by hand and the
  same Apply says *its data did not survive* instead.
  What remains, and what the fix traded for:
  - `odin destroy` deletes the volume with the instance, by design, and there is
    no snapshot to restore from. `odin export` carries control-plane state, not
    container volumes, so a restored env's database comes back empty.
  - A repair now REUSES the old data directory, so a genuinely corrupt one is no
    longer papered over by a fresh container: Postgres refuses to start, the
    record goes `failed` with the real reason and the Apply reports
    `applied_resources_unhealthy`. That is the honest outcome, but it does mean
    `docker volume rm odin-rds-<env>-<node>-data` is the manual step for "give me
    a blank database back".
  - Nothing sweeps volumes on a schedule, so a `.odin` store deleted while
    containers are still up orphans them. `docker volume ls --filter
    label=odin=1` lists every volume odin made.
- **An EC2 VM gets 300s to boot** (`limactl start --timeout`), and that ceiling is
  real: the two-VM mesh e2e finishes in 74.6s on an idle Mac, but at the tail of a
  57-minute test run a VM reached the hypervisor's `running` state in one second
  and never signalled a running guest before the clock ran out — the instance goes
  `terminated` and the Apply fails with it. Raise `ODIN_BOOT_TIMEOUT` (seconds) on
  a slow or loaded machine. The default deliberately stays put: a longer one makes
  a genuinely hung boot take longer to report, and the two look identical until
  the timeout fires.
- **EventBridge is a control plane with no delivery.** `aws_cloudwatch_event_rule`,
  `aws_cloudwatch_event_target` and `aws_cloudwatch_event_bus` apply, refresh,
  tag and destroy for real, and the records survive a restart — so a rule you draw
  is really there. **Nothing runs its targets.** `PutEvents` therefore does not
  answer `FailedEntryCount: 0` the way real EventBridge does; it fails with a
  message naming the missing half, because an accepted event that is never
  delivered is a worse answer than a refused one. Invoke the target directly
  (`lambda:Invoke` for a lambda target) until the dispatcher lands — the design
  for it is `docs/event-dispatch-design.md`. Also unmodelled: event-pattern
  matching, archives and replays, API destinations and connections, partner event
  sources, `PutPermission`, and rule/target pagination (every list returns one
  page and `Limit` truncates).
- **S3 bucket notifications do not work at all, and the failure is loud in one
  place and silent in another.** `aws_s3_bucket_notification` is forwarded to
  RustFS, which **rejects every notification ARN form with `InvalidArgument` and
  stores the configuration anyway** (measured against `rustfs/rustfs:latest`). So
  `tofu apply` fails, the next `plan` reads the config back through GET and
  reports no drift, and nothing ever fires. Do not draw an S3 trigger yet.
- **RDS** is Terraform-managed (`aws_db_instance` → a Postgres container)
  and Postgres-only: MySQL or MariaDB is declined with the reason.
  `allocated_storage` and `instance_class` round-trip faithfully but resize
  nothing, there are no snapshots, and a node's label must be a valid RDS
  identifier (lowercase, hyphen-separated).
- **Nebula** is live single-host. VPC and SG config compiles to Nebula
  network and firewall primitives, and every VPC-joined EC2 VM runs a
  `nebula` daemon carrying the compiled SG firewall. The per-environment
  lighthouse is fully unprivileged when it runs (no root, no sudo, no one-time
  setup) — and it runs **once the first member joins the mesh**, stopping when
  the last one leaves. A member is not only an EC2 VM: a backing joins too, so a
  VPC + RDS canvas with no EC2 node at all does run a lighthouse (measured). What
  a VPC *alone* gets is the network, the CA and an assigned lighthouse address,
  with `GET /mesh` reporting `"lighthouse_running": false` — the honest answer,
  since there is nothing to coordinate yet. Cross-Mac reachability lands with multi-Mac support.
- **Which endpoint fact your security groups actually govern.** A database
  publishes up to three. `DATABASE_URL` (for a container) and `DATABASE_URL_VM`
  (for an EC2 Lima VM) are the same raw published host port under two host
  aliases, and **security groups gate neither** — the documented residual gap,
  and it applies to VMs as much as to host processes. `DATABASE_URL_MESH` (the
  Nebula overlay address, present only when a VPC is drawn) is the only SG-gated
  path, and the only address that survives a database recreation, since the host
  port is ephemeral. So on a canvas with a VPC, point VM consumers at
  `${{db.DATABASE_URL_MESH}}`; `_VM` is kept for envs with no VPC and for
  existing canvases, not because it is the safe default. Same for `REDIS_URL_VM`:
  ElastiCache has no mesh fact, so a cache has no gated path at all. A mesh
  address is checked before publication and withheld if the overlay is down (the
  node then reports `crashed` with the reason), but that check is cached for 30
  seconds on success, so it means "reachable recently", not "reachable now".

