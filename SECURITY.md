# Security

This document is the threat model: what odin trusts, what it executes on
your behalf, and what that means for you. It's written plainly, not to
alarm you — odin does real, useful things to your machine by design, and
you should go in with your eyes open.

## The short version

**Odin runs whatever you draw on the canvas, for real, on your Mac.** A
canvas is not a sandboxed description of infrastructure — it's closer to a
shell script. If you wouldn't run a script from a given source, don't
`odin canvas set` or `odin import-tf` a canvas from that source either.

## What odin trusts

Odin has **no authentication of its own**. Anything that can reach the
control app's port (default `127.0.0.1:4200`, loopback-only — see below)
can apply a canvas, and applying a canvas can do anything the local user
account can do: run containers, boot VMs, write files under `.odin/`.

The trust boundary is **the machine and the person running odin**, not
odin's own routes. There is no login, no API token, no per-user permission
model. This is intentional for a local-first dev tool, not an oversight —
but it means the bind address and the canvas source are your only real
controls.

## What odin executes

Clicking Apply materializes and runs a canvas's nodes for real:

- **Container images** — an ECS node's `image` field is pulled and run
  verbatim (`agent/hcl.py` → the gateway → `compute/tasks.py`), with the
  image's **own entrypoint**: odin's generated task definition carries only
  `name`/`image`/`essential`/`portMappings`, and the canvas has no `command`
  field for ecs, so a canvas cannot supply the process to run — only the
  image that supplies it. (`compute/tasks.py` *would* honour a `command` in a
  task definition, and a task definition registered against odin's gateway by
  something other than the canvas — a direct AWS-SDK `RegisterTaskDefinition`
  call — can therefore run one. That is the same trust boundary as every
  other gateway call: the machine.) Nothing sandboxes or scans the image;
  odin trusts it the way `docker run <image>` trusts an image.
- **Lambda code** — any Lambda node's inline code (`agent/hcl.py`,
  `compute/functions.py`) is zipped and executed verbatim inside the
  function's runtime container. Same absence of sandboxing or scanning.
- **EC2 user-data as root** — an EC2 node's `userData` field
  (`agent/hcl.py`) becomes a cloud-init script (`compute/cloud_init.py`)
  that runs as **root** inside a real Lima VM on first boot. There is no
  review step between drawing the node and that script executing.
- **Terraform/OpenTofu itself** — Apply shells out to `tofu apply` against
  odin's own gateway. `import-tf` parses arbitrary HCL you paste in.

None of this is a bug to be patched away — it's the point of the tool
(NORTHSTAR.md: "a local-first AWS"). The mitigation is the same one you'd
apply to any script: know where the canvas came from.

**Treat a canvas (or a `.tf` file you `import-tf`) from someone else
exactly like a shell script you're about to run** — read it, or trust the
source, before you Apply it.

## The control app binds to loopback by default

`odin start` (and `--dev`) bind `127.0.0.1` unless you explicitly pass
`--host`. Given there's no authentication, the bind address is the actual
access boundary: loopback means only processes on your own machine can
reach it at all.

`--host 0.0.0.0` (or any LAN address) is available for people who
genuinely want that — e.g. driving odin from another machine on a trusted
network — but it's opt-in, and doing it prints a warning every time:
`odin has no authentication — anyone who can reach this port can run
containers on this machine`. Don't do it on a network you don't trust.

As defense-in-depth against a browser-based attack (a malicious page
sitting open in a tab, POSTing to `localhost:4200` behind your back), the
control app also rejects any state-changing request whose `Origin` or
`Referer` header is present and not loopback. This doesn't replace the
bind-address boundary — it narrows the blast radius of a compromised or
malicious web page you happen to have open while odin is running.

The **gateway** (the SigV4-verifying reverse proxy on port 4266) is
different and binds `0.0.0.0` deliberately: workload containers reach it
via `host.docker.internal`, which needs the host's real interface, not
loopback. That's safe specifically because every single request the
gateway handles is SigV4-verified before it's classified or forwarded —
there's no equivalent per-request check on the control app, which is why
its own default is loopback instead.

## Security groups gate the overlay, not the published host port

A drawn security group is real enforcement — a compiled Nebula firewall on
the mesh — but it governs exactly ONE path: the overlay address odin
publishes as `DATABASE_URL_MESH` / `endpoint_mesh` (and an EC2 node's
`MESH_IP`). Every backing also keeps its **published Docker host port**,
because odin's own probes, the gateway's forwarding and host-side clients all
ride it, and **nothing gates that port**: any process on your Mac, any
container that can reach the host, and any EC2 Lima VM (via
`host.lima.internal:<port>`, which is precisely what the `DATABASE_URL_VM`
fact hands it) reaches the database with no security group in the path.

Closing the host path would mean making the Mac itself a data-plane mesh
member — a host `tun` device, i.e. root/sudoers — which odin rejects
outright. So the honest boundary is: security groups govern traffic between
drawn resources over the mesh; they do not sandbox your own machine. If you
want a consumer to be subject to its security group, give it the `*_MESH`
fact.

