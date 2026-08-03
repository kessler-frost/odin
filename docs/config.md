# Configuration

Every knob odin has is an `ODIN_*` environment variable, and every one of them
is declared in **[`src/odin/settings.py`](../src/odin/settings.py)** — typed,
bounded, and validated at startup. This page is the human-readable index; that
file is the source of truth.

Nothing here needs setting. Every default is what odin ships with, and most of
them are load-bearing measurements rather than round numbers — `settings.py`
carries the reasoning next to each one.

## How it behaves

- **Validated at startup.** `create_app` calls `settings.validate_all()`, so a
  malformed value fails the process immediately and pydantic's error names the
  variable — not the code path that would have read it half an hour later.
- **Read at use time.** Each section is built from the environment at the moment
  it is touched, so changing a variable takes effect on the next call. Nothing
  is captured at import.
- **Empty means unset.** `ODIN_MIN_DISK_GIB=` (an empty export from a shell
  script) falls back to the default rather than erroring.
- **Numbers are strict, on/off flags are lenient — deliberately.** A
  non-numeric timeout is a startup error. A *mistyped* safety flag is not: an
  unrecognised `ODIN_REAP_EC2_VMS`, `ODIN_AI`, `ODIN_DEBUG_AGENT`,
  `ODIN_TRANSLATE_REFINE` or `ODIN_BACKING_MESH` never enables the dangerous
  side. A safety net you disabled by mistyping the value is not a safety net.

## Gateway — `GatewaySettings`

| Variable | Default | What it does |
| --- | --- | --- |
| `ODIN_GATEWAY_PORT` | `4266` | Port for odin's checking reverse proxy. **`0` means "bind an ephemeral port"** — that is what the test suite runs on, and the right isolation for two odins on one Mac. |
| `ODIN_REAP_EC2_VMS` | on | `0`/`false`/`no`/`off` turns off the startup reaper that deletes EC2 VMs no store claims. Set it when running a SECOND odin on one machine — the second instance's store knows nothing about the first's VMs and would reap them. |
| `ODIN_ECS_STEADY_TIMEOUT` | `60` s | Post-apply ECS convergence budget. The same 60s as `iac/hcl.py`'s `timeouts.update`, on purpose. |
| `ODIN_LAMBDA_ACTIVE_TIMEOUT` | `210` s | Post-apply Lambda readiness budget (`READY_TIMEOUT` 180s + 30s margin, derived in `lambdactl.py`). |
| `ODIN_RDS_AVAILABLE_TIMEOUT` | `210` s | Post-apply RDS readiness budget (`_CREATE_TIMEOUT` 180s + 30s margin, derived in `rdsctl.py`). |

## Reconciler cadence — `ReconcileSettings`

| Variable | Default | What it does |
| --- | --- | --- |
| `ODIN_DISPATCH_TICKS` | `1` | Ticks between event-dispatch passes. **Do not turn this down or up in a test** — `tests/reconcile/test_dispatch_cadence.py` is a repo-wide ratchet against it. A dispatcher being late is a trigger the user calls broken, so the shipped cadence is already the minimum. |
| `ODIN_DRIFT_SWEEP_TICKS` | `10` | Ticks between drift sweeps (~10 s at the production 1 s poll). A sweep is a report, not an action — hence ten where the dispatcher has one. |

## Simulate / OpenTofu — `SimulateSettings`

| Variable | Default | What it does |
| --- | --- | --- |
| `ODIN_TOFU_TIMEOUT` | `600` s | Budget for `init` and for `apply` — each gets its own, not one shared across the call. |
| `ODIN_TOFU_DESTROY_TIMEOUT` | `300` s | A deadline across the WHOLE destroy call, `init` included. Sized from real teardowns (a 12-resource env in 63 s, three EC2 VMs in 62 s). |
| `ODIN_TOFU_PARALLELISM` | `4` | `-parallelism` for tofu. Lower than tofu's own 10, because a big canvas fans out EC2 boots and ECS waits on top of whatever Apply is already doing. |

## Compute and admission — `ComputeSettings`

