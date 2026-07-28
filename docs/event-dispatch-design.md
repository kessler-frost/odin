# The event dispatcher — design note for the half that is not built

odin can now *record* that "when X happens, run Y": EventBridge rules and
targets are a real, durable control plane (`gateway/models/eventsctl.py`), and
`gateway/classify.py` classifies `events:*` so a rule survives a real `tofu
apply`. Nothing yet **reads** that record and runs Y.

That gap is deliberate and it is visible on the wire: `events:PutEvents`
answers a named `InternalException` rather than the `{"FailedEntryCount": 0}`
real EventBridge sends, because an accepted-and-never-delivered event is
exactly the "reports success it did not achieve" shape this repo's honesty
rules exist for. An edge that renders as a trigger and never fires is the same
bug one layer up.

This note says where the delivery half goes, what drives it, and — more
usefully — which specific ways of building it would be wrong. It is written to
be falsified: every claim about existing code names the file, and every claim
about behaviour that was *measured* says so.

---

## 1. Where it lives

**`src/odin/reconcile/dispatch.py`**, beside `drift.py`.

- **The reconciler tick is the only cadence odin has.** `drift.py` is the
  precedent for "a bounded sweep on that tick with injectable substrate seams",
  down to the `containers`/`vms`/`probe` constructor arguments that make it
  unit-testable with no Docker at all. A dispatcher wants the identical shape.
- **Not in `gateway/`.** The gateway is request-scoped and `GatewayState` is
  rebuilt wholesale every tick ("the gateway is stateless", `app.py`). A
  dispatcher is a background actor with its own bookkeeping; putting it there
  fights that invariant.
- **Not inside `eventsctl.py`.** It spans three trigger sources (EventBridge
  rules, SQS event-source mappings, S3 bucket notifications) and one sink (a
  Lambda invoke). A dispatcher owned by one source is three dispatchers.
- **The import arrow already exists and does not cycle.** `reconcile/drift.py`
  already does `from odin.gateway.models.lambdactl import mark_function_failed`
  and imports `ecsctl`, `ec2compute`, `rdsctl`. So `reconcile/dispatch.py`
  importing `eventsctl` and `lambdactl` follows an arrow the tree already has.
  (The reverse direction is the one that bites: `gateway/wiring.py` documents
  why it cannot import `tf_status` — `tf_status` reaches back into
  `gateway/models/ecsctl.py`, which imports `wiring`.)

Wire it where `DriftSweeper.verdicts` is already called from
`reconcile/reconciler.py`.

---

## 2. Cadence — every tick, and the ratchet against fabricating that

`ODIN_DRIFT_SWEEP_TICKS` (`drift.py::_sweep_ticks`, default 10 ticks ≈ 10s at
the production 1s poll) is the precedent for a bounded sweep, and the
dispatcher should have the same shape of override — `ODIN_DISPATCH_TICKS`, read
fresh rather than cached, same one-liner.

**But its default must be 1, not 10, and the reason is the honesty rule that
`ODIN_DRIFT_SWEEP_TICKS` itself taught.** A drift sweep is a *report*: being
10s late means a crashed resource is reported 10s late. A dispatcher is an
*action*: being 10s late means a trigger a user calls broken. And field test 5
found the specific way this goes wrong — the lambda/rds e2e tests set
`ODIN_DRIFT_SWEEP_TICKS=1` and *waited for the sweep* before asserting, which
measured the guard only after its input had provably arrived and stepped around
the whole residual. Measured without that help the residual was four times
worse than the prose disclosing it.

So, three rules, in order of how easy they are to violate:

1. **Default 1.** A pass that finds nothing must cost nothing: for a scheduled
   rule that is arithmetic against a stored `last_fired_at`; for an SQS mapping
   it is one `ReceiveMessage` against a local container. Neither is a `docker`
   shell-out, which is what made the drift sweep expensive enough to need a
   cadence in the first place.
2. **No e2e may shorten the cadence.** Test at the cadence the user gets, and
   publish the measured worst-case latency in `docs/limits.md` as a number, not
   a hope.
3. **Suspend during an apply, exactly as the drift sweep does.**
   `Reconciler._watch` passes `sweep=False` while `tofu` holds the daemon
   (`DriftSweeper.verdicts`' own docstring explains why: a sweep *corrects*
   records, and doing that off a sample taken mid-apply is a hazard). Invoking a
   function while `tofu apply` is mid-`UpdateFunctionCode` is the same hazard
   with teeth — `lambdactl`'s deploy path deliberately `rm -f`s the old RIE
   container before starting the new one, so there is a real window where
   `State` reads `Active` and no container exists. `drift._function_records`
   already encodes the exemption (`LastUpdateStatus == "InProgress"`); the
   dispatcher must honour the same one.

