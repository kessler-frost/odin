# Odin — a local-first AWS-compatible cloud (repo: odin)

## Overview
[NORTHSTAR.md](../NORTHSTAR.md) (repo root) governs — read it first for the
full direction. In short: a drag-drop canvas where people design real AWS
architectures; an agent (claude-agent-sdk) translates canvas ↔ Terraform/
OpenTofu both ways; **Simulate** runs a real `tofu apply` against odin's own
gateway, which fulfills the AWS calls with local substitutes (RustFS for S3,
etc.) at full API compatibility; IAM permissions drawn as edges are enforced
for real; Nebula is the network layer. Today, live code covers the AWS
substitute layer (RDS/S3/SQS/SNS/DynamoDB applied for real per environment) —
the gateway, the translation agent, Simulate, and IAM enforcement are still
being built (see ROADMAP.md).

**PARKED (2026-07-22, tag `app-layer-parked`):** the app-workload layer —
service/dep/batch/llm node kinds, the memory-aware scheduler
(`reconcile/scheduler.py`), the per-kind probe registry (`reconcile/
probes.py`), and the claude-agent-sdk config-completion brain (`agent/
brain.py`, `agent/completion.py`) — was ripped from live code and lives at
that git tag. It may return as a layer on top of the AWS core later; until
then don't resurrect it from memory of the old "Railway-like" direction.

The even-older Moto/OpenTofu *validate* path is retired too — don't resurrect
that either; `tofu apply` returns as the **Simulate** button per NORTHSTAR.md,
not as the old validate-only flow.

## Tech Stack
- **Backend:** Python 3.12+ (uv), FastAPI + WebSocket, Pydantic.
- **AWS substitutes:** real per-env backing containers — RustFS (S3), goaws (SQS+SNS, one container serves both), dynalite (DynamoDB) — plus a real Postgres container per RDS node. Run through the same `RuntimeDriver` as everything else, no emulator holding fake state.
- **Runtime:** `ColimaRuntime` (containers directly on Colima — the default) and `LimaRuntime` (containers inside a Lima VM, VM isolation), both behind the `RuntimeDriver` protocol.
- **Translation agent:** parked for now (see above). `claude-agent-sdk` is still a dependency; it's being repurposed toward canvas↔Terraform translation next, not config-completion.
- **UI:** React 19 + ReactFlow + Tailwind v4 + Vite (`ui/`, `bun`). High-contrast dark industrial aesthetic.

## Architecture (`src/odin/`)
A Spec Store spine: the desired-state Stack is authored by the canvas (and,
later, the translation agent); deterministic code reconciles; deterministic
assertions verify.

- `spec/` — `models.py` (Stack=desired, World=observed, WorldDelta, provenance-tagged fields), `store.py` (append-only content-addressed per-env revisions + `list_envs`), `translate.py` (canvas → Stack; `${{node.attr}}` → Ref; kinds today: rds/s3/sqs/sns/dynamodb).
- `reconcile/` — `plan.py` (pure `plan(Stack,World)→[Action]`, total+idempotent; rds + the AWS-shaped PROVISIONED kinds), `reconciler.py` (the loop: observe → plan → execute, supervision, AWS env injection, per-env, gc), `assertions.py` (pg_ready — the rds health check), `actions.py`.
- `runtime/` — `driver.py` (protocol), `colima.py`, `lima.py`.
- `aws/` — `backings.py` (`BackingAws`: the real RustFS/goaws/dynalite backings — provision/exists/deprovision/gc/aws_env/facts, a host-side boto3 `client` for tests), `rds.py` (`PostgresRds`: rds nodes as direct Postgres containers).
- `fabric/` — resolve `${{node.VAR}}` from World facts. `localhost.py` (loopback, the default); `nebula.py` + `models.py` = the **self-hosted Nebula mesh fabric** for multi-Mac: `NebulaFabric` is a drop-in for `resolve` (the overlay IP rides in via facts), plus recovered nebula-cert/lighthouse primitives (one network per env, sticky overlay IPs) + `mesh_state`/`GET /mesh?env=` for a mesh UI. Nebula NOT Tailscale (you own the lighthouse, build a control plane on top). The old per-EC2 Nebula *Simulate* overlay was deleted; this host-level mesh is the new thing — don't confuse them.
- `compute/` — `lima_yaml.py`/`cloud_init.py`/`models.py`: the EC2-as-real-Lima-VM substrate for the next service-coverage phase (not wired into the reconciler yet).
- `agent/` — parked (just `__init__.py`); the canvas↔Terraform translation agent lands here next. `api/canvas.py`, `api/ws.py`, `server.py`.