| Variable | Default | What it does |
| --- | --- | --- |
| `ODIN_BOOT_TIMEOUT` | `300` s | How long an EC2 node's Lima VM may take to report a running guest. Raise it on a loaded Mac; the **default deliberately does not move**, because a longer one makes a genuinely hung boot slower to report. |
| `ODIN_MAX_CONCURRENT_VM_BOOTS` | `3` | Concurrent `limactl create`/`start` calls. Unbounded, N EC2 nodes stampede the machine at once. |
| `ODIN_MIN_DISK_GIB` | `10` | Free-disk floor an Apply requires. Matches `odin doctor`'s own figure, so the two agree on "enough disk". |
| `ODIN_CONTAINER_MEMORY_BUDGET_MIB` | 70 % of the container runtime's memory | Absolute MiB ceiling for the CONTAINER pool (rds/ecs/lambda/elasticache/alb and the shared s3/sqs/sns/dynamodb backings). |
| `ODIN_MEMORY_BUDGET_MIB` | — | **The original name of the line above, still accepted.** The qualified name wins when both are set. |
| `ODIN_VM_MEMORY_BUDGET_MIB` | 70 % of real host memory | Absolute MiB ceiling for the HOST/VM pool (`ec2` nodes are real Lima VMs). **Disjoint from the container pool** — raising one does nothing for a rejection on the other, which is why the qualified names exist. |

## Nebula mesh — `MeshSettings`

| Variable | Default | What it does |
| --- | --- | --- |
| `ODIN_LIGHTHOUSE_PORT` | allocated from `4342`+ | Pins the lighthouse's UDP port, honoured verbatim — no probing, no reallocation. |
| `ODIN_BACKING_MESH` | on | `0` keeps backing containers off the overlay entirely. Membership is already off for an env with no Nebula network, so an env of bare s3/sqs nodes pays nothing either way. |
| `ODIN_MESH_UNDERLAY` | `192.168.5.2` | The address a mesh sidecar dials to reach the host lighthouse. Override for a host whose user-mode gateway differs. |
| `ODIN_MESH_SWEEP_SECONDS` | `30` s | How often a PASSING mesh verdict is re-taken. |
| `ODIN_MESH_RECHECK_SECONDS` | `5` s | How often a FAILING one is, so a recovery shows up promptly. |

## AI — `AiSettings`

`ODIN_AI` is the master switch and wins over everything else here.

| Variable | Default | What it does |
| --- | --- | --- |
| `ODIN_AI` | unset | `0` disables **every** model call odin can make; `1` allows them. Left unset, the switch in the top bar decides and its default is off. An unrecognised value disables them too, with a warning naming what you typed. |
| `ODIN_TRANSLATE_REFINE` | off | Opts IN to the model refine pass over the deterministic canvas → Terraform translation. Off by default: the translation is already correct, and the pass can only attach comments/tags/unset arguments. |
| `ODIN_TRANSLATE_TIMEOUT` | `45` s | Budget for that background refine pass. Nobody waits on it. |
| `ODIN_DEBUG_AGENT` | on | `0`/`false`/`no`/`off` disables the failure-explanation agent. ON by default — unlike the refine pass — because it is read-only and prose-out, so it cannot corrupt anything. |
| `ODIN_DEBUG_TIMEOUT` | `90` s | Budget for that explanation. 90 s because it covers a COLD nested-CLI start (~65 s measured); a 60 s budget turned good diagnoses into "agent unavailable" on startup cost alone. |
| `ODIN_CHAT_TIMEOUT` | `60` s | Budget for one canvas-chat turn. |

## Read by something other than odin

| Variable | Read by | What it does |
| --- | --- | --- |
| `ODIN_URL` | typer, via `envvar=` on `--url` | Base URL the CLI dials. Not in `settings.py` because it never reaches `os.environ` in odin's own code. |
| `ODIN_KEEP_IT_ARTIFACTS` | `tests/` only | Keeps integration-test artifacts instead of cleaning them up. |

---

`tests/test_settings_inventory.py` holds this list as literals and fails the
build if a variable stops working, if one appears with no record here, or if any
module goes back to reading the environment directly.
