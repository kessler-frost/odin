# Known limits

Every one of these is a thing odin does not do, or does differently from AWS.
They are listed because finding one by surprise is worse than reading it here.


## If you take the generated Terraform to real AWS

Most of this file does NOT follow you there, and the split is worth knowing
before you read 60 entries trying to work it out.

`main.tf` is portable by construction: **no `endpoints` block, no `skip_*`
flags, no credentials**. Everything that redirects AWS calls at odin lives in a
runtime-generated `override.tf` and environment variables, never in the file
you would commit. It is `tofu fmt`-canonical and passes a real `tofu validate`.

**What evaporates on real AWS — most of this document.** Every limit that is
about how odin FULFILLS a call locally stops existing the moment AWS fulfills it
instead: RustFS rejecting every notification ARN, an EBS attach rebooting the
instance because Lima has no hot-attach verb, `device_name` being advisory
(`/dev/sdf` in, `/dev/vdb` out), the mesh covering VMs and RDS but not
containers, odin's process sitting in the data path for an ECS route. None of
that is in the HCL. (An S3 removal over-firing for a key that never existed was
in this list until v0.8.21, when it stopped happening locally too — see the
entry below for what the pre-forward probe costs.)

**What FOLLOWS you — the emitted HCL is a real but NARROW subset.** These are
choices baked into the generated file, so they are what you would actually get:

- **Every ALB is `internal = true`.** Deliberate: odin has no internet gateway,
  so an internet-facing scheme would be a claim nothing backs. On real AWS you
  get an internal-only load balancer, and only `application` type — no NLB.
- **ECS is `launch_type = "EC2"` with `requires_compatibilities = ["EC2"]` and
  no `network_configuration`.** So no Fargate, and no awsvpc-style ENIs.
- **No `aws_internet_gateway`.** A VPC with no route to the internet.
- **API Gateway: the `$default` stage only, payload format 2.0 only, and no
  authorizers** — every route is `authorization_type = "NONE"`, i.e. public.
- **Every SNS→SQS subscription is `raw_message_delivery = true`.**
- **A security group is IPv4-only**, and its rule ports must be literals.
- **Some arguments are re-emitted with odin's own value whatever you wrote** —
  those are enumerated further down, and they apply on real AWS too.

So: odin generates real, valid, portable AWS Terraform **of the subset it
models**. Nothing in it is odin-specific and nothing would break an apply. What
you would hit is not breakage but SCOPE — an internal ALB where you wanted
internet-facing, EC2 launch type where you wanted Fargate, no IGW.



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
- **A security group's OUTBOUND rules are enforced ON THE MESH, and the mesh is
  narrower than the canvas looks.** This entry read "authored but not ENFORCED"
  until v0.8.17, and that was the whole of it: `sg_rules_to_firewall` ended with
  a hardcoded `outbound=[any/any]` and `ec2net._compiled_firewall` filtered the
  egress rules out of the store before they could reach it, so a restricted
  egress survived generation, `tofu plan` and the gateway's own record — and
  gated nothing. Nebula had supported outbound rules the whole time; odin never
  compiled any.
  MEASURED (`tests/simulate/test_sg_egress_gates_e2e.py`, 2026-07-30): a member
  whose group's only egress rule is `tcp:6000` gets `pong6000` from a peer's
  tcp:6000 and **empty** from that same peer's tcp:5432, same instant, same
  overlay. Falsified rather than assumed — reverting the compiler makes the
  same probe return `pong5432`.
  MEASURED ON A REAL EC2 LIMA VM TOO (`tests/simulate/
  test_sg_egress_gates_vm_e2e.py`, 2026-07-31), which is a separate claim and
  was for one release an INFERENCE: that test's container proof said in its own
  release note "no real Lima VM ever wore a restricted egress … that is an
  inference, not a measurement". A VM is a mesh member by a different route
  entirely (`compute/instances.py` writes `/etc/nebula/config.yml` with
  `limactl shell … sudo tee` and runs nebula under systemd as root; a container
  gets a bind-mounted config and runs it as pid 1 in the backing's network
  namespace), so "same bytes" was never the same as "same effect". One canvas,
  two real VMs, IDENTICAL ingress on both groups so `egressRules` is the only
  field that differs:

  | from → to | `locked` (egress `tcp:6000`) | `peer` (egress allow-all) |
  |---|---|---|
  | → `:6000` | `HTTP/1.0 200 OK` | `HTTP/1.0 200 OK` |
  | → `:5432` | **`REFUSED timed out`** | `HTTP/1.0 200 OK` |

  The bottom-right cell is the control a single VM cannot give: it rules out
  "no VM can reach that port on this overlay", which is the VM-specific
  confound a container-level proof hides. Falsified the same way — with
  `sg_rules_to_firewall`'s outbound compilation reverted to the pre-v0.8.17
  hardcoded allow-all, the compiled rule became `[('any','any')]` and the same
  probe returned `HTTP/1.0 200 OK`, failing on that one line and no other.
  **What it does NOT gate, and this is the part to read before trusting it:**
  Nebula gates OVERLAY traffic. A container or VM reaching the internet, or the
  host, or another container's published port through Colima/Lima NAT is not on
  the overlay, and no egress rule touches it — an `egressRules` line cannot stop
  a workload calling out to the network at large. That used to be reasoning;
  it is now MEASURED on the same VM in the same run, seconds after its overlay
  packet to `:5432` was dropped: a TCP connection to the **host** at
  `192.168.64.1` over vzNAT returned `CONNECTED`, and `curl https://example.com`
  returned **HTTP 200** over slirp. A locked-down egress rule stops nothing
  leaving the box by either path. Nor is every kind on the mesh:
  only **EC2 Lima VMs** and **RDS Postgres containers** (plus the other backings
  via `fabric/sidecar.py`) join it today. **ECS tasks, the ALB nginx proxy,
  Lambda RIE containers and ElastiCache are NOT mesh members**, so their egress
  is ungated regardless of what group they are drawn into — the asymmetry is
  real and is not narrowed by this change. The same boundary the ingress side
  has always had (see *Which endpoint fact your security groups actually
  govern*) applies unchanged: the raw published host port is reachable from the
  host and SGs gate neither direction on it.
  Three things that follow, all measured rather than reasoned:
  - **An empty `egressRules` field still means allow-all.** AWS's own default:
    the gateway seeds every group with the allow-all egress rule and `hcl.py`
    emits that identical block for an empty field, so a canvas that never
    mentioned egress admits exactly the packets it always did.
  - **A group whose egress was revoked and never re-authorized blocks
    everything outbound**, which is AWS's behaviour too. `outbound: []` is a
    real deny to nebula, not an absent ruleset that defaults open — checked
    against nebula 1.10.3 directly, because a firewall that read an empty list
    as "no restriction" would have made this whole feature a silent no-op.
  - **The firewall is STATEFUL, so restricting a database's egress does not
    break replies.** A member with `outbound: []` still answers a connection its
    inbound rules admitted (nebula keeps a conntrack entry per flow), which is
    what an AWS security group does. Egress restricts what a member *initiates*,
    never what it may answer.
  - **A restricted egress drops the member's OWN re-handshake poke, and that is
    a real consequence nothing had asked about.** `fabric/nebula.py::
    rehandshake_script` closes a measured 10–60s dead window after a nebula
    restart by having the restarted member ping every peer — and ICMP is a
    protocol like any other to the outbound firewall. MEASURED on the VM test
    above, with both groups admitting `icmp` INBOUND identically so the result
    is attributable to the sender: `locked → ping peer` **fails** (rc=1) while
    `peer → ping locked` succeeds (rc=0). So a member whose `egressRules` omit
    `icmp` buys no tunnel state from `_converge`, and its first real connection
    after a restart pays the window instead. It fails CLOSED (a slower
    re-handshake, never a wider firewall), and the fix if it ever matters is to
    name `icmp` in the group's egress — but do not read `_converge` as
    unconditional any more.
  - **A conntrack entry can make a working outbound firewall look broken —
    watch the ORDER of your probes.** Found the hard way while measuring the
    line above: run `peer → ping locked` FIRST and `locked → ping peer` then
    PASSES, because admitting the inbound ping opened an ICMP conntrack entry
    for that pair which the locked member's own ping rides as an established
    flow. Measured both ways on the same pair of VMs. Anything probing an
    outbound rule must do it on a flow with no state in the other direction.
  One upgrade cost, small but real: the outbound rule's SHAPE changed even where
  its meaning did not. It used to render as `host: any` (a hardcoded constant
  describing no group at all) and now renders as the seeded rule's true
  compilation, `cidr: 0.0.0.0/0`. Those admit the same packets on odin's
  IPv4-only overlay, but the config TEXT moves, so an environment already on the
  mesh takes one firewall-only reload (a SIGHUP; no tunnel is dropped). Byte-
  identical would have needed the wide-open rule special-cased back into
  `host: any`, making it the one rule odin compiles unlike every other.