**Node kinds today:** rds (a direct Postgres container) + s3/sqs/sns/dynamodb
(AWS-shaped resources in shared per-env backing containers). service/dep/
batch/llm are parked (see above); ec2/ecs/lambda and the rest of the AWS
catalog are unbuilt placeholders in the UI catalog pending service-coverage
work.

## Conventions
- **`bun`** (not npm/npx/yarn/pnpm); **`uv`** (not pip); **`python`** (not python3).
- Pathlib for paths; imports at top; minimize if/else + try/except; structured I/O (Pydantic) over regex.
- Permissive licenses only (Apache/MIT/BSD/MPL). Branch work on `develop`; merge to `main` only for releases (locally, no PRs), then push.
- Lima via `limactl` CLI; containers via Colima `docker` (default) or `nerdctl` in a Lima VM.

## Honesty rules (earned the hard way — four audits, 2026-07-25/26)
Nearly every serious bug found in odin was the same shape: **odin claiming
something it had never verified.** These three rules exist so it stops
recurring. Read them before writing a guard, a status, or a caveat.

1. **A guard must read a signal that actually arrives.** Four separate guards
   silently never fired: Lambda's `FunctionError` read a header real RIE never
   sends; the store-liveness check substring-matched process argv; the mesh
   gate withheld facts that never reached World; the nebula re-handshake poke
   waited for `/sys/class/net/nebula1` when the device is `tun0`. Each passed
   review and its own tests. **Probe the real component and print what it
   returns before you code against it** — that is what the fixes that HELD did
   (nebula SIGHUP semantics on the wire, tofu's in-place state rewrite, ECS's
   waiter counting stale-revision tasks). A unit test that fabricates the
   upstream signal proves the parser, not the integration. **Mutation-test it:**
   break the guard and confirm a test fails.
1b. **A guard can read a signal that arrives LATE — and a test can fabricate
   its promptness.** Rule 1's sibling, found by field test 5. The lambda/rds
   steady-state checks are real, but both e2e tests set
   `ODIN_DRIFT_SWEEP_TICKS=1` and *wait for the sweep* before the failing
   apply — measuring the guard only after the input it depends on has provably
   arrived, and stepping around the entire residual. Measured without that
   help: FOUR consecutive `applied`/exit-0 applies over ~8s with zero
   containers, and `/world` green for the same window — four times worse than
   the prose that disclosed it. So: if a guard depends on a signal produced on
   a cadence, **test it at the cadence the user gets**, not one you shortened;
   and state the window in the docs as a measured number, not a hope.

2. **Never report success you did not achieve, and fix the SHAPE not the
   instance.** `odin destroy` returned exit 0 in three distinct forms across
   three releases (interrupted apply, timeout, no-op), each surviving the last
   fix; `apply` did the same for ecs, then lambda, then rds. Set status from
   the OUTCOME, never optimistically at the top of a route; key on the real
   exit code, not parsed text; name **what is still standing** (resource,
   observed vs desired, real reason). The contract: *a command answering a
   question returns the answer; a command performing an action returns whether
   the end state holds.* When you fix one instance, immediately hunt siblings —
   other kinds, the timeout path, the no-op path. **What finally worked** for
   `/destroy` after four rounds: stop initialising the status at all. Branches
   report an *outcome*; the status is derived from a map; an unmapped outcome
   falls through to failure. A branch that forgets now fails loudly instead of
   inheriting a lie — patching branches one at a time never got there.