---

## 3. Draining an SQS → Lambda mapping without a thread

Per mapping, per pass:

```
receive (batch ≤ 10, VisibilityTimeout ≥ the function's timeout)
  → invoke, awaited
  → delete ONLY the entries whose invoke succeeded
```

The details that are not free:

- **`WaitTimeSeconds=0`. Do not long-poll.** Real AWS long-polls for up to 20s,
  and the equivalent here would park a coroutine for 20s per mapping. On one
  shared event loop that is *fine if and only if* the client is genuinely
  async — and odin's existing SQS client is not: `aws/backings.py::client` is a
  **blocking boto3 client, deliberately host-side and for tests**. Using it here
  reintroduces exactly failure mode 1 from CLAUDE.md — an `async def` whose body
  still blocks — and it would stall the gateway *and* the reconciler together.
  Use `httpx.AsyncClient`. Short-poll every tick is the same throughput at a 1s
  cadence and none of the risk.
- **Read/delete go DIRECT to the backing port; the invoke goes THROUGH
  `lambdactl`.** The asymmetry is deliberate and worth stating because it looks
  inconsistent. The receive/delete is odin's own bookkeeping, not a workload's
  call — the same boundary `logsctl.ingest` keeps by being reachable only
  in-process and never over the SigV4 wire. The invoke is different: it is the
  user's code running, and its outcome must be recorded where the rest of odin
  already looks.
- **Structured concurrency across mappings:** `asyncio.TaskGroup` (stdlib, per
  the concurrency directive — do not introduce `anyio`). One receive per mapping
  per tick, so a busy queue cannot starve the tick.
- **Never `to_thread`.** It is at zero call sites and `tests/test_thread_inventory.py`
  is the ratchet.
- **Never hold a lock across the invoke.** CLAUDE.md failure mode 4: a
  `threading.Lock` held across an `await` is a *deadlock*, not a stall, because
  the task that would release it can never be resumed. And the invoke is
  **re-entrant** — the handler's own boto3 calls come back through the very
  gateway running on this loop (`compute/functions.py::invoke`'s docstring, and
  the measured 25.11s + `TimeoutError` that motivated it).

### Which entry point — the wrapper, not the seam

`compute/functions.py::FunctionRuntime.invoke(env, name, payload_bytes)` is the
execution seam: async, container-backed, payload forwarded verbatim. **The
dispatcher must not call it.** Its only caller is
`gateway/models/lambdactl.py::_invoke`, and everything that makes an invocation
*visible* lives in that wrapper:

- the `State != Active` guard (a 502 `ResourceNotReadyException` instead of a
  dial at a container that is not there),
- CloudWatch log shipping (`_ship_logs`) — the handler's traceback, which is the
  whole reason a user opens `odin logs`,
- and `_update_function(..., last_invocation_error=...)`, the **durable**
  verdict that `reconcile/tf_status.py::_invocation_verdict` projects into
  `/world`.

Bypass the wrapper and a function that fails every single dispatched invocation
reports `healthy` with no verdict — which is field test 2, finding #4, exactly,
re-created through a new door.

---

## 4. What a failed invoke does

- **Do not delete the message.** goaws returns it after the visibility timeout;
  that is SQS's own redrive and it is free. Which is why the receive must set a
  `VisibilityTimeout` at least as long as the function's timeout — otherwise the
  message is re-delivered while the first invoke is still running and the
  function is invoked twice for one message.
- **Record the outcome per trigger, not only per function.** A rule whose target
  is dead must not read healthy forever. Emit a `label -> verdict` overlay in
  the same shape `DriftSweeper.verdicts` already returns, so the existing
  WorldDelta pipeline carries it with no new plumbing.
- **Derive the status from the outcome; never initialise it.** This is the
  `/destroy` lesson, and it is the one that took four rounds to learn: branches
  report an *outcome*, the status comes from a map, and an unmapped outcome
  falls through to failure. A dispatch branch that forgets then fails loudly
  instead of inheriting a lie.
- **A dispatcher exception must not stop the tick, and must not be swallowed
  either.** Log it and record a verdict against the trigger.
- **Name what is still standing.** "rule `nightly` could not run `thumbnailer`:
  the function is `Failed` (container gone — re-Apply to recreate)" is
  actionable; "dispatch failed" is not.