### What revoking access does and does not stop

Moving a resource out of a security group on the canvas is real: odin
re-signs that member's Nebula certificate with its new groups and restarts
its daemon, so the peer re-handshakes under the new identity and **new
connections are refused before Apply even returns** (measured at 0.11s).
A membership change odin cannot apply fails the Apply rather than reporting
success.

It also kills a connection that was **already open** through the path you
just revoked. That took a second mechanism, because nebula's firewall keeps a
conntrack entry per flow and re-validates it only when its own ruleset version
changes — never when a peer's certificate does. So odin also advances the
*admitting* member's ruleset version whenever anyone's group membership moves
in that environment: a reload whose rules are byte-for-byte identical, which
makes nebula re-check every flow it is already holding against the peer's
current certificate. It is a reload, not a restart — no other tunnel is
dropped.

Measured end to end, with a real VM holding a real TCP session to a real
Postgres across the revoke and then pushing a genuine startup packet down it:
the session **timed out** (dropped), while a still-permitted port on the *same
database over the same tunnel* answered `connection refused` at the same
instant — so the silence is a firewall decision, not a dead overlay. Before
this, that same session was answered
(`R\x00\x00\x00\x17\x00\x00\x00\nSCRAM-SHA-256`).

What that does **not** mean:

- **It is not instant.** The flow dies when the Apply carrying the revoke
  finishes its mesh passes — 12.8s in the measurement above, and longer on a
  busy machine or a large environment. Until then the open session keeps
  working.
- **It depends on an ordering odin controls but cannot prove.** The admitting
  member re-checks the flow against the certificate it currently holds for the
  peer, so the peer must already have re-handshaked under its new one. odin
  enforces that (every re-certified member is restarted and pokes each peer
  before any admitting member is reloaded, all synchronously). If that poke
  fails, the admitting member can re-validate against the old certificate,
  stamp the flow as current, and that one flow survives — until it closes on
  its own or nebula's `firewall.conntrack` timeouts expire it. A later Apply
  will not revisit it, because nothing about the membership has changed by
  then; another membership change, or restarting the admitting member, will.
- **It covers members odin gates.** The admitting member can be an EC2 VM or a
  database (a backing container with a mesh sidecar); both are handled. The
  published host port is still not gated at all — see above.

ROADMAP's security-group section documents the mechanism in full.

## Secrets

A field like an RDS `password` is stored, and used, in cleartext:

- It lands in `.odin/canvas.json`, in every immutable Stack revision under
  `.odin/<env>/stacks/`, and in `.odin/<env>/world.json` (which can also
  carry a live, resolved `DATABASE_URL` with the password embedded — the
  Fabric reads it from there to wire up other nodes' env vars, so it can't
  be redacted without breaking that). The same resolved fact is appended to
  the durable event log `.odin/<env>/events.jsonl`, and the value tofu was
  sent is in the generated `.odin/<env>/tf/main.tf` and comes back in
  `.odin/<env>/tf/terraform.tfstate` (plus `terraform.tfstate.backup`).