3. **Caveats outlive their fixes — audit docs against source.** README and
   ROADMAP both documented the `/world` freeze after it was fixed; an audit
   found two more stale entries. Grep the docs for the bug's own words when a
   fix lands. Watch direction: a doc that oversells safety (bind address, file
   modes, "we verify X") is a claim you cannot back. Verify mode/bind/permission
   claims with the real command (`find … ! -perm 700`, `lsof`) and make the doc
   say what that command prints.

**Corollary: reported-fixed ≠ verified.** Have someone who did not write the fix
try to falsify it. That judgement — "here is where the original proof was weaker
than claimed" — was the single most valuable output of the field tests.

4. **Read the REAL exit code, and verify a change by CONTENT not by message.**
   The rules above are about odin lying to a user; this one is about a tool lying
   to *you*, and it cost more time in one session than any single bug.
   `cmd | head; echo $?` gives you **head's** status, not `cmd`'s. Worst case
   seen: `git apply -3 p 2>&1 | head -6; echo "APPLIED"` printed six "Applied
   patch … cleanly" lines, so the merge looked done — but `git apply` is atomic,
   a later file failed, it **rolled the whole patch back**, `head` ate the error,
   and the unconditional `echo` manufactured the success. The full suite then
   passed against pristine code. Caught only by grepping for the fix's own
   identifier and getting zero hits. Two different agents hit this the same day
   (the other: `git stash pop | tail -3` reported 0 while git had failed, leaving
   the work stashed — nearly five false "fixes" reported).
   So: **never pipe a command whose success you are about to rely on** — redirect
   to a file, `echo $?` on its own line; **never write an unconditional
   `echo "DONE"`** after a fallible command; and after any merge/patch/edit,
   **grep for something the change introduces** before believing it landed.
   Also verify your own *test harness* before blaming the code: a fault injection
   that silently does nothing looks exactly like a bug (a `kill -STOP` that
   signalled nothing because the container's BusyBox lacks `pgrep -o`, and odin
   was right to keep reporting healthy).

## Concurrency: asyncio, not threads (owner directive, 2026-07-27)
**No `threading` and no `multiprocessing` unless genuinely unavoidable — use
`asyncio`/`anyio`.** The reason is locking: threads force it, and on a single
event loop a synchronous read-modify-write is already atomic with respect to
other tasks, because nothing preempts it without an `await`. So when threads
go, DELETE the locks they existed for rather than porting `threading.Lock` to
`asyncio.Lock` — check first whether the critical section contains an `await`;
if it doesn't, it needs no lock. If a thread really is unavoidable (a blocking
C call, a library with no async API), isolate it behind one
`asyncio.to_thread` boundary and say in a comment why.

odin's remaining threads as of v0.7.6, all scheduled to go in v0.7.7: the
gateway's own uvicorn (`serve_in_thread`), the substrate boot threads
(`ec2compute._finish_boot`, `lambdactl`'s deploy thread, `ecsctl._launch_task`,
`cachectl._finish_create`), and the `threading.Lock` per env in
`gateway/stores.py` that exists because of them.

## Working in parallel (subagents and teammates)
Hard-won mechanics. Ignoring these has already destroyed work in this repo.
- **An agent worktree is branched from whatever HEAD existed when it was
  created, which is often NOT current `develop`.** Two agents filed findings
  against stale trees, and copying an agent's files wholesale into the main
  checkout once silently **reverted 192 lines** of newer work. So: before
  trusting anything from a worktree, `git diff <agent-base> HEAD -- <files>`; and
  prefer `git apply -3` of the agent's own diff over any file copy. Agents:
  compare your `git log --oneline -1` against the main checkout before concluding
  a fix is missing.