---

## 5. S3 → Lambda/SQS/SNS — synthesized, not forwarded

This one is settled by measurement rather than preference. `rustfs/rustfs:latest`
was probed directly (scoped throwaway container, since removed):

```
fresh bucket, never configured   -> {}
'probe' after a REJECTED put     -> {'QueueConfigurations': [{...}]}
PUT 'arn:aws:sqs:us-east-1:000000000000:jobs' -> InvalidArgument
PUT 'arn:aws:sqs::000000000000:jobs'          -> InvalidArgument
PUT 'arn:minio:sqs::jobs:webhook'             -> InvalidArgument
'fresh' after those attempts     -> {'QueueConfigurations': [{'Id': 't', ...}]}
```

**RustFS rejects every ARN form with `InvalidArgument` and stores the config
anyway.** The GET side is otherwise honest — a never-configured bucket really
does return `{}` — so this is not a read artifact.

odin classifies `s3:PutBucketNotification` today
(`classify._S3_BUCKET_CONFIG_WRITE_ACTIONS`) and, having no handler, forwards it
verbatim. That produces a three-way contradiction: `tofu apply` **fails**,
`tofu plan`/refresh sees the config present via GET and reports **no drift**,
and nothing ever **fires**.

So S3 notifications must be odin's own state, not RustFS's:

1. **`s3:PutBucketNotification` / `s3:GetBucketNotification` become real
   handlers** in `gateway/synth.py`'s `_PURE_HANDLERS` (or a small
   `gateway/models/s3notify.py` if it grows), storing the configuration in a
   per-env `JsonStore` with a `records.py` schema like every other store. Once
   they are pure they stop being forwarded at all — `app.py` returns a
   non-`None` `pure_answer` before it ever reaches the backing.
2. **Delivery hooks off the write, not off a poll.** Every object write already
   reaches the gateway classified with the bucket as its resource, and `app.py`
   already has an after-successful-forward hook:
   `synth.is_postprocess_action(action)` → `synth.postprocess(...)`, gated on
   `upstream.status_code < 300`. Adding `s3:PutObject` / `s3:DeleteObject`
   entries to `_POSTPROCESS_HANDLERS` is the whole mechanism, and it serves
   `s3 → lambda`, `s3 → sqs` and `s3 → sns` at once.
3. **The hook must ENQUEUE, not invoke.** `synth.postprocess` is a **synchronous**
   function called from `catch_all`'s async body — it cannot `await`. Two ways
   out, and the tempting one is wrong:
   - *Fire inline* would mean making `postprocess` a coroutine (6 handlers + 1
     call site) **and** would block the object-write response for the whole
     handler duration, which real S3 notifications never do — they are
     asynchronous and at-least-once.
   - *Enqueue* — write a pending-notification record, exactly the kind of
     synchronous store write `postprocess` already does for tags and queue
     state — and let the tick-driven dispatcher deliver it. The write stays
     fast, the asynchrony matches AWS, and there is **one** dispatcher with
     three sources instead of one dispatcher and one special case.

   (If a future change does make `postprocess` async, make *all* of it async in
   one commit. `synth.pure_answer`'s own docstring records why: while some model
   answers were coroutines and some were not, a branch that forgot its `await`
   returned a **coroutine object**, which is truthy, so the gateway answered
   with a coroutine instead of a `Response`.)

**Scope call, stated rather than assumed:** this note *recommends* the
`PutBucketNotification` handler; it is not implemented here. Two reasons.
Storing a notification config that nothing delivers turns today's honest
`tofu apply` failure into a **silent** one — apply succeeds, plan is clean, and
nothing fires — which is a strictly worse false green than the contradiction it
replaces. And the change touches the s3 forwarding path, which is not this
task's. Land it **in the same commit as the delivery half**, not before.

**The boundary, for `docs/limits.md` when the feature lands:** only writes that
go **through the gateway** can fire a notification. In practice that is all of
them — `AWS_ENDPOINT_URL` is what every workload gets — but a write made
directly against a backing container's port fires nothing, and that belongs in
the limits list on the day the feature ships, not after someone finds it.

---

## 6. Which trigger to build first

An independent analysis ranks **`sqs → lambda` first**, on the grounds that real
AWS's event source mapping *is* a poller, so odin polling goaws is the actual
architecture rather than a substitute-shaped compromise. **That reasoning is
correct and I am not going to manufacture a disagreement with it** — of the
three triggers it is the only one where odin's implementation and Amazon's are
the same design, its failure semantics (don't delete → visibility timeout →
redrive) come free and are AWS's own, and goaws already serves the queue.