- **Every file odin creates that can carry a secret or a credential is
  `0600`** (owner read/write only), and **every directory odin creates** to
  hold one is `0700`: `.odin/` itself, each `.odin/<env>/`, and the
  `gateway/`, `stacks/`, `tf/` and `nebula/` (plus `nebula/hosts/`)
  directories inside an env — the only real protection is that another local
  account on the same machine can't read them. Anyone with your user account,
  or root, can. The exhaustive file list, because a mode you can't verify is
  worth nothing: `canvas.json`, `<env>/stacks/*.json`, `<env>/HEAD`,
  `<env>/world.json`, `<env>/events.jsonl`, `<env>/keys.json`,
  `<env>/gateway/*.json`, the files in `<env>/tf/` (including tofu's own
  `terraform.tfstate` and its `.backup`), `<env>/nebula/*`, and an
  `odin export` archive.
  - Two of those are not odin's files to write: tofu creates and rewrites
    `terraform.tfstate`/`.backup` itself, at `0644` under the default umask.
    Odin pre-creates both `0600` before every `tofu` invocation, which sticks
    because tofu's local state manager rewrites state **in place** (same
    inode, truncate, write — no rename), verified against OpenTofu 1.12
    across two applies and a destroy. Anything else tofu leaves in the
    workspace is re-tightened on the next materialize.
  - v0.7.0 and earlier got this wrong for exactly the files listed above as
    "the ones that matter": `tf/main.tf`, both state files, `events.jsonl`
    and the export archive were world-readable (`0644`). Fixed in v0.7.1;
    restoring an old archive tightens its files on the way in, and an old
    workspace is tightened on the next Apply.
  - The **directory** half of the claim was false through v0.7.2, and a
    fresh-user audit caught it: `.odin/` and `.odin/<env>/` were `0755`
    whenever a writer that used a plain `mkdir` (the goaws config file,
    `odin start`'s pidfile directory) created them before the private-mkdir
    helper did — while `.odin/default/` was `0700`, because there the order
    happened to run the other way. Every such writer now goes through the one
    helper, and that helper tightens a directory it finds group- or
    world-accessible, so an existing store is healed rather than left as the
    first writer set it.
  - One directory under `.odin/` is **not** `0700`, and deliberately so:
    `<env>/tf/.terraform/`, which is tofu's, not odin's. `tofu init` creates
    it (`0755` under the default umask) and re-creates it on every run; it
    holds provider plugins and their metadata — hundreds of files, no canvas
    value ever among them — so `_lock_down` in `simulate/workspace.py` skips
    that subtree instead of re-chmod'ing plugins on every apply. The three
    workspace files that DO carry secrets (`main.tf`, `terraform.tfstate`,
    `terraform.tfstate.backup`) sit beside it, not inside it, and are `0600`.
    Concretely: `find .odin -type d ! -perm 700` prints `<env>/tf/.terraform`
    and its subdirectories, and nothing else — the only other thing that can
    appear there is a directory a pre-v0.7.3 odin left `0755` and nothing has
    written to since, which the helper above tightens on the next write.
- `.odin/` is gitignored, so a normal `git add`/`commit` won't leak it into
  a repo — but nothing stops you from committing it deliberately, so don't.
- Fields that look like a secret (`password`, `secret`, `token`, `key` in
  the name) are flagged internally and kept out of places that don't need
  the real value: the translation agent's prompt to the Claude API, and
  every line of `tofu`'s own apply/destroy log output. They are **not**
  redacted anywhere the real value is functionally required (the
  reconciler, the generated Terraform that actually gets applied, `world.
  json`'s ref-resolution facts) — redacting those would just break the
  feature while giving a false sense of security.

A `secret` node (Secrets Manager) and an `ssm` node hold values that **are**
the secret — that's the whole point of the node — and they land in the same
places, plus one more:

- The canvas JSON, every immutable Stack revision, and now the per-env
  gateway sidecars the values actually live in (`secretsctl.json` and
  `ssmctl.json` under `.odin/<env>/gateway/`), all written `0600`.
- **A `SecureString` is not encrypted at rest.** There is no KMS in odin: a
  SecureString parameter is stored byte-for-byte like a plain String one, and
  `KmsKeyId`/`KeyId` are accepted and echoed back for Terraform fidelity
  while encrypting nothing. `SecureString` buys you the file mode and nothing
  else. It's stated plainly rather than implied away, because it's the single
  assumption people are most likely to bring to odin and be wrong about.
- What **does** protect a value: it never enters a World fact, so it never
  travels on the WebSocket or into `world.json` or `events.jsonl`; it's
  redacted out of the translation agent's prompt and out of every streamed
  `tofu` log line; and reading it back through the gateway requires a
  principal an IAM edge allows — no edge, real `AccessDenied`. (On the tofu
  log path odin's scrub is belt-and-braces rather than the sole protection:
  the AWS provider already prints `secret_string` as `(sensitive value)`.
  Belt-and-braces is deliberate — the provider's judgement of what is
  sensitive is not odin's to rely on.)
- The **tofu workspace** is where the plaintext legitimately appears, because
  `tofu` has to send it: `tf/main.tf` on the way out and
  `tf/terraform.tfstate` (+ `.backup`) on the way back. Three files, not one —
  and the file mode above is the whole of their protection.

- An `odin export` archive is a copy of all of the above: it contains the
  env's issued gateway credentials (`keys.json`), the gateway's secret and
  parameter sidecars, and every canvas secret in the stack revisions,
  `world.json` and the tofu workspace, unencrypted, in a file that is easy to
  email or drop in cloud storage. The archive is written `0600` and every
  member inside it is stored `0600`, so a restore can only ever tighten a
  store's modes, never loosen them. Treat it like a private key file — and
  note that the mode does not survive the things people do to archives: `scp`,
  a chat upload, or an object store will give the copy whatever mode it likes.
- **`odin import` refuses to restore into a live store**, because a running
  odin holds reconcilers and an in-memory World that would keep reconciling
  against a store they never read. Liveness is proved, not guessed: a running
  server holds an exclusive lock on `.odin/lock`, and the guard asks the
  kernel who holds it. It reads no process's command line — v0.7.1 did, and
  called an operator's own shell a live server mid-restore. The lock file
  holds only a pid; it is not a secret, and it is not a security boundary
  either. Anyone who can write your `.odin/` can already do worse than take
  a lock, and `--ignore-live-server` deliberately skips the check, so treat
  the refusal as a safety interlock for you, never as access control.

If you need real secret hygiene (rotation, least-privilege access,
encryption at rest), odin's local `.odin/` store is not that system — treat
canvas secrets as dev/test-grade, not production credentials. That holds for
a `secret` or `ssm` node exactly as much as for an RDS `password`: it's a
faithful API surface for the thing you drew, not a vault.

## Reporting a vulnerability

Please don't open a public GitHub issue for a security problem. Use
GitHub's private reporting instead: on the repo, go to **Security → Report
a vulnerability** (or `https://github.com/kessler-frost/odin/security/advisories/new`).
Include what you found, how to reproduce it, and the impact you think it
has. We'll get back to you.
