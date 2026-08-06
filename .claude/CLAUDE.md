# Odin — a local-first AWS-compatible cloud (repo: odin)

## Overview
[NORTHSTAR.md](../NORTHSTAR.md) (repo root) governs — read it first for the
full direction. In short: a drag-drop canvas where people design real AWS
architectures; **`iac/` translates canvas ↔ Terraform DETERMINISTICALLY** in
both directions (no model — see the `iac/` note below); Apply runs a real
`tofu apply` against odin's own gateway, which fulfills the AWS calls with
local substitutes (RustFS for S3, etc.); IAM permissions drawn as edges are
enforced for real; Nebula is the network layer.

**All of that is LIVE as of v0.8.21** — gateway, Apply, IAM enforcement and 22
node kinds, each backed by a real substrate and covered by an integration gate
over every `-m integration` file. `docs/limits.md` is the honest boundary and
`ROADMAP.md` is what is left. This paragraph said those four were "still being
built" until 2026-08-04, which had been false for weeks.

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
- **Backend:** Python 3.12+ (uv), FastAPI + SSE, Pydantic.
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
- `settings.py` — EVERY `ODIN_*` knob, as six domain `BaseSettings` classes on one
  `settings` singleton. Sections are built from the environment at ACCESS time, not
  at import — see the Configuration section below for why that is load-bearing.
  `docs/config.md` is the human-readable index; `tests/test_settings_inventory.py`
  is the ratchet.
- `aws/` — `backings.py` (`BackingAws`: the real RustFS/goaws/dynalite backings — provision/exists/deprovision/gc/aws_env/facts, a host-side boto3 `client` for tests), `rds.py` (`PostgresRds`: rds nodes as direct Postgres containers).
- `fabric/` — resolve `${{node.VAR}}` from World facts. `localhost.py` (loopback, the default); `nebula.py` + `models.py` = the **self-hosted Nebula mesh fabric** for multi-Mac: `NebulaFabric` is a drop-in for `resolve` (the overlay IP rides in via facts), plus recovered nebula-cert/lighthouse primitives (one network per env, sticky overlay IPs) + `mesh_state`/`GET /mesh?env=` for a mesh UI. Nebula NOT Tailscale (you own the lighthouse, build a control plane on top). The old per-EC2 Nebula *Simulate* overlay was deleted; this host-level mesh is the new thing — don't confuse them.
- `compute/` — `lima_yaml.py`/`cloud_init.py`/`models.py`: the EC2-as-real-Lima-VM substrate for the next service-coverage phase (not wired into the reconciler yet).
- `iac/` — **DETERMINISTIC, and the directory name now says so.** `hcl.py`
  (Stack → Terraform) and `import_tf.py` (Terraform → Stack) moved here from
  `agent/` in v0.8.21: neither imports `claude_agent_sdk`, and a reader seeing
  `agent/hcl.py` concluded a model generates their infrastructure. It does not,
  and that is the layer's most important property (NORTHSTAR's 2026-07-30
  deterministic-first amendment). `tests/test_iac_is_deterministic.py` pins it:
  no SDK import, and `iac` never imports from `agent`.