- **A security group is IPv4 only.** An IPv6 CIDR in either rule field is
  declined with that reason, and the reason is real rather than a parser
  limitation: `sg_rules_to_firewall` compiles `IpRanges` and `UserIdGroupPairs`
  and nothing else, so an IPv6 rule would be carried by Terraform, stored by the
  gateway, visible in `tofu plan`, and enforced by nothing. Making it real is a
  Nebula change, not a canvas one. One bad line still declines the whole group,
  deliberately: silently dropping one rule from a firewall is worse than
  refusing the group.
- **A security group's rule port must be a literal number or range.**
  `tcp:443:0.0.0.0/0` or `tcp:8000-8100:0.0.0.0/0`, in either rule field. This
  entry used to read "a single port each … an imported ingress block with a port
  RANGE is reported and left out, and the regenerated group allows *less* than
  the source", and that was a correctness bug wearing a limit's clothes: a round
  trip through odin handed back a NARROWER firewall than the Terraform you gave
  it. Since v0.8.17 both bounds survive import → canvas → regenerate, and a
  single port is simply the degenerate range, so every canvas drawn before this
  emits byte-identical HCL. What is still left out is a port that is not a
  literal number — `from_port = var.port`, or any computed expression — which is
  named and counted like any other unimportable rule. A MALFORMED range
  (`8000-`, `8100-8000`, `8000 - 8100`) declines the whole group and names the
  offending line; it is never half-parsed into its low bound.
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
  allow-all default, so a group whose only outbound rule is an IPv6 CIDR (or a
  port that is not a literal number) does not come back with no egress, it comes
  back with all of it. The import says which of the two happened rather than
  leaving you to find it. *A port RANGE was on that list until v0.8.17 and is
  not any more — it round-trips with both bounds.*
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
- **A drawn edge carries a modelled TYPE for only 52 of the 378 kind pairs.**
  The honest majority answer is `unmodelled` — 326 of the 378 unordered pairs,
  drawn as a grey line labelled *Not modelled*, stored in the Stack and read by
  nothing. It was called `network` until v0.8.14, which was a claim about layer 3
  that odin never checked. Re-measured 2026-08-03 over the real 27 canvas kinds,
  the pairs that do mean something: `iam`
  (34 pairs, a real policy), `connection` **and** `iam` together (4),
  `sg` (2, security-group membership), `target` (4 — `alb ↔ ecs`, and since
  v0.8.15 `alb ↔ ec2`), `volume` (1 — `ebs ↔ ec2`, a real block device on a real VM),
  `mount` (2 — `efs ↔ ecs|lambda`, a real host directory bind-mounted into the
  real container behind each of those nodes; `efs ↔ ec2` is deliberately NOT
  among them, because odin's Lima VMs are created with `mounts: []` and a host
  directory is not visible inside one at all),
  `encryption` (2 — `kms ↔ secret|ssm`, the only two sidecars odin holds the
  plaintext of; `kms ↔ s3|rds|dynamodb` stays `unmodelled` ON PURPOSE, because
  those live in RustFS, Postgres and dynalite containers odin holds no key for
  and a line there would claim an encryption that does not happen),
  `role` (1 — `iam_role ↔ lambda`), `subscription` (1 — `sns ↔ sqs`)
  and, since v0.8.19, `dns` (1 — `route53 ↔ ec2`, a real
  `aws_route53_record`, and behind it real name resolution: an `--add-host`
  entry on every container in the env and an `/etc/hosts` line on every Lima
  VM in it). Drawing anything else is decoration, and now
  says so.
  (These five numbers are not written by hand: `ui/src/lib/edge-types.test.ts`
  recomputes them from the live registry and fails if this paragraph disagrees.
  This block carried TWO CONTRADICTORY versions of itself until v0.8.19 — 47/331
  and 42/336, spliced together by a merge — and the ratchet passed anyway,
  because `toContain` only needs the correct variant to be present somewhere.
  If it doubles again, delete the stale half rather than adding a third.)
  Three more pairs carry a SECOND meaning on top of the grant, added in
  v0.8.15 because a permission whose subject is not wired is the same
  decoration under a colour: `logs ↔ lambda|ecs` decides which group the
  workload's output lands in, and `ecr ↔ ecs` decides the service's image. Both
  are described in their own entries below. The edge *type* is unchanged for
  all three — they stay `iam` — because the passes that read them key on the
  two NODE kinds. `connection` is the one that does get its own type, because
  unlike those it is a meaning a user has to CHOOSE (see the next entry).
- **A `dns` edge to an alb or an rds is REFUSED BY NAME on Apply**, rather than
  silently dropped. A hosts entry is `<ip> <name>`: it carries no port and no
  scheme. Measured 2026-08-02 against the real projectors in
  `reconcile/tf_status.py`, an alb publishes `http://127.0.0.1:<dynamic port>`
  (dynamic so two load balancers, or two envs, cannot collide on 80) and an rds
  publishes `host.docker.internal:<port>`, so a name pointing at either would
  resolve and then fail to connect — a green resource that does not work. Only
  `ec2` publishes a bare address (`PRIVATE_IP`), so `route53 ↔ ec2` is the whole
  of the edge; every other target stays `unmodelled` on the canvas and
  `iac/hcl.py::_dns_target_unsupported` names the target and the reason in the
  apply's `unsupported` list.
  The emitted TTL (60s) exists so the generated project stays portable to Amazon;
  odin's own substrate is a hosts FILE and has no TTL at all, so a changed
  record lands on the next container launch or hosts push.
- **A withdrawn record stops resolving WITHIN THE TTL, not instantly —
  measured at ~2.2 seconds against a published 60s.** Removing a record empties
  the guest's odin block immediately (`grep -c` for the name → 0 the moment
  `push_hosts` returns), and `getent hosts` inside that VM keeps answering the
  old address for about 2.2s more: `systemd-resolved` is active on the stock
  Lima image (`nsswitch: hosts: files dns`) and still holds what the file used
  to say. Measured 2026-08-03 on a real Lima VM.

  **That is inside the contract odin publishes, not a defect against it.** Every
  record carries `ttl = 60` (`iac/hcl.py::_DNS_RECORD_TTL`), so a resolver is
  entitled to keep answering for up to a minute; real Route 53 defaults an A
  record to 300s. The 2.2s is ~27× tighter than odin's own TTL and ~136× tighter
  than AWS's default.
  `tests/test_compute/test_hosts_resolution_e2e.py::test_removing_the_record_
  stops_the_name_resolving` asserts both halves: the block is empty immediately,
  and the name stops resolving within a 15s budget — an order of magnitude above
  what was measured, well under the TTL, and still fatal to an append-only
  writer, which never converges at all.

  Worth recording because the first two attempts at this were both wrong.
  `resolvectl flush-caches` was added as a fix and is not one: it keeps the
  *after-boot* case immediate — which was already immediate — and changes
  nothing for a record cloud-init seeded at BOOT, the case that actually lagged.
  It was validated against the scenario that already passed. It is kept anyway,
  because the after-boot case is what an edited record really is. The second
  attempt was to make `push_hosts` poll `getent` before returning `pushed`; that
  is the known stronger fix and is **not built** — it changes `push_hosts`'s
  contract and its unit tests, and it is only worth doing if odin ever wants to
  promise something tighter than the TTL it emits.

- **odin serves NO DNS.** There is no resolver, no port 53, and nothing answers
  a DNS query. A hosted zone and its records are stored, round-trip for `tofu`,
  and are real IAM targets — but the only thing that ever *resolves* a name is a
  hosts entry odin writes (`--add-host` on a container, `/etc/hosts` on a VM).
  So `dig`, `nslookup` and anything that talks the DNS protocol will not find an
  odin record; `getent hosts` and any ordinary client library will. Stated
  because "Route 53 works" and "names resolve" are the same sentence to most
  readers and only the second one is true here.
