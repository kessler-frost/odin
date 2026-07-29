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
- **The IMPORT direction reads both of those back**, so the round trip closes:
  `odin translate import` turns `odin:ref:` tags into the node's `env` (for ecs
  and lambda) and `egress` blocks into `egressRules`, and a re-generated project
  carries the same wiring and the same `depends_on`. Two residues, both narrower
  than what they replace. **A project odin did not generate has no `odin:ref:`
  tags**, and for it nothing has changed: only the ordering is in the file, odin
  re-derives `depends_on` *from* the references it cannot recover, so neither
  survives — the import names the producers and says to re-add the references.
  And **a rule odin cannot express empties the field**, which for egress is the
  dangerous direction: an empty `egressRules` is exactly what selects the
  allow-all default, so a group whose only outbound rule is a port range or an
  IPv6 CIDR does not come back with no egress, it comes back with all of it. The
  import says which of the two happened rather than leaving you to find it.
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
- **A drawn edge carries a modelled TYPE for only 40 of the 378 kind pairs.**
  Since v0.8.14 every ordered pair of node kinds resolves to exactly one edge
  type, and the honest majority answer is `unmodelled` — 338 of the 378 unordered
  pairs, drawn as a grey line labelled *Not modelled*, stored in the Stack and
  read by nothing. It was called `network` until now, which was a claim about
  layer 3 that odin never checked. The pairs that do mean something: `iam`
  (35 pairs, a real policy), `sg` (2, security-group membership), `role`
  (`iam_role ↔ lambda`), `target` (`alb ↔ ecs`, and `alb ↔ ec2` since v0.8.15)
  and `subscription`
  (`sns ↔ sqs`). Drawing anything else is decoration, and now says so.
- **An edge's TYPE is not always the whole of what it does.** Three pairs
  carry a second meaning on top of the grant, added in v0.8.15 because a
  permission whose subject is not wired is the same decoration under a
  different colour: `logs ↔ lambda|ecs` decides which group the workload's
  output lands in, `ecr ↔ ecs` decides the service's image, and `alb ↔ ec2`
  registers a real load-balancer target. Their type is unchanged — the first
  two stay `iam` — because the passes that read them key on the two NODE
  kinds and never on `edge.kind`. Each is documented in its own entry below;
  none of them is inferable from the label the canvas draws.
- **A Log Group drawn as a workload's sink is created under the WORKLOAD's
  name, not the node's label.** odin's two log shippers write to a name derived
  from the workload and read no destination from anywhere:
  `lambdactl._ship_logs` → `/aws/lambda/{function}`, `ecsctl._ship_task_logs` →
  `/ecs/{service}`. Before v0.8.15, drawing a Log Group tile (default label
  `/odin/logs`) to a lambda `myfn` therefore produced **two** groups — the drawn
  one, which the policy granted `logs:PutLogEvents` on, and `/aws/lambda/myfn`,
  which collected every line. The drawn one stayed empty forever, and the only
  canvas that appeared to work was one whose label happened to coincide.
  It is fixed in the direction that needed no new signal: the emitted
  `aws_cloudwatch_log_group` takes the name the substrate already writes to, so
  the node you drew backs the group that receives. **Two costs, both real:**
  (a) code inside the workload that calls `PutLogEvents` on the *label*
  (`/odin/logs`) will now be denied and find no such group — the grant follows
  the group, deliberately, since granting on a name nothing creates is the same
  decorative-permission bug; use the destination name, which `odin logs --node
  <label>` and `/world` still resolve for you through the `odin:node` tag.
  (b) `odin import-tf` reads a group's label from its `name` argument
  (`agent/import_tf.py::_label` prefers the literal over the tag), so importing
  the generated file back labels the node `/aws/lambda/myfn`. The file then
  regenerates byte-identically and the edge and policy stay self-consistent —
  it is a visible label change, not a broken round trip.
  Drawing ONE Log Group as the sink for two workloads is declined
  (`unsupported`, naming both destinations): a group has one name and the two
  substrates write to two. An `ec2 ↔ logs` edge is untouched — nothing ships a
  VM's output into CloudWatch Logs, so that edge is a grant for the code inside
  the VM to call `PutLogEvents` itself, which works exactly as drawn.
- **An `ecr ↔ ecs` edge sets the service's image; an `ecr ↔ lambda` edge sets
  nothing.** Until v0.8.15 `_ecs_container_definitions` read the node's
  hand-typed `image` field and consulted no edge at all, so drawing a
  repository to a service granted `ecr:BatchGetImage` and left the service
  running whatever was typed (in practice `nginx:alpine`). The edge now emits
  `image = "${aws_ecr_repository.<n>.repository_url}:latest"` — a real
  Terraform interpolation, so tofu resolves it at apply time to the address
  `gateway/models/ecr.py` actually publishes (`127.0.0.1:{port}/{name}`, whose
  port is minted per env and cannot be typed in advance), and
  `compute/tasks.py` hands that straight to `docker run`. A **hand-typed
  `image` always wins**; an `imageTag` field overrides `latest` but has no UI
  control yet, so it is reachable only from hand-authored JSON, `odin canvas
  set` and `import-tf`. Two repositories drawn to one service is declined
  rather than guessed.
  For a lambda the edge authors nothing and says so in `wiring_errors` (never
  `unsupported` — the function is built and applied perfectly well): odin's
  Lambda substrate packages the node's code as a zip and runs it in an AWS RIE
  container, so `package_type = "Image"` is not modelled at all.