- `agent/` — what actually calls a model: `ai.py` (the master switch), `chat.py`,
  `debugger.py`, `translate.py` (the refine pass). Those three are the ONLY
  modules in odin that can reach `claude_agent_sdk`. `api/canvas.py`, `api/ws.py`, `server.py`.

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
   arrived, and stepping around the entire residual.
   **HALF-RETIRED as of v0.8.16, and the fix is the more useful lesson.**
   `test_rds_tf_e2e.py` now `delenv`s that variable and runs at the production
   cadence; only `test_rds_noop_apply_outage_e2e.py` still sets it. What
   replaced it was not a longer wait but a different SIGNAL: the test had paired
   a CADENCE-FREE fact (`/world` reading `crashed`, which `tf_status.project`
   derives on every projection) with a CADENCED one (the record, written by the
   background `DriftSweeper`) and given the second zero retries. The assertion
   moved onto the recovery apply's own `recovered_resources`, which is
   synchronous and in the response body. Falsified rather than assumed: with the
   sweep pinned so it can NEVER run (`TICKS=1000000`) the test still passes. So
   when a test waits on a cadence, the question is not "how long" but "is there
   an in-band witness for this at all".) Measured without that
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
2b. **`docs/architecture.md` + `docs/architecture.html` are a DIAGRAM of what
   runs — keep them current with the thing they draw, and with each other.**
   TWO files on purpose: GitHub does not render an HTML file in a repo (clicking
   one shows source), so the markdown is what a reader clicks; the HTML is the
   styled standalone, with the mermaid PRE-RENDERED to inline SVG so it needs no
   JS, no CDN and works offline (mermaid is 3.4MB — too heavy to vendor for a
   docs page). Regenerate the HTML by rendering the diagrams headlessly with
   `agent-browser` and inlining the SVG; the markdown is generated from the same
   diagram sources. Update BOTH in the same change or they diverge, which is the
   failure this rule exists to prevent one level up. One mermaid diagram per implemented service plus two
   for the system, each with a note saying what is really underneath (the
   substrate image, the enforcement path, the measured number). GitHub renders
   it inline, and the README links it as the picture version of `internals.md`.

   It goes stale the same way prose does, and worse: a diagram is believed
   faster than a paragraph. So when a service gains a substrate, an enforcement
   path changes, a measured number moves, or a kind is added or dropped, update
   the diagram IN THE SAME CHANGE — not the release after. Two rules it must
   keep: every arrow corresponds to something that runs, and a boundary that is
   NOT gated is drawn as not gated (the mesh covers overlay traffic only; ECS
   tasks, the ALB proxy, Lambda and ElastiCache are not members). Drawing an
   ungated path as gated is the same lie as a doc claiming an unfired guard,
   with better graphics.

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