- **A `dns` record resolves to a DIFFERENT address depending on who is asking,
  and for a VM with no mesh it does not resolve at all.** This is the sharper
  half of the entry above and it was got wrong first time, so it is stated in
  full. The emitted `aws_route53_record` carries `aws_instance.<n>.private_ip` —
  the portable, AWS-shaped answer, and a pure function of the canvas, because
  making `main.tf` depend on runtime mesh state would break the round trip and
  show `tofu plan` drift with nothing changed. What odin writes into a hosts
  file is not always that value:

  | consumer | address written | why |
  |---|---|---|
  | container | the instance's `private_ip` | measured reachable |
  | VM | the instance's Nebula **overlay** address | `private_ip` is **100% packet loss** VM→VM |
  | VM, env with no mesh | **nothing is written** | there is no address that would work |

  Stock Lima `vz` NATs each VM into its OWN isolated address space: a raw ping
  between two VMs' vzNAT addresses is 100% loss *before nebula is involved*
  (`fabric/nebula.py`'s R5 note, confirmed live with two real VMs — it is why
  VM-to-VM traffic relays through the lighthouse at all). So handing a VM a
  `private_ip` would be a name that resolves and then hangs, which is this
  kind's own trap one layer down. The divergence between the emitted argument
  and the substrate is the same shape as EBS's advisory `device_name`.

  **The no-mesh case is REPORTED in World, not merely withheld**, and that
  correction is the point. The first design simply wrote no hosts line —
  modelled on `tf_status._ec2_facts` dropping `MESH_IP` when the lighthouse is
  down. That is the right INPUT and the wrong whole story: honesty rule 1 lists
  *"the mesh gate withheld facts that never reached World"* among the four
  guards that silently never fired, and a user drawing a record, watching the