- **Never remove an agent's worktree until the whole run is over.** A "completed"
  notification does not mean terminated — agents get resumed, and two that were
  resumed after their worktrees were deleted lost their shells entirely, one
  losing an uncommitted refactor it then reported as destroyed (it had actually
  landed elsewhere; the false report was nearly acted on as a rebuild).
- **Assign every agent its own server port, `ODIN_GATEWAY_PORT`, and store dir**
  (never `/tmp` — macOS TMPDIR isn't shared into Colima), and its own env-name
  prefix. Two agents defaulting to :4200/:4266 produced a bogus 401 and two
  phantom "bugs" that had to be retracted.
- **Cleanup must be scoped to the env names that agent created.** `docker ps -aq
  --filter label=odin=1 | xargs -r docker rm -f` is machine-wide and has already
  deleted another agent's containers mid-verification. Use
  `--filter name=<prefix>`.
- **"I read it" needs a "when."** With many agents in flight, one read
  `catalog.ts` before a commit landed and another after, and they reported
  contradictory states — both honestly. Timestamp claims about the tree.

## Cleanup / Disk (limited headroom — clean up after EVERY heavy step)
- **Containers:** every test/run tears down its own; `docker ps -aq --filter label=odin=1 | xargs -r docker rm -f`. Tests use the `runtime` fixture's teardown.
- **Lima VMs:** the LimaRuntime VM is `odin-host`; integration tests delete it after. Never leave stray VMs (`limactl list -q`); delete by exact name (the user's own VMs like `veronica` are off-limits).
- **Misc:** prune `.odin/`, `.playwright-cli/*.yml`, `/tmp/*.png`, `__pycache__`, `.pytest_cache`, `.ruff_cache`.

## CLI / running
- `uv run uvicorn odin.server:create_app --factory --host 127.0.0.1 --port 4200` (the real app: reconciler + AWS backings in lifespan).
- `odin start` / `odin start --dev` (Vite :4200 + uvicorn :4201). Tests: `uv run pytest` (unit), `uv run pytest -m integration` (real Colima backings — slow).

## Status / lifecycle
- Canonical resource id = the node **label**. World phases: `pending` / `starting` / `healthy` / `crashed` are what's actually emitted today (rds + the PROVISIONED AWS kinds). `blocked` / `queued` / `running` / `done` / `evicted` / `error` remain in the `Phase` type for the parked workload layer and future gateway/Simulate error states — don't be surprised they're unreachable right now.
- Status is a one-way projection: drivers + assertions author facts → Reconciler emits `WorldDelta` → `ConnectionManager.broadcast` (WS) + append-only `.odin/<env>/world.json` + `events.jsonl`. The UI is a pure projection; `StatusBadge` maps phases to colors; deltas carry `env` (UI filters by the active env).

## Environments
Multiple named envs reconciled independently (`/apply?env=`, `/world?env=`, `/destroy?env=`, `/envs`); each env gets its own `BackingAws` + `PostgresRds` (containers named `odin-aws-<backing>-<env>` / `odin-rds-<env>-<id>`) → isolated AWS-shaped state per env. UI has an env field in the TopBar.

## UI Design Rules
- **Grid alignment:** 20px grid; node sections are multiples of 20px (header=40, single-line meta=20, two-line=40, button row=40). Snap to 20px; node positions/sizes multiples of 20.
- **High-contrast dark theme:** near-black backgrounds (#050508, #0a0a10), bright borders, neon per-type accents. Solid borders. Catalog nodes render via the generic `ServiceNode`; bespoke nodes (s3/dynamodb) have their own components in `ui/src/components/nodes/`.
- **Catalog:** `ui/src/lib/catalog.ts` — hand-curated AWS-shaped nodes (sqs/sns/rds live today; the rest are future-coverage placeholders per NORTHSTAR.md directive 5).
- **Z-index:** all catalog/bespoke nodes at 2; `elevateNodesOnSelect={false}`. Reference edges auto-drawn from `${{node.VAR}}` fields.