The one factual correction, and it changes the *sequencing* rather than the
ranking: **`sqs → lambda` is not zero-control-plane either.** An event source
mapping is created by `CreateEventSourceMapping`
(`POST /2015-03-31/event-source-mappings`), and that route is **not** in
`classify._LAMBDA_ROUTES` and **not** modelled in `lambdactl.py`. Terraform's
`aws_lambda_event_source_mapping` would therefore fail `tofu apply` with
`AccessDenied`/unmappable-action today — the same wall EventBridge was behind
this morning. So it needs: 4 new lambda routes (create/get/update/delete), a
`mapping:` record shape, and a `records.py` schema, before one message moves.

By contrast a **scheduled EventBridge rule** (`ScheduleExpression` + a lambda
target) needs *no new wire work at all* — that control plane landed today. So:

1. **Walking skeleton: the scheduled rule.** It proves the whole spine — tick →
   is-it-due → `lambdactl` invoke → durable verdict → World — with zero new
   classify routes, no polling protocol and no event-pattern matcher. Every one
   of §§2–4 gets exercised and mutation-tested against something small.
2. **First real trigger: `sqs → lambda`,** on the reasoning above, once
   `CreateEventSourceMapping` exists.
3. **Then S3 notifications,** which need §5's synthesized control plane first.

If the skeleton step is judged not worth its own commit, fold it into (2) — the
disagreement is about the ordering *criterion* (smallest correct increment vs.
closest to AWS), not about the facts.

**Do not ship pattern-matched EventBridge routing until the matcher exists.** A
rule carrying an `EventPattern` odin cannot evaluate must be reported as
*unroutable*, in the same voice `PutEvents` uses today — never stored as a rule
that silently never fires.

---

## 7. The false-green shapes to design against

Each of these is a bug this repo has already shipped once, in the words of the
rule that came out of it.

| Shape | The dispatcher's version of it |
| --- | --- |
| **A guard reads a signal that never arrives.** | Do not read `x-amz-function-error` off the response — real RIE never sends it, which is one of the four guards that silently never fired. Read `InvokeResult.function_error` (derived from the response body by `compute/functions.py::_function_error`) via `lambdactl`, which persists it. |
| **A guard reads a LATE signal, and a test fabricates its promptness.** | Do not write an e2e that sets `ODIN_DISPATCH_TICKS=1` and waits for a pass. Measure at the default, publish the number. |
| **Success reported that was not achieved.** | "Received" is not "delivered". The success signal is the invoke's own verdict **and** the delete acknowledgement — never the receive, and never the fact that a rule exists. |
| **Fix the shape, not the instance.** | One outcome→status map for every trigger source. When one source's failure path is fixed, the other two must already be covered by construction. |
| **`await f(...).attr` reads the attribute off the coroutine.** | New async code; `await` binds looser than attribute access. Nine real instances last time, two of them inside `except` blocks returning plausible degraded answers. |
| **`create_task(await f())` schedules nothing.** | `Reconciler.start()` did exactly this and the hang was indistinguishable from "still working". |
| **A lock held across an `await` is a deadlock.** | See §3. Also: hold no lock across the re-entrant invoke. |
| **Caveats outlive their fixes.** | The `PutEvents` error text and this file both claim delivery does not exist. Grep for their own words on the day it does. |

---

## 8. What exists today, for the next reader

- `gateway/classify.py::_classify_events` — `events:*` → `(action, bare rule
  name)`. `PutEvents` resolves to its **event bus**, read out of
  `Entries[].EventBusName` (not a top-level member).
- `gateway/models/eventsctl.py` — rules, targets, buses, tags; store keys
  `rule:{bus}:{name}`, `targets:{bus}:{rule}`, `bus:{name}`. Targets are stored
  **verbatim**, so `Target.Arn` is the ARN terraform really wrote and is the
  dispatcher's input.
- `eventsctl.rules(stores, env)` / `eventsctl.targets_of(stores, env, record)` —
  deliberately in-process only, never on an HTTP route. **These are the reads a
  dispatcher starts from.**
- `gateway/records.py` — `EventRule` / `EventTarget` / `EventBus`, validated on
  every store read.
- Not built: delivery, event-pattern matching, `CreateEventSourceMapping`,
  S3 bucket-notification handlers, archives/replays, `PutPermission`.