- **Four kind pairs mean two things at once, and odin asks.** `rds` and
  `elasticache` against `ecs` and `lambda` — 8 ordered pairs — are simultaneously
  a `connection` (the workload's environment is wired to the endpoint) and an
  `iam` grant (the workload calls the service's control plane), because in AWS
  both readings are true at the same time. These are the only ambiguous pairs
  odin has, the config panel offers a **multi-select** on them rather than
  picking silently, and `ui/src/lib/edge-ambiguity.test.ts` fails by name if a
  ninth appears. `data.edgeType` then stores a `+`-joined set
  (`"connection+iam"`), which `spec/translate.py::_edges` splits into one `Edge`
  per meaning; a single meaning has no separator and is stored exactly as it
  always was, which is why no saved canvas needed migrating.
- **A `connection` edge works for ecs and lambda only, and for rds and
  elasticache only.** It authors the ref a user would otherwise type by hand —
  `DATABASE_URL=${{db.DATABASE_URL}}`, `REDIS_URL=${{cache.REDIS_URL}}` — into the
  consumer node, where `gateway/wiring.py::node_env` resolves it and injects it
  into the real container at launch. **ec2 is deliberately excluded**: `node_env`
  has exactly two callers, `gateway/models/ecsctl.py` and
  `gateway/models/lambdactl.py`, and `gateway/models/ec2compute.py` never calls
  it, so a ref authored onto an ec2 node would reach nothing at all. `alb` and
  `ecr` publish wiring facts too (`ALB_ENDPOINT`, `REPOSITORY_URI`) and are
  excluded for the other reason: neither has one obvious variable name, and
  guessing one authors a field the app does not read. Those pairs stay
  IAM-only or `unmodelled`, the same rule the role edge above holds.
  A **hand-typed value wins**: `odin canvas set`, the README's JSON schema and
  the translation agent all write `env` directly, and an edge must not become a
  second source of truth beside a field. Where the two genuinely disagree —
  including two databases edged to one service, both wanting `DATABASE_URL` —
  the merge keeps the typed value deterministically and the disagreement is
  reported in `wiring_errors`, which **refuses the apply**: odin cannot tell
  which one you meant, so it changes nothing and names both.
  It only takes effect on an edge you drew or ticked as a `connection`. Every
  canvas saved before v0.8.15 types this pair `iam` and is completely unaffected
  — no new variable, no new `depends_on`, no conflict.
  The address it hands out is the plain published-port one; see *Which endpoint
  fact your security groups actually govern* below for why `DATABASE_URL_MESH`
  is the gated form and is still not what the edge writes.
  These counts are not prose: `ui/src/lib/edge-types.test.ts` recomputes them
  from the real registry and fails if this paragraph disagrees. They went stale
  within a day of being written — `alb ↔ ec2` moved one pair out of `unmodelled`
  and the numbers here still read 40/338 — which is the whole argument for
  measuring them from a test rather than trusting a careful writer.
- **An edge's TYPE is not always the whole of what it does.** Three pairs
  carry a second meaning on top of the grant, added in v0.8.15 because a
  permission whose subject is not wired is the same decoration under a
  different colour: `logs ↔ lambda|ecs` decides which group the workload's
  output lands in, `ecr ↔ ecs` decides the service's image, and `alb ↔ ec2`
  registers a real load-balancer target. Their type is unchanged — the first
  two stay `iam` — because the passes that read them key on the two NODE
  kinds and never on `edge.kind`. Each is documented in its own entry below;
  none of them is inferable from the label the canvas draws.
  `connection` is the counter-example, and the contrast is the point: it is the
  one second meaning a user has to CHOOSE, so it gets its own type, its own
  colour and a `+`-joined `edgeType` — see the two entries above.
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
  (`iac/import_tf.py::_label` prefers the literal over the tag), so importing
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
  **You must `docker push` before you apply the service**, and that ordering is
  the real constraint rather than a test artefact: odin never builds or pushes
  anything, so an apply that creates the repository and the service together
  asks ECS to run a tag that does not exist yet, and the service fails its
  converge timeout. Apply the repository, push, then apply the service.
  MEASURED end to end on 2026-07-29 (`tests/simulate/test_ecr_image_edge_e2e.py`),
  because one link in that chain had never been exercised: `ecr.py`'s docstring
  only ever claimed `127.0.0.1:{port}/{name}` works for a HOST-side `docker`
  CLI, and nothing had asked the *daemon inside Colima* to pull from it. It
  does — tofu resolved the attribute before RegisterTaskDefinition (`ecsctl`
  stored `127.0.0.1:32824/edgebld-app:latest`, not a literal `${...}`), and the
  task container came up running exactly that image after the local tag had
  been deleted, so the pull genuinely went to the registry.
  For a lambda the edge authors nothing and says so in `wiring_errors` (never
  `unsupported` — the function is built and applied perfectly well): odin's
  Lambda substrate packages the node's code as a zip and runs it in an AWS RIE
  container, so `package_type = "Image"` is not modelled at all.
- **An ECR grant covers the CONTROL plane only — nothing gates a `docker pull`.**
  This entry said "`ecr:GetAuthorizationToken` / `ecr:BatchGetImage` are grants
  that can never bite. Neither has a gateway handler", and was **half wrong in
  the direction that undersells odin**: `GetAuthorizationToken` IS one of
  `gateway/models/ecr.py::_HANDLERS`' seven entries, so the docker-login step
  really is classified and really is enforced. It also called the fix Open after
  it had landed.
  What is true is the image bytes: the data plane is a real `registry:2`
  container that a docker client dials on its own published port, the gateway
  does not proxy the registry v2 protocol at all (`ecr.py`'s own docstring), and
  that registry runs auth-less by design — so no IAM edge can stop anyone pulling
  the image. `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` and
  `ecr:BatchCheckLayerAvailability` have no handler and no request could reach
  them anyway.
  They are still TICKABLE, because the generated Terraform is meant to be
  portable and on real AWS these are exactly the verbs a pull needs — but they
  stopped being PRE-TICKED in v0.8.15, since a default is what odin ticks for you
  and must not assert a protection odin has not got. The distinction is kept
  honest by `tests/gateway/test_ecr_vocabulary_has_handlers.py`, which fails both
  if an offered op has no handler and no `PORTABLE_ONLY` declaration, and if a
  `PORTABLE_ONLY` op ever gains one.
- **An `alb ↔ ec2` edge registers a real target.** Since v0.8.15 the builder's
  `_ALB_TARGET_KINDS` is `("ecs", "ec2")` and the edge emits an
  `aws_lb_target_group_attachment` naming `aws_instance.<n>.id` — the form
  `elbv2ctl._target_host` resolves through the EC2-compute store to the VM's
  real address. The canvas half (`ui/src/lib/iam.ts::albTargetTypes`) and the
  builder half briefly disagreed, which would have labelled a live target
  *Not modelled*; they are now kept in step by
  `tests/spec/test_edge_registry_matches_builders.py`, which compares the two
  lists across the language boundary and fails naming whichever side is behind.
  **That resolution had never once run**, and the sentence above was true only
  of the docstring until v0.8.15: `_instance_address` read
  `private_ip_address`/`public_ip_address`, two keys `gateway/models/
  ec2compute.py` has never written (its record carries `private_ip`/
  `public_ip`). It returned `None` for every real instance, so the proxy was
  handed the bare `i-…` id — and its one test had fabricated a record with the
  key the reader wanted, which is honesty rule 1 living inside a test. Nothing
  on the canvas could produce an `i-…` target either, so the field never
  contradicted it. It is recorded here rather than quietly fixed because the
  same docstring is what a reader (and one of this repo's own briefs) already
  trusted once. MEASURED end to end on 2026-07-29,
  `tests/simulate/test_alb_ec2_target_e2e.py`: a real Lima VM at
  `192.168.64.2`, a real nginx proxy, `GET` the load balancer's published port
  → **200** with the VM's own bytes. Re-injecting the old keys fails that test
  on the rendered nginx config, which reads
  `server i-994e52be19f90b93a:80 max_fails=1 fail_timeout=30s;` — an upstream
  nginx cannot resolve.
- **A lambda cannot be a load-balancer target.** `alb ↔ lambda` is declined with
  the reason rather than silently ignored: odin's load-balancer substrate is a
  real nginx container whose upstreams are `host:port` (`compute/proxy.py`), and
  a lambda target needs an HTTP request translated into the RIE's invoke
  envelope and the response translated back. That is the identical shim an
  `apigateway → lambda` route needs, so it is deliberately built once, there,
  rather than twice.
- **A `role` edge works for lambda only.** `iam_role → lambda` folds into the
  lambda's `role` field, which is what `iac/hcl.py` already reads, so the edge
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
- **`edge.kind` decides nothing in any BUILDER.** The subscription and ALB passes
  in `iac/hcl.py`, and `reconcile/reconciler.py::_desired_subs`, all match on
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
  Three edge kinds ARE gated on the kind, and all three are gated in
  `spec/translate.py` rather than in a builder: `sg`, `role` and `connection`
  each fold into a field or a ref the builder already reads, so the builder
  still cannot tell how the value got there. That is the safe direction of the
  same rule. A gate that can **remove** something already being built is the
  hazard; a new meaning gated on a new kind only withholds a new feature from an
  old canvas, which destroys nothing and is what upgrading should mean.
- **There is no IAM database authentication, so `rds-db:connect` gates nothing.**
  `gateway/classify.py` builds every rds action as `rds:<Action>` out of the
  query protocol's `Action` param, so the `rds-db:` prefix is unreachable and a
  policy granting it could never match. Nothing in odin consults IAM when a
  workload opens a Postgres connection — the container takes the password out of
  `DATABASE_URL`. It stays TICKABLE for the same portability reason ECR's layer
  verbs do, and it stopped being the DEFAULT a drawn `rds` edge ticks in
  v0.8.15; the default is now `rds:DescribeDBInstances`, which is classified and
  enforced, and what a user drawing that line usually wants is the `connection`
  edge above.
- **SNS→SQS subscriptions** are all generated with `raw_message_delivery = true`,
  so the queue gets the published body verbatim rather than SNS's JSON envelope,
  and that holds on an import round trip even if your `.tf` said otherwise. It
  keeps `tofu apply` and Apply delivering identically, but it changes what a
  consumer reads.
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
  - **Nothing sweeps volumes on a schedule, and nothing will.** A reclaim on the
    reconciler tick is the one shape this must not take: a reconciler is per-env
    while Docker volumes are per-*machine*, so "no environment claims this volume"
    is a question no per-env loop can answer — a second odin with its own store
    root (every parallel agent worktree has one) owns environments the first has
    never heard of, and a tick sweeping on that reasoning would delete its
    databases. Nor is a live env's volume ever swept on "its node left the
    Stack": a database dragged off the canvas mid-edit still holds your rows.
    So reclaiming is always driven by an env **name you supplied**:
    - `odin env rm <env>` reclaims that env's volumes as part of its teardown
      (v0.8.15), scoped to the `odin.env` label docker filters on and nothing
      else — a *name* filter cannot be used, because `odin-rds-conn2-app-db-data`
      is env `conn2` database `app-db` **and** env `conn2-app` database `db`, and
      the string does not say which. It works even when the env is gone from
      everywhere else: no directory, no reconciler, absent from `odin envs`, which
      is exactly the shape of the four orphans measured on the development machine
      before this landed.
    - `odin volumes` lists every volume odin holds, which environment's label each
      one carries, and the one command that reclaims it. A volume odin could not
      remove is **named** with docker's own reason (`volume is in use - [<id>]`
      means a container is still attached), and `odin env rm` reports it as
      `remove_failed_volumes_standing` and exits 1 rather than deleting the env's
      state over the top of a leak.
  - **Residual: a volume created before v0.8.15 has no `odin.env` label, so
    `odin env rm` cannot reach it.** Four such volumes were found by hand on the
    development machine — `odin-rds-conn-{app,other}-db-data`,
    `odin-rds-conn2-{app,other}-db-data`, from two environments with no container,
    no `.odin/<env>/` and no entry in `odin envs`. That listing is the measurement;
    that they carry no `odin.env` is an *inference* from the label not existing
    before v0.8.15, not a re-probe (the machine's docker was busy with a release
    gate when this landed). `odin volumes` will show which it is: they appear with
    `env: null` and `docker volume rm <name>` as the manual step. Left manual on
    purpose rather than as a gap — attributing them would mean parsing the name,
    and the ambiguity above is exactly why nothing that *deletes* may do that. The
    set is closed: `create_volume` is the only thing that makes an `odin=1` volume
    and it now always writes the env label, so this can shrink and never grow.
  - **Residual: `odin volumes` judges "live" against the store root of the server
    you asked.** Two odin servers on one machine share docker's volumes but not
    their `.odin/`, so the other one's live environments are listed as orphaned.
    Nothing on that route deletes anything, which is why it is a reading to check
    rather than a hazard; `odin env rm <that env>` typed against the wrong server
    is the hazard, and its container witness is what catches the realistic case
    (a live database has a container, running or exited).
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
- **One tick spends at most 5 seconds STARTING notification deliveries, plus
  the one already in flight.** `aws s3 cp --recursive` over a few thousand
  objects enqueues a few thousand records in one burst, and the drain is
  bounded so one upload cannot stall the reconciler for every other resource.
  Nothing is lost — the records are durable and the next pass is one tick away.
  This entry read "at most 10 pending notifications" until v0.8.21, and that
  count was a bound on the wrong quantity: deliveries are serial,
  `FunctionRuntime.invoke` has a 30.0s ceiling and `lambdactl.invoke` passes no
  shorter one, so ten of them was **up to 300 seconds** during which the
  reconciler observed nothing, `/world` reported nothing new and the drift
  sweep did not run — every tick queues behind the reconciler's own lock. The
  entry admitted the hole in its own last sentence ("a slow handler makes the
  pass itself exceed one tick") without saying by how much. The bound is now a
  deadline, which is the unit the hazard is measured in: worst case **~35s**
  (the budget, plus one handler that cannot be preempted once started), and for
  a fast handler the pass moves as many records as fit rather than exactly ten.
  5 seconds is five production reconciler polls; it is not tuned to a measured
  invoke duration, because odin has none for a real RIE container and inventing
  one would put the arbitrary number straight back.
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
- **SQS long-polling works, and a wait above 20s is REFUSED rather than
  clamped.** This entry used to say long polling failed outright: the gateway's
  forward client is a plain `httpx.AsyncClient()` whose default read timeout is
  5s, so a `ReceiveMessage` with `WaitTimeSeconds >= 5` on an empty queue
  exceeded it and the gateway answered **503 ServiceUnavailable** — a healthy
  queue reported as a dead backing, for the recommended way to consume a queue.
  Fixed: the forward's read timeout is now **derived per request** (the client's
  own 5s plus `WaitTimeSeconds` × a measured backing-overshoot factor —
  `gateway/app.py::_long_poll` / `_forward_timeout`), so every *other* forwarded
  call still fails fast at 5s instead of inheriting a blanket larger number.
  A long poll holds a connection, never the event loop.
  - **goaws holds a poll ~1.5x LONGER than the wait it was given, and the first
    version of this fix did not survive that.** The numbers this entry used to
    quote — `WaitTimeSeconds=5` → 5.01s, `10` → 10.00s, `20` → 20.01s — were
    measured against a real socket standing in for goaws, which slept exactly
    the wait it was handed. Against the real container they are wrong. MEASURED
    2026-07-31 with a 300s client-side read timeout, so the number is goaws's
    own: `1` → held 1.38s/1.47s, `5` → 7.37s/7.78s, `20` → 29.83s–30.94s
    (1.47–1.57x), and the queue-attribute door overshoots identically (20 →
    31.39s). `loops := waitTimeSeconds * 10` with a 100ms timer per loop had
    been read off the pinned v0.5.4 source and then reasoned about as if a 100ms
    Go timer in a container costs 100ms; each iteration also rescans the queue.
    So a 20s poll got 25s of patience, took 30.5s, and came back as the same
    503 — the fix's own integration test (`test_sqs_long_poll_on_an_empty_queue_
    is_not_a_503`) was written at the time but never run, and it caught this the
    first time it executed. odin now allows `5 + wait × 2.5` (55s for the
    longest legal poll, inside botocore's 60s default), and the socket stand-in
    overshoots like the real backing so the gap fails a build.
  What remains, and it is deliberate:
  - **`WaitTimeSeconds` outside 0..20 gets `InvalidParameterValue` (HTTP 400).**
    Real SQS rejects it; **goaws v0.5.4 does not validate it at all** (read from
    `app/gosqs/receive_message.go`: `loops := waitTimeSeconds * 10`, one 100ms
    timer per loop — so `WaitTimeSeconds=3600` really would poll for an hour), so
    odin is the only place that can. Clamping was rejected: answering sooner than
    the caller asked is indistinguishable from an empty queue.
  - **A queue configured with `ReceiveMessageWaitTimeSeconds` above 20 still
    hits the old 503 on a wait-less receive.** goaws falls back to that queue
    attribute when the request's wait is 0, and odin accommodates it up to AWS's
    own maximum of 20s — beyond that it stops waiting and reports the backing
    unavailable. Real SQS refuses such a queue at creation and odin does not
    (`CreateQueue` is forwarded, attributes unvalidated), so this needs a queue
    real AWS would never have accepted.
  - **odin only knows the attribute for queues created THROUGH the gateway**
    (`synth._sqs_create_queue` stores it). A queue created by dialling the goaws
    container's published port directly is invisible to that lookup, and a
    wait-less receive against it long-polls in goaws while odin gives up at 5s.
  odin's own event dispatcher short-polls (`WaitTimeSeconds=0`) and is
  deliberately left that way — a 20s poll inside a reconciler tick would stall
  everything else the tick does (`reconcile/dispatch.py`).
- **S3 bucket notifications fire only for a LAMBDA target, and only for writes
  through the gateway.** They work as of v0.8.15, and the way they got there is
  the reason for the shape: `aws_s3_bucket_notification` used to be forwarded to
  RustFS, which **rejects every notification ARN form with `InvalidArgument` and
  stores the configuration anyway** (measured against `rustfs/rustfs:latest`) —
  so an apply failed, the next `plan` read the config back through GET and
  reported no drift, and nothing ever fired. Pass-through was therefore
  impossible, and odin answers `PutBucketNotificationConfiguration` itself; the
  request never reaches RustFS. Delivery is synthesized from traffic the gateway
  already sees, so a write sent straight at a backing container's published port
  fires nothing — in practice every workload gets `AWS_ENDPOINT_URL`, so every
  workload write goes through. A queue or topic target is refused at PUT with
  `InvalidArgument` rather than stored, because nothing would drain it.
- **An S3 removal notification costs one HEAD per key that would fire, and that
  is the price of not over-firing.** This entry used to say the opposite half:
  odin enqueued `s3:ObjectRemoved:Delete` for a key that never existed, where
  real AWS sends nothing. The response genuinely cannot tell — re-measured
  against `rustfs/rustfs:latest` on 2026-08-03, a single-object `DELETE` of a
  key that never was answers **204**, exactly as for one that did, and
  `DeleteObjects` reports **both** under `<Deleted>` with zero `<Error>`
  entries. So since v0.8.21 the question is asked BEFORE the forward, which is
  the last moment it has an answer: `gateway/app.py::_absent_keys` issues a
  signed HEAD per key and `s3notify._writes` drops the ones the backing 404'd.
  What that costs, measured the same day, loopback, otherwise-idle machine:
  one HEAD is **0.89 ms median** (p95 1.25) for a key that is present and
  **0.74 ms** (p95 0.98) for one that is not; RustFS answers a concurrent batch
  near-serially, so 100 keys is **97.5 ms** and 1000 keys is **1.51 s**. It is
  charged only to buckets that have a matching `ObjectRemoved` configuration,
  and only for the keys inside its prefix/suffix filter — a bucket with no
  notification issues no HEAD at all. Delivering those same 1000 notifications
  spans several reconciler passes (5 s of starting-work each, see the entry
  above), so the probe costs less than one pass's budget. Every failure of the
  probe (timeout, 403, unparseable
  request body) falls back to firing, so the old over-fire is the degraded
  mode rather than silence. Genuine per-object FAILURES are still handled
  separately: a key S3 reports under `<Error>` fires nothing.
  Not used, though it would be free: RustFS puts `x-amz-delete-marker: false`
  on a single-object delete that removed something and omits the header when it
  did not (20/20 both ways, plus a zero-byte object). Real AWS sends that header
  only for a versioned bucket, `backings.py` pins the image at `:latest`, and
  its failure direction is silence rather than noise — so odin reads 200-vs-404
  on HEAD, which every S3 implementation answers the same way.
- **RDS** is Terraform-managed (`aws_db_instance` → a Postgres container)
  and Postgres-only: MySQL or MariaDB is declined with the reason.
  `allocated_storage` and `instance_class` round-trip faithfully but resize
  nothing, there are no snapshots, and a node's label must be a valid RDS
  identifier (lowercase, hyphen-separated).
- **EBS: a real disk, but ATTACHING ONE REBOOTS THE INSTANCE, and the device
  name you write is not the one the guest sees.** An `ebs` node is a real
  `limactl disk` volume that really joins a real Lima VM; these are the three
  ways it is not AWS. All three were MEASURED against limactl 2.1.3 on macOS
  (vz driver, Ubuntu 24.04 guest) on 2026-08-02, before any of the code was
  written, and the probe output is quoted rather than paraphrased.

  1. **No hot-attach. `AttachVolume` and `DetachVolume` restart the VM.** AWS
     attaches a volume to a running instance as a routine online operation.
     Lima cannot: `limactl disk` has create/delete/ls/import/resize/unlock and
     **no attach verb at all**, attachment exists only as an instance's
     `additionalDisks:`, and editing that on a live instance is refused
     outright —

     ```
     $ limactl edit ebs-probe --set '.additionalDisks = [{"name":"ebs-probe-vol"}]'
     time="..." level=fatal msg="cannot edit a running instance"
     exit 1
     ```

     …while the guest's block devices were, before and after that attempt,
     byte-identical. So odin does the only thing that works: **stop, rewrite
     `additionalDisks`, start** — a genuine reboot of your instance, taking
     roughly as long as a boot. It is not hidden: the instance moves to
     `pending` (World phase `starting`) for the duration instead of continuing
     to claim `running`, because during the reboot it is not running. An
     `aws_volume_attachment` in a `tofu apply` therefore costs an instance
     restart, and a canvas that adds a volume to a live instance restarts it.
     odin does NOT refuse the operation, because refusing would make every
     `aws_volume_attachment` fail; it performs it and tells you what it cost.
     **Two volumes on one instance cost two restarts.** Terraform plans those
     attachments in parallel, so both calls arrive before either reboot
     finishes; both are accepted and the VM work is serialised per instance,
     the second rebooting with both disks. It has to be serialised rather
     than merely allowed: a `limactl edit` landing during a `limactl start`
     is refused outright. An instance is attachable while `running` or
     already mid-attach; `stopped`, `shutting-down` and `terminated` are
     refused by name.

  2. **`device_name` is ADVISORY. The guest calls it `/dev/vdb`.** The
     attachment odin emits says `/dev/sdf` because that is what the AWS
     provider expects to write, and nothing on this substrate honours it —
     Lima attaches disks as virtio devices in yaml order. What a guest with
     one 3GiB volume really reports:

     ```
     $ limactl shell ebs-probe2 lsblk
     NAME    MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS
     vda     253:0    0    10G  0 disk
     ├─vda1  253:1    0     9G  0 part /
     ├─vda15 253:15   0    99M  0 part /boot/efi
     └─vda16 259:0    0   923M  0 part /boot
     vdb     253:16   0     3G  0 disk
     └─vdb1  253:17   0     3G  0 part /mnt/lima-ebs-probe-vol2
     vdc     253:32   0 268.7M  1 disk /mnt/lima-cidata
     ```

     Three things in that output are worth reading twice. The size is exact
     (`3221225472` bytes for a 3GiB request). **Lima formats and mounts the
     disk for you** — it arrives partitioned `vdb1`, ext4, mounted at
     `/mnt/lima-<disk name>` — where AWS hands you a raw device to `mkfs`
     yourself, so a `user_data` script that formats `/dev/sdf` will not find
     it and does not need to. And the cloud-init `cidata` ISO **moved from
     `vdb` to `vdc`** the moment a volume existed: device letters here are
     positional, not a contract, so address the disk by its mount point and
     never by `/dev/vdX`.

     **How stable that ordering really is, stated precisely, because the
     first draft of this entry overstated it.** odin hands Lima the disks
     sorted by volume id, which makes the order DETERMINISTIC — the same
     canvas always produces the same list — but not insertion-stable: a new
     volume whose id sorts earlier takes an earlier slot and pushes the
     others' `/dev/vd*` letters along. What saves a workload from caring is
     that Lima mounts each disk at `/mnt/lima-<disk name>`, derived from the
     NAME and not the position, and that an attach reboots the VM anyway, so
     every device is re-enumerated and re-mounted by name at boot. So: mount
     points are stable, device letters are not, and the earlier sentence
     about disks never being "renumbered under a mounted filesystem" was
     true only in the sense that the filesystem is not mounted at the moment
     it happens.

     **The version of this that DOES cost you something is one layer up, in
     Terraform, and it is the sharpest edge on this feature.**
     `aws_volume_attachment.device_name` is **ForceNew** — measured against
     OpenTofu 1.12.3 with a real state file, changing it plans
     `~ device_name = "/dev/sdf" -> "/dev/sdg" # forces replacement`,
     `Plan: 1 to add, 0 to change, 1 to destroy`. And odin assigns those
     names positionally from `/dev/sdf`…`/dev/sdp` over the instance's
     volumes sorted by LABEL, so adding a volume whose label sorts earlier
     renumbers every later one. Measured through odin's own generator:

     ```
     before         : data=/dev/sdf   logs=/dev/sdg
     after +archive : archive=/dev/sdf  data=/dev/sdg  logs=/dev/sdh
     ```

     Both existing disks are therefore **detached and reattached** by a
     change that had nothing to do with them — and on this substrate each of
     those is a VM reboot. **Name volumes so that new ones sort last if you
     care.** This is not fixable inside the generator: `generate_tf` is a
     pure function of the canvas with no memory of the last apply and the
     device pool has 11 slots, so no rule can be insertion-stable; hashing
     labels into slots would only make the renumbering rarer and
     unpredictable instead of rare and explainable. The real fixes are a
     canvas field the tile does not have, or reading the live attachment
     back. Until one lands, it is written here rather than discovered during
     an apply, and pinned by
     `test_hcl_ebs.py::test_adding_an_earlier_sorting_volume_renumbers_the_others`.

  3. **A disk is reclaimed with the env, and refuses to vanish quietly.**
     `limactl disk delete` will not remove a disk an instance still holds —
     `fatal msg="cannot delete disk ebs-probe-vol2 in use by instance
     ebs-probe2"`, exit 1 — which is the guard working, so `/destroy` deletes
     the VMs first and the disks second, and `DeleteVolume` refuses anything
     not `available`. Both `/destroy` and `odin env rm` sweep the machine for
     `odin-ebs-<env>-*` and not merely the store, so a disk whose record was
     lost is still found; anything that will not go is named, and neither
     command reports success over it.

  What is NOT modelled: snapshots, `iops`/`throughput`/`encrypted` (reported
  as gp3/3000/false regardless of what you ask for), resize after creation,
  multi-attach, and `DeleteOnTermination` for a drawn volume — terminating an
  instance frees its volumes back to `available` rather than deleting them,
  which is AWS's own behaviour for a non-root volume.
- **EFS: a real SHARED DIRECTORY, mounted into containers only, at ONE path per
  file system — and there are no mount targets, no IAM authorization and no
  encryption anywhere in it.** An `efs` node is a real host directory
  (`.odin/<env>/gateway/efs/<fs-id>/`) bind-mounted into the real container
  behind every `ecs`/`lambda` node you edge it to, through the same
  `ContainerSpec.volumes` that already hands a Lambda its code directory.

  **VERIFIED END TO END, and falsified.** The acceptance test ran: task A wrote
  inside its container, the bytes were read ON THE HOST, and task B — a separate
  container, different task id asserted — read them back from inside its own.
  `2 passed, exit 0, in 3.41s`. Because that is fast enough to look like nothing
  ran, `efsctl.task_mounts` was mutated to `return {}`: `1 failed in 181.64s`,
  burning the full deadline and failing on the host-side half. So the test waits,
  watches the host, and is quick only because a working bind mount is instant.

  These are the ways it is not AWS, each measured before the code was written.

  1. **`efs → ec2` is NOT modelled, and this one is a refusal rather than a
     gap.** odin's EC2 nodes are real Lima VMs, and odin creates them with
     `"mounts": []` (`compute/lima_yaml.py`), so a host directory is not visible
     inside one *at all* — the mount would succeed and the directory would be
     empty. There is also no Terraform expression for it: an EFS mount on an
     instance is an fstab line in user-data, not an argument on
     `aws_instance`. So the pair stays `unmodelled` and the canvas draws it as a
     grey *Not modelled* line rather than a fuchsia one. **The same trap one
     level down, and it is why the file system lives under `.odin/`:** a `-v` of
     a path under macOS's per-user temp dir (`/private/var/folders/…`) mounts an
     EMPTY directory under Colima's virtiofs and errors on nothing, because the
     path does exist inside the VM. That burn is measured and on record. The
     inference drawn from it — that `.odin/` under `$HOME` therefore mounts
     NON-empty, `$HOME` being the tree Colima shares in — is **reasoned, not
     measured**: confirming it needs a real container reading a real file, which
     is the embargoed suite. It is why the file system lives under `.odin/`
     rather than a temp dir, and it is the assumption to check first if a mount
     ever comes up empty.

     **The case that exclusion does NOT cover, and it is the one a user is most
     likely to hit by accident:** excluding `ec2` from the mount kinds handles an
     `efs → ec2` EDGE. It does nothing about an ECS service PLACED inside an ec2
     box, which runs under `LimaRuntime` (`ecsctl.py::runtime_for_service`) — the
     same VM, the same empty `mounts:`. **odin REFUSES that combination rather
     than starting it.** `compute/tasks.py::TaskRuntime._refuse_unmountable`
     raises before any container starts, naming the instance you drew, the mount
     path, and `mounts: []` as the reason — so the message says why and a user
     can check it. `ecsctl.py::_launch_task` catches that and writes the task
     `last_status="STOPPED"` with the exception's text as `stopped_reason`,
     which becomes the apply's failure line and the node's World verdict rather
     than a green tile over an empty directory.

     The refusal is pinned in BOTH directions, so it can neither under- nor
     over-fire: deleting the `_placed_on` half of the condition fails
     `test_a_placed_task_that_mounts_efs_is_refused_and_never_started`, and
     deleting the `volumes` half fails `test_a_placed_task_with_no_mounts_still_runs`
     — placement alone is not the problem, the combination is. And
     `test_odins_lima_vms_really_share_no_host_directories` pins the PREMISE,
     asserting that `generate_lima_yaml` really does emit `mounts: []`, so if
     odin's VMs ever gain a mount that test fails and points at the guard
     instead of leaving a refusal that forbids something newly possible.

     **What remains unmeasured is narrow and specific:** the real-VM
     confirmation — that a placed task on a genuine Lima VM would indeed see an
     empty directory — because that needs the embargoed integration suite. The
     refusal itself is implemented and unit-tested.
  2. **One mount path for the whole file system, not one per consumer.** The
     tile has a single `path` field (default `/mnt/efs`), so every workload that
     mounts a given efs node mounts it at the same place — a canvas wanting
     `/mnt/shared` in one service and `/mnt/data` in another needs two efs
     nodes. AWS models it per consumer (`mountPoints[].containerPath` on ECS,
     `FileSystemConfig.LocalMountPath` on Lambda) and odin's generator writes
     the node's one value into both. **What AWS itself allows there is narrower
     than people expect**, and it is checked rather than assumed: botocore's
     own `LocalMountPath` shape is `pattern /mnt/[a-zA-Z0-9-_.]+`, `max 160` —
     exactly ONE segment under `/mnt`. `/mnt/efs` is legal, `/mnt/efs/data` is
     not, and a node whose `path` does not match is declined by name with the
     reason rather than emitted into a project real AWS would reject.
  3. **No mount targets, no security groups on the file system, no IAM
     authorization, no encryption.** `aws_efs_mount_target` is not modelled, and
     that is not an oversight about ordering: the substrate is a directory on
     the machine odin runs on, so there is no ENI to place in a subnet and
     nothing for a security group to gate. Reachability here is container
     configuration, not networking. Likewise the two encryption knobs AWS
     offers — `encrypted`/`kms_key_id` at rest and `transitEncryption` on the
     ECS volume — are neither emitted nor honoured, for the reason the kms work
     already recorded: emitting them would claim a property the substrate does
     not have. And `authorizationConfig.iam` cannot bite, because a bind mount
     is performed by the container runtime and odin's gateway never sees a
     signed request for it. That is exactly why the EFS tile offers no IAM
     actions at all, where every other data-plane tile does.

     **Four fields are accepted, stored and echoed back with NO substrate
     meaning** — they round-trip so a plan stays clean, and they change nothing:
     `PerformanceMode` (always `generalPurpose`), `ThroughputMode` (always
     `bursting`), `Encrypted` (always answers `False` — odin encrypts nothing
     here), and `PosixUser`, which is the one worth knowing: a `{Uid, Gid}` is
     stored and echoed but **NOT applied**, so files are created as whatever
     user the container runs as, not the uid/gid you asked for. Exactly one
     field is real rather than echoed: `SizeInBytes` is measured by walking the
     directory, so `du` agrees with it.
  4. **Mounts are read-write and whole-file-system, and odin REFUSES the two
     task-definition fields that say otherwise rather than mis-mounting.**
     `ContainerSpec.volumes` is a `dict[source → container_path]` and the one
     renderer both drivers share is literally
     `args += ["-v", f"{host}:{container}"]` (`runtime/colima.py`) — no `:ro`
     form, no sub-path form. So:
     - **`readOnly: true` is refused**, not downgraded to a writable mount.
       Measured before the refusal existed: the mount came up **writable with no
       word to the user**, which is a protection claimed and not delivered. The
       message names the volume and says what to do —
       *"…odin cannot enforce that: its container runtime renders a bind mount
       as `-v source:target` with no `:ro` form. Nothing was mounted, rather
       than mounting it WRITABLE and letting you believe the data was
       protected. Set `readOnly: false` if a writable share is acceptable."*
     - **A `rootDirectory` other than `/` is refused**, on both the ECS volume
       and the access point. Measured before the refusal: a volume scoped to
       `/data` silently received **the WHOLE file system — every other
       consumer's files**, which is the sharper of the two, since it is a
       confinement the author asked for and did not get.

     Neither can fire on an odin-generated project: `hcl.py` hardcodes
     `readOnly: false` and emits `path = "/"`. Both bite only a HAND-WRITTEN or
     IMPORTED task definition — which is exactly the case the importer can
     produce, so they are reachable in normal use.
  5. **A Lambda mounts at most ONE file system, and odin declines the second by
     name.** That is AWS's own limit, not odin's: `file_system_config` carries
     `max_items: 1` in the provider schema and `FileSystemConfigs` is `max: 1`
     in botocore's Lambda model. Drawing a second `efs → lambda` mount is
     refused with the reason rather than emitted as two blocks that fail
     `tofu validate` for the whole project.
  6. **odin's mount-path rule is STRICTER than ECS's, because one field serves
     both consumers.** `/mnt/efs` is validated against Lambda's
     `LocalMountPath` pattern (above) for every consumer, but ECS's
     `containerPath` has no such pattern at all — so `/data` is a mount ECS
     would happily accept and odin declines. A real limitation created by the
     tile having one `path` field rather than one per edge, and the honest
     trade for the tile staying legible.

  7. **Importing someone else's EFS drops `posix_user`, and re-roots the access
     point at `/`.** `aws_efs_access_point` folds ONTO the file-system node on
     import, and an access point has arguments a node has nowhere to keep:
     `posix_user` forces every file the mount creates to one uid/gid and odin
     re-emits nothing for it, so a project that had one loses it. Its
     `root_directory` is carried in the weaker sense that odin always re-emits
     `path = "/"`, so a source rooted at a subdirectory comes back as a CHANGED
     argument rather than silently — which is the honest half of the same
     limitation. **Read the DIRECTION of that change, because it is the half
     that matters here: re-rooting at `/` WIDENS the mount.** A source that
     confined a consumer to one subtree comes back with the whole file system
     visible to it, so the imported project grants more access than the one you
     handed in, never less. Pinned by
     `test_an_access_point_rooted_at_a_subdirectory_is_reported_as_CHANGED` and
     its ecs sibling.

  **A migration note with a real date on it, because an efs node CAN already be
  sitting in a canvas you saved.** The tile was added draggable on 2026-06-20
  (`ac796d6` — sublabel `Elastic file system`, no `(placeholder)` marker) and
  placeholders were not hidden from the palette until 2026-07-27 (`41d214b`),
  whose own commit message records that the hiding is palette-only: "CATALOG
  keeps every entry, so a canvas already containing a placeholder node still
  renders properly". So a canvas saved in those five weeks can hold an efs node
  whose edges are typed `network`, the pre-v0.8.14 catch-all. Those edges still
  compile, because the mount pass keys on the two NODE kinds and never on
  `edge.kind` — which is exactly why it must keep doing so. Gating it on the
  name would silently ignore every one of those edges and produce a file system
  mounted nowhere, with no error. Milder than the equivalent `ebs` mistake and
  worth saying so: the old node was a placeholder Apply skipped, so there is no
  live file system behind it for `tofu` to tear down.

  **odin models exactly the seven operations a real apply/plan/destroy calls,
  and answers everything else with a protocol-correct 400 naming the op** —
  `CreateMountTarget`, `DescribeMountTargets`, `PutBackupPolicy`, `TagResource`
  and `PutLifecycleConfiguration` all return `BadRequest`, never a 503 and never
  a plausible-looking empty success. Which seven was measured rather than
  guessed: against OpenTofu 1.12.3 + hashicorp/aws 6.57.1, with
  `AWS_ENDPOINT_URL` pointed at a recording endpoint, a real
  apply/plan/destroy over `aws_efs_file_system` + `aws_efs_access_point` never
  calls `DescribeMountTargets`, `DescribeBackupPolicy`,
  `DescribeFileSystemPolicy`, `ListTagsForResource` or `TagResource` at all. So
  they are unimplemented because implementing them would be dead code on this
  path, not because they were forgotten — and a caller that reaches for one is
  told so by name.

  **Teardown is swept from the DISK, not from the records**, the same rule the
  ebs entry states: `/destroy` walks `.odin/<env>/gateway/efs/` itself, so it
  also finds a directory whose record an interrupted apply lost, and `odin env
  rm` reclaims it with the state dir. A file system that will NOT go is never
  reported destroyed — the env is refused with the count, the id, the path and
  the real OS error, on the grounds that each one holds whatever the workloads
  that mounted it wrote.
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


<!-- apigateway (v0.8.19) -->
- **The closed world is method-independent NOW, and was not before v0.8.19.**
  odin's stated posture is that a request the gateway cannot map to an
  `(action, resource)` pair is denied, never guessed at
  (`gateway/classify.py`'s docstring says so twice). That property lived inside
  `catch_all`, and the route table in front of it listed five verbs — so a
  **PATCH or an OPTIONS never reached odin's code at all**. Starlette answered
  `405 Method Not Allowed`: no SigV4 verification, no policy evaluation, no
  `access_denied` event. Measured on the real app:

      PATCH http://127.0.0.1:62976/v2/apis/api123
      status: 405
      body  : Method Not Allowed

  It was latent rather than exploitable — no modeled service used PATCH — but
  nothing in the docs said the guarantee stopped at five verbs, and apigateway
  makes it live (`UpdateApi` is `PATCH /v2/apis/{apiId}`). Both verbs are routed
  now, and the property is pinned by
  `tests/gateway/test_closed_world_is_method_independent.py`, which asserts a
  denial for each of GET/PUT/POST/DELETE/PATCH/OPTIONS **and** fails if a verb is
  added to the router that it does not check. Mutation-tested both ways: deleting
  PATCH from the table fails it with `PATCH was refused by the ROUTER (405 Method
  Not Allowed), so odin's closed world never ran`, and deleting OPTIONS fails it
  the same way.

  **The remaining edge, stated rather than discovered later:** verbs outside that
  list (`TRACE`, `CONNECT`, `PROPFIND`, any extension method) are still answered
  405 by the router. No AWS API uses one, so nothing odin models can reach it —
  but if a future service does, the answer will be a 405 and not a denial, and
  the test above is where to add it.

<!-- apigateway (v0.8.19) -->
- **API Gateway: the reachable address is `api_endpoint`, NOT the stage's
  `invoke_url`.** MEASURED against real terraform-provider-aws 5.100.0 driving
  odin's own gateway, in one state file:

      aws_apigatewayv2_api.public_api   api_endpoint = "http://127.0.0.1:39999"
      aws_apigatewayv2_stage.default    invoke_url   = "https://api75a2c592.execute-api.us-east-1.amazonaws.com/"

  `api_endpoint` is odin's answer and points at the real nginx container.
  `invoke_url` is built CLIENT-SIDE by the provider from the API id and the
  region — it never asks the API, so nothing odin returns can change it, and
  curling it reaches Amazon or nothing. An earlier probe concluded the opposite
  because a throwaway stub happened to answer `apiEndpoint` with exactly the
  string the provider constructs; the match was read as causation. Read
  `api_endpoint`, or `${{<api node>.API_ENDPOINT}}` on the canvas.
- **One route per target, at the target's own label, and you cannot choose the
  path.** A drawn `apigateway → lambda|ecs` edge emits `ANY /<target label>` and
  `ANY /<target label>/{proxy+}` — two route keys because that is what AWS needs
  to serve a whole prefix, collapsing to one nginx `location` pair. The path
  comes from the label because `generate_tf` is a pure function of the canvas
  and the tile has no route field. Importing a project whose route key is
  something else (`POST /checkout` against a function labelled `orders`) does not
  fail — it reports the key as CHANGED and names what odin would emit instead,
  because the next Apply really would serve a different path.
- **Only the `$default` stage is served.** odin emits exactly one stage per API,
  always `$default` with `auto_deploy = true` — the stage whose invoke path
  carries no stage segment, which is what lets the nginx prefix and the route key
  mean the same thing. A stage with any other name is STORED and ECHOED (so
  `tofu plan` is clean) and routes nothing; an imported one is reported CHANGED.
- **An `apigateway → ecs` route names the service through a hostname only odin
  resolves.** `integration_uri` is
  `http://${aws_ecs_service.<n>.name}.odin.internal`. `integration_uri` must be a
  URI and an ECS service has no URL on real AWS either — an HTTP API reaches a
  private service through a VPC link, which odin does not model — so the
  generated file is NOT something that would work against Amazon for this edge.
  The alternative was requiring an ALB in between, which would make the simplest
  useful canvas a four-node one.
- **odin's process is in the data path for an ECS route, and there is no
  failover across tasks.** An ALB's nginx dials the task directly; an API's nginx
  dials odin's own invoke shim, which resolves the task's address per request.
  That is deliberate: a task's published host port changes every time it is
  replaced, so a baked-in address needs a push from `ecsctl` on every lifecycle
  transition (four call sites that must all stay correct forever), and a miss is
  a 502 with no explanation. Resolving per request cannot go stale. What is given
  up is nginx's request-level `proxy_next_upstream` retry across tasks: the shim
  dials the FIRST running task and reports the failure if it is not there. An ECS
  service with no running task yet answers **503 naming the service**, not a
  bare 502.
- **The invoke shim is an UNAUTHENTICATED route on the gateway port, bounded by a
  per-API token.** An HTTP API route with `authorization_type = "NONE"` — the only
  kind odin emits — is a public endpoint; that is what an API Gateway is. So
  `/_odin/apigw/{env}/{api}/{integration}` on the gateway's own port accepts
  unsigned requests. It is bounded rather than open: it takes no function name
  (only ids resolved through stored records, so it can invoke only what a route
  already points at) and requires the API's `route_token`, a 32-hex secret minted
  at CreateApi, kept in the 0600 `apigwctl.json` sidecar and injected by nginx.
  Without it: 403, with no detail about which id was wrong. That makes the shim
  no more powerful than dialing the API's own published port — which is the bar,
  not "the endpoint is protected". `_odin` is a reserved path prefix on the
  gateway.
- **A crashed handler is a 502, and that took reading what RIE really sends.**
  RIE answers a RAISED handler with **HTTP 200** and an `{"errorMessage",
  "errorType", ...}` body, with no `X-Amz-Function-Error` header at all. Payload
  format 2.0's rule that "anything without a `statusCode` is the response body"
  would therefore serve a crashed function as `200 OK` with a stack trace as the
  payload. odin answers 502 with the function's own error document, which is what
  a real API Gateway does.
- **Payload format 2.0 only.** A `1.0` integration is REFUSED at
  CreateIntegration rather than served a 2.0 event: a 1.0 handler reads
  `event["httpMethod"]`, which a 2.0 event does not have, so the mismatch would
  surface as a `KeyError` inside the user's own function and blame their code.
  A `WEBSOCKET` protocol type is refused at CreateApi for the same class of
  reason — nginx answers a handshake with a 200 and the caller hangs.
- **What is NOT modelled:** authorizers (JWT, Lambda, IAM) — every route is
  `authorization_type = "NONE"`; custom domains and base-path mappings; API keys
  and usage plans; throttling and quotas; `request_parameters` path/header
  rewrites; `tls_config`; per-route `timeout_milliseconds` (the shim's own 30s
  matches the Lambda default); CORS configuration; access logging; and
  `aws_lambda_permission` — odin does not enforce Lambda resource policies, so
  the permission real AWS would require to let the API invoke the function is
  neither emitted nor needed. Every one of these that appears in an imported
  project is reported by name rather than dropped in silence.
- **The API Gateway v1 (REST) resource types are not modelled at all.**
  `aws_api_gateway_rest_api` and its `_resource`/`_method`/`_integration`/
  `_deployment`/`_stage` companions import as `unsupported`. v2 was chosen
  because its companion set is flat, which is what makes odin's own output
  re-import; v1's `parent_id` chains are a tree the importer would have to
  rebuild. Both v1 and v2 sign under the SigV4 credential scope `apigateway`,
  so a v1 call reaches the gateway and is classified — it simply has no handler.
- **The nginx prefix location's upstream path ends in `/`, and it has to.** When
  `proxy_pass` carries a URI, nginx REPLACES the matched location prefix with it
  TEXTUALLY. Without the trailing slash, `location /hello/` turned `/hello/a/b`
  into `/_odin/apigw/<env>/<api>/<int>a/b` — the remainder concatenated onto the
  integration id. Measured against a real container, which answered
  `404 {"message": "No integration intbc3b5d20a on API api843ac33f"}` for an
  integration really called `intbc3b5d20`. Only the `{proxy+}` half broke (an
  exact-match location appends nothing), so it read as "the greedy route is
  broken" rather than as string concatenation. Pinned in BOTH directions by
  `tests/gateway/test_apigwctl.py` — the prefix location must gain the slash and
  the exact-match location must not.