5. **When the check and its subject share a source, the check cannot fail.**
   Rule 1 is about a guard whose signal never ARRIVES. This is the opposite
   failure: the signal arrives fine, and the guard is asking the subject to
   grade itself. FIVE instances in one night (2026-08-02), every one green,
   every one reviewed, none visible from inside:
   - **A test deriving its expectation from the expression under test.**
     `tests/gateway/test_ecsctl.py` asserted
     `== container_gone_reason(task["container_name"])` — the very call the
     source made. Green for MONTHS while ecsctl passed the task DEFINITION's
     name (`web`) where drift.py passed the real container
     (`odin-ecs-{env}-{id}-web`), so odin told users to go look at something
     `docker inspect` cannot find. Caught by the release gate, never by review.
   - **A guard parametrized over the thing it guards, so the regression DELETES
     the case.** The closed-world method-independence test drew its cases from
     `gateway/app.py`'s own route table: removing PATCH removed the `[PATCH]`
     case, and the file went 5 passed where 6 had. A property test, green, on
     exactly the regression it existed to catch.
   - **A measurement pattern matching the subject's own styling.**
     `grep -c 'card-head' docs/architecture.html` also matches two CSS rules in
     that page's stylesheet — 15 where the real count is 13. I relayed the
     phantom mismatch to three agents as a caution before it was measured
     properly. A false claim about the repo becomes load-bearing fast.
   - **Asserting a property of the PRIOR state without reading the prior
     state** ("the new SVG uses a stable id, matching what `mermaid-kms`
     already does" — `mermaid-kms` did so only BECAUSE of that commit).
   - **An inference that decayed under a still-valid input.** An efs guard
     warned a bad mount path would drop the whole file system; the builder had
     since narrowed it to decline only the offending function. Its input stayed
     true; the SENTENCE built on it rotted. Only the end-to-end round trip could
     tell them apart — the guard's own unit test passed on the half that still
     fired, which is what makes a stale inference invisible at unit scope.

   **So:** the expectation must come from somewhere the subject cannot reach — a
   literal spelled out in full, an independently-owned list, a second producer.
   **Mutation-test by DELETING an element, not only by corrupting one, and treat
   a mutation run whose test COUNT drops as a failure** — that is the quietest
   of the five and it reads as success. Anchor a measuring pattern to the real
   element (`<div class="card-head">`, not `card-head`) or the file's own
   comments and styling will answer for it. Pin an inference guard in BOTH
   directions: a mutant removing the gate must kill the silence test, a mutant
   forcing it must kill the declined test.

5c. **A test that CONSUMES the thing it is asserting on races anything else
   consuming it.** MEASURED, v0.8.20 gate:
   `test_dispatch_sqs_e2e::test_a_message_survives_when_the_function_cannot_run`
   passed alone in 77s and FAILED inside a 26-file partition, at
   `120.0s, "the undelivered message was lost rather than redelivered"`.
   Not a product regression and not luck: the test polls `ReceiveMessage` to
   prove the message was not deleted, while the DISPATCHER polls the same queue
   every tick -- and **every receive resets the visibility timeout**. The test
   only wins when its poll lands between expiry and the dispatcher's next one,
   so machine load decides the outcome.
   The tell is a test that passes alone and fails in a suite, on an assertion
   about a resource something else is also reading.
   **Two fixes, in order of preference:** (a) assert with a NON-CONSUMING
   witness (a count, an attribute, a record) so nothing is taken from under the
   other reader; (b) if the assertion genuinely needs the item, first STOP the
   competing reader -- here, delete the event-source mapping before polling,
   which costs nothing because the property (survived N failed deliveries) is
   already established by the wait that precedes it. Raising the timeout is not
   a fix: it makes the race rarer and the diagnosis slower.

5b. **Two-point sampling is the same error over time.** "The broken diagram
   shipped for however long" became "broke at `7693f08`, repaired at `08ba9a3`,
   never in any tag" only by walking EVERY commit in the window instead of the
   endpoints. Checking two points and inferring the middle turns a six-hour
   develop-only regression into a claim about released software.

## Configuration is a Settings class. A REGISTRY is not configuration.
(owner directive, 2026-08-03)

**Config goes in `src/odin/settings.py`** — domain-specific pydantic
`BaseSettings` classes (`GatewaySettings`, `ReconcileSettings`,
`SimulateSettings`, `ComputeSettings`, `MeshSettings`, `AiSettings`) composed
onto ONE singleton, imported where needed. `env_prefix="ODIN_"`, typed fields,
validated at construction.

**Why:** the state this replaced was 30 distinct `ODIN_*` variables read
directly in 19 files, each with its own ad-hoc parsing and none validated — so
`ODIN_DISPATCH_TICKS=abc` failed whenever that code path first ran, if ever,
rather than at startup. And there was nowhere to look up what odin's knobs even
are. A scalar tunable added anywhere else is a knob nobody can find.

**Carry a default's REASON with it.** Several are load-bearing and measured —
`READY_TIMEOUT = 120.0` is sized for first-run image pulls, and the EC2 boot
ceiling's default "deliberately stays put" because a longer one makes a
genuinely hung boot slower to report. A default without its reason is the next
thing someone tidies.

**THE TRAP, and it is rule 5's shape: tests monkeypatch these variables.** A
singleton read ONCE at import time makes every `monkeypatch.setenv` silently
ineffective — the test sets the var, the code reads a value captured at import,
and the test passes for the wrong reason with nothing failing. So settings must
be read at USE time, and the proof is two mutations, not one: a test that
monkeypatches a var must still pass when you break the production default, and
a test that relies on the default must FAIL when you change it. Demonstrate
both or the object is being read at the wrong moment.

**A REGISTRY IS NOT A SETTING, and must not become one.** `_CARRIED_ATTRS`,
`_KIND`, `_BUILDERS`, `TF_OWNED_KINDS`, `_ALB_COMPANION_TYPES`,
`_RESTJSON_SERVICES` are static DOMAIN KNOWLEDGE the code dispatches on. Nobody
overrides them with an environment variable. Making them settings fields would
imply they are user-configurable, put env lookup on a hot path, and lose the
exhaustiveness checking that is the point of them.

**What registries DO owe you is a test, because an entry is a PROMISE.**
`_CARRIED_ATTRS["ecs"]` claims a round trip reproduces that argument — and
membership SUPPRESSES the warning that would otherwise name it, so a false
entry is worse than a missing one. Measured: `alb → ecs` silently lost its whole
`load_balancer` block on import with `unsupported == []` and no warning at all,
precisely because the entry was there. Every registry entry needs a test that
would fail if the promise broke, and that test must use a value THE GENERATOR'S
DEFAULT CANNOT REPRODUCE — a round trip over a default passes even when import
drops the field, because the generator refills it.

## Browser automation: `agent-browser` (playwright-cli removed 2026-07-27)
`agent-browser` (brew, Apache-2.0) is the only browser driver. `@playwright/cli`
and its skill are gone; nothing in odin depended on them
(`grep -in playwright ui/package.json pyproject.toml` → no match). Load usage at
runtime with **`agent-browser skills get core --full`** — the skill in
`~/.claude/skills/agent-browser/` is only a discovery stub, so instructions
always match the installed CLI instead of going stale.

**Two measured gotchas. Both are this repo's own "reports success it did not
achieve" pattern, living inside the tool:**

1. **Pointer-drawn UI (odin's IAM edges) is NOT reliably drivable — treat this as
   OPEN.** Separate `mouse move`/`down`/`up` CLI calls draw no edge; that much I
   reproduced. `agent-browser batch "mouse move …" "mouse down" … "mouse up"` was
   reported to work by the evaluating agent, and **I could not reproduce it** over
   several attempts. Instrumenting the page showed why the attempt fails but not
   how to fix it: `pointerdown`/`mousedown` arrive with the target NOT a handle,
   even though the preceding `mouse move` used the handle's measured centre, while
   `pointerup` DOES land `@handle`. Handles are 6px wide. So: HTML5 drag-and-drop
   (the sidebar → canvas path) is solid and verified; connection-dragging needs a
   working recipe before anyone depends on it, and `eval`-dispatched synthetic
   pointer events are the obvious fallback to try first.
2. **`drag` has no target-position option** -- it always drops at the target's
   CENTRE. For an exact coordinate use `eval` with a synthetic `DataTransfer`
   (measured: places a node at the precise flow position). Use `drag` for the
   visible gesture in a recording, `eval` when the position matters.

Verified end-to-end against odin on 2026-07-27: drag places a real node
(`data-id s3-101`, grid-snapped by `Canvas.tsx`'s onDrop); the full sequence
fires (`dragstart` → `dragover` → `drop` → `dragend`) carrying
`application/odin-resource`; clicking APPLY against a LIVE server committed a
Stack revision and `/world` reported the resource `healthy`; and
`record start/stop` writes WebM natively for the README GIFs (set the viewport
AFTER `record start` -- recording opens a fresh context with its own viewport).
For WebSocket taps use `--init-script`, which registers before the app's own
connection; an `eval` after load always loses that race.

## Concurrency: async, not threads (owner directive, 2026-07-27)
**No `threading` and no `multiprocessing` unless genuinely unavoidable.** The reason is locking: threads force it, and on a single
event loop a synchronous read-modify-write is already atomic with respect to
other tasks, because nothing preempts it without an `await`. So when threads
go, DELETE the locks they existed for rather than porting `threading.Lock` to
`asyncio.Lock` — check first whether the critical section contains an `await`;
if it doesn't, it needs no lock. **This includes `to_thread`** — `asyncio.to_thread` /
`anyio.to_thread.run_sync` IS a thread pool, so it does not satisfy the rule,
it hides it. Three facts make full elimination realistic here, all verified:
- **Subprocess work is natively async.** `anyio.run_process` /
  `anyio.open_process` / `asyncio.create_subprocess_exec` all exist. odin has
  ~20 `subprocess.run` sites (docker, limactl, tofu, nebula-cert) and that is
  the dominant blocking work — convert them, don't wrap them.
- **File I/O should stay SYNCHRONOUS.** `anyio.AsyncFile.read` is literally
  `await to_thread.run_sync(fp.read, ...)`, so "async file I/O" would
  reintroduce threads invisibly. A page-cached few-KB store read is
  sub-millisecond: inline is cheaper and simpler than a thread hop. Judge by
  DURATION, not by whether it is "I/O".
- **CPU-only work is not blocking I/O.** SigV4 signing needs neither.
Prefer an async driver where a production-grade one exists (`psycopg` v3's
`AsyncConnection` over psycopg2, which is what `assertions.py::pg_ready` and
`aws/rds.py` use today). If something is genuinely unavoidable, leave the
boundary VISIBLE and say why — an honest limit beats a hidden thread.

**Which library: core `asyncio`, and stay in it.** odin's own code imports
`anyio` NOWHERE — it is purely transitive via Starlette — while `src/odin`
already uses `asyncio.Lock`, `asyncio.create_subprocess_exec`,
`asyncio.create_task` and friends throughout. Consistency is the whole point,
so do not introduce a second async idiom; a transitive dependency is not a
reason to start calling it directly. On Python 3.13 the stdlib covers what
`anyio` is usually reached for: **`asyncio.TaskGroup`** for structured
concurrency, `asyncio.create_subprocess_exec` for async subprocesses,
`asyncio.timeout`, `asyncio.Lock`. That is also what uvicorn/FastAPI run on.

**Status: DONE, and pinned by a test rather than by this paragraph.**
`asyncio.to_thread` is at ZERO call sites (down from 28) and a ratchet keeps it
there. `tests/test_thread_inventory.py` is the live inventory — the prose here
listed five surviving modules when there were two, twice, which is why it is a
test now. Two remain, both deliberate: `gateway/app.py::serve_in_thread`
(TEST-ONLY; two sync integration tests must dial a real bound port while doing
blocking work inline) and `gateway/stores.py`'s `JsonStore` locks, whose only
contender is that helper — **delete them in the same change that deletes
`serve_in_thread`, not before.**

**Four failure modes this conversion creates. All are silent, none is caught
by an ordinary test, and each now has a mutation-tested ratchet under
`tests/` — read them before converting anything.**
1. **A coroutine that blocks.** `await f()` on a SYNC function raises, so that
   mistake reports itself. The reverse does not: an `async def` whose body
   still blocks awaits happily and stalls the reconciler and gateway together.
   A blocking `httpx.post(timeout=30)` reached the shared loop this way and
   could DEADLOCK a re-entrant lambda invoke — measured 25.11s and a
   `TimeoutError`, against 0.10s once fixed.
2. **`await f(...).attr` reads the attribute off the COROUTINE.** `await` binds
   looser than attribute access, subscription and calls. Nine real instances.
   Two sat in `except` blocks, so they returned a plausible-looking degraded
   answer instead of raising.
3. **`create_task(await f())` schedules nothing** — `f` runs inline, and if `f`
   loops forever the caller never returns. `Reconciler.start()` did exactly
   that. A HANG is indistinguishable from "still working".
4. **A `threading.Lock` held across an `await` is a DEADLOCK, not a stall.**
   Task B blocks the whole loop in `lock.acquire()`, so task A can never be
   resumed to release it. Verified: a repro's own `asyncio.timeout(2)` never
   fired, because nothing was left to service it. The lock did not change —
   there is just one thread to block now.

Two more, learned the hard way: **typer SILENTLY DROPS an `async def` command**
(measured on 0.26.7 — exit 0, body never runs, no output), so CLI entry points
stay sync and bridge with `asyncio.run`; and **an `asyncio.Task` has no head
start**, where `Thread.start()` runs immediately — any test asserting on state
right after a converge call was always racy and the thread was hiding it. Await
the task; never add a sleep, which restores the coincidence rather than the
guarantee.

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
- **A FIXED port is the wrong isolation for a parallel run.** Assigning each
  agent its own `ODIN_GATEWAY_PORT` partitions agents and then collides with
  that agent's OWN xdist workers: measured, `ODIN_GATEWAY_PORT=5311` with
  `-n auto` gave every worker but one `OSError: [Errno 48] Address already in
  use` and made the run WORSE than leaving it unset (39 errors vs 34). The
  tests already default to an ephemeral port (`0`), which is STRONGER
  isolation than any fixed number -- nothing can collide with it. So: fixed
  ports for a long-running server you must dial, ephemeral for test runs.
  Same lesson as the one below, one level down.
- **Isolation is PER-PROCESS, not per-agent.** An agent with its own port, store
  dir and env prefix then ran two of its own `pytest` processes through them at
  once, and the collision produced a phantom "1-in-6 flaky test" that was
  reported to the lead and later retracted. Seven clean runs were identical
  (90/814/5); the two anomalous runs were both its own contamination. If you
  start a second process, it needs its own everything too.
- **A source mutation is a TREE-WIDE edit.** Mutation testing rewrites a file
  that every concurrent reader of that worktree sees. The same incident had a
  live `policy.py` mutation land in an unrelated full-suite run (three extra
  failures, exactly the three that mutation breaks) and made a *different*
  agent report an inexplicable `AssertionError` from a file rewritten
  mid-import. So: mutate only in a private copy of the tree, or hold a hard
  rule that nothing else runs during the window — and when a result surprises
  you, suspect your own harness before the code.
- **Cleanup must be scoped to the env names that agent created.** `docker ps -aq
  --filter label=odin=1 | xargs -r docker rm -f` is machine-wide and has already
  deleted another agent's containers mid-verification. Use
  `--filter name=<prefix>`.
- **Ports and store dirs partition STATE; nothing partitions PROCESSES.** When
  something you started hangs, cancel it by the handle you already have —
  `TaskStop` for a harness background task, or `kill "$pid"` from
  `cmd & pid=$!`. Reach for `pkill` only with no handle, and then scope it to
  your own worktree path (`pkill -f "/worktrees/<my-agent-id>/"`). **Never
  `pkill -f pytest`** or any pattern that can match another agent: it is the
  process equivalent of the machine-wide docker sweep above.
  Two agents did this independently within one hour — one `pkill -f "pytest
  tests/gateway"`, one `pkill -9 -f pytest` — and each disclosed it to the
  other unprompted. Both had correctly taken their own port, store dir and env
  prefix, and both had used a properly scoped cancel earlier in the same
  session. So this is REFLEX UNDER TIME PRESSURE, not ignorance of scoping,
  which is why the cheap correct action has to be named first rather than the
  rule merely saying "scope your pattern".
  It also poisons diagnosis: a killed run looks exactly like a hang, and this
  cost a false "the suite hangs at 12%" that was really someone else's `pkill`
  landing on it.
- **`git add -A <path>` is path-limited; `git commit` is NOT.** In a shared
  worktree that combination swept 24 of another agent's staged files into an
  unrelated commit. Commit with explicit pathspecs and read `git show --stat`
  before trusting it.
- **A base check DECAYS — re-check it, don't establish it once.** An agent
  correctly verified its worktree base at task start, worked for hours, and
  reported a breakage list measured 8 commits behind the tip; every diagnosis
  in it was right and every one was already fixed. Its own summary is the rule:
  *"my error wasn't failing to know about staleness — it was that I did check,
  got a clean answer, and never re-checked across a multi-hour session in a
  worktree that had moved under me twice."* Re-run `git log --oneline -1`
  against the main checkout immediately BEFORE reporting, not only before
  starting. Being right about a cause while wrong about whether it is still
  live costs a reviewer's attention as surely as being wrong.
- **"I read it" needs a "when."** With many agents in flight, one read
  `catalog.ts` before a commit landed and another after, and they reported
  contradictory states — both honestly. Timestamp claims about the tree.

## Cleanup / Disk (limited headroom — clean up after EVERY heavy step)
- **Containers:** every test/run tears down its own; `docker ps -aq --filter label=odin=1 | xargs -r docker rm -f`. Tests use the `runtime` fixture's teardown.
- **Lima VMs:** the LimaRuntime VM is `odin-host`; integration tests delete it after. Never leave stray VMs (`limactl list -q`); delete by exact name (the user's own VMs like `veronica` are off-limits).
- **Misc:** prune `.odin/`, `/tmp/*.png`, `__pycache__`, `.pytest_cache`, `.ruff_cache`. (Browser work leaves no litter to prune: `agent-browser` keeps state in its own session store, unlike playwright-cli which dumped a YAML snapshot per command.)

## CLI / running
- `uv run uvicorn odin.server:create_app --factory --host 127.0.0.1 --port 4200 --timeout-graceful-shutdown 5` (the flag is REQUIRED: an open SSE stream otherwise blocks uvicorn's graceful shutdown forever — measured, two ignored SIGTERMs) (the real app: reconciler + AWS backings in lifespan).
- `odin start` / `odin start --dev` (Vite :4200 + uvicorn :4201). Tests: `uv run pytest` (unit), `uv run pytest -m integration` (real Colima backings — slow).

## Status / lifecycle
- Canonical resource id = the node **label**. World phases: `draft` / `pending` / `starting` / `healthy` / `crashed` are what's actually emitted today (rds + the PROVISIONED AWS kinds). `blocked` / `queued` / `running` / `done` / `evicted` / `error` remain in the `Phase` type for the parked workload layer and future gateway/Simulate error states — don't be surprised they're unreachable right now.
  - **`draft` was missing from this list AND from the `Phase` literal until v0.8.18**, while the reconciler broadcast it on every prune and `StatusBadge.tsx` styled it. It stayed invisible because `_prune` hand-built a dict instead of a `WorldDelta`, so pydantic never validated the wire (`WorldDelta(phase="draft")` would have raised). Both are fixed; the prune now constructs a real `WorldDelta`, so the wire and the type cannot diverge again. Treat any hand-built delta dict as the same bug waiting.
- Status is a one-way projection: drivers + assertions author facts → Reconciler emits `WorldDelta` → `ConnectionManager.broadcast` (SSE) + `.odin/<env>/world.json` + append-only `events.jsonl`. **`world.json` is NOT append-only** — `spec/store.py::write_world` overwrites it wholesale, every tick; the append-only pair is `stacks/<rev>.json` (content-addressed revisions) and `events.jsonl`. The UI is a pure projection; `StatusBadge` maps phases to colors; deltas carry `env` (UI filters by the active env).

## Environments
Multiple named envs reconciled independently (`/apply?env=`, `/world?env=`, `/destroy?env=`, `/envs`); each env gets its own `BackingAws` + `PostgresRds` (containers named `odin-aws-<backing>-<env>` / `odin-rds-<env>-<id>`) → isolated AWS-shaped state per env. UI has an env field in the TopBar.

## UI Design Rules
- **Grid alignment:** 20px grid; node sections are multiples of 20px (header=40, single-line meta=20, two-line=40, button row=40). Snap to 20px; node positions/sizes multiples of 20.
- **High-contrast dark theme:** near-black backgrounds (#050508, #0a0a10), bright borders, neon per-type accents. Solid borders. Catalog nodes render via the generic `ServiceNode`; bespoke nodes (s3/dynamodb) have their own components in `ui/src/components/nodes/`.
- **Catalog:** `ui/src/lib/catalog.ts` — hand-curated AWS-shaped nodes (sqs/sns/rds live today; the rest are future-coverage placeholders per NORTHSTAR.md directive 5).
- **Z-index:** all catalog/bespoke nodes at 2; `elevateNodesOnSelect={false}`.
- **Edges are drawn by the user, never by odin.** `Canvas.tsx` produces edges in
  exactly two places: `edgesFromCanvas` (replaying what was saved) and `onConnect`
  (a drag). This line used to claim "reference edges auto-drawn from `${{node.VAR}}`
  fields" — that describes a June design that did not survive, and nothing has
  ever done it. Two separate agents built reasoning on the claim before checking
  it, which is what makes a false line here expensive rather than merely untidy.