- **`ecr:GetAuthorizationToken` / `ecr:BatchGetImage` are grants that can never
  bite.** Neither has a gateway handler, and image-layer traffic never reaches
  the gateway in the first place: ECR's data plane is a real `registry:2`
  container that a docker client dials directly, and the gateway does not proxy
  the registry v2 protocol at all (`gateway/models/ecr.py`'s own docstring). So
  an ECR permission edge is enforced by nothing. Open; it is a catalog change
  in `ui/src/lib/iam.ts`, not a translator one.
- **An `alb ↔ ec2` edge registers a real target, and the UI has not caught up.**
  Since v0.8.15 the builder's `_ALB_TARGET_KINDS` is `("ecs", "ec2")` and the
  edge emits an `aws_lb_target_group_attachment` naming `aws_instance.<n>.id` —
  the form `elbv2ctl._target_host` resolves through the EC2-compute store to the
  VM's real address. **`ui/src/lib/iam.ts::albTargetTypes` is still
  `new Set(['ecs'])`**, so the canvas labels that line *Not modelled* while tofu
  registers a live target for it. Presentational only — the generated Terraform
  is correct either way — but the two disagree on `develop` today, and the fix
  is that one-line set.
- **A lambda cannot be a load-balancer target.** `alb ↔ lambda` is declined with
  the reason rather than silently ignored: odin's load-balancer substrate is a
  real nginx container whose upstreams are `host:port` (`compute/proxy.py`), and
  a lambda target needs an HTTP request translated into the RIE's invoke
  envelope and the response translated back. That is the identical shim an
  `apigateway → lambda` route needs, so it is deliberately built once, there,
  rather than twice.
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
- **EventBridge fires SCHEDULES only, and refuses everything else at the door.**
  A rule with a `rate(N minutes|hours|days)` expression and a **Lambda** target
  really runs — `reconcile/dispatch.py` checks every rule on the reconciler
  tick. Everything else this API can express is **refused rather than stored**,
  because a rule that applies, plans clean and never fires is worse than an
  error: `PutRule` declines an `EventPattern` (odin has no event bus, so nothing
  could ever match one) and any non-`rate` schedule including **`cron(...)`**
  (odin has no cron evaluator; firing at a time nobody asked for is worse than
  not firing), and `PutTargets` declines a non-Lambda target ARN — SQS, SNS, ECS
  and Step Functions targets are not delivered. A batch containing one
  undeliverable target fails whole rather than partially, so an apply cannot
  succeed with a target silently missing.
  `PutEvents` is still refused for a narrower reason that survived the
  dispatcher landing: its entries are routed to rules by event PATTERN, and
  there is no pattern matcher, so there is no rule an event could reach.
  Also unmodelled: archives and replays, API destinations and connections,
  partner event sources, `PutPermission`, and rule/target pagination.
- **How late a trigger can be: at most one poll interval.** Measured on this
  machine at the production wiring (`poll_interval=1.0s`, `ODIN_DISPATCH_TICKS`
  unset), 20 runs at randomised phase: **min 0.02s, median 0.54s, max 0.94s**
  between a rule becoming due and its target being invoked. The dispatcher runs
  on **every** tick, not on the drift sweep's 10-tick cadence, because a late
  sweep is a late report while a late dispatcher is a broken trigger. A rule's
  own minimum period is one minute (AWS's), so the shortest end-to-end wait
  after `PutTargets` is ~60s — measured at 60.5s against a real RIE container.
  Nothing in the test suite may shorten the cadence; a repo-wide ratchet
  (`tests/reconcile/test_dispatch_cadence.py`) fails the build if any file
  assigns `ODIN_DISPATCH_TICKS`.
- **Triggers do not fire during an apply.** Dispatch is suspended for the whole
  of `/apply-full`, exactly as the drift sweep is, and for a sharper reason: a
  Lambda redeploy removes the old RIE container before starting the new one, so
  invoking mid-`UpdateFunctionCode` would report a function unreachable that is
  merely being rebuilt. A rule that came due during the apply fires on the first
  tick after it, rather than losing its turn.
- **`sqs → lambda` works; the mapping is a real poller.**
  `aws_lambda_event_source_mapping` applies for real (the five
  `/2015-03-31/event-source-mappings` routes are modelled) and odin drains the
  queue on each tick, invoking the function with the same `Records` envelope
  real Lambda sends. Messages are deleted **only** when the invoke actually ran,
  so a function that is down leaves them for the queue's own visibility timeout
  to redeliver — SQS's redrive, unchanged. A handler that RAISES still counts as
  delivered (the failure is recorded as the node's verdict), because
  redelivering it forever would turn one bad message into an infinite invoke
  loop. There is no DLQ, no `maximum_retry_attempts`, no batching window, and no
  partial-batch response: `FunctionResponseTypes` round-trips but is not
  honoured. Only **SQS** sources are accepted — a Kinesis, DynamoDB-Streams, MSK
  or self-managed-Kafka `event_source_arn` is refused at create time rather than
  stored as a poller that could never run.
- **An S3 notification is retried five times, then dropped with a verdict.**
  A write that matches a bucket notification is enqueued and delivered on the
  next tick. If the function cannot run, the record is **kept** and retried on
  each following tick — at-least-once, which is what S3 notifications are — but
  only up to **5 attempts**, after which it is dropped and the node's verdict
  says `GIVING UP after 5 attempts` and names the object. That bound exists
  because there is no visibility timeout here to space retries out (that is
  SQS's, which is why the queue drain needs no counter): an unbounded retry
  would invoke a broken function once per tick forever, which looks like a busy
  machine rather than a fault. It stands in for the dead-letter queue odin does
  not have — the notification really is lost, and the verdict is the only record
  of it.
- **One tick delivers at most 10 pending notifications.** `aws s3 cp
  --recursive` over a few thousand objects enqueues a few thousand records in
  one burst; the drain is bounded so one upload cannot stall the reconciler for
  every other resource. Nothing is lost — the records are durable and the next
  pass is one tick away, so the steady drain rate is ~10/second at the
  production poll. A **slow handler** makes the pass itself exceed one tick, in
  which case ticks queue behind the reconciler's own lock rather than
  overlapping: odin falls behind, it does not double-run.
- **Only writes that go THROUGH the gateway fire a notification.** Delivery is
  synthesized from the gateway's own view of an object write, not forwarded from
  RustFS (which cannot hold the configuration at all). In practice that is every
  workload write, since `AWS_ENDPOINT_URL` is what they all get — but a write
  made directly against the RustFS container's published port fires nothing.
- **The ETag in a delivered event is computed, and it was checked.** odin never
  observes RustFS's response headers on the notification path, so a single-part
  PUT's ETag is computed as `md5(body)` — S3's own definition. Measured against
  the real thing: RustFS reported `7e7a063e…` and `md5(body)` gave
  `7e7a063e…`, identical. A multipart completion and every delete carry an
  empty ETag and size `0`, because neither is knowable there; treat `""` as
  "not reported", not as an error.
- **SQS long-polling through the gateway fails when the queue is empty.**
  Pre-existing and unrelated to triggers, but measured while building them: the
  gateway's forward client is a plain `httpx.AsyncClient()` whose default read
  timeout is 5s, so a `ReceiveMessage` with `WaitTimeSeconds >= 5` against an
  empty queue exceeds it, and the gateway answers **503 ServiceUnavailable**
  (a `ReadTimeout` is an `httpx.HTTPError`, which the gateway maps to
  "the backing isn't there"). Short-poll, or keep `WaitTimeSeconds` under 5.
  odin's own dispatcher short-polls (`WaitTimeSeconds=0`) and is unaffected.
- **S3 bucket notifications do not work at all, and the failure is loud in one
  place and silent in another.** `aws_s3_bucket_notification` is forwarded to
  RustFS, which **rejects every notification ARN form with `InvalidArgument` and
  stores the configuration anyway** (measured against `rustfs/rustfs:latest`). So
  `tofu apply` fails, the next `plan` reads the config back through GET and
  reports no drift, and nothing ever fires. Do not draw an S3 trigger yet.
- **An S3 removal notification OVER-FIRES for a key that never existed.**
  Applies once bucket notifications land (`gateway/models/s3notify.py` +
  the dispatcher). S3 deletes are idempotent: deleting a key that was never
  there SUCCEEDS. Measured against `rustfs/rustfs:latest` — deleting one real
  key and one that never existed returned HTTP 200 and reported **both** as
  `<Deleted>`, with zero `<Error>` entries; the single-object `DELETE` answers
  204 the same way regardless. So the response carries no signal that separates
  "removed something" from "removed nothing", and odin's hook runs after the
  forward, when the answer is already unrecoverable. odin therefore enqueues an
  `s3:ObjectRemoved:Delete` notification in both cases; **real AWS sends nothing
  when nothing was removed.** A handler that assumes the object existed (and,
  say, deletes a matching database row) will run for a key that never did.
  Over-firing is the milder direction — a trigger that never fires is worse —
  but it is a real divergence, so it is written here rather than discovered.
  Genuine per-object FAILURES are handled correctly: a key S3 reports under
  `<Error>` (AccessDenied, an object-lock retention) fires nothing.
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

